"""Runtime: wiring, dependency fault injection, replay harness and the system-level reactions to control events.

The harness (not the agent) knows about injected faults. The agent only sees their symptoms - exceptions, timeouts,
slow reads - and has to cope (timeouts, retry, circuit breaker, cache, smaller action, no action).
"""
import hashlib
import heapq
import os
import time
from collections import Counter
from datetime import timedelta

from .agent import ViewerAgent
from .config import Config
from .facts import resolve_viewer
from .goals import GoalRegistry, judge_goal
from .model import MockModel, ModelGateway
from .planner import build_home_page
from .state import Ledger, StateStore
from .util import dumps, iso, ts
from .verifier import Verifier


def h01(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class FaultInjector:
    """Dependency windows taken from replay/control_events.jsonl (registered when the control event is delivered)."""

    def __init__(self):
        self.windows = []   # dicts: dependency, kind, start, until

    def register(self, c):
        kind = 'unavailable' if c['type'] == 'dependency_unavailable' else 'degraded'
        self.windows.append({'dependency': c['dependency'], 'kind': kind, 'start': ts(c['at']), 'until': ts(c['until']),
                             'detail': c.get('detail')})

    def _active(self, dep, t):
        for w in self.windows:
            if w['dependency'] == dep and w['start'] <= t < w['until']:
                return w
        return None

    def model_down(self, t):
        w = self._active('model_provider', t)
        return bool(w and w['kind'] == 'unavailable')

    def store_healthy(self, t):
        return self._active('user_data_store', t) is None

    def store_latency(self, t, user, did, attempt):
        """None = healthy/instant; float seconds = read latency; inf = the read hangs (times out)."""
        w = self._active('user_data_store', t)
        if w is None:
            return None
        if w['kind'] == 'unavailable':
            return float('inf')
        if h01('store-timeout', user, did, attempt) < 0.20:   # 20% of reads time out
            return float('inf')
        return 0.3 + 9.0 * h01('store-lat', user, did, attempt) ** 3   # p95 ~ 8s


class Runtime:
    def __init__(self, pack, cfg=None, out_dir=None, model=None):
        self.pack, self.cfg = pack, cfg or Config()
        self.out_dir = out_dir
        self.idx = pack.history if pack.history is not None else pack.load_history()
        self.registry = GoalRegistry(pack.goals_cfg)
        self.ledger = Ledger()
        self.store = StateStore(os.path.join(out_dir, 'viewer_state') if out_dir else None)
        self.injector = FaultInjector()
        self.gateway = ModelGateway(model or MockModel(pack.faults, self.injector), self.cfg)
        self.verifier = Verifier(pack, self.idx, self.ledger, self.registry, self.cfg)
        self.decisions, self.incidents, self.metrics = [], [], Counter()
        self.open_goals, self.agents = {}, {}
        self._d = self._a = 0

    def next_decision_id(self):
        self._d += 1
        return 'D-%05d' % self._d

    def next_action_id(self):
        self._a += 1
        return 'A-%05d' % self._a

    def agent(self, user):
        a = self.agents.get(user)
        if a is None:
            a = self.agents[user] = ViewerAgent(self, user)
        return a

    # ------------------------------------------------------------------ control events
    def on_control(self, c):
        at = ts(c['at'])
        t = c['type']
        if t == 'title_withdrawn':
            self.on_title_withdrawn(c, at)
        elif t == 'goal_rule_corrected':
            self.on_goal_rule_corrected(c, at)
        elif t in ('dependency_unavailable', 'dependency_degraded'):
            self.injector.register(c)
            self.incidents.append({'at': at, 'kind': t, 'dependency': c['dependency'], 'until': ts(c['until']),
                                   'detail': c.get('detail'),
                                   'handling': 'agent sees only symptoms: per-call timeouts, model circuit breaker, '
                                               'cached facts, smaller action / no action; writes queued until recovery'})
        else:
            self.incidents.append({'at': at, 'kind': 'unknown_control_event:' + t, 'detail': c})

    def on_title_withdrawn(self, c, at):
        cat = self.pack.catalogue
        cat.withdraw(c['title_id'], at)
        t = cat.get(c['title_id'])
        slug = t['content_slug'] if t else None
        slug_of = lambda tid: bool(slug) and (cat.get(tid) or {}).get('content_slug') == slug
        aff = self.ledger.affected_by_title(slug_of, at, self.cfg.page_ttl)
        cancelled, repaired = [], []
        for a in aff:
            if a['type'] == 'home_page':
                self.ledger.supersede(a, at, 'title %s withdrawn' % c['title_id'])
                d = self.agent(a['user_id']).repair_page(a, c, at)
                repaired.append({'old_action': a['action_id'], 'viewer_id': a['user_id'], 'decision_id': d['decision_id'],
                                 'new_action': d['actions'][0]['action_id'] if d['actions'] else None})
            else:
                self.ledger.cancel(a, at, 'title %s withdrawn before send' % c['title_id'])
                cancelled.append(a['action_id'])
        self.incidents.append({'at': at, 'kind': 'title_withdrawn', 'title_id': c['title_id'], 'content_slug': slug,
                               'cancelled_pending_messages': cancelled, 'live_pages_superseded': repaired,
                               'affected_viewers': sorted({r['viewer_id'] for r in repaired} | {a['user_id'] for a in aff}),
                               'handling': 'catalogue availability updated from this instant (all language variants); '
                                           'pending messages cancelled; live pages replaced by a corrected page'})

    def on_goal_rule_corrected(self, c, at):
        gid = c['goal_id']
        v = self.registry.correct(gid, c['new_success'], at)
        affected, now_succeeded = [], []
        for user in sorted(self.open_goals.get(gid, set())):
            ag = self.agent(user)
            goal = ag.st['active_goal']
            if not goal or goal['goal_id'] != gid or goal['status'] != 'open':
                continue
            old = dict(goal['success'])
            goal['rule_version'], goal['success'] = v['version'], dict(v['success'])
            if v['success'].get('window_hours'):
                goal['deadline'] = goal['started_at'] + timedelta(hours=v['success']['window_hours'])
            ag.st['rule_migrations'].append({'at': at, 'goal_id': gid, 'from': old, 'to': goal['success'],
                                             'reason': c.get('note')})
            j = judge_goal(goal, self.idx.upto(user, at), self.pack.catalogue, at)
            if j['status'] == 'succeeded':
                ag._close(goal, 'succeeded', at, j)
                now_succeeded.append(user)
            self.store.save(user)
            affected.append(user)
        total_open = sum(len(s) for s in self.open_goals.values())
        self.incidents.append({'at': at, 'kind': 'goal_rule_corrected', 'goal_id': gid, 'new_version': v['version'],
                               'new_success': v['success'], 'affected_open_goals': len(affected),
                               'affected_viewers': affected[:200], 'succeeded_under_new_rule_now': now_succeeded,
                               'other_open_goals_untouched': total_open - len(affected),
                               'handling': 'only open %s goals were migrated to %s and re-evaluated; '
                                           'no other viewer was reprocessed' % (gid, v['version'])})

    # ------------------------------------------------------------------ replay
    def run(self, limit=None, progress=False):
        pack, cfg = self.pack, self.cfg
        triggers = sorted(pack.triggers, key=lambda e: e['deliver_at'])
        if limit:
            triggers = triggers[:limit]
        others = [(ts(e['deliver_at']), 1, 'live', e) for e in pack.live] + \
                 [(ts(c['at']), 0, 'control', c) for c in pack.control]
        others.sort(key=lambda x: (x[0], x[1]))
        horizon_stop = ts(triggers[-1]['deliver_at']) if triggers else None
        p = 0
        free = [ts(triggers[0]['deliver_at'])] * cfg.workers if triggers else []
        heapq.heapify(free)
        wall0 = time.perf_counter()

        def apply_others(upto):
            nonlocal p
            while p < len(others) and others[p][0] <= upto:
                at, _, kind, e = others[p]
                p += 1
                if kind == 'control':
                    self.on_control(e)
                else:
                    from .pack import stamp
                    stamp(e)
                    if self.idx.add(e) and e['user_id'] in self.agents:
                        self.agent(e['user_id']).react(e, at)
                if not self.injector.store_healthy(at):
                    continue
                if self.store.dirty:
                    self.store.flush()
        for i, trig in enumerate(triggers):
            deliver = ts(trig['deliver_at'])
            apply_others(deliver)
            start = max(deliver, heapq.heappop(free))
            apply_others(start)
            wait = (start - deliver).total_seconds()
            dec, dur = self.agent(trig['user_id']).on_trigger(trig, deliver, start, wait)
            heapq.heappush(free, start + timedelta(seconds=dur))
            if progress and i % 500 == 0:
                print('  %d/%d decisions' % (i, len(triggers)), flush=True)
        apply_others(ts('2999-01-01T00:00:00Z'))
        n = self.store.flush()
        self.metrics['deferred_writes_flushed_at_end'] = n
        self.metrics['wall_seconds'] = round(time.perf_counter() - wall0, 2)
        self.incidents += [dict(i_, kind='model_circuit_breaker_opened', dependency='model_provider') for i_ in self.gateway.incidents]
        return self

    # ------------------------------------------------------------------ output
    def write(self):
        out = self.out_dir
        os.makedirs(out, exist_ok=True)

        def w(name, rows):
            with open(os.path.join(out, name), 'w', encoding='utf-8') as f:
                for r in rows:
                    f.write(dumps(r) + '\n')
        w('decisions.jsonl', self.decisions)
        exported = [self._export(a) for a in self.ledger.actions if a['status'] in ('PUBLISHED', 'SUPERSEDED')]
        w('actions.jsonl', exported)
        w('actions_log.jsonl', self.ledger.log)
        w('incidents.jsonl', sorted(self.incidents, key=lambda i: str(i['at'])))
        return self

    @staticmethod
    def _export(a):
        """Simulator-ready action: only what the README's action schema defines (+ goal for traceability)."""
        drop = {'status', 'cancelled_at', 'cancel_reason', 'superseded_at', 'supersede_reason'}
        return {k: v for k, v in a.items() if k not in drop}
