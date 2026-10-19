"""Independent verifier. It shares NO code path with the planner's content/goal choice: it takes the proposal as a
set of claims, re-reads the viewer's facts itself (fresh, at publish time) and re-checks every rule. It can and does
reject. Model text is never an input here except as a claim to be re-derived.

Checks: holdout, duplicate, goal eligibility, goal measurability, rule version, dialect, availability, page structure,
consent, quiet hours, frequency cap, copy policy, template, injection.
"""
import re
from datetime import timedelta

from .facts import resolve_viewer
from .goals import ELIGIBILITY, still_valid
from .pack import looks_like_injection
from .util import in_quiet_hours, ist_day

ALLOWED_ROWS = {'next_episode', 'because_you_watched', 'popular_in_dialect', 'new_releases', 'genre'}
PRICE_RE = re.compile(r'(?:₹|rs\.?|inr)\s*([0-9][0-9,]*)|([0-9][0-9,]*)\s*(?:rupees|rs\b|₹)', re.I)


def check_model_claim(claim, F, pack, registry, now):
    """A model claim 'viewer is eligible for G' is accepted only if the rules re-derive it from facts AND every
    evidence ref it cites exists in the viewer's real evidence. Returns (accepted, reason)."""
    if not claim or not claim.get('eligible'):
        return False, 'model did not claim eligibility'
    gid = claim['goal_id']
    if gid not in ELIGIBILITY or not registry.measurable(gid):
        return False, 'unknown or unmeasurable goal %s' % gid
    r = ELIGIBILITY[gid](F, pack, now)
    if not r['ok']:
        return False, 'UNSUPPORTED CLAIM: rules find no evidence for %s (%s)' % (gid, r['reason'])
    if not claim.get('evidence_refs'):
        return False, 'UNSUPPORTED CLAIM: no evidence refs cited for %s' % gid
    allowed = set(r['refs'])
    bad = [x for x in claim['evidence_refs'] if x not in allowed]
    if bad:
        return False, 'UNSUPPORTED CLAIM: cited evidence not in the viewer record: %s' % bad[:3]
    return True, 'claim re-derived from events/profile'


class Verifier:
    def __init__(self, pack, idx, ledger, registry, cfg):
        self.pack, self.idx, self.ledger, self.registry, self.cfg = pack, idx, ledger, registry, cfg

    def verify(self, user, now, trigger, goal, goal_is_new, actions, facts=None, session_actions_ok=True):
        """Returns {'passed': [...], 'failed': [{check, detail, action}], 'approved': [...], 'degraded': bool}."""
        pack, cfg = self.pack, self.cfg
        passed, failed = [], []
        F = facts
        degraded = facts is not None
        if F is None:
            F = resolve_viewer(pack, self.idx, user, now, trigger, cfg)

        def fail(check, detail, action=None):
            failed.append({'check': check, 'detail': detail, 'action': action['type'] if action else None})

        def ok(check):
            if check not in passed:
                passed.append(check)

        global_fail = False
        if goal is not None:
            gid = goal['goal_id']
            if not self.registry.measurable(gid):
                fail('measurable', 'goal %s has no measurable success signal' % gid)
                global_fail = True
            else:
                ok('measurable')
            if goal['rule_version'] != self.registry.current(gid)['version']:
                fail('rule_version', 'goal on %s but current rule is %s' % (goal['rule_version'], self.registry.current(gid)['version']))
                global_fail = True
            else:
                ok('rule_version')
            if degraded:
                ok('eligibility(cached)')
            elif goal_is_new:
                r = ELIGIBILITY[gid](F, pack, now)
                if r['ok']:
                    ok('eligibility')
                else:
                    fail('eligibility', r['reason'])
                    global_fail = True
            else:
                v, why = still_valid(goal, F, pack, now)
                if v:
                    ok('eligibility')
                else:
                    fail('eligibility', 'open goal no longer valid: ' + why)
                    global_fail = True
        # holdout: zero actions of any kind (goal checks above still run: holdout users carry a shadow goal)
        if pack.arm(user) != 'agent':
            if actions:
                fail('holdout', 'viewer is not in the agent arm')
            else:
                ok('holdout_no_actions')
            return {'passed': passed, 'failed': failed, 'approved': [], 'degraded': degraded, 'facts': F}
        ok('holdout')
        if global_fail:
            return {'passed': passed, 'failed': failed, 'approved': [], 'degraded': degraded, 'facts': F}

        approved = []
        for a in actions:
            if self.ledger.has_key('%s:%s' % (a['trigger_event_id'], a['type'])):
                fail('duplicate', 'action for this trigger already published', a)
                continue
            n0 = len(failed)
            if a['type'] == 'home_page':
                self._page(a, F, now, fail, ok)
            else:
                self._message(a, F, now, fail, ok, user)
            self._injection(a, fail, ok)
            if len(failed) == n0:
                ok('duplicate')
                approved.append(a)
        return {'passed': passed, 'failed': failed, 'approved': approved, 'degraded': degraded, 'facts': F}

    # ------------------------------------------------------------------------------ page
    def _page(self, a, F, now, fail, ok):
        cat, cfg = self.pack.catalogue, self.cfg
        problems = []
        d = a['dialect']
        if d != F['dialect']['value']:
            problems.append(('dialect', 'page dialect %s != dialect the app will request (%s)' % (d, F['dialect']['value'])))
        if not F['dialect']['supported']:
            problems.append(('dialect', 'dialect %s has no catalogue content' % F['dialect']['value']))
        through = now + cfg.page_availability_margin
        titles = ([a['hero_title_id']] if a.get('hero_title_id') else []) + [t for r in a['rows'] for t in r['title_ids']]
        slugs = set()
        for tid in titles:
            t = cat.get(tid)
            if t is None:
                problems.append(('availability', '%s not in catalogue' % tid))
                continue
            if t['dialect'] != d:
                problems.append(('dialect', '%s is %s, page is %s' % (tid, t['dialect'], d)))
            if not cat.is_available(tid, now, through):
                problems.append(('availability', '%s not available for the whole page life (or withdrawn)' % tid))
            if tid in cat.flagged:
                problems.append(('injection', '%s is quarantined (instruction-like text)' % tid))
            if t['content_slug'] in slugs:
                problems.append(('page_structure', 'content %s appears twice' % t['content_slug']))
            slugs.add(t['content_slug'])
        if len(a['rows']) > 6:
            problems.append(('page_structure', 'more than 6 rows'))
        for r in a['rows']:
            if r['row_type'] not in ALLOWED_ROWS:
                problems.append(('page_structure', 'row type %s not allowed' % r['row_type']))
            if not 3 <= len(r['title_ids']) <= 10:
                problems.append(('page_structure', 'row %s has %d titles' % (r['row_type'], len(r['title_ids']))))
        if not a['rows'] and not a.get('hero_title_id'):
            problems.append(('page_structure', 'empty page'))
        for c, dtl in problems:
            fail(c, dtl, a)
        for c in ('dialect', 'availability', 'page_structure'):
            if not any(p[0] == c for p in problems):
                ok(c)

    # ------------------------------------------------------------------------------ messages
    def _message(self, a, F, now, fail, ok, user):
        cat, pack = self.pack.catalogue, self.pack
        typ = a['type']
        send_at = a.get('send_at') or a['published_at']
        problems = []
        if typ in ('push', 'whatsapp'):
            ch = F['consent'][typ]
            if not ch['value']:
                problems.append(('consent', '%s consent not held: %s' % (typ, ch['why'])))
            if in_quiet_hours(send_at):
                problems.append(('quiet_hours', 'send_at falls in 22:00-08:00 IST'))
            day = ist_day(send_at)
            if self.ledger.messages_on_day(user, day):
                problems.append(('frequency_cap', 'a push/WhatsApp is already scheduled for IST day %s' % day))
            elif any(e['event'] == 'message_delivered' and e['properties'].get('channel') in ('push', 'whatsapp') and ist_day(e['_t']) == day
                     for e in self.idx.upto(user, now)):
                problems.append(('frequency_cap', 'a push/WhatsApp was already delivered on IST day %s' % day))
        else:
            if self.ledger.messages_on_day(user, ist_day(send_at), ('in_app_message',)):
                problems.append(('frequency_cap', 'in-app message already queued for this IST day'))
        if send_at < now - timedelta(seconds=1):
            problems.append(('quiet_hours', 'send_at is in the past'))
        t = cat.get(a.get('title_id'))
        if t is None:
            problems.append(('availability', 'title %s not in catalogue' % a.get('title_id')))
        else:
            if t['dialect'] != F['dialect']['value']:
                problems.append(('dialect', 'title is %s, viewer sees %s' % (t['dialect'], F['dialect']['value'])))
            if not F['dialect']['certain']:
                problems.append(('dialect', 'dialect inferred (trigger carried no valid app setting): no messages'))
            if not cat.is_available(t['title_id'], send_at, send_at + timedelta(hours=3)):
                problems.append(('availability', 'title not available at send time'))
            if t['title_id'] in cat.flagged:
                problems.append(('injection', 'title is quarantined'))
        if typ == 'whatsapp':
            tpl = {x['template_id']: x for x in pack.interventions['whatsapp']['approved_templates']}.get(a.get('template_id'))
            v = a.get('variables') or {}
            if tpl is None:
                problems.append(('template', 'template %s is not approved' % a.get('template_id')))
            elif set(v) != set(tpl['variables']):
                problems.append(('template', 'variables %s != template variables %s' % (sorted(v), tpl['variables'])))
            elif t is not None:
                names = {x['title'] for x in cat.by_slug[t['content_slug']]}
                if 'title_name' in v and v['title_name'] not in names:
                    problems.append(('template', 'title_name not from the catalogue'))
                if 'dialect_name' in v and v['dialect_name'] != t['dialect']:
                    problems.append(('template', 'dialect_name not the title dialect'))
                if 'plan_price' in v and v['plan_price'] not in {pack.price_text(p['plan_id']) for p in pack.pricing['plans']}:
                    problems.append(('template', 'plan_price not from pricing.json'))
                if 'episode_number' in v:
                    eps = {x[1] for x in pack.episodes.get(t['content_slug'], [])}
                    if not v['episode_number'].isdigit() or int(v['episode_number']) not in eps:
                        problems.append(('template', 'episode_number does not exist for this title'))
                if tpl['template_id'] == 'wa_trial_ending_v1' and F['subscription']['value'] != 'trial_active':
                    problems.append(('template', 'trial-ending template but viewer is not on a trial'))
        else:
            txt = a.get('text') or {}
            c = pack.interventions['push']['copy']
            if len(txt.get('title', '')) > c['title_max_chars'] or len(txt.get('body', '')) > c['body_max_chars']:
                problems.append(('copy_policy', 'title/body too long'))
            low = (txt.get('title', '') + ' ' + txt.get('body', '')).lower()
            for ph in c['forbidden_phrases']:
                if ph in low:
                    problems.append(('copy_policy', 'forbidden phrase "%s"' % ph))
            for m in PRICE_RE.finditer(low):
                val = int((m.group(1) or m.group(2)).replace(',', ''))
                if val not in pack.valid_prices():
                    problems.append(('copy_policy', 'price %s does not exist in pricing.json' % val))
            if typ == 'push' and a.get('deeplink') != 'ott://title/%s' % a.get('title_id'):
                problems.append(('copy_policy', 'deeplink must be ott://title/<title_id>'))
        for c, dtl in problems:
            fail(c, dtl, a)
        want = ['consent', 'quiet_hours', 'frequency_cap', 'dialect', 'availability', 'copy_policy' if typ != 'whatsapp' else 'template']
        for c in want:
            if not any(p[0] == c for p in problems) and not (c == 'consent' and typ == 'in_app_message'):
                ok(c)

    def _injection(self, a, fail, ok):
        txt = ' '.join(str(v) for v in list((a.get('text') or {}).values()) + list((a.get('variables') or {}).values()))
        if looks_like_injection(txt):
            fail('injection', 'instruction-like text in outgoing copy', a)
        else:
            ok('injection')
