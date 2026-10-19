"""Outcome judge: closes the loop. Judges every committed goal from attributable events only, at the rule version
recorded on the goal, then writes outcomes and what was learned back into each viewer's durable state.
"""
import json
import os
from collections import defaultdict
from datetime import timedelta

from .goals import judge_goal, signal_text
from .pack import stamp
from .util import dumps, ist_midnight, iso, ts, read_jsonl, ist_day


def replay_day(pack):
    """IST calendar day of the replay (the day most triggers are delivered on): never hard-coded."""
    days = [ist_day(ts(t['deliver_at'])) for t in pack.triggers]
    return max(set(days), key=days.count)


def horizon_end(pack, cfg):
    """The simulator observes `observation_days` after the end of the replay day."""
    return ist_midnight(replay_day(pack)) + timedelta(days=1 + cfg.observation_days)


def load_outcome_events(path):
    by = defaultdict(list)
    for e in read_jsonl(path):
        stamp(e)
        by[e['user_id']].append(e)
    return by


def all_goals(st):
    gs = list(st['goal_history'])
    if st['active_goal'] is not None:
        gs.append(st['active_goal'])
    return gs


def judge_goals(rt, outcome_by_user, horizon, users=None):
    """Pure: returns {(user, index): judgement}. Does not touch state."""
    cat = rt.pack.catalogue
    out = {}
    for user, ag in rt.agents.items():
        if users is not None and user not in users:
            continue
        events = None
        for i, g in enumerate(all_goals(ag.st)):
            if g['status'] == 'superseded':
                out[(user, i)] = {'status': 'inconclusive', 'reason': 'goal superseded by a plan change: %s' % g['close_info'].get('reason'), 'evidence': []}
                continue
            if events is None:
                events = sorted(rt.idx.by_user.get(user, []) + outcome_by_user.get(user, []), key=lambda e: e['_r'])
            out[(user, i)] = judge_goal(g, events, cat, horizon)
    return out


def judge_and_learn(rt, outcome_path, cfg, out_dir):
    horizon = horizon_end(rt.pack, cfg)
    outcome_by_user = load_outcome_events(outcome_path)
    judgements = judge_goals(rt, outcome_by_user, horizon)
    outcomes = []
    for user, ag in rt.agents.items():
        st = ag.st
        st['outcomes'], st['learnings'] = [], []
        st['goal_stats'] = {}
        for i, g in enumerate(all_goals(st)):
            j = judgements[(user, i)]
            ext = sorted(outcome_by_user.get(user, []), key=lambda e: e['_r'])
            win = [e for e in ext if g['started_at'] < e['_r'] <= min(g['deadline'], horizon)]
            shown = [e for e in win if e['event'] == 'home_page_shown' and e['properties'].get('personalised')]
            deliv = [e for e in win if e['event'] == 'message_delivered']
            clicks = [e for e in win if e['event'] == 'message_clicked']
            first_exposure = min([e['_r'] for e in shown + deliv], default=None)
            succ_ev = next((e for e in win if j['evidence'] and e['event_id'] == j['evidence'][0][6:]), None)
            attributable_exposure = bool(succ_ev and first_exposure and first_exposure <= succ_ev['_r'])
            gs = st['goal_stats'].setdefault(g['goal_id'], {'attempts': 0, 'succeeded': 0, 'failed': 0, 'inconclusive': 0})
            gs['attempts'] += 1
            gs[j['status']] += 1
            acts = [a['action_id'] for a in rt.ledger.by_user.get(user, []) if a.get('goal_id') == g['goal_id'] and a['status'] != 'CANCELLED']
            opens = len([e for e in win if e['event'] == 'app_open'])
            plays = len([e for e in win if e['event'] == 'play_started'])
            if j['status'] == 'succeeded':
                lesson = 'achieved %s%s' % (g['goal_id'], ' after personalised exposure (association only, not proof of cause)' if attributable_exposure else ' with no prior exposure by the agent')
            elif j['status'] == 'failed':
                lesson = '%s not achieved (%d failure(s) for this goal): %d app opens and %d plays in window%s' % (
                    g['goal_id'], gs['failed'], opens, plays, ', exposure delivered' if (shown or deliv) else ', no exposure')
                if gs['failed'] >= 2:
                    lesson += '; goal deprioritised for this viewer in future selection'
            else:
                lesson = 'inconclusive: ' + j['reason']
            rec = {'viewer_id': user, 'arm': st['arm'], 'goal_id': g['goal_id'], 'goal_index': i,
                   'goal_name': g['name'], 'rule_version': g['rule_version'], 'success_signal': signal_text(g['success']),
                   'started_at': g['started_at'], 'deadline': g['deadline'], 'observed_until': min(g['deadline'], horizon),
                   'status': j['status'], 'reason': j['reason'], 'evidence': j['evidence'],
                   'attribution': {'agent_actions': acts, 'personalised_pages_shown': len(shown), 'messages_delivered': len(deliv),
                                   'messages_clicked': len(clicks), 'success_after_exposure': attributable_exposure,
                                   'note': 'exposure-before-success is an association; causal impact is measured only by the holdout comparison'},
                   'app_opens_in_window': opens, 'plays_in_window': plays, 'learning': lesson,
                   'decision_id': g['decision_id']}
            outcomes.append(rec)
            st['outcomes'].append({k: rec[k] for k in ('goal_id', 'status', 'reason', 'evidence', 'rule_version', 'deadline')})
            st['learnings'].append({'goal_id': g['goal_id'], 'lesson': lesson, 'status': j['status']})
            g['final_judgement'] = {'status': j['status'], 'reason': j['reason'], 'evidence': j['evidence']}
        rt.store.save(user)
    rt.store.flush()
    with open(os.path.join(out_dir, 'outcomes.jsonl'), 'w', encoding='utf-8') as f:
        for r in outcomes:
            f.write(dumps(r) + '\n')
    return outcomes
