"""One agent per viewer.

The ViewerAgent is the ONLY writer of a viewer's goal, plan and history (ownership). Control flow for a trigger:

  idempotency -> staleness -> read facts (timeout/cache fallback) -> keep/revise/choose ONE goal (model = advice only)
  -> plan intervention -> independent verification -> publish -> persist -> decision record

A viewer in the holdout arm runs the same goal logic (a "shadow" goal, so the holdout is judged on the same
definition of success) but receives zero actions. Model calls are made identically for both arms.
"""
import time
from datetime import timedelta

from .facts import resolve_viewer
from .goals import (evaluate_all, new_goal_record, still_valid, judge_goal, signal_text)
from .model import ModelGateway
from .pack import stamp
from .planner import build_home_page, generic_safe_page, plan_message
from .util import iso, ist_day
from .verifier import check_model_claim

SUB_KINDS = ('purchase_completed', 'subscription_renewed', 'subscription_lapsed')


class Budget:
    def __init__(self, cfg):
        self.cfg, self.models, self.tools = cfg, 0, 0

    def tool(self, n=1):
        if self.tools + n > self.cfg.max_tool_calls:
            return False
        self.tools += n
        return True

    def model(self):
        if self.models >= self.cfg.max_model_calls:
            return False
        self.models += 1
        return True


class CallCtx:
    def __init__(self, now, trigger_event_id, timeout_s, waits):
        self.now, self.trigger_event_id, self.timeout_s, self._waits = now, trigger_event_id, timeout_s, waits

    def wait(self, seconds):
        self._waits[0] += seconds
        self.now = self.now + timedelta(seconds=seconds)


class ViewerAgent:
    def __init__(self, rt, user):
        self.rt, self.user = rt, user
        self.st = rt.store.get(user, rt.pack.arm(user))
        self.cache = None

    # ================================================================== trigger
    def on_trigger(self, trig, deliver_at, start, queue_wait_s):
        rt, cfg, pack, st, user = self.rt, self.rt.cfg, self.rt.pack, self.st, self.user
        t0 = time.perf_counter()
        waits = [0.0]
        budget = Budget(cfg)
        did = rt.next_decision_id()
        if '_r' not in trig:
            stamp(trig)
        elapsed = lambda: (time.perf_counter() - t0) + waits[0]
        eid = trig['event_id']
        arm = pack.arm(user)
        dec = {'decision_id': did, 'viewer_id': user, 'arm': arm, 'kind': 'trigger_decision',
               'trigger': {'event': trig['event'], 'event_id': eid, 'received_at': trig['_r'], 'deliver_at': deliver_at,
                           'event_age_s': round((start - trig['_t']).total_seconds(), 1),
                           'buffer_age_s': round(trig['_buf'], 1), 'queue_wait_s': round(queue_wait_s, 3),
                           'session_id': trig['session_id']}}
        # ---- 1. idempotency
        if eid in st['seen_triggers']:
            dec.update({'goal': None, 'action': 'NO_ACTION', 'status': 'DUPLICATE_SUPPRESSED', 'actions': [],
                        'no_action_reason': 'duplicate delivery of trigger %s already handled by %s' % (eid, st['seen_triggers'][eid]),
                        'checks': {'passed': ['idempotency'], 'failed': []}, 'content': [], 'evidence': ['event:' + eid],
                        'confidence': 1.0, 'confidence_reason': 'idempotency key matched; no new work', 'assumptions': [],
                        'conflicts': [], 'model_calls': 0, 'tool_calls': 0, 'fallback_used': None})
            return self._finish(dec, t0, waits, start, did, budget)
        st['seen_triggers'][eid] = did
        rt.idx.add(trig)
        buffer_stale = trig['_buf'] > cfg.stale_after_s
        lag_stale = (start - trig['_r']).total_seconds() > cfg.stale_after_s
        stale = buffer_stale or lag_stale
        now = start
        fallback = None
        # ---- 2. read facts (data-store dependency: timeout -> retry -> cache -> generic)
        F, degraded = None, False
        for attempt in range(1 + cfg.store_retries):
            if not budget.tool():
                break
            lat = rt.injector.store_latency(now + timedelta(seconds=waits[0]), user, did, attempt)
            if lat is None or lat <= cfg.store_timeout_s:
                waits[0] += lat or 0.0
                F = resolve_viewer(pack, rt.idx, user, now, trig, cfg)
                break
            waits[0] += cfg.store_timeout_s
            rt.metrics['store_read_timeouts'] += 1
        if F is None:
            c = self.cache
            if c and (start - c['at']).total_seconds() <= cfg.cache_max_age_s:
                F = dict(c['F'])
                F['dialect'] = dict(F['dialect'], value=trig['app_dialect'], source='app_setting(trigger; viewer facts from cache)',
                                    ref='event:' + eid)
                degraded = True
                fallback = 'store_unavailable:cached_facts(age %ds)' % (start - c['at']).total_seconds()
            else:
                fallback = 'store_unavailable:no_cache'
            rt.metrics['store_fallbacks'] += 1
        # ---- 3. goal: keep / revise / choose (ownership: this is the only place a goal is committed)
        goal, goal_is_new, audit, model_info, cands = st['active_goal'], False, None, None, []
        goal_note = None
        if goal is not None:
            if now >= goal['deadline']:
                self._close(goal, 'expired_open', now, {'reason': 'deadline passed; final judgement at close of replay'})
                goal = None
            elif F is not None and not degraded:
                valid, why = still_valid(goal, F, pack, now)
                if not valid:
                    self._cancel_pending(goal['goal_id'], now, 'goal no longer valid: ' + why)
                    self._close(goal, 'superseded', now, {'reason': why})
                    goal_note = 'previous goal %s superseded: %s' % (goal['goal_id'], why)
                    goal = None
        if goal is None and F is not None and not degraded:
            cands, audit = evaluate_all(F, pack, rt.registry, now, st['goal_stats'])
            chosen = cands[0] if cands else None
            ask = rt.cfg.model_policy != 'never' and (cands or self._may_ask_without_goal(now))
            if ask:
                if queue_wait_s > cfg.shed_backlog_s:
                    model_info = {'status': 'skipped_load_shed', 'calls': 0}
                else:
                    task = {'task': 'goal_advice', 'candidates': [{'goal_id': c['goal_id'], 'reason': c['reason'], 'refs': c['refs']} for c in cands[:5]]}
                    ctx = CallCtx(now + timedelta(seconds=waits[0]), eid, self._call_timeout(elapsed()), waits)
                    advice, status, calls = rt.gateway.call(task, ctx, budget)
                    model_info = {'status': status, 'calls': calls}
                    if not cands:
                        st['model_asked_no_goal_at'] = now
                    if status != 'ok':
                        fallback = (fallback + '; ' if fallback else '') + 'model_%s:rule_based_goal' % status
                        rt.metrics['model_fallbacks'] += 1
                    if advice is not None:
                        accepted, why = check_model_claim(advice, F, pack, rt.registry, now)
                        model_info.update({'claimed_goal': advice['goal_id'], 'claimed_eligible': advice['eligible'],
                                           'claimed_confidence': advice['confidence'], 'claim_accepted': accepted,
                                           'claim_check': why})
                        if advice['eligible'] and not accepted:
                            rt.metrics['unsupported_model_claims'] += 1
                            rt.incidents.append({'at': now, 'kind': 'unsupported_model_claim', 'viewer_id': user,
                                                 'decision_id': did, 'detail': why, 'claim': advice})
                        if accepted:
                            match = [c for c in cands if c['goal_id'] == advice['goal_id']]
                            if match:
                                chosen = match[0]
            if chosen is not None:
                goal = new_goal_record(rt.registry, chosen, now, did, F)
                goal_is_new = True
        # ---- 4. intervention
        proposals, reasons = [], []
        no_action_reason = None
        page = None
        extra = []          # creative model jobs (hero pick, copy) attempted for this decision
        pick_hero = lambda sl: self._pick_hero(sl, F, goal, now, eid, waits, budget, queue_wait_s, elapsed, extra)
        write_body = lambda c: self._write_body(c, now, eid, waits, budget, queue_wait_s, elapsed, extra)
        if arm != 'agent':
            no_action_reason = 'holdout arm: zero actions by policy (shadow goal recorded for measurement)'
        elif stale:
            no_action_reason = ('stale trigger: app_open was buffered offline for %.0fs (>%ds) - not a live session; '
                                'recorded as history only' % (trig['_buf'], cfg.stale_after_s)) if buffer_stale else \
                'trigger processed %.0fs after receipt (>%ds): session over' % ((start - trig['_r']).total_seconds(), cfg.stale_after_s)
        elif F is None:
            gp = generic_safe_page(pack, trig['app_dialect'], trig.get('app_language') or 'hin', now, cfg) if trig.get('app_dialect') else None
            if gp:
                proposals.append(self._page_action(gp, did, eid, goal=None))
                reasons.append('viewer facts unavailable: generic dialect-correct page only, no goal, no messages')
            else:
                no_action_reason = 'viewer facts unavailable and no safe generic page'
        elif F is not None and not F['dialect']['supported']:
            no_action_reason = 'dialect %s (app setting) has no catalogue content: nothing valid to show or promote' % F['dialect']['value']
        elif goal is None:
            no_action_reason = 'no goal can be chosen yet: ' + '; '.join(
                '%s: %s' % (a['goal_id'], a['reason']) for a in (audit or []) if a['goal_id'] in ('G01', 'G02', 'G05', 'G06', 'G07', 'G12'))[:400]
        else:
            live = rt.ledger.live_page(user, now, cfg.page_ttl)
            if live is not None and live['dialect'] == F['dialect']['value'] and live.get('goal_id') == goal['goal_id'] \
                    and all(rt.pack.catalogue.is_available(t, now, now + timedelta(minutes=30)) for t in rt.ledger.titles_in(live)):
                reasons.append('personalised page %s still live (TTL): not republished' % live['action_id'])
            else:
                page = build_home_page(pack, F, goal, now, cfg, pick_hero=pick_hero)
                if page:
                    proposals.append(self._page_action(page, did, eid, goal))
                else:
                    reasons.append('no valid content for a home page in this dialect')
            if not degraded:
                msg, why = plan_message(pack, F, goal, page, now, cfg, lambda at_, typ: not rt.ledger.messages_on_day(
                    user, ist_day(at_), ('in_app_message',) if typ == 'in_app_message' else ('push', 'whatsapp')),
                    write_body=write_body)
                if msg is not None:
                    proposals.append(self._message_action(msg, did, eid))
                reasons.append('message: ' + why)
            else:
                reasons.append('message: skipped (degraded mode: viewer consent facts are cached)')
            if not proposals:
                no_action_reason = 'nothing to change: ' + '; '.join(reasons)
        # ---- 5. independent verification (fresh read, own checks)
        budget.tool()
        res = {'passed': [], 'failed': [], 'approved': []}
        if proposals or goal is not None:
            pub_est = start + timedelta(seconds=elapsed())
            res = rt.verifier.verify(user, pub_est, trig, goal, goal_is_new, proposals, facts=F if degraded else None)
        approved = res['approved']
        goal_rejected = any(f['check'] in ('eligibility', 'measurable', 'rule_version') for f in res['failed'])
        if goal_rejected and goal_is_new:
            goal, goal_is_new = None, False
            rt.metrics['goals_rejected_by_verifier'] += 1
            no_action_reason = 'verifier rejected the goal: ' + '; '.join(f['detail'] for f in res['failed'] if f['check'] in ('eligibility', 'measurable', 'rule_version'))
        elif goal_is_new:
            self._commit(goal, now)
        if proposals and not approved and no_action_reason is None:
            no_action_reason = 'verifier rejected every proposed action: ' + '; '.join('%s(%s)' % (f['check'], f['detail']) for f in res['failed'])[:500]
        # ---- 6. publish
        pub_at = start + timedelta(seconds=elapsed() + 0.002)
        for a in approved:
            a['published_at'] = pub_at
            a['action_id'] = rt.next_action_id()
            if a['type'] != 'home_page' and a['send_at'] < pub_at:
                a['send_at'] = pub_at
            rt.ledger.add(a, '%s:%s' % (eid, a['type']))
            st['actions'].append(a['action_id'])
            if a['type'] != 'home_page' and goal:
                goal['messages_sent'] = goal.get('messages_sent', 0) + 1
        # ---- 7. decision record
        content = []
        for a in approved:   # key content: hero + first 3 of each row (the full lists are in `actions`)
            if a['type'] == 'home_page':
                content += ([a['hero_title_id']] if a.get('hero_title_id') else []) + [t for r in a['rows'] for t in r['title_ids'][:3]]
            else:
                content.append(a['title_id'])
        status = 'PUBLISHED' if approved else 'NO_ACTION'
        if arm != 'agent':
            status = 'NO_ACTION'
        types = [a['type'] for a in approved]
        dec.update({
            'goal': self._goal_view(goal, goal_is_new) if goal else None,
            'goal_considered': audit,
            'action': '+'.join(t.upper() for t in types) if types else 'NO_ACTION',
            'actions': [self._public_action(a) for a in approved],
            'dialect': {'used': (F or {}).get('dialect', {}).get('value') or trig.get('app_dialect'),
                        'source': ((F or {}).get('dialect', {}).get('source') or 'app_setting'),
                        'conflicts': (F or {}).get('dialect', {}).get('conflicts', []),
                        'resolution': 'app setting decides: it is the dialect the app will actually request'},
            'content': ['title:' + t for t in content],
            'status': status,
            'no_action_reason': no_action_reason if not approved else None,
            'plan_notes': reasons + ([goal_note] if goal_note else []),
            'checks': {'passed': res['passed'], 'failed': res['failed']},
            'model': model_info,
            'model_extra': extra,
            'fallback_used': fallback,
            'degraded_mode': degraded,
            'shadow': arm != 'agent',
            'rule_version': goal['rule_version'] if goal else None,
        })
        self._explain(dec, F, goal, cands, trig, page, degraded, stale)
        dec['model_calls'], dec['tool_calls'] = budget.models, budget.tools
        # ---- 8. persist (deferred, not lost, if the store is down)
        budget.tool()
        if F is not None and not degraded:
            self.cache = {'F': F, 'at': start}
            self._snapshot(F, now)
        st['decisions'].append({'decision_id': did, 'at': pub_at, 'action': dec['action'], 'goal_id': goal['goal_id'] if goal else None,
                                'reason': dec.get('no_action_reason')})
        st['plan'] = {'goal_id': goal['goal_id'], 'since': goal['started_at'], 'deadline': goal['deadline'],
                      'last_action': dec['action'], 'revised_at': now} if goal else None
        rt.store.save(user, healthy=rt.injector.store_healthy(now + timedelta(seconds=waits[0])))
        return self._finish(dec, t0, waits, start, did, budget, pub_at)

    # ================================================================== events
    def react(self, e, at):
        """A live event for this viewer arrived (already in the event index). Re-plan only if the situation changed."""
        rt, st, pack = self.rt, self.st, self.rt.pack
        k = e['event']
        out = []
        if k == 'notification_opt_out':
            ch = e['properties'].get('channel')
            for a in rt.ledger.pending(self.user, at):
                if ch not in ('push', 'whatsapp') or a['type'] == ch:
                    rt.ledger.cancel(a, at, 'viewer opted out (%s)' % ch)
                    out.append(a['action_id'])
            rt.store.save(self.user)
            return out
        goal = st['active_goal']
        replan = False
        if goal is not None:
            j = judge_goal(goal, rt.idx.upto(self.user, at), pack.catalogue, at)
            if j['status'] == 'succeeded':
                self._cancel_pending(goal['goal_id'], at, 'goal already achieved: message no longer needed')
                self._close(goal, 'succeeded', at, j)
                goal = None
                replan = k in SUB_KINDS
            elif k in SUB_KINDS:
                F = resolve_viewer(pack, rt.idx, self.user, at, None, rt.cfg)
                valid, why = still_valid(goal, F, pack, at)
                if not valid:
                    cancelled = self._cancel_pending(goal['goal_id'], at, 'situation changed (%s): %s' % (k, why))
                    self._close(goal, 'superseded', at, {'reason': why})
                    goal, replan = None, True
        elif k in SUB_KINDS:
            replan = True
        if replan:
            self.replan(e, at)
        rt.store.save(self.user)
        return out

    def repair_page(self, old, ctl, at):
        """A title in a live page was withdrawn: publish a corrected page for the same dialect (verified like any other)."""
        rt, st, cfg, pack = self.rt, self.st, self.rt.cfg, self.rt.pack
        did = rt.next_decision_id()
        t0 = time.perf_counter()
        F = resolve_viewer(pack, rt.idx, self.user, at, None, cfg)
        goal = st['active_goal']
        eid = 'ctl:%s:%s:%s' % (ctl['type'], ctl['title_id'], self.user)
        page = build_home_page(pack, F, goal, at, cfg, dialect=old['dialect'])
        actions, res = [], {'passed': [], 'failed': [], 'approved': []}
        if page and pack.arm(self.user) == 'agent':
            prop = self._page_action(page, did, eid, goal)
            res = rt.verifier.verify(self.user, at, None, goal, False, [prop])
            for a in res['approved']:
                a['published_at'] = at + timedelta(milliseconds=(time.perf_counter() - t0) * 1000 + 1)
                a['action_id'] = rt.next_action_id()
                rt.ledger.add(a, '%s:%s' % (eid, a['type']))
                st['actions'].append(a['action_id'])
                actions.append(a)
        dec = {'decision_id': did, 'viewer_id': self.user, 'arm': pack.arm(self.user), 'kind': 'repair',
               'trigger': {'event': 'title_withdrawn', 'event_id': eid, 'received_at': at, 'deliver_at': at, 'event_age_s': 0.0},
               'goal': self._goal_view(goal, False) if goal else None, 'goal_considered': None,
               'action': 'HOME_PAGE' if actions else 'NO_ACTION', 'actions': [self._public_action(a) for a in actions],
               'status': 'PUBLISHED' if actions else 'NO_ACTION',
               'no_action_reason': None if actions else 'withdrawn title removed; no valid replacement page: %s' % res['failed'],
               'dialect': {'used': old['dialect'], 'source': 'previous page dialect', 'conflicts': F['dialect']['conflicts']},
               'content': ['title:' + t for a in actions for t in rt.ledger.titles_in(a)],
               'checks': {'passed': res['passed'], 'failed': res['failed']}, 'model_calls': 0, 'tool_calls': 2,
               'fallback_used': None, 'shadow': False, 'rule_version': goal['rule_version'] if goal else None,
               'plan_notes': ['replaces %s which contained withdrawn title %s' % (old['action_id'], ctl['title_id'])]}
        self._explain(dec, F, goal, [], dec['trigger'], page, False, False)
        dec['decided_in_ms'] = round((time.perf_counter() - t0) * 1000, 1)
        dec['published_at'] = actions[0]['published_at'] if actions else at
        dec['latency_s'] = round((dec['published_at'] - at).total_seconds(), 3)
        st['decisions'].append({'decision_id': did, 'at': at, 'action': dec['action'], 'goal_id': goal['goal_id'] if goal else None,
                                'reason': 'page repair after title withdrawal'})
        rt.decisions.append(dec)
        rt.store.save(self.user)
        return dec

    def replan(self, e, at):
        """Situation changed (e.g. purchase): choose a new goal now, not at the next scheduled visit."""
        rt, st = self.rt, self.st
        F = resolve_viewer(rt.pack, rt.idx, self.user, at, None, rt.cfg)
        cands, audit = evaluate_all(F, rt.pack, rt.registry, at, st['goal_stats'])
        did = rt.next_decision_id()
        goal = None
        if cands:
            goal = new_goal_record(rt.registry, cands[0], at, did, F)
            self._commit(goal, at)
        dec = {'decision_id': did, 'viewer_id': self.user, 'arm': rt.pack.arm(self.user), 'kind': 'replan',
               'trigger': {'event': e['event'], 'event_id': e['event_id'], 'received_at': e['_r'], 'deliver_at': at,
                           'event_age_s': round((at - e['_t']).total_seconds(), 1)},
               'goal': self._goal_view(goal, True) if goal else None, 'goal_considered': audit, 'action': 'NO_ACTION',
               'actions': [], 'status': 'NO_ACTION',
               'no_action_reason': 'plan revised after %s; the next visit will act on the new goal (no unsolicited action)' % e['event'],
               'dialect': {'used': F['dialect']['value'], 'source': F['dialect']['source'], 'conflicts': F['dialect']['conflicts']},
               'content': [], 'checks': {'passed': ['goal_eligibility(rules)'], 'failed': []}, 'model_calls': 0,
               'tool_calls': 1, 'fallback_used': None, 'shadow': rt.pack.arm(self.user) != 'agent',
               'rule_version': goal['rule_version'] if goal else None, 'published_at': at, 'decided_in_ms': 0.0, 'latency_s': 0.0}
        self._explain(dec, F, goal, cands, e, None, False, False)
        st['decisions'].append({'decision_id': did, 'at': at, 'action': 'NO_ACTION', 'goal_id': goal['goal_id'] if goal else None,
                                'reason': dec['no_action_reason']})
        rt.decisions.append(dec)
        rt.metrics['replans'] += 1
        return dec

    # ================================================================== helpers
    def _call_timeout(self, elapsed_s):
        """Per-call timeout that keeps even a retried call inside the 60 s budget (about 50 s total, 2 attempts)."""
        gw = self.rt.gateway
        return min(gw.timeout_s, max(2.0, (50.0 - elapsed_s) / (1 + self.rt.cfg.model_retries)))

    def _creative(self, task, now, eid, waits, budget, queue_wait_s, elapsed, extra):
        """Optional model job (advisory). Returns the parsed answer or None; every attempt is logged on the decision."""
        rt, cfg = self.rt, self.rt.cfg
        if not rt.gateway.creative:
            return None
        kind = task['task']
        if queue_wait_s > cfg.shed_backlog_s:
            extra.append({'task': kind, 'status': 'skipped_load_shed', 'calls': 0})
            return None
        if elapsed() > cfg.decision_deadline_s:
            extra.append({'task': kind, 'status': 'skipped_deadline', 'calls': 0})
            return None
        ctx = CallCtx(now + timedelta(seconds=waits[0]), eid, self._call_timeout(elapsed()), waits)
        out, status, calls = rt.gateway.call(task, ctx, budget)
        extra.append({'task': kind, 'status': status, 'calls': calls})
        return out

    def _pick_hero(self, shortlist, F, goal, now, eid, waits, budget, queue_wait_s, elapsed, extra):
        task = {'task': 'hero_pick',
                'goal': {'id': goal['goal_id'], 'name': goal['name']} if goal else {'id': None, 'name': 'none'},
                'viewer_top_genres': F['hist']['top_genres'][:2],
                'candidates': [{'title_id': t['title_id'], 'title': t['title'], 'type': t['content_type'],
                                'genres': t['genre_list'][:3], 'premium': str(t['premium'])} for t in shortlist]}
        out = self._creative(task, now, eid, waits, budget, queue_wait_s, elapsed, extra)
        picked = next((t for t in shortlist if out and t['title_id'] == out['title_id']), None)
        if out is not None:
            extra[-1]['accepted'] = picked is not None
            if picked is None:
                extra[-1]['status'] = 'rejected_not_in_shortlist'
        return picked

    def _write_body(self, c, now, eid, waits, budget, queue_wait_s, elapsed, extra):
        out = self._creative({'task': 'push_copy', 'facts': c}, now, eid, waits, budget, queue_wait_s, elapsed, extra)
        return out['body'] if out else None

    def _may_ask_without_goal(self, now):
        last = self.st.get('model_asked_no_goal_at')
        return last is None or (now - last) > timedelta(hours=6)

    def _commit(self, goal, now):
        st = self.st
        goal['committed_at'] = now
        st['active_goal'] = goal
        st['goal_stats'].setdefault(goal['goal_id'], {'attempts': 0, 'succeeded': 0, 'failed': 0, 'inconclusive': 0})['attempts'] += 1
        self.rt.open_goals.setdefault(goal['goal_id'], set()).add(self.user)

    def _close(self, goal, status, at, info):
        st = self.st
        goal['status'] = status
        goal['closed_at'] = at
        goal['close_info'] = info
        st['goal_history'].append(goal)
        if st['active_goal'] is goal:
            st['active_goal'] = None
        self.rt.open_goals.get(goal['goal_id'], set()).discard(self.user)

    def _cancel_pending(self, goal_id, at, reason):
        n = []
        for a in self.rt.ledger.pending(self.user, at):
            if a.get('goal_id') == goal_id:
                self.rt.ledger.cancel(a, at, reason)
                n.append(a['action_id'])
        return n

    def _page_action(self, page, did, eid, goal):
        return {'decision_id': did, 'user_id': self.user, 'trigger_event_id': eid, 'type': 'home_page',
                'dialect': page['dialect'], 'hero_title_id': page['hero_title_id'], 'rows': page['rows'],
                'goal_id': goal['goal_id'] if goal else None}

    def _message_action(self, m, did, eid):
        a = {'decision_id': did, 'user_id': self.user, 'trigger_event_id': eid, 'type': m['type'], 'send_at': m['send_at'],
             'title_id': m['title_id'], 'goal_id': m['goal_id']}
        for k in ('text', 'deeplink', 'template_id', 'variables'):
            if k in m:
                a[k] = m[k]
        return a

    @staticmethod
    def _public_action(a):
        return {k: v for k, v in a.items() if k != 'status'}

    def _goal_view(self, goal, is_new):
        return {'id': goal['goal_id'], 'name': goal['name'], 'deadline': goal['deadline'], 'started_at': goal['started_at'],
                'success_signal': signal_text(goal['success']), 'rule_version': goal['rule_version'],
                'is_new': is_new, 'reason': goal.get('reason')}

    def _snapshot(self, F, now):
        H = F['hist']
        self.st['facts'] = {
            'as_of': now, 'signup': F['signup'], 'subscription': {k: v for k, v in F['subscription'].items()},
            'dialect': F['dialect'], 'language': F['language'], 'consent': F['consent'], 'conflicts': F['conflicts'],
            'profile': {'present': F['profile_present'], 'rows': F['profile_rows']},
            'watch': {'plays': H['plays'], 'completes': H['completes'], 'last_play_at': H['last_play_at'],
                      'play_days_14d': len(H['play_days_14d']), 'opens_14d_excl_current': len([o for o in H['opens'] if (now - o[0]) <= timedelta(days=14)]),
                      'top_genres': H['top_genres'][:3], 'unfinished_series': sorted(s for s, v in H['series'].items() if v['unwatched_left'] and v['type'] != 'film')[:5]},
            'events_seen': F['events_seen']}

    def _explain(self, dec, F, goal, cands, trig, page, degraded, stale):
        """evidence (inspectable refs + freshness), confidence WITH reason, assumptions, conflicts."""
        rt = self.rt
        now = dec['trigger']['deliver_at']
        ev = ['event:' + dec['trigger']['event_id']]
        assumptions, conflicts = [], []
        if goal:
            ev += goal.get('evidence', []) + ['goal_rule:%s@%s' % (goal['goal_id'], goal['rule_version'])]
            assumptions += goal.get('assumptions', [])
        if F is not None:
            ev.append((F['dialect'].get('ref') or '').strip() or 'app_setting')
            conflicts = F['conflicts']
        content = dec.get('content', [])
        ev += [c.replace('title:', 'catalogue:') for c in content[:6]]
        seen, uniq = set(), []
        for x in ev:
            if x and x not in seen:
                seen.add(x)
                uniq.append(x)
        dec['evidence'] = uniq
        dec['evidence_freshness'] = {x: self._freshness(x, now) for x in uniq}
        if F is not None and F['hist']['plays'] == 0:
            assumptions.append('viewer has not started any episode (no play_started in the observed event window)')
        if F is not None and not F['profile_present']:
            assumptions.append('no profile row: facts built from events; consent unknown so no push/WhatsApp')
        if dec['trigger'].get('buffer_age_s', 0) > 0 and not stale:
            assumptions.append('trigger was briefly buffered (%.0fs); treated as live' % dec['trigger']['buffer_age_s'])
        if page and page.get('notes'):
            assumptions.append('page notes: ' + '; '.join(page['notes'][:3]))
        dec['assumptions'] = assumptions
        dec['conflicts'] = [c if isinstance(c, dict) else {'detail': c} for c in conflicts]
        # confidence = confidence that THIS goal/action is appropriate (not a probability the viewer converts)
        parts, c = [], 0.55
        if goal:
            if any(r.startswith('event:') for r in goal.get('evidence', [])):
                c += 0.15; parts.append('+0.15 goal backed by a viewer event')
            elif goal.get('evidence'):
                c += 0.05; parts.append('+0.05 goal backed by profile only (up to a day stale)')
            if goal.get('assumptions'):
                c -= 0.08; parts.append('-0.08 goal rests on stated assumptions')
        else:
            c = 0.5; parts.append('no goal: abstaining on thin evidence')
        if F is not None and F['dialect']['conflicts']:
            c -= 0.07; parts.append('-0.07 dialect sources disagree (app setting used)')
        if degraded:
            c -= 0.2; parts.append('-0.20 degraded mode: cached facts')
        if F is None:
            c -= 0.25; parts.append('-0.25 no viewer facts')
        if stale:
            c += 0.1; parts.append('+0.10 abstaining on a stale trigger is low-risk')
        if page and len(page['rows']) >= 4:
            c += 0.05; parts.append('+0.05 full page of valid rows')
        if dec.get('shadow') and not stale:
            parts.append('(shadow decision: holdout, no action taken)')
        dec['confidence'] = round(max(0.05, min(0.95, c)), 2)
        dec['confidence_reason'] = '0.55 base ' + ' '.join(parts)
        if goal:
            dec['judged_by'] = {'deadline': goal['deadline'], 'signal': signal_text(goal['success']), 'rule_version': goal['rule_version']}

    def _freshness(self, ref, now):
        rt = self.rt
        try:
            if ref.startswith('event:'):
                eid = ref[6:]
                for e in reversed(rt.idx.by_user.get(self.user, [])):
                    if e['event_id'] == eid:
                        return {'as_of': iso(e['_r']), 'age_s': round((now - e['_r']).total_seconds(), 1), 'clock': 'received_at'}
            elif ref.startswith('profile:'):
                at = ref.split('@')[1]
                from .util import ts
                return {'as_of': at, 'age_s': round((now - ts(at)).total_seconds(), 1), 'clock': 'profile.updated_at'}
            elif ref.startswith('catalogue:'):
                t = rt.pack.catalogue.get(ref[10:])
                if t:
                    return {'as_of': iso(t['available_from_dt']), 'available_until': iso(t['until_dt']), 'clock': 'catalogue window'}
        except Exception:
            pass
        return {'as_of': None, 'age_s': None}

    def _finish(self, dec, t0, waits, start, did, budget, pub_at=None):
        ms = (time.perf_counter() - t0) * 1000 + waits[0] * 1000
        dec['decided_in_ms'] = round(ms, 1)
        dec.setdefault('model_calls', budget.models)
        dec.setdefault('tool_calls', budget.tools)
        pub = pub_at or (start + timedelta(milliseconds=ms))
        dec['published_at'] = pub
        dec['started_at'] = start
        dec['latency_s'] = round((pub - dec['trigger']['deliver_at']).total_seconds(), 3)
        self.rt.decisions.append(dec)
        return dec, ms / 1000.0
