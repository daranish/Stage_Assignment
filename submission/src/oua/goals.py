"""Goal catalogue: versioned rules, evidence-based eligibility, deterministic selection, and outcome judging.

Eligibility is computed ONLY from resolved facts (events/profile). A model saying "eligible" is never evidence.

INTERPRETATIONS of under-specified rules (each is also written into the decision's `assumptions`)
-----------------------------------------------------------------------------------------------
G10 / G11  UNMEASURABLE. G10 has `success.event = null` ("engages meaningfully"); G11 names `tv_app_open`, an event that
           does not exist anywhere in the event stream (platform is only android/ios). The agent never commits to a goal
           it cannot judge; they are recorded as "considered, not selectable".
G06        "recently lapsed" = a subscription_lapsed event within the last 30 days (2x the 14-day goal window).
           Fallback (weaker, flagged): the profile says lapsed and the row was updated within 30 days.
G07        "has not played it for 3 or more days" = last play_started of that content_slug (estimated true time) is
           >= 72h ago (and <= 21 days, and >= 2 distinct episodes played: resume intent, not a sample); "unwatched episodes left" = episodes.csv has episodes the viewer has no episode_completed for.
           The series must exist, be available, and be in the dialect the viewer will see.
G05        Never-paid is almost everyone (~60% of viewers). To keep the goal proportionate we additionally require a
           purchase-intent signal (paywall_viewed or premium title interest in the last 14 days).
G02/G01    "never" is judged on the 30-day event window; for viewers older than that the absence of a play/completion
           is an assumption, stated as such, with lower confidence.
G12        "No app_open in the 14 days before the trigger" excludes the trigger's own session; a viewer must also be
           an established viewer (signed up >= 14 days ago) so brand-new viewers are not "returning".
G09        "plays on at most 1 day" counts distinct IST days with a play_started in the last 14 days.
"""
from datetime import timedelta

from .pack import OBSERVABLE_EVENTS
from .util import iso, ist_day

# Selection priority: scarce/time-critical and high-value-per-viewer goals first, then activation, then engagement.
PRIORITY = ['G04', 'G01', 'G07', 'G12', 'G03', 'G02', 'G06', 'G05', 'G09', 'G08']
UNSELECTABLE = ['G10', 'G11']


class GoalRegistry:
    """Versioned goal rules. A correction creates a new version for ONE goal; nothing else is touched."""

    def __init__(self, goals_cfg):
        self.cfg_version = goals_cfg['version']
        self.goals = {g['goal_id']: g for g in goals_cfg['goals']}
        self.versions = {gid: [{'version': 'v1', 'effective_at': None, 'success': dict(g['success'])}]
                         for gid, g in self.goals.items()}

    def current(self, gid):
        v = self.versions[gid][-1]
        return {'version': v['version'], 'success': dict(v['success'])}

    def correct(self, gid, new_success, at):
        n = len(self.versions[gid]) + 1
        merged = dict(self.versions[gid][-1]['success'])
        merged.update({k: v for k, v in new_success.items() if k in ('event', 'condition', 'window_hours')})
        self.versions[gid].append({'version': 'v%d' % n, 'effective_at': iso(at), 'success': merged})
        return self.versions[gid][-1]

    def measurable(self, gid):
        return self.current(gid)['success'].get('event') in OBSERVABLE_EVENTS

    def window_hours(self, gid):
        return self.current(gid)['success']['window_hours']

    def name(self, gid):
        return self.goals[gid]['name']


def signal_text(success):
    return '%s (%s) within %sh' % (success.get('event'), success.get('condition'), success.get('window_hours'))


def _R(ok, reason, refs=(), assumptions=(), **extra):
    d = {'ok': ok, 'reason': reason, 'refs': list(refs), 'assumptions': list(assumptions)}
    d.update(extra)
    return d


def _signup_age_days(F, now):
    v = F['signup']['value']
    return None if v is None else (now - v).total_seconds() / 86400.0


def _window_note(F, now):
    age = _signup_age_days(F, now)
    if age is None or age > 30:
        return ['history is limited to the 30-day event window; earlier activity is not visible']
    return []


def elig_G01(F, pack, now):
    age = _signup_age_days(F, now)
    if age is None:
        return _R(False, 'no signup evidence')
    if age > 7:
        return _R(False, 'signed up %.1f days ago (>7)' % age)
    if F['hist']['plays'] > 0:
        return _R(False, 'viewer has already started a play')
    return _R(True, 'signed up %.1f days ago and no play_started observed since signup' % age, F['signup']['refs'])


def elig_G02(F, pack, now):
    if F['hist']['completes'] > 0:
        return _R(False, 'viewer has completed an episode', [F['hist']['last_complete_ref']])
    refs = F['signup']['refs'] or ['absence:episode_completed(%d events observed)' % F['events_seen']]
    return _R(True, 'no episode_completed observed', refs, _window_note(F, now))


def elig_G03(F, pack, now):
    age = _signup_age_days(F, now)
    if age is None:
        return _R(False, 'no signup evidence')
    if age > 2:
        return _R(False, 'signed up %.1f days ago (>2)' % age)
    return _R(True, 'signed up %.1f days ago' % age, F['signup']['refs'])


def elig_G04(F, pack, now):
    s = F['subscription']
    if s['value'] != 'trial_active':
        return _R(False, 'subscription status is %s' % s['value'])
    return _R(True, 'trial_active per %s' % s['source'], s['refs'])


def elig_G05(F, pack, now):
    s = F['subscription']
    if s['value'] != 'never' or s['paid_evidence']:
        return _R(False, 'subscription status is %s' % s['value'])
    h = F['hist']
    intent = None
    if h['paywall_at'] is not None and (now - h['paywall_at']) <= timedelta(days=14):
        intent = h['paywall_ref']
    elif h['premium_interest_at'] is not None:
        intent = h['last_play_ref']
    if intent is None:
        return _R(False, "never-paid but no purchase-intent signal in 14 days (goal would not be proportionate)")
    return _R(True, 'never paid; purchase-intent signal present', s['refs'] + [intent],
              ['"never" rests on the snapshot and a 30-day event window'])


def elig_G06(F, pack, now):
    s = F['subscription']
    if s['value'] != 'lapsed':
        return _R(False, 'subscription status is %s' % s['value'])
    if s['lapse_event_at'] is not None and (now - s['lapse_event_at']) <= timedelta(days=30):
        return _R(True, 'subscription_lapsed event within 30 days', s['refs'])
    if s['source'] == 'profile' and s['as_of'] is not None and (now - s['as_of']) <= timedelta(days=30):
        return _R(True, 'profile lapsed, row updated within 30 days', s['refs'],
                  ['profile.updated_at is used as a proxy for the lapse time (no lapse event in the window)'])
    return _R(False, 'lapsed but recency cannot be shown (>30 days or undated)')


def series_candidate(F, pack, now, dialect=None):
    """Most recently played unfinished show/micro-drama, left for >= 3 days, available in the viewer's dialect."""
    dialect = dialect or F['dialect']['value']
    best = None
    for slug, s in F['hist']['series'].items():
        if s['type'] not in ('show', 'micro_drama') or s['last_play'] is None or not s['unwatched_left']:
            continue
        idle = now - s['last_play']
        if idle < timedelta(days=3) or idle > timedelta(days=21):
            continue
        if len(s.get('eps_played', set()) | s['completed']) < 2:   # not invested in the series: no resume intent
            continue
        t = pack.catalogue.variant(slug, F['language'], now, now + timedelta(hours=2))
        if t is None or t['dialect'] != dialect:
            continue
        if best is None or s['last_play'] > best[1]['last_play']:
            best = (slug, s, t)
    return best


def elig_G07(F, pack, now):
    c = series_candidate(F, pack, now)
    if c is None:
        return _R(False, 'no unfinished series idle >= 3 days that is available in the viewer dialect')
    slug, s, t = c
    return _R(True, 'unfinished series %s idle %.1f days, %d episodes left' % (
        slug, (now - s['last_play']).total_seconds() / 86400, s['unwatched_left']),
        [s['ref'], 'catalogue:' + t['title_id']], [], slug=slug)


def new_release_slugs(F, pack, now, dialect=None):
    dialect = dialect or F['dialect']['value']
    out = []
    for t in pack.catalogue.by_dialect.get(dialect, []):
        rd = t['release_dt']
        if rd and now - timedelta(days=30) <= rd <= now and t['content_slug'] not in [o[0] for o in out]:
            v = pack.catalogue.variant(t['content_slug'], F['language'], now, now + timedelta(hours=2))
            if v:
                out.append((t['content_slug'], v))
    return out


def elig_G08(F, pack, now):
    if F['subscription']['value'] != 'active':
        return _R(False, 'subscription status is %s' % F['subscription']['value'])
    n = new_release_slugs(F, pack, now)
    if len(n) < 3:
        return _R(False, 'fewer than 3 releases from the last 30 days in the viewer dialect')
    return _R(True, 'active subscriber; %d recent releases available' % len(n), F['subscription']['refs'] + ['catalogue:' + n[0][1]['title_id']])


def elig_G09(F, pack, now):
    if F['subscription']['value'] != 'active':
        return _R(False, 'subscription status is %s' % F['subscription']['value'])
    d = len(F['hist']['play_days_14d'])
    if d > 1:
        return _R(False, 'plays on %d distinct IST days in 14 days (>1)' % d)
    return _R(True, 'active subscriber with plays on %d IST day(s) in 14 days' % d, F['subscription']['refs'])


def elig_G12(F, pack, now):
    age = _signup_age_days(F, now)
    if age is None or age < 14:
        return _R(False, 'not an established viewer (signup unknown or <14 days)')
    recent = [o for o in F['hist']['opens'] if (now - o[0]) <= timedelta(days=14)]
    if recent:
        return _R(False, 'app_open %.1f days ago' % ((now - recent[-1][0]).total_seconds() / 86400), [recent[-1][1] and 'event:' + recent[-1][1]])
    return _R(True, 'no app_open in the 14 days before this trigger', F['signup']['refs'], _window_note(F, now))


def elig_unmeasurable(gid):
    def f(F, pack, now):
        return _R(False, '%s has no measurable success signal in the event stream' % gid)
    return f


ELIGIBILITY = {'G01': elig_G01, 'G02': elig_G02, 'G03': elig_G03, 'G04': elig_G04, 'G05': elig_G05,
               'G06': elig_G06, 'G07': elig_G07, 'G08': elig_G08, 'G09': elig_G09, 'G10': elig_unmeasurable('G10'),
               'G11': elig_unmeasurable('G11'), 'G12': elig_G12}


def evaluate_all(F, pack, registry, now, learned=None):
    """Return (ordered eligible candidates, audit list of all 12 goals with reason)."""
    audit, cands = [], []
    for gid in sorted(registry.goals):
        r = ELIGIBILITY[gid](F, pack, now)
        if r['ok'] and not registry.measurable(gid):
            r = _R(False, 'success signal not measurable in the event stream')
        audit.append({'goal_id': gid, 'eligible': r['ok'], 'reason': r['reason']})
        if r['ok']:
            r['goal_id'] = gid
            cands.append(r)
    learned = learned or {}

    def rank(r):
        fails = learned.get(r['goal_id'], {}).get('failed', 0)
        return (1 if fails >= 2 else 0, PRIORITY.index(r['goal_id']) if r['goal_id'] in PRIORITY else 99)
    cands.sort(key=rank)
    return cands, audit


def new_goal_record(registry, cand, now, decision_id, F):
    gid = cand['goal_id']
    cur = registry.current(gid)
    hours = cur['success']['window_hours']
    return {'goal_id': gid, 'name': registry.name(gid), 'rule_version': cur['version'], 'success': cur['success'],
            'started_at': now, 'deadline': now + timedelta(hours=hours), 'status': 'open', 'decision_id': decision_id,
            'slug': cand.get('slug'), 'assumptions': list(cand['assumptions']), 'evidence': list(cand['refs']),
            'reason': cand['reason'], 'messages_sent': 0}


def still_valid(goal, F, pack, now):
    """Is an open goal still the right goal given the freshest facts? (re-plan trigger)"""
    gid = goal['goal_id']
    s = F['subscription']['value']
    if gid == 'G04':
        return s == 'trial_active', 'subscription is %s' % s
    if gid == 'G05':
        return s == 'never' and not F['subscription']['paid_evidence'], 'subscription is %s' % s
    if gid == 'G06':
        return s == 'lapsed', 'subscription is %s' % s
    if gid in ('G08', 'G09'):
        return s == 'active', 'subscription is %s' % s
    return True, 'ok'


# ----------------------------------------------------------------------------------------- judging
def judge_goal(goal, events, cat, horizon_end):
    """Judge one goal from attributable events only.

    Attributable = same viewer, after the goal started (both by received_at and by estimated true time, so a
    buffered event from before the goal cannot count), at or before min(deadline, observation horizon), matching the
    goal's success rule AT ITS RECORDED RULE VERSION. Returns a dict with status in {succeeded, failed, inconclusive}.
    """
    start, deadline, succ, gid = goal['started_at'], goal['deadline'], goal['success'], goal['goal_id']
    ev = succ.get('event')
    if ev not in OBSERVABLE_EVENTS:
        return {'status': 'inconclusive', 'reason': 'no_success_signal_in_event_stream', 'evidence': []}
    end = min(deadline, horizon_end)
    slack = timedelta(seconds=1)
    hits, days = [], {}
    for e in events:
        if e['event'] != ev or e['_r'] <= start or e['_r'] > end or e['_t'] < start - slack:
            continue
        p = e['properties']
        if gid in ('G04', 'G05', 'G06'):
            if p.get('is_trial'):
                continue
        elif gid == 'G03':
            if ist_day(e['_t']) <= ist_day(start):
                continue
        elif gid == 'G07':
            t = cat.get(p.get('title_id'))
            if not t or t['content_slug'] != goal.get('slug'):
                continue
        elif gid == 'G08':
            t = cat.get(p.get('title_id'))
            if not t or not t['release_dt'] or not (start - timedelta(days=30) <= t['release_dt'] <= start):
                continue
        elif gid == 'G09':
            days.setdefault(ist_day(e['_t']), e['event_id'])
            if len(days) >= 3:
                hits.append(e['event_id'])
                break
            continue
        hits.append(e['event_id'])
        break
    if hits:
        return {'status': 'succeeded', 'reason': 'success event within window', 'evidence': ['event:' + hits[0]]}
    if deadline <= horizon_end:
        extra = ' (%d of 3 distinct play days)' % len(days) if gid == 'G09' else ''
        return {'status': 'failed', 'reason': 'window closed without a qualifying event' + extra, 'evidence': []}
    return {'status': 'inconclusive', 'reason': 'goal window extends beyond the observed horizon', 'evidence': []}
