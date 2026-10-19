"""Behavioural tests. Run: python -m unittest discover -s tests -v   (stdlib only, no API key, no data pack needed)."""
import unittest
from datetime import timedelta

from fixtures import (INJECTION, NIGHT, NOW, World, catalogue_rows, ev, iso, old_viewer_history, profile, tid, ts)

from oua.facts import resolve_viewer
from oua.goals import GoalRegistry, evaluate_all, judge_goal
from oua.model import ModelMalformed, parse_advice
from oua.pack import looks_like_injection, stamp
from oua.planner import build_home_page
from oua.util import in_quiet_hours, ist_day
from oua.verifier import Verifier


def trig(user, at=NOW, **kw):
    return ev(user, 'app_open', at, **kw)


class LateAndDuplicateTriggers(unittest.TestCase):
    def test_duplicate_delivery_creates_no_second_action(self):
        u = 'U-1'
        t = trig(u)
        dup = dict(t, received_at=iso(NOW + timedelta(minutes=5)))      # same event_id redelivered 5 min later
        with World([profile(u)], old_viewer_history(u), [t, dup]) as w:
            w.run()
            ds = w.decisions(u, 'trigger_decision')
            self.assertEqual([d['status'] for d in ds], ['PUBLISHED', 'DUPLICATE_SUPPRESSED'])
            self.assertEqual(len(w.rt.ledger.actions), len(ds[0]['actions']))
            self.assertIn('already handled', ds[1]['no_action_reason'])

    def test_offline_buffered_open_is_not_a_live_session(self):
        u = 'U-2'
        old = trig(u, sent_gap=3 * 86400)                                # occurred 3 days before it was sent
        with World([profile(u)], old_viewer_history(u), [old]) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertEqual(d['status'], 'NO_ACTION')
            self.assertTrue(d['no_action_reason'].startswith('stale trigger'))
            self.assertEqual(w.rt.ledger.actions, [])
            self.assertGreater(d['trigger']['event_age_s'], 3 * 86400 - 5)       # age comes from the buffer gap
            self.assertIn(old['event_id'], w.rt.idx.ids)                         # still recorded as history

    def test_wrong_phone_clock_does_not_make_a_live_open_stale(self):
        u = 'U-3'
        t = trig(u)
        t['occurred_at'] = t['sent_at'] = iso(NOW - timedelta(hours=7))          # phone clock 7h slow, no buffering
        with World([profile(u)], old_viewer_history(u), [t]) as w:
            w.run()
            self.assertEqual(w.decisions(u, 'trigger_decision')[0]['status'], 'PUBLISHED')


class ConflictingSources(unittest.TestCase):
    def test_three_dialects_app_setting_wins_and_conflicts_stay_visible(self):
        u = 'U-4'
        t = ev(u, 'app_open', NOW, dialect='d3', deeplink='ott://c/cmp9/d2/title/T0001')
        with World([profile(u, dialect='d1')], [], [t]) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertEqual(d['dialect']['used'], 'd3')
            self.assertEqual(d['dialect']['source'], 'app_setting')
            self.assertIn('profile:d1', d['dialect']['conflicts'])
            self.assertIn('campaign_link:d2', d['dialect']['conflicts'])
            cat = w.pack.catalogue
            for a in d['actions']:
                for x in w.rt.ledger.titles_in(a):
                    self.assertEqual(cat.get(x)['dialect'], 'd3')

    def test_purchase_event_beats_stale_profile_never(self):
        u = 'U-5'
        hist = [ev(u, 'purchase_completed', NOW - timedelta(days=2), plan_id='P_MONTH', price_inr=149, is_trial=False)]
        with World([profile(u, status='never', updated='2026-09-30T00:00:00.000Z')], hist, []) as w:
            F = resolve_viewer(w.pack, w.rt.idx, u, NOW)
            self.assertEqual(F['subscription']['value'], 'active')
            self.assertEqual(F['subscription']['source'], 'event')
            self.assertTrue(any('one-way' in c.get('resolution', '') for c in F['subscription']['conflicts']))

    def test_duplicate_profile_rows_drop_the_one_contradicted_by_events(self):
        u = 'U-6'
        rows = [profile(u, status='lapsed', dialect='d2', updated='2026-09-22T00:00:00.000Z'),
                profile(u, status='never', dialect='d1', updated='2026-09-29T00:00:00.000Z')]
        hist = [ev(u, 'purchase_completed', NOW - timedelta(days=20), plan_id='P_MONTH', price_inr=149, is_trial=False)]
        with World(rows, hist, []) as w:
            F = resolve_viewer(w.pack, w.rt.idx, u, NOW)
            self.assertEqual(F['dialect']['conflicts'] and F['profile_conflicts'][0]['fact'], 'profile_duplicate_row')
            self.assertNotEqual(F['subscription']['value'], 'never')

    def test_consent_most_restrictive_and_opt_out_event_overrides(self):
        u = 'U-7'
        rows = [profile(u, push=True, wa=True), profile(u, push=False, wa=True, updated='2026-09-29T00:00:00.000Z')]
        hist = [ev(u, 'notification_opt_out', NOW - timedelta(hours=1), channel='whatsapp')]
        with World(rows, hist, []) as w:
            F = resolve_viewer(w.pack, w.rt.idx, u, NOW)
            self.assertFalse(F['consent']['push']['value'])          # duplicate rows disagree -> most restrictive
            self.assertFalse(F['consent']['whatsapp']['value'])      # opt-out event overrides the snapshot


class ToolFailures(unittest.TestCase):
    def _fault(self, t, kind, **kw):
        return dict({'trigger_event_id': t['event_id'], 'user_id': t['user_id'], 'fault': kind}, **kw)

    def test_model_timeout_falls_back_to_rules_within_budget(self):
        u = 'U-8'
        t = trig(u)
        with World([profile(u)], old_viewer_history(u), [t], faults=[self._fault(t, 'timeout', seconds=45)]) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertIn('model_timeout', d['fallback_used'])
            self.assertEqual(d['status'], 'PUBLISHED')
            self.assertEqual(d['goal']['id'], 'G07')                      # rule-based goal still chosen
            self.assertLess(d['latency_s'], w.cfg.model_timeout_s + 1)    # did not wait for the 45s timeout

    def test_malformed_model_output_is_parsed_safely_retried_then_ignored(self):
        u = 'U-9'
        t = trig(u)
        f = self._fault(t, 'malformed_output', model_output='{"goal_id": "G02", "eligible": tr')
        with World([profile(u)], old_viewer_history(u), [t], faults=[f]) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertEqual(d['model']['status'], 'malformed')
            self.assertEqual(d['model']['calls'], 2)                      # one retry, within the model budget
            self.assertEqual(d['status'], 'PUBLISHED')
        with self.assertRaises(ModelMalformed):
            parse_advice('{"goal_id": "G02", "eligible": tr')
        with self.assertRaises(ModelMalformed):
            parse_advice({'goal_id': 'G02', 'eligible': 'yes', 'confidence': 2})

    def test_model_outage_opens_breaker_so_later_decisions_do_not_wait(self):
        ctl = {'at': iso(NOW - timedelta(minutes=1)), 'type': 'dependency_unavailable', 'dependency': 'model_provider',
               'until': iso(NOW + timedelta(minutes=5)), 'detail': 'HTTP 503'}
        us = ['U-1%02d' % i for i in range(5)]
        ts_ = [trig(u, NOW + timedelta(seconds=i)) for i, u in enumerate(us)]
        with World([profile(u) for u in us], [x for u in us for x in old_viewer_history(u)], ts_, control=[ctl]) as w:
            w.run()
            ds = [w.decisions(u, 'trigger_decision')[0] for u in us]
            self.assertIn('model_unavailable', ds[0]['fallback_used'])
            self.assertTrue(all('model_skipped_breaker_open' in d['fallback_used'] for d in ds[1:]))
            self.assertTrue(all(d['status'] == 'PUBLISHED' and d['latency_s'] < 1 for d in ds))

    def test_data_store_down_without_cache_publishes_only_a_generic_safe_page(self):
        u = 'U-10'
        ctl = {'at': iso(NOW - timedelta(minutes=1)), 'type': 'dependency_unavailable', 'dependency': 'user_data_store',
               'until': iso(NOW + timedelta(minutes=10)), 'detail': 'down'}
        with World([profile(u)], old_viewer_history(u), [trig(u)], control=[ctl]) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertEqual(d['fallback_used'], 'store_unavailable:no_cache')
            self.assertEqual([a['type'] for a in d['actions']], ['home_page'])      # no message without consent facts
            self.assertIsNone(d['goal'])
            self.assertLess(d['latency_s'], 5)
            self.assertGreaterEqual(w.rt.store.deferred_writes, 1)                    # write queued (then flushed), not dropped

    def test_data_store_down_with_cache_uses_cached_facts_and_no_message(self):
        u = 'U-11'
        ctl = {'at': iso(NOW + timedelta(minutes=30)), 'type': 'dependency_unavailable', 'dependency': 'user_data_store',
               'until': iso(NOW + timedelta(minutes=60)), 'detail': 'down'}
        t1, t2 = trig(u), trig(u, NOW + timedelta(minutes=130))
        ctl['until'] = iso(NOW + timedelta(minutes=140))
        with World([profile(u)], old_viewer_history(u), [t1, t2], control=[ctl]) as w:
            w.run()
            d1, d2 = w.decisions(u, 'trigger_decision')
            self.assertIsNone(d1['fallback_used'])
            self.assertIn('cached_facts', d2['fallback_used'])
            self.assertTrue(all(a['type'] == 'home_page' for a in d2['actions']))
            self.assertEqual(d2['goal']['id'], d1['goal']['id'])                     # the goal was kept, not re-chosen


class CorrectedGoalRule(unittest.TestCase):
    def test_only_open_g03_goals_are_migrated_and_judged_under_the_new_rule(self):
        a, b, c = 'U-A', 'U-B', 'U-C'
        signup = lambda u: ev(u, 'signup_completed', NOW - timedelta(days=1), method='phone')
        play = lambda u: ev(u, 'play_started', NOW - timedelta(days=1, minutes=-5), title_id=tid('d1', 1), episode_id='title01-d1-s1e1')
        done = ev(c, 'episode_completed', NOW - timedelta(days=30), title_id=tid('d1', 2), episode_id='title02-d1-s1e1')
        hist = [signup(a), play(a), signup(b), play(b), done]
        ctl = {'at': iso(NOW + timedelta(hours=1)), 'type': 'goal_rule_corrected', 'goal_id': 'G03',
               'old_success': {'event': 'app_open'}, 'new_success': {'event': 'play_started', 'condition': 'on a later IST calendar day within the window'},
               'applies_to': 'every G03 goal that is still open', 'note': 'an app open alone was never a return'}
        trigs = [trig(a, NOW), trig(c, NOW + timedelta(minutes=1)), trig(b, NOW + timedelta(hours=2))]
        with World([profile(c)], hist, trigs, control=[ctl]) as w:
            w.run()
            ga = w.rt.agents[a].st['active_goal']
            gb = w.rt.agents[b].st['active_goal']
            gc = w.rt.agents[c].st['active_goal']
            self.assertEqual((ga['goal_id'], ga['rule_version'], ga['success']['event']), ('G03', 'v2', 'play_started'))
            self.assertEqual((gb['goal_id'], gb['rule_version']), ('G03', 'v2'))             # opened after the correction
            self.assertEqual(gc['rule_version'], 'v1')                                       # untouched
            inc = [i for i in w.rt.incidents if i['kind'] == 'goal_rule_corrected'][0]
            self.assertEqual(inc['affected_open_goals'], 1)
            self.assertEqual(inc['affected_viewers'], [a])
            self.assertEqual(w.rt.agents[a].st['rule_migrations'][0]['goal_id'], 'G03')
            # judging: an app_open on a later IST day no longer counts; a play does
            cat = w.pack.catalogue
            next_day = NOW + timedelta(days=1)
            opened = stamp(ev(a, 'app_open', next_day))
            played = stamp(ev(a, 'play_started', next_day, title_id=tid('d1', 3), episode_id='x'))
            horizon = NOW + timedelta(days=10)
            self.assertEqual(judge_goal(ga, [opened], cat, horizon)['status'], 'failed')
            self.assertEqual(judge_goal(ga, [opened, played], cat, horizon)['status'], 'succeeded')
            v1_goal = dict(ga, rule_version='v1', success={'event': 'app_open', 'condition': 'later day', 'window_hours': 48})
            self.assertEqual(judge_goal(v1_goal, [opened], cat, horizon)['status'], 'succeeded')   # old rule would have counted it

    def test_registry_versions_a_single_goal(self):
        import json, os
        from fixtures import CONFIG_DIR
        reg = GoalRegistry(json.load(open(os.path.join(CONFIG_DIR, 'goals.json'))))
        reg.correct('G03', {'event': 'play_started'}, NOW)
        self.assertEqual(reg.current('G03')['version'], 'v2')
        self.assertEqual(reg.current('G02')['version'], 'v1')


class Verification(unittest.TestCase):
    def setUp(self):
        self.u = 'U-V'
        self.w = World([profile(self.u, push=False, wa=True)], old_viewer_history(self.u), [trig(self.u)])
        self.v = Verifier(self.w.pack, self.w.rt.idx, self.w.rt.ledger, self.w.rt.registry, self.w.cfg)
        self.goal = {'goal_id': 'G07', 'rule_version': 'v1', 'success': {}}

    def tearDown(self):
        self.w.close()

    def check(self, action, at=NOW):
        base = {'decision_id': 'D', 'user_id': self.u, 'trigger_event_id': 'e1', 'published_at': at}
        return self.v.verify(self.u, at, trig(self.u, at), self.goal, False, [dict(base, **action)])

    def failed(self, res):
        return {f['check'] for f in res['failed']}

    def test_verifier_rejects_wrong_dialect_page(self):
        page = {'type': 'home_page', 'dialect': 'd1', 'hero_title_id': tid('d3', 1), 'rows': []}
        self.assertIn('dialect', self.failed(self.check(page)))

    def test_verifier_rejects_row_under_three_and_duplicate_content(self):
        rows = [{'row_type': 'popular_in_dialect', 'title_ids': [tid('d1', 3), tid('d1', 4)]}]
        self.assertIn('page_structure', self.failed(self.check({'type': 'home_page', 'dialect': 'd1', 'hero_title_id': None, 'rows': rows})))
        dup = [{'row_type': 'genre', 'title_ids': [tid('d1', 3), tid('d1', 3, 'en') or tid('d1', 3), tid('d1', 4)]}]
        self.assertIn('page_structure', self.failed(self.check({'type': 'home_page', 'dialect': 'd1', 'hero_title_id': None, 'rows': dup})))

    def test_verifier_rejects_push_without_consent_in_quiet_hours_with_bad_copy(self):
        push = {'type': 'push', 'send_at': NIGHT, 'title_id': tid('d1', 3), 'deeplink': 'ott://title/%s' % tid('d1', 3),
                'text': {'title': 'Hurry', 'body': 'Only ₹5 today, last chance'}}
        f = self.failed(self.check(push, NIGHT))
        self.assertTrue({'consent', 'quiet_hours', 'copy_policy'} <= f, f)

    def test_verifier_rejects_unapproved_template_and_invented_variable(self):
        wa = {'type': 'whatsapp', 'send_at': NOW + timedelta(hours=1), 'title_id': tid('d1', 3), 'template_id': 'wa_big_sale_v9',
              'variables': {'title_name': 'x'}}
        self.assertIn('template', self.failed(self.check(wa)))
        wa2 = dict(wa, template_id='wa_trial_ending_v1', variables={'plan_price': '₹99'})
        self.assertIn('template', self.failed(self.check(wa2)))

    def test_verifier_rejects_injected_instruction_in_copy(self):
        wa = {'type': 'in_app_message', 'send_at': NOW, 'title_id': tid('d1', 3), 'text': {'title': 'Note', 'body': INJECTION}}
        self.assertIn('injection', self.failed(self.check(wa)))

    def test_verifier_blocks_a_goal_with_no_measurable_signal(self):
        self.goal = {'goal_id': 'G10', 'rule_version': 'v1', 'success': {}}
        res = self.check({'type': 'home_page', 'dialect': 'd1', 'hero_title_id': tid('d1', 3), 'rows': []})
        self.assertIn('measurable', self.failed(res))
        self.assertEqual(res['approved'], [])

    def test_unsupported_model_claim_is_not_evidence(self):
        from oua.verifier import check_model_claim
        F = resolve_viewer(self.w.pack, self.w.rt.idx, self.u, NOW)
        claim = {'goal_id': 'G04', 'eligible': True, 'confidence': 0.97, 'reason': 'trial confirmed in billing', 'evidence_refs': []}
        ok, why = check_model_claim(claim, F, self.w.pack, self.w.rt.registry, NOW)
        self.assertFalse(ok)
        self.assertIn('UNSUPPORTED', why)
        fabricated = dict(claim, evidence_refs=['event:does-not-exist', 'billing:invoice-77'])
        ok, why = check_model_claim(fabricated, F, self.w.pack, self.w.rt.registry, NOW)
        self.assertFalse(ok)                      # cited evidence that is not in the viewer record
        from oua.goals import ELIGIBILITY
        real = dict(claim, goal_id='G02', evidence_refs=ELIGIBILITY['G02'](F, self.w.pack, NOW)['refs'])
        self.assertTrue(check_model_claim(real, F, self.w.pack, self.w.rt.registry, NOW)[0])   # a supported claim passes


class Safety(unittest.TestCase):
    def test_holdout_viewer_gets_no_action_but_a_shadow_goal(self):
        u = 'U-H'
        with World([profile(u)], old_viewer_history(u), [trig(u)], arms={u: 'holdout'}) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertEqual((d['status'], d['actions'], d['shadow']), ('NO_ACTION', [], True))
            self.assertEqual(d['goal']['id'], 'G07')
            self.assertEqual(w.rt.ledger.actions, [])

    def test_quiet_hours_push_scheduled_after_08_ist_and_one_message_per_ist_day(self):
        u = 'U-Q'
        with World([profile(u, push=True, wa=True)], old_viewer_history(u), [trig(u, NIGHT), trig(u, NIGHT + timedelta(minutes=40))]) as w:
            w.run()
            msgs = [a for a in w.rt.ledger.actions if a['type'] in ('push', 'whatsapp')]
            self.assertEqual(len(msgs), 1)
            self.assertFalse(in_quiet_hours(msgs[0]['send_at']))
            self.assertEqual(len({ist_day(a['send_at']) for a in msgs}), 1)

    def test_no_message_without_consent_and_unknown_dialect_is_never_messaged(self):
        u, v = 'U-N', 'U-D'
        t_nc = trig(u)
        t_na = ev(v, 'app_open', NOW, dialect='NA')
        with World([profile(u, push=False, wa=False), profile(v)], old_viewer_history(u) + old_viewer_history(v), [t_nc, t_na]) as w:
            w.run()
            self.assertFalse([a for a in w.rt.ledger.by_user[u] if a['type'] in ('push', 'whatsapp')])
            self.assertFalse([a for a in w.rt.ledger.by_user[v] if a['type'] in ('push', 'whatsapp', 'in_app_message')])
            self.assertFalse(w.decisions(v, 'trigger_decision')[0]['dialect']['conflicts'] == [])

    def test_injected_catalogue_text_is_data_never_obeyed(self):
        self.assertTrue(looks_like_injection(INJECTION))
        bad = tid('d1', 39)
        crowd = []
        for k in range(20):                                    # make the flagged title the most popular one
            crowd.append(ev('U-X%d' % k, 'play_started', NOW - timedelta(days=3), title_id=bad, episode_id='title39-d1-s1e1'))
        u = 'U-I'
        with World([profile(u)], crowd + old_viewer_history(u), [trig(u)]) as w:
            self.assertIn(bad, w.pack.catalogue.flagged)
            w.run()
            for a in w.rt.ledger.actions:
                self.assertNotIn(bad, w.rt.ledger.titles_in(a))
            self.assertLessEqual(len([a for a in w.rt.ledger.actions if a['type'] in ('push', 'whatsapp')]), 1)    # not a mass message

    def test_page_row_with_fewer_than_three_titles_is_dropped(self):
        u = 'U-R'
        hist = [ev(u, 'play_started', NOW - timedelta(days=4), dialect='d2', title_id=tid('d2', i), episode_id='title%02d-d2-s1e1' % i) for i in range(3)]
        with World([profile(u, dialect='d2')], hist, []) as w:
            F = resolve_viewer(w.pack, w.rt.idx, u, NOW, ev(u, 'app_open', NOW, dialect='d2'))
            page = build_home_page(w.pack, F, None, NOW, w.cfg)
            self.assertTrue(all(3 <= len(r['title_ids']) <= 10 for r in page['rows']))
            self.assertEqual(page['rows'], [])
            self.assertTrue(any('dropped' in n for n in page['notes']))

    def test_unmeasurable_goals_are_never_selected(self):
        u = 'U-M'
        with World([profile(u)], old_viewer_history(u), [trig(u)]) as w:
            F = resolve_viewer(w.pack, w.rt.idx, u, NOW)
            cands, audit = evaluate_all(F, w.pack, w.rt.registry, NOW)
            self.assertNotIn('G10', [c['goal_id'] for c in cands])
            self.assertNotIn('G11', [c['goal_id'] for c in cands])
            reasons = {a['goal_id']: a['reason'] for a in audit}
            self.assertIn('no measurable success signal', reasons['G10'])
            self.assertIn('no measurable success signal', reasons['G11'])

    def test_unsupported_model_claim_in_replay_is_rejected_and_logged(self):
        u = 'U-S'
        t = trig(u)
        f = {'trigger_event_id': t['event_id'], 'user_id': u, 'fault': 'unsupported_claim',
             'model_output': {'goal_id': 'G04', 'eligible': True, 'confidence': 0.97, 'reason': 'trial ends tomorrow (confirmed in billing)'}}
        with World([profile(u)], old_viewer_history(u), [t], faults=[f]) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertNotEqual(d['goal']['id'], 'G04')
            self.assertFalse(d['model']['claim_accepted'])
            self.assertTrue([i for i in w.rt.incidents if i['kind'] == 'unsupported_model_claim'])


class SituationChanges(unittest.TestCase):
    def test_purchase_after_decision_replans_and_cancels_pending_message(self):
        u = 'U-P'
        hist = [ev(u, 'subscription_lapsed', NOW - timedelta(days=5)), ev(u, 'app_open', NOW - timedelta(days=3)),
                ev(u, 'episode_completed', NOW - timedelta(days=3), title_id=tid('d1', 2), episode_id='title02-d1-s1e1')]
        buy = ev(u, 'purchase_completed', NOW + timedelta(minutes=2), plan_id='P_MONTH', price_inr=149, is_trial=False)
        with World([profile(u, status='lapsed', updated='2026-09-25T00:00:00.000Z')], hist, [trig(u)], live=[buy]) as w:
            w.run()
            d = w.decisions(u, 'trigger_decision')[0]
            self.assertEqual(d['goal']['id'], 'G06')
            msgs = [a for a in w.rt.ledger.by_user[u] if a['type'] != 'home_page']
            self.assertTrue(msgs and all(a['status'] == 'CANCELLED' for a in msgs))
            st = w.rt.agents[u].st
            self.assertEqual(st['goal_history'][0]['status'], 'succeeded')
            self.assertTrue(w.decisions(u, 'replan'))
            self.assertNotEqual(st['active_goal'] and st['active_goal']['goal_id'], 'G06')

    def test_withdrawn_title_cancels_pending_message_and_repairs_live_page(self):
        u = 'U-W'
        ctl = {'at': iso(NOW + timedelta(hours=1)), 'type': 'title_withdrawn', 'title_id': tid('d1', 1), 'reason': 'licence'}
        with World([profile(u)], old_viewer_history(u), [trig(u)], control=[ctl]) as w:
            w.run()
            first = w.decisions(u, 'trigger_decision')[0]
            self.assertIn('title:' + tid('d1', 1), first['content'])          # promoted before the withdrawal
            led = w.rt.ledger.by_user[u]
            self.assertEqual([a['status'] for a in led if a['type'] != 'home_page'], ['CANCELLED'])
            self.assertTrue(w.decisions(u, 'repair'))
            repaired = [a for a in led if a['type'] == 'home_page' and a['status'] == 'PUBLISHED'][0]
            self.assertNotIn(tid('d1', 1), w.rt.ledger.titles_in(repaired))
            self.assertFalse(w.pack.catalogue.is_available(tid('d1', 1), NOW + timedelta(hours=2)))
            self.assertFalse(w.pack.catalogue.is_available(tid('d1', 1, 'en') or tid('d1', 1), NOW + timedelta(hours=2)))


class LoadSpike(unittest.TestCase):
    def test_four_hundred_triggers_in_one_second_all_publish_inside_budget_with_model_down(self):
        n = 400
        us = ['U-L%03d' % i for i in range(n)]
        ctl = {'at': iso(NOW - timedelta(minutes=1)), 'type': 'dependency_unavailable', 'dependency': 'model_provider',
               'until': iso(NOW + timedelta(minutes=30)), 'detail': 'HTTP 503'}
        trigs = [trig(u, NOW + timedelta(milliseconds=i)) for i, u in enumerate(us)]
        with World([profile(u) for u in us], [x for u in us for x in old_viewer_history(u)], trigs, control=[ctl]) as w:
            w.run()
            ds = w.decisions(kind='trigger_decision')
            self.assertEqual(len(ds), n)
            self.assertLess(max(d['latency_s'] for d in ds), w.cfg.latency_target_s)
            self.assertTrue(all(d['status'] == 'PUBLISHED' for d in ds))


if __name__ == '__main__':
    unittest.main()
