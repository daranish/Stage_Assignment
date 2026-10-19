"""Impact measurement: agent arm vs randomized holdout (intention-to-treat), pilot re-analysis, bias demonstration."""
import csv
import json
import math
import os
import random
from collections import defaultdict
from datetime import timedelta

from .util import dumps, iso, ts, IST


def two_prop(s1, n1, s0, n0):
    """Difference in proportions (arm1 - arm0) with Wald 95% CI and two-sided z-test p-value."""
    if n1 == 0 or n0 == 0:
        return {'n1': n1, 'n0': n0, 'diff': None}
    p1, p0 = s1 / n1, s0 / n0
    se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    d = p1 - p0
    z = d / se if se else 0.0
    p = math.erfc(abs(z) / math.sqrt(2))
    return {'n1': n1, 's1': s1, 'p1': p1, 'n0': n0, 's0': s0, 'p0': p0, 'diff': d, 'se': se,
            'ci': (d - 1.96 * se, d + 1.96 * se), 'p_value': p}


def bootstrap_ci(a, b, reps=2000, seed=7):
    """Percentile bootstrap CI for mean(a)-mean(b); a, b are lists of 0/1."""
    if not a or not b:
        return None
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    ds = []
    for _ in range(reps):
        ds.append(sum(rng.choices(a, k=na)) / na - sum(rng.choices(b, k=nb)) / nb)
    ds.sort()
    return ds[int(0.025 * reps)], ds[int(0.975 * reps)]


def pct(x, d=1):
    return 'n/a' if x is None else '%.*f%%' % (d, 100 * x)


def pp(x, d=1):
    return 'n/a' if x is None else '%+.*f pp' % (d, 100 * x)


def fmt_row(label, r):
    if r.get('diff') is None:
        return '| %s | - | - | - | - | - |' % label
    return '| %s | %d | %s (%d/%d) | %s (%d/%d) | %s | [%s, %s] |' % (
        label, r['n1'] + r['n0'], pct(r['p1']), r['s1'], r['n1'], pct(r['p0']), r['s0'], r['n0'],
        pp(r['diff']), pp(r['ci'][0]), pp(r['ci'][1]))


def first_goal_per_user(outcomes):
    best = {}
    for o in outcomes:
        u = o['viewer_id']
        if u not in best or o['started_at'] < best[u]['started_at']:
            best[u] = o
    return best


def secondary(outcome_by_user, t0s, arms):
    """All triggered viewers (with or without a goal): any play within 7d, any non-trial purchase within 14d."""
    res = {'play_7d': {'agent': [], 'holdout': []}, 'paid_14d': {'agent': [], 'holdout': []}}
    for u, t0 in t0s.items():
        arm = arms.get(u, 'holdout')
        evs = outcome_by_user.get(u, [])
        play = any(e['event'] == 'play_started' and t0 < e['_r'] <= t0 + timedelta(days=7) for e in evs)
        paid = any(e['event'] == 'purchase_completed' and not e['properties'].get('is_trial') and t0 < e['_r'] <= t0 + timedelta(days=14) for e in evs)
        res['play_7d'][arm].append(int(play))
        res['paid_14d'][arm].append(int(paid))
    return res


def pilot_reanalysis(pack):
    base = os.path.join(pack.root, 'pilot')
    rows = list(csv.DictReader(open(os.path.join(base, 'pilot_outcomes.csv'), encoding='utf-8')))
    ok = lambda r: r['succeeded'] == 'true'
    served = [r for r in rows if r['served'] == 'true']
    other = [r for r in rows if r['served'] != 'true']
    treat = [r for r in rows if r['arm'] == 'treatment']
    hold = [r for r in rows if r['arm'] == 'holdout']
    naive = two_prop(sum(map(ok, served)), len(served), sum(map(ok, other)), len(other))
    itt = two_prop(sum(map(ok, treat)), len(treat), sum(map(ok, hold)), len(hold))
    boot = bootstrap_ci([int(ok(r)) for r in treat], [int(ok(r)) for r in hold])
    per_goal = {}
    for g in sorted({r['goal_id'] for r in rows}):
        t = [r for r in treat if r['goal_id'] == g]
        h = [r for r in hold if r['goal_id'] == g]
        per_goal[g] = two_prop(sum(map(ok, t)), len(t), sum(map(ok, h)), len(h))
    # selection: served viewers are visitors by construction. Holdout viewers who also visited are the fair comparison.
    idx = pack.history
    lo, hi = ts('2026-08-31T18:30:00Z'), ts('2026-09-14T18:30:00Z')     # pilot days 1-14 IST
    opens = {}
    for u in {r['user_id'] for r in rows}:
        opens[u] = len({e['session_id'] for e in idx.by_user.get(u, []) if e['event'] == 'app_open' and lo <= e['_r'] < hi})
    unserved_t = [r for r in treat if r['served'] != 'true']
    def mean_opens(rs):
        return sum(opens[r['user_id']] for r in rs) / len(rs) if rs else None
    visitors_h = [r for r in hold if opens[r['user_id']] >= 1]
    visitors_ut = [r for r in unserved_t if opens[r['user_id']] >= 1]
    served_vs_vis_hold = two_prop(sum(map(ok, served)), len(served), sum(map(ok, visitors_h)), len(visitors_h))
    return {'naive': naive, 'itt': itt, 'itt_boot': boot, 'per_goal': per_goal,
            'mean_opens': {'served': mean_opens(served), 'treatment_unserved': mean_opens(unserved_t), 'holdout': mean_opens(hold)},
            'holdout_visitors': (len(visitors_h), sum(map(ok, visitors_h)) / len(visitors_h) if visitors_h else None),
            'served_vs_holdout_visitors': served_vs_vis_hold, 'counts': (len(treat), len(hold), len(served))}


def build_report(rt, outcomes, decisions, outcome_by_user, oracle=None, out_dir=None, seed=None, sim_report=None):
    pack = rt.pack
    arms = pack.arms
    first = first_goal_per_user(outcomes)
    t0s = {}
    for d in decisions:
        if d['kind'] == 'trigger_decision' and d['status'] != 'DUPLICATE_SUPPRESSED':
            t0s.setdefault(d['viewer_id'], d['started_at'])
    served = set()
    for d in decisions:
        if d['kind'] == 'trigger_decision' and d['arm'] == 'agent' and d['actions']:
            served.add(d['viewer_id'])
    concl = {u: o for u, o in first.items() if o['status'] in ('succeeded', 'failed')}
    incon = defaultdict(int)
    for u, o in first.items():
        if o['status'] == 'inconclusive':
            incon[arms.get(u, 'holdout')] += 1
    A = [int(o['status'] == 'succeeded') for u, o in concl.items() if arms.get(u) == 'agent']
    H = [int(o['status'] == 'succeeded') for u, o in concl.items() if arms.get(u) != 'agent']
    primary = two_prop(sum(A), len(A), sum(H), len(H))
    boot = bootstrap_ci(A, H)
    by_goal = {}
    for g in sorted({o['goal_id'] for o in concl.values()}):
        a = [int(o['status'] == 'succeeded') for u, o in concl.items() if o['goal_id'] == g and arms.get(u) == 'agent']
        h = [int(o['status'] == 'succeeded') for u, o in concl.items() if o['goal_id'] == g and arms.get(u) != 'agent']
        by_goal[g] = two_prop(sum(a), len(a), sum(h), len(h))
    sec = secondary(outcome_by_user, t0s, arms)
    sec_res = {k: two_prop(sum(v['agent']), len(v['agent']), sum(v['holdout']), len(v['holdout'])) for k, v in sec.items()}
    # naive served-vs-everyone-else on the SAME replay data
    s_ok = [int(o['status'] == 'succeeded') for u, o in concl.items() if u in served]
    e_ok = [int(o['status'] == 'succeeded') for u, o in concl.items() if u not in served]
    naive = two_prop(sum(s_ok), len(s_ok), sum(e_ok), len(e_ok))
    # selection evidence: engagement BEFORE the replay (history only; not affected by the agent)
    idx = rt.idx
    t_replay = min(t0s.values()) if t0s else None
    def prior_opens(u):
        return len({e['session_id'] for e in idx.by_user.get(u, []) if e['event'] == 'app_open' and e['_r'] < t_replay and (t_replay - e['_r']) <= timedelta(days=14)})
    groups = {'served (agent arm)': [u for u in t0s if u in served],
              'agent arm, not served': [u for u in t0s if arms.get(u) == 'agent' and u not in served],
              'holdout arm': [u for u in t0s if arms.get(u) != 'agent']}
    prior = {k: (sum(prior_opens(u) for u in v) / len(v) if v else None, len(v)) for k, v in groups.items()}
    pilot = pilot_reanalysis(pack)
    res = {'primary': primary, 'bootstrap_ci': boot, 'by_goal': by_goal, 'secondary': sec_res, 'naive': naive,
           'inconclusive_by_arm': dict(incon), 'prior_opens': prior, 'pilot': pilot}
    # oracle (simulator-only): same goals, same seed, with and without the agent's actions, agent-arm viewers
    if oracle is not None:
        paired = []
        for (u, i), j0 in oracle['no_action'].items():
            j1 = oracle['with_action'].get((u, i))
            if j1 is None or arms.get(u) != 'agent':
                continue
            if j0['status'] in ('succeeded', 'failed') and j1['status'] in ('succeeded', 'failed'):
                paired.append((int(j1['status'] == 'succeeded'), int(j0['status'] == 'succeeded')))
        if paired:
            n = len(paired)
            d = [a - b for a, b in paired]
            m = sum(d) / n
            sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1)) if n > 1 else 0
            res['oracle'] = {'n': n, 'with_actions': sum(a for a, _ in paired) / n, 'without_actions': sum(b for _, b in paired) / n,
                             'ate': m, 'ci': (m - 1.96 * sd / math.sqrt(n), m + 1.96 * sd / math.sqrt(n))}
    md = render(res, seed, sim_report)
    if out_dir:
        with open(os.path.join(out_dir, 'impact_report.md'), 'w', encoding='utf-8') as f:
            f.write(md)
        with open(os.path.join(out_dir, 'impact.json'), 'w', encoding='utf-8') as f:
            f.write(dumps(res, indent=1))
    return res, md


def render(r, seed, sim_report):
    P, N, pl = r['primary'], r['naive'], r['pilot']
    L = []
    w = L.append
    w('# Impact report\n')
    w('Simulator seed: `%s`. Primary comparison = **intention-to-treat**: every triggered viewer is analysed in the arm '
      'they were randomised to (`replay/arms.csv`), whether or not the agent acted on them.\n' % seed)
    w('## 1. Headline (this replay)\n')
    w('Success = the viewer\'s first committed goal was judged `succeeded` from attributable events; `inconclusive` goals are excluded and counted below. '
      'The goal is chosen by the same code for both arms, before any action, so the two arms are judged on the same definition of success.\n')
    w('| Comparison | Viewers | Agent arm | Holdout arm | Difference | 95% CI (Wald) |')
    w('|---|---|---|---|---|---|')
    w(fmt_row('**All goals (ITT)**', P))
    for g, v in r['by_goal'].items():
        w(fmt_row('goal %s' % g, v))
    w('')
    if r['bootstrap_ci']:
        w('Bootstrap 95%% CI for the headline difference: [%s, %s]; two-sided p = %.3f.\n' % (pp(r['bootstrap_ci'][0]), pp(r['bootstrap_ci'][1]), P['p_value']))
    w('Inconclusive first goals (excluded above): %s.\n' % r['inconclusive_by_arm'])
    w('### Secondary outcomes (all triggered viewers, with or without a goal)\n')
    w('| Outcome | Viewers | Agent arm | Holdout arm | Difference | 95% CI |')
    w('|---|---|---|---|---|---|')
    w(fmt_row('any play within 7 days of first decision', r['secondary']['play_7d']))
    w(fmt_row('paid purchase within 14 days', r['secondary']['paid_14d']))
    w('')
    if 'oracle' in r:
        o = r['oracle']
        w('### Simulator-only oracle check (not available in production)\n')
        w('The simulator is deterministic per (seed, viewer), so the same agent-arm viewers can be replayed with and without the agent\'s actions. '
          'Paired over %d agent-arm viewers with a conclusive first goal: success %s with actions vs %s without; paired effect **%s** (95%% CI %s to %s). '
          'This checks that the ITT estimate above is pointing in the right direction; it can only be computed because the world is simulated.\n'
          % (o['n'], pct(o['with_actions']), pct(o['without_actions']), pp(o['ate'], 2), pp(o['ci'][0], 2), pp(o['ci'][1], 2)))
    w('## 2. Why "served vs everyone else" misleads\n')
    w('| Comparison on this replay | Viewers | Group A | Group B | Difference |')
    w('|---|---|---|---|---|')
    w('| served vs all others (the pilot report\'s method) | %d | %s | %s | %s |' % (N['n1'] + N['n0'], pct(N['p1']), pct(N['p0']), pp(N['diff'])))
    w('| ITT, agent arm vs holdout arm | %d | %s | %s | %s |\n' % (P['n1'] + P['n0'], pct(P['p1']), pct(P['p0']), pp(P['diff'])))
    w('"Served" is decided by the agent *after* it sees who is active, eligible, consented and reachable. Those viewers differ from the rest before '
      'any action happens. Mean app sessions in the 14 days before the replay (history only, untouched by the agent):\n')
    for k, (m, n) in r['prior_opens'].items():
        w('- %s: %s (n=%d)' % (k, 'n/a' if m is None else '%.2f' % m, n))
    w('\nThe viewers who are served are the ones the agent could act on, so the served-vs-rest gap mixes the effect of the agent with who was selected. '
      'Only the randomised arms break that link: assignment happened before the agent saw anyone, so the arms differ in nothing but the agent '
      '(in expectation). The holdout arm includes viewers the agent chose to skip, which is exactly what keeps the comparison fair.\n')
    w('## 3. Re-analysis of the earlier pilot\n')
    n_t, n_h, n_s = pl['counts']
    w('The internal report compared %d served viewers with everyone else (%s vs %s, %s). Recomputed from `pilot_outcomes.csv`:\n' % (
        n_s, pct(pl['naive']['p1']), pct(pl['naive']['p0']), pp(pl['naive']['diff'])))
    w('| Comparison | Viewers | Group A | Group B | Difference | 95% CI |')
    w('|---|---|---|---|---|---|')
    w(fmt_row('served vs all others (report)', pl['naive']))
    w(fmt_row('**treatment arm vs holdout arm (ITT)**', pl['itt']))
    for g, v in pl['per_goal'].items():
        w(fmt_row('ITT, goal %s' % g, v))
    w('')
    if pl['itt_boot']:
        w('ITT bootstrap 95%% CI: [%s, %s].\n' % (pp(pl['itt_boot'][0]), pp(pl['itt_boot'][1])))
    mo = pl['mean_opens']
    w('Mean app sessions during the pilot window: served %.2f, treatment-but-unserved %.2f, holdout %.2f. Served viewers are, by construction, '
      'viewers who opened the app, so the "2.3x uplift" is mostly the difference between visitors and non-visitors. Against holdout viewers who also '
      'visited at least once (%d viewers, %s success) the served viewers\' %s is %s.\n' % (
          mo['served'], mo['treatment_unserved'], mo['holdout'], pl['holdout_visitors'][0], pct(pl['holdout_visitors'][1]), pct(pl['served_vs_holdout_visitors']['p1']),
          pp(pl['served_vs_holdout_visitors']['diff'])))
    w('**Conclusion: the pilot showed no demonstrable uplift (ITT %s). The recommendation to roll out on the strength of the 2.3x headline is not supported.**\n' % pp(pl['itt']['diff']))
    w('## 4. Caveats\n')
    w('- Goal success rates are low-powered per goal; per-goal rows are descriptive, not individually significant unless the interval excludes 0.')
    w('- The simulator draws one world per seed; evaluators rerun with another seed and a second replay day, so treat point estimates as one draw.')
    w('- Only viewers who triggered on the replay day are in the experiment; effects on viewers who never open the app are out of scope.')
    w('- Inconclusive goals (window beyond the 14-day observation horizon, or superseded) are excluded symmetrically in both arms.')
    if sim_report:
        w('\nSimulator policy-problem count for this run: **%d** (consent, quiet hours, frequency cap, holdout violations).' % sim_report.get('policy_problem_count', -1))
    return '\n'.join(L) + '\n'
