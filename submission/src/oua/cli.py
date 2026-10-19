"""Command line.

  python -m oua run-all --pack <pack> --out sample_run [--seed S]     replay -> simulate -> judge -> impact -> reports
  python -m oua replay  --pack <pack> --out sample_run                decide + act only
"""
import argparse
import json
import os
import subprocess
import sys

from .config import Config
from .impact import build_report
from .judge import horizon_end, judge_and_learn, judge_goals, load_outcome_events
from .model import load_dotenv, make_model
from .pack import Pack
from .report import audit_viewer, final_report, latency_report
from .runtime import Runtime
from .util import read_jsonl


def make_runtime(a, cfg):
    pack = Pack(a.pack, a.replay_dir, a.arms)
    pack.load_history()
    model, note = make_model(a.model)
    print('model provider:', note)
    return pack, Runtime(pack, cfg, a.out, model=model)


def simulate(pack_root, actions, out, seed):
    sim = os.path.join(pack_root, 'simulator', 'simulate_outcomes.py')
    subprocess.run([sys.executable, '-I', sim, '--pack', pack_root, '--actions', actions, '--out', out, '--seed', seed],
                   check=True, stdout=subprocess.DEVNULL)
    return json.load(open(os.path.join(out, 'simulator_report.json'), encoding='utf-8'))


def cmd_replay(a, cfg):
    pack, rt = make_runtime(a, cfg)
    rt.run(limit=a.limit, progress=True)
    rt.write()
    print('decisions: %d  actions: %d  replay wall: %ss' % (len(rt.decisions), len(rt.ledger.actions), rt.metrics['wall_seconds']))
    return pack, rt


def cmd_run_all(a, cfg):
    pack, rt = cmd_replay(a, cfg)
    out = a.out
    print('simulating outcomes (seed=%s) ...' % a.seed)
    sim_dir = os.path.join(out, 'sim')
    rep = simulate(pack.root, os.path.join(out, 'actions.jsonl'), sim_dir, a.seed)
    print('judging goals and writing outcomes back into viewer state ...')
    outcomes = judge_and_learn(rt, os.path.join(sim_dir, 'outcome_events.jsonl'), cfg, out)
    oracle = None
    if not a.no_oracle:
        print('oracle: re-simulating without any agent action ...')
        empty = os.path.join(out, 'sim_noaction')
        os.makedirs(empty, exist_ok=True)
        open(os.path.join(empty, 'no_actions.jsonl'), 'w').close()
        simulate(pack.root, os.path.join(empty, 'no_actions.jsonl'), empty, a.seed)
        hz = horizon_end(pack, cfg)
        oracle = {'with_action': judge_goals(rt, load_outcome_events(os.path.join(sim_dir, 'outcome_events.jsonl')), hz),
                  'no_action': judge_goals(rt, load_outcome_events(os.path.join(empty, 'outcome_events.jsonl')), hz)}
    decisions = rt.decisions
    obu = load_outcome_events(os.path.join(sim_dir, 'outcome_events.jsonl'))
    res, _ = build_report(rt, outcomes, decisions, obu, oracle, out, a.seed, rep)
    lat = latency_report(rt, decisions, out)
    final_report(rt, pack, decisions, outcomes, lat, res, out, a.seed, rep)
    P = res['primary']
    print('\nITT: agent %.1f%% (n=%d) vs holdout %.1f%% (n=%d)  diff %+.1f pp  CI [%+.1f, %+.1f]' % (
        100 * P['p1'], P['n1'], 100 * P['p0'], P['n0'], 100 * P['diff'], 100 * P['ci'][0], 100 * P['ci'][1]))
    print('simulator policy problems: %d' % rep['policy_problem_count'])
    print('latency p99 %.3fs  max %.3fs  over 60s: %d' % (lat['latency_s']['p99'], lat['latency_s']['max'], lat['over_60s']))
    print('reports written to', out)


def cmd_audit(a, cfg):
    print(audit_viewer(a.out, a.viewer, a.at))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='oua', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('replay', 'run-all'):
        p = sub.add_parser(name)
        p.add_argument('--pack', default=os.environ.get('OUA_PACK', '.'), help='root of the data pack (or env OUA_PACK)')
        p.add_argument('--replay-dir', help='replay folder (default <pack>/replay)')
        p.add_argument('--arms', help='arms.csv (default <replay-dir>/arms.csv)')
        p.add_argument('--out', default='sample_run')
        p.add_argument('--limit', type=int, help='only the first N triggers (debug)')
        p.add_argument('--model', choices=['auto', 'mock', 'openrouter', 'anthropic'], default='auto',
                       help='auto = OpenRouter if OPENROUTER_API_KEY is set (env or .env), else the deterministic mock')
        p.add_argument('--live-call-cap', type=int, default=0, help='max real model calls per run (0 = unlimited); free tiers are rate-limited')
        p.add_argument('--seed', default='oua-practice', help='simulator seed')
        p.add_argument('--no-oracle', action='store_true', help='skip the simulator-only paired check')
        p.add_argument('--max-model-calls', type=int, default=8)
        p.add_argument('--max-tool-calls', type=int, default=20)
        p.add_argument('--workers', type=int, default=8)
    au = sub.add_parser('audit', help='print what the agent knew, decided and observed for one viewer')
    au.add_argument('--out', default='sample_run')
    au.add_argument('--viewer', required=True)
    au.add_argument('--at', help='only events up to this ISO time')
    a = ap.parse_args(argv)
    load_dotenv()
    cfg = Config(live_call_cap=getattr(a, 'live_call_cap', 0), max_model_calls=getattr(a, 'max_model_calls', 8), max_tool_calls=getattr(a, 'max_tool_calls', 20), workers=getattr(a, 'workers', 8))
    {'replay': cmd_replay, 'run-all': cmd_run_all, 'audit': cmd_audit}[a.cmd](a, cfg)


if __name__ == '__main__':
    main()
