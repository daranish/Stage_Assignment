#!/usr/bin/env python3
"""Independent end-of-run audit of sample_run/actions.jsonl and decisions.jsonl against the pack (final checklist).

Deliberately does not import the agent: it re-reads the pack and the published files and re-applies the rules.
    python check_run.py --pack <pack> --run sample_run
Exit code 1 if any violation is found.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
ts = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None
jl = lambda p: [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pack', required=True)
    ap.add_argument('--run', default='sample_run')
    a = ap.parse_args()
    P = lambda *x: os.path.join(a.pack, *x)
    arms = {r['user_id']: r['arm'] for r in csv.DictReader(open(P('replay', 'arms.csv')))}
    cat = {r['title_id']: r for r in csv.DictReader(open(P('data', 'catalogue.csv'), encoding='utf-8'))}
    prof = {}
    for r in csv.DictReader(open(P('data', 'profiles_snapshot.csv'), encoding='utf-8')):
        prof.setdefault(r['user_id'], []).append(r)
    ctl = jl(P('replay', 'control_events.jsonl'))
    withdrawn = {c['title_id']: ts(c['at']) for c in ctl if c['type'] == 'title_withdrawn'}
    optout = {}
    for src in (P('data', 'events.jsonl'), P('replay', 'live_events.jsonl')):
        for e in jl(src):
            if e['event'] == 'notification_opt_out':
                optout.setdefault((e['user_id'], e['properties'].get('channel')), ts(e['received_at']))
    pol = json.load(open(P('config', 'interventions.json')))
    templates = {t['template_id'] for t in pol['whatsapp']['approved_templates']}
    forbidden = pol['push']['copy']['forbidden_phrases']
    acts = jl(os.path.join(a.run, 'actions.jsonl'))
    decs = jl(os.path.join(a.run, 'decisions.jsonl'))
    bad = []
    per_day = {}

    def avail(tid, at):
        t = cat.get(tid)
        if not t:
            return False
        if ts(t['available_from']) > at:
            return False
        if t['available_until'] and ts(t['available_until']) <= at:
            return False
        return not (tid in withdrawn and withdrawn[tid] <= at)

    for x in acts:
        u, typ, pub = x['user_id'], x['type'], ts(x['published_at'])
        if arms.get(u, 'holdout') != 'agent':
            bad.append(('holdout_action', x['action_id']))
        if typ == 'home_page':
            d = x['dialect']
            seen = set()
            tids = ([x['hero_title_id']] if x.get('hero_title_id') else []) + [t for r in x['rows'] for t in r['title_ids']]
            for r in x['rows']:
                if not 3 <= len(r['title_ids']) <= 10:
                    bad.append(('row_size', x['action_id']))
            if len(x['rows']) > 6:
                bad.append(('too_many_rows', x['action_id']))
            for t in tids:
                if cat[t]['dialect'] != d:
                    bad.append(('wrong_dialect', x['action_id'], t))
                if not avail(t, pub):
                    bad.append(('unavailable_at_publish', x['action_id'], t))
                if cat[t]['content_slug'] in seen:
                    bad.append(('duplicate_slug', x['action_id']))
                seen.add(cat[t]['content_slug'])
        else:
            send = ts(x.get('send_at') or x['published_at'])
            t = x.get('title_id')
            if t and (cat[t]['dialect'] != next((dd['dialect']['used'] for dd in decs if dd['decision_id'] == x['decision_id']), cat[t]['dialect']) or not avail(t, send)):
                bad.append(('message_title', x['action_id'], t))
            if typ in ('push', 'whatsapp'):
                col = 'notifications_opt_in' if typ == 'push' else 'whatsapp_opt_in'
                rows = prof.get(u, [])
                if not rows or not all(r[col] == 'true' for r in rows):
                    bad.append(('no_consent', x['action_id']))
                oo = optout.get((u, typ))
                if oo and oo <= send:
                    bad.append(('opted_out', x['action_id']))
                h = send.astimezone(IST).hour
                if h >= 22 or h < 8:
                    bad.append(('quiet_hours', x['action_id']))
                k = (u, send.astimezone(IST).date())
                per_day[k] = per_day.get(k, 0) + 1
                if per_day[k] > 1:
                    bad.append(('cap', x['action_id']))
            if typ == 'whatsapp' and x.get('template_id') not in templates:
                bad.append(('template', x['action_id']))
            if typ in ('push', 'in_app_message'):
                txt = x['text']
                if len(txt['title']) > 40 or len(txt['body']) > 120 or any(f in (txt['title'] + txt['body']).lower() for f in forbidden):
                    bad.append(('copy', x['action_id']))
            if typ == 'push' and x.get('deeplink') != 'ott://title/%s' % t:
                bad.append(('deeplink', x['action_id']))
    late = [d['decision_id'] for d in decs if d.get('latency_s', 0) > 60]
    missing = [d['decision_id'] for d in decs if d['status'] != 'DUPLICATE_SUPPRESSED' and not all(
        k in d for k in ('evidence', 'confidence', 'confidence_reason', 'assumptions', 'checks', 'decided_in_ms', 'conflicts'))]
    noact = [d['decision_id'] for d in decs if d['status'] == 'NO_ACTION' and not d.get('no_action_reason')]
    print('actions audited: %d | decisions: %d' % (len(acts), len(decs)))
    print('violations: %d %s' % (len(bad), bad[:5]))
    print('decisions over 60s: %d | missing required fields: %d | no-action without reason: %d' % (len(late), len(missing), len(noact)))
    sys.exit(1 if bad or late or missing or noact else 0)


if __name__ == '__main__':
    main()
