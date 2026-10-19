"""Outcome judging and impact maths."""
import unittest
from datetime import timedelta

from fixtures import NOW, ev, tid

from oua.goals import judge_goal
from oua.impact import two_prop, bootstrap_ci
from oua.pack import stamp


class Judging(unittest.TestCase):
    def goal(self, gid='G01', hours=24, **kw):
        g = {'goal_id': gid, 'started_at': NOW, 'deadline': NOW + timedelta(hours=hours), 'status': 'open', 'rule_version': 'v1',
             'success': {'event': 'play_started', 'condition': 'any', 'window_hours': hours}}
        g.update(kw)
        return g

    def test_buffered_event_from_before_the_goal_is_not_attributable(self):
        # received after the goal started, but the phone says it happened 2 hours earlier
        e = stamp(ev('U', 'play_started', NOW + timedelta(minutes=5), sent_gap=7200, title_id=tid('d1', 1), episode_id='x'))
        self.assertEqual(judge_goal(self.goal(), [e], {}, NOW + timedelta(days=5))['status'], 'failed')

    def test_event_after_deadline_does_not_count_and_open_window_is_inconclusive(self):
        late = stamp(ev('U', 'play_started', NOW + timedelta(hours=30), title_id=tid('d1', 1), episode_id='x'))
        self.assertEqual(judge_goal(self.goal(), [late], {}, NOW + timedelta(days=5))['status'], 'failed')
        self.assertEqual(judge_goal(self.goal(), [], {}, NOW + timedelta(hours=3))['status'], 'inconclusive')

    def test_goal_without_a_success_event_is_inconclusive_not_failed(self):
        g = self.goal('G10', success={'event': None, 'condition': 'engages meaningfully', 'window_hours': 168})
        self.assertEqual(judge_goal(g, [], {}, NOW + timedelta(days=30))['reason'], 'no_success_signal_in_event_stream')

    def test_trial_purchase_does_not_satisfy_a_paid_goal(self):
        g = self.goal('G05', hours=168, success={'event': 'purchase_completed', 'condition': 'is_trial = false', 'window_hours': 168})
        trial = stamp(ev('U', 'purchase_completed', NOW + timedelta(hours=1), plan_id='P_TRIAL', price_inr=1, is_trial=True))
        paid = stamp(ev('U', 'purchase_completed', NOW + timedelta(hours=2), plan_id='P_MONTH', price_inr=149, is_trial=False))
        self.assertEqual(judge_goal(g, [trial], {}, NOW + timedelta(days=30))['status'], 'failed')
        self.assertEqual(judge_goal(g, [trial, paid], {}, NOW + timedelta(days=30))['status'], 'succeeded')


class Impact(unittest.TestCase):
    def test_difference_and_interval(self):
        r = two_prop(40, 100, 20, 100)
        self.assertAlmostEqual(r['diff'], 0.20)
        self.assertLess(r['ci'][0], 0.20 < r['ci'][1] and 0.20)
        self.assertLess(r['p_value'], 0.01)
        null = two_prop(20, 100, 20, 100)
        self.assertAlmostEqual(null['diff'], 0.0)
        self.assertLess(null['ci'][0], 0)
        self.assertGreater(null['ci'][1], 0)

    def test_bootstrap_interval_brackets_the_difference(self):
        lo, hi = bootstrap_ci([1] * 40 + [0] * 60, [1] * 20 + [0] * 80, reps=500)
        self.assertLess(lo, 0.20)
        self.assertGreater(hi, 0.20)


if __name__ == '__main__':
    unittest.main()
