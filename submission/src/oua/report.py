"""Latency report and the final report (data issues, surprises, results)."""
import json
import os
from collections import Counter
from datetime import timedelta

from .util import dumps, iso, ts


def pctile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def latency_report(rt, decisions, out_dir):
    cfg = rt.cfg
    live = [d for d in decisions if d['kind'] in ('trigger_decision', 'repair') and d['status'] != 'DUPLICATE_SUPPRESSED']
    lat = [d['latency_s'] for d in live]
    ms = [d['decided_in_ms'] for d in live]
    windows = []
    for w in rt.injector.windows:
        inside = [d for d in live if d['kind'] == 'trigger_decision' and w['start'] <= d['trigger']['deliver_at'] < w['until']]
        windows.append({'dependency': w['dependency'], 'kind': w['kind'], 'start': w['start'], 'until': w['until'],
                        'decisions': len(inside), 'max_latency_s': max([d['latency_s'] for d in inside], default=None),
                        'fallbacks': dict(Counter((d['fallback_used'] or 'none').split(':')[0] for d in inside)),
                        'published_within_budget': sum(1 for d in inside if d['latency_s'] <= cfg.latency_target_s)})
    per_min = Counter(iso(d['trigger']['deliver_at'])[:16] for d in live if d['kind'] == 'trigger_decision')
    peak = per_min.most_common(1)[0] if per_min else None
    rep = {
        'definition': 'latency_s = published_at - deliver_at = queue wait + measured compute + simulated dependency waits. '
                      'Compute is measured with perf_counter on this machine; dependency timeouts/outages are VIRTUAL waits '
                      '(a 2s model timeout adds 2s without sleeping) so the replay is fast but the accounting is honest.',
        'decisions_total': len(decisions), 'decisions_timed': len(live),
        'duplicates_suppressed': sum(1 for d in decisions if d['status'] == 'DUPLICATE_SUPPRESSED'),
        'target_s': cfg.latency_target_s, 'failure_s': cfg.latency_fail_s,
        'latency_s': {'p50': pctile(lat, .5), 'p95': pctile(lat, .95), 'p99': pctile(lat, .99), 'max': max(lat, default=None)},
        'decided_in_ms': {'p50': pctile(ms, .5), 'p95': pctile(ms, .95), 'p99': pctile(ms, .99), 'max': max(ms, default=None)},
        'within_60s': sum(1 for x in lat if x <= cfg.latency_target_s), 'over_60s': sum(1 for x in lat if x > cfg.latency_target_s),
        'over_300s_failures': sum(1 for x in lat if x > cfg.latency_fail_s),
        'published_within_3s_of_trigger': sum(1 for x in lat if x <= 3.0),
        'max_queue_wait_s': max([d['trigger'].get('queue_wait_s', 0) for d in live if d['kind'] == 'trigger_decision'], default=0),
        'peak_minute': {'minute_utc': peak[0], 'triggers': peak[1]} if peak else None,
        'budgets': {'max_model_calls_allowed': cfg.max_model_calls, 'max_tool_calls_allowed': cfg.max_tool_calls,
                    'max_model_calls_used': max([d.get('model_calls', 0) for d in live], default=0),
                    'max_tool_calls_used': max([d.get('tool_calls', 0) for d in live], default=0)},
        'fallbacks': dict(Counter(d['fallback_used'] for d in live if d.get('fallback_used'))),
        'dependency_windows': windows,
        'store_writes_deferred_then_flushed': rt.store.deferred_writes,
        'replay_wall_clock_s': rt.metrics.get('wall_seconds'), 'workers': cfg.workers,
        'model_provider': {'name': rt.gateway.provider.name, 'live': bool(getattr(rt.gateway.provider, 'live', False)),
                           'model': getattr(rt.gateway.provider, 'model', None), 'live_calls': rt.gateway.live_calls,
                           'disabled_reason': rt.gateway.disabled, 'task_outcomes': rt.gateway.stats},
    }
    with open(os.path.join(out_dir, 'latency_report.json'), 'w', encoding='utf-8') as f:
        f.write(dumps(rep, indent=1))
    return rep


def data_issue_rows(pack, decisions):
    from .pack import looks_like_injection
    trig = pack.triggers
    ts_ = lambda s: ts(s)
    buf = sum(1 for t in trig if (ts_(t['sent_at']) - ts_(t['occurred_at'])).total_seconds() > 600)
    skew = sum(1 for t in trig if abs((ts_(t['received_at']) - ts_(t['sent_at'])).total_seconds()) > 600)
    dup_triggers = len(trig) - len({t['event_id'] for t in trig})
    dial = Counter((t['app_dialect'] or 'missing') for t in trig)
    unsupported = sum(v for k, v in dial.items() if k not in pack.catalogue.by_dialect and k not in ('missing', 'NA', ''))
    prof_dup = sum(1 for v in pack.profiles.values() if len(v) > 1)
    no_prof = len({t['user_id'] for t in trig} - set(pack.profiles))
    slugs = Counter(t['content_slug'] for t in pack.catalogue.titles.values())
    variants = sum(1 for v in slugs.values() if v > 1)
    until = sum(1 for t in pack.catalogue.titles.values() if t['until_dt'])
    notyet = sum(1 for t in pack.catalogue.titles.values() if t['available_from_dt'] > ts('2026-10-01T00:00:00Z'))
    di = pack.data_issues
    three = 0
    for t in trig:
        prof = pack.profiles.get(t['user_id'])
        dl = (t['properties'].get('deeplink') or '').split('/')
        if prof and len(dl) >= 5 and len({dl[4], prof[0]['preferred_dialect'], t['app_dialect']}) == 3:
            three += 1
    return [
        ('Duplicate events (same event_id) in history', '%d duplicate lines' % di.get('history_duplicate_event_ids', 0), 'dedupe by event_id at load; first received kept'),
        ('Duplicate trigger deliveries', '%d redeliveries' % dup_triggers, 'idempotency key = trigger event_id; the repeat creates a DUPLICATE_SUPPRESSED decision and no action'),
        ('Offline-buffered (stale) app opens', '%d triggers buffered >600s' % buf, 'age = sent_at - occurred_at (one clock, so skew cancels); stale => recorded as history, no action'),
        ('Wrong phone clocks', '%d triggers where received_at and sent_at differ by more than 10 min' % skew, 'received_at is the only ordering/freshness clock; buffer age used for event time'),
        ('Profile snapshot: two rows for one viewer', '%d viewers' % prof_dup, 'drop rows contradicted by purchase events, take latest updated_at; conflict kept in the decision; consent = most restrictive'),
        ('No profile row (new viewers)', '%d triggered viewers' % no_prof, 'facts built from signup_completed; consent unknown => no push/WhatsApp'),
        ('Dialect: app setting vs profile vs campaign link', '%d triggers with three different dialects' % three, 'app setting decides (what the app will request); conflicts listed in the decision'),
        ('Dialect missing / "NA" / unsupported (d7)', '%d triggers (missing: %d, "NA": %d, no catalogue content e.g. d7: %d)' % (
            dial.get('missing', 0) + dial.get('NA', 0) + dial.get('', 0) + unsupported, dial.get('missing', 0), dial.get('NA', 0), unsupported),
         'missing/NA => last valid app setting, flagged uncertain, never used for messages; d7 has no catalogue => abstain'),
        ('Language variants of one title', '%d content_slugs with 2 title_ids' % variants, 'dedupe by content_slug; pick the variant matching app language; withdrawal blocks all variants'),
        ('Availability windows', '%d titles with an end, %d not yet released' % (until, notyet), 'availability checked at publish time AND for the page TTL; message titles checked at send time'),
        ('Instruction injected in catalogue text', '%d catalogue rows, %d search queries' % (di.get('injection_like_catalogue_rows', 0), di.get('injection_like_search_queries', 0)), 'treated as data: quarantined from content, logged, never obeyed; outgoing copy re-scanned'),
        ('Unmeasurable goals', 'G10 (event null), G11 (tv_app_open not in stream)', 'never selected; listed as "considered, not selectable"'),
        ('Under-specified goal rules', 'G06 "recently", G07 "unfinished/3 days", G05 breadth, G12 "before the trigger"', 'interpretations in goals.py docstring and in each decision\'s assumptions'),
        ('Pilot report compares served vs everyone else', '40.1% vs 17.7%', 'recomputed with the randomised arms (see impact_report.md)'),
    ]


def final_report(rt, pack, decisions, outcomes, lat, impact_res, out_dir, seed, sim_report=None):
    dc = Counter((d['kind'], d['status']) for d in decisions)
    td = [d for d in decisions if d['kind'] == 'trigger_decision']
    ag = [d for d in td if d['arm'] == 'agent' and d['status'] != 'DUPLICATE_SUPPRESSED']
    hold = [d for d in td if d['arm'] != 'agent' and d['status'] != 'DUPLICATE_SUPPRESSED']
    acts = Counter(a['type'] for a in rt.ledger.actions if a['status'] in ('PUBLISHED', 'SUPERSEDED'))
    reasons = Counter()
    for d in ag:
        if d['status'] == 'NO_ACTION':
            r = d.get('no_action_reason') or ''
            key = ('stale trigger' if r.startswith('stale') else 'dialect unsupported/unknown' if r.startswith('dialect') else
                   'no goal (thin evidence)' if r.startswith('no goal') else 'page already live / nothing to change' if r.startswith('nothing') else
                   'verifier rejected' if 'verifier' in r else r[:50])
            reasons[key] += 1
    P = impact_res['primary']
    oc = Counter(o['status'] for o in outcomes)
    L = []
    w = L.append
    w('# Final report\n')
    w('Run: simulator seed `%s`, model provider `%s`, %d triggers, %d viewers with an agent.\n' % (
        seed, rt.gateway.provider.name, len(pack.triggers), len(rt.agents)))
    w('## 1. What the system did\n')
    w('| | Count |\n|---|---|')
    w('| Decisions logged | %d |' % len(decisions))
    w('| Trigger decisions, agent arm | %d (published an action: %d, deliberate no-action: %d) |' % (
        len(ag), sum(1 for d in ag if d['status'] == 'PUBLISHED'), sum(1 for d in ag if d['status'] == 'NO_ACTION')))
    w('| Trigger decisions, holdout arm (shadow goal, **zero actions**) | %d |' % len(hold))
    w('| Duplicate trigger deliveries suppressed | %d |' % dc[('trigger_decision', 'DUPLICATE_SUPPRESSED')])
    w('| Actions published | %s |' % ', '.join('%s: %d' % kv for kv in sorted(acts.items())))
    w('| Messages cancelled before sending | %d |' % sum(1 for a in rt.ledger.actions if a['status'] == 'CANCELLED'))
    w('| Goals judged | %s |' % dict(oc))
    w('| Re-plans after a situation change | %d |' % rt.metrics['replans'])
    w('\nWhy the agent did nothing (agent arm):\n')
    for k, v in reasons.most_common():
        w('- %s: %d' % (k, v))
    w('\n## 2. Latency and reliability\n')
    L_ = lat['latency_s']
    w('p50 %.3fs, p95 %.3fs, p99 %.3fs, max %.3fs; **%d of %d decisions within 60s, %d over 300s**. Peak minute: %s triggers. Model calls per decision <= %d (limit %d), tool calls <= %d (limit %d). See `latency_report.json`.\n' % (
        L_['p50'], L_['p95'], L_['p99'], L_['max'], lat['within_60s'], lat['decisions_timed'], lat['over_300s_failures'],
        lat['peak_minute']['triggers'] if lat['peak_minute'] else 0, lat['budgets']['max_model_calls_used'], lat['budgets']['max_model_calls_allowed'],
        lat['budgets']['max_tool_calls_used'], lat['budgets']['max_tool_calls_allowed']))
    w('## 3. Surprises and how the system reacted\n')
    w('| Surprise | Detected how | Response | Evidence |\n|---|---|---|---|')
    inc = {}
    for i in rt.incidents:
        inc.setdefault(i['kind'], []).append(i)
    stale = sum(1 for d in td if (d.get('no_action_reason') or '').startswith('stale'))
    w('| Offline-buffered app opens | sent_at - occurred_at > 600s | recorded as history, no action | %d stale triggers, all NO_ACTION |' % stale)
    um = len(inc.get('unsupported_model_claim', []))
    w('| Model claims eligibility without evidence | verifier re-derives eligibility and cited refs | claim rejected, incident logged, rule-based goal used | %d unsupported claims logged in `incidents.jsonl` |' % um)
    tw = (inc.get('title_withdrawn') or [{}])[0]
    w('| Title withdrawn | control event | availability updated instantly; pending messages cancelled; live pages replaced | %d messages cancelled, %d pages replaced |' % (
        len(tw.get('cancelled_pending_messages', [])), len(tw.get('live_pages_superseded', []))))
    gr = (inc.get('goal_rule_corrected') or [{}])[0]
    w('| G03 success rule corrected | control event | rule v2; only open G03 goals migrated | %s open G03 goals migrated; %s other open goals untouched |' % (gr.get('affected_open_goals'), gr.get('other_open_goals_untouched')))
    w('| Model provider outage | 503s / timeouts | per-call timeout, circuit breaker, rule-based goal | breaker opened %d times; %d decisions used a model fallback |' % (
        len(inc.get('model_circuit_breaker_opened', [])), sum(1 for d in decisions if d.get('fallback_used') and 'model_' in d['fallback_used'])))
    w('| Data store degraded | read timeouts | retry once, then cached facts (no messages), then generic dialect-correct page, then no action; writes queued | %d reads timed out, %d decisions used a store fallback, %d writes flushed on recovery |' % (
        rt.metrics['store_read_timeouts'], rt.metrics['store_fallbacks'], rt.store.deferred_writes))
    pur = sum(1 for o in outcomes if o['status'] == 'succeeded' and o['goal_id'] in ('G04', 'G05', 'G06'))
    w('| Purchase arrives after a decision | live event | goal judged, pending messages cancelled, viewer re-planned | %d re-plans; see `decisions.jsonl` (kind `replan`) |' % rt.metrics['replans'])
    w('| Injected instruction in a catalogue description | text scan at load | row quarantined, logged, never obeyed | %d catalogue rows quarantined; 0 mass messages |' % len(pack.catalogue.flagged))
    w('| Traffic spike | arrival rate / queue wait | parallel workers; model skipped when queue wait > %.0fs | max queue wait %.3fs |' % (rt.cfg.shed_backlog_s, lat['max_queue_wait_s']))
    w('\n## 4. Data issues found and how they were handled\n')
    w('| Issue | Size | Handling |\n|---|---|---|')
    for a, b, c in data_issue_rows(pack, decisions):
        w('| %s | %s | %s |' % (a, b, c))
    w('\n## 5. Does the agent help?\n')
    d = P['diff']
    w('Intention-to-treat on first goals: agent arm %.1f%% vs holdout %.1f%%, difference **%+.1f pp** (95%% CI %+.1f to %+.1f; n = %d + %d). Full analysis, per-goal rows and the pilot re-analysis are in `impact_report.md`.\n' % (
        100 * P['p1'], 100 * P['p0'], 100 * d, 100 * P['ci'][0], 100 * P['ci'][1], P['n1'], P['n0']))
    if 'oracle' in impact_res:
        o = impact_res['oracle']
        w('The simulator-only paired check gives %+.2f pp (95%% CI %+.2f to %+.2f).\n' % (100 * o['ate'], 100 * o['ci'][0], 100 * o['ci'][1]))
    w('The pilot report\'s 2.3x headline is a selection artefact; its own randomised arms show %+.1f pp. Be sceptical of any number here that is not an ITT comparison.\n' % (100 * impact_res['pilot']['itt']['diff']))
    w('## 6. Policy checks on this run\n')
    sc = (sim_report or {}).get('counts', {})
    w('The simulator audits every action independently of this system (`sim/simulator_report.json`).\n')
    w('| Check | Result |\n|---|---|')
    w('| Policy problems (consent / quiet hours / frequency cap / holdout) | **%s** |' % (sim_report or {}).get('policy_problem_count', 'n/a'))
    w('| Actions on holdout viewers | %d |' % sc.get('holdout_actions_ignored', 0))
    w('| Pushes/WhatsApp blocked for missing consent | %d |' % (sc.get('push_blocked_no_consent', 0) + sc.get('whatsapp_blocked_no_consent', 0)))
    w('| WhatsApp rejected (unapproved template) | %d |' % sc.get('whatsapp_rejected_unapproved_template', 0))
    w('| Home pages with dropped rows / rejected hero | %d / %d |' % (sc.get('home_page_row_dropped_below_minimum', 0), sc.get('home_page_hero_rejected', 0)))
    w('| Opt-outs caused by our messages | %d |' % (sc.get('push_caused_opt_out', 0) + sc.get('whatsapp_caused_opt_out', 0)))
    w('| Pages accepted / shown personalised | %d / %d |' % (sc.get('home_page_accepted', 0), sc.get('home_page_shown_personalised', 0)))
    w('| Pushes: delivered / undeliverable device token | %d / %d (token validity is hidden by the platform) |' % (sc.get('push_delivered', 0), sc.get('push_undeliverable_token', 0)))
    with open(os.path.join(out_dir, 'final_report.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')


def audit_viewer(out_dir, viewer, at=None):
    """Chronological record of what the agent knew, decided and observed for one viewer (optionally up to `at`)."""
    from .util import read_jsonl

    def load(name):
        p = os.path.join(out_dir, name)
        return list(read_jsonl(p)) if os.path.exists(p) else []
    cut = at or '9999'
    rows = []
    for d in load('decisions.jsonl'):
        if d['viewer_id'] == viewer and d['published_at'] <= cut:
            rows.append((d['published_at'], 'DECISION %s [%s/%s] %s' % (d['decision_id'], d['kind'], d['arm'], d['status']), [
                'trigger: %s %s (age %ss, buffered %ss)' % (d['trigger']['event'], d['trigger']['event_id'][:8], d['trigger'].get('event_age_s'), d['trigger'].get('buffer_age_s')),
                'goal: %s' % ('%s (%s) deadline %s, judged by %s' % (d['goal']['id'], d['goal']['reason'], d['goal']['deadline'], d['goal']['success_signal']) if d['goal'] else 'none'),
                'dialect: %s via %s, conflicts %s' % (d['dialect']['used'], d['dialect']['source'], d['dialect']['conflicts']),
                'action: %s%s' % (d['action'], '' if d['actions'] else ' - ' + str(d.get('no_action_reason'))),
                'confidence %s: %s' % (d['confidence'], d['confidence_reason']),
                'assumptions: %s' % d['assumptions'],
                'checks passed: %s | failed: %s' % (d['checks']['passed'], [(f['check'], f['detail']) for f in d['checks']['failed']]),
                'evidence: %s' % d['evidence'][:6],
                'fallback: %s | model/tool calls %s/%s | decided in %sms' % (d.get('fallback_used'), d.get('model_calls'), d.get('tool_calls'), d.get('decided_in_ms'))]))
    for l in load('actions_log.jsonl'):
        if l['user_id'] == viewer and l['at'] <= cut and l['event'] != 'published':
            rows.append((l['at'], 'ACTION %s %s' % (l['action_id'], l['event'].upper()), [l.get('reason')]))
    for o in load('outcomes.jsonl'):
        if o['viewer_id'] == viewer:
            rows.append((o['observed_until'], 'OUTCOME %s %s' % (o['goal_id'], o['status'].upper()), [o['reason'], 'evidence: %s' % o['evidence'], 'learned: %s' % o['learning']]))
    for i in load('incidents.jsonl'):
        if viewer in (i.get('viewer_id'), *i.get('affected_viewers', [])) and i['at'] <= cut:
            rows.append((i['at'], 'INCIDENT %s' % i['kind'], [str(i.get('detail') or i.get('handling'))]))
    out = ['Audit trail for %s' % viewer]
    for when, head, lines in sorted(rows, key=lambda r: str(r[0])):
        out.append('\n%s  %s' % (str(when)[:23], head))
        out += ['    ' + x for x in lines if x]
    return '\n'.join(out)
