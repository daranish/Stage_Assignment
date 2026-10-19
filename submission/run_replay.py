#!/usr/bin/env python3
"""One command: replay the trigger script, simulate outcomes, judge goals, measure impact, write all reports.

    python run_replay.py --pack <path to one_user_one_agent_pack> --out sample_run [--seed S]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from oua.cli import main  # noqa: E402

if __name__ == '__main__':
    main(['run-all'] + sys.argv[1:])
