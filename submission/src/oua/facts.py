"""Fact resolver: turns raw sources into viewer facts with provenance, freshness and visible conflicts.

PRECEDENCE RULES (one per fact; "newest" or "largest" never wins by default)
---------------------------------------------------------------------------
signup_at     profile.signup_at (immutable, warehouse) > signup_completed event. Both kept; >24h disagreement is a
              conflict. A brand-new viewer with no profile row gets signup from the event (received_at-based).
subscription  Timestamped comparison: profile.updated_at vs the received_at of the latest purchase/renewal/lapse
              event. The later one wins (events are first-party and fresher than a day-stale snapshot), EXCEPT:
              - 'never' can never override evidence of a paid purchase (the transition never->paid is one way);
              - a trial_active profile whose plan_ends_at is in the past is not trusted as trial_active.
              Duplicate profile rows: drop rows contradicted by event evidence, then take the latest updated_at.
dialect       The dialect the app will request = app_dialect on the trigger (the app settings feed). Profile
              preferred_dialect, campaign deeplink dialect and the last app_dialect_changed are LISTED as conflicts.
consent       Most restrictive wins: every profile row must opt in, and any later notification_opt_out event
              overrides the snapshot. No profile row (new viewer) or a snapshot older than the limit => no consent.
time          received_at is the only trusted clock. Event time for recency = received_at - (sent_at - occurred_at),
              i.e. the offline-buffer gap measured on the phone's own (possibly wrong) clock.
"""
from datetime import timedelta

from .config import Config
from .util import IST, iso, ist_day

SUB_EVENTS = ('purchase_completed', 'subscription_renewed', 'subscription_lapsed')


def _age(now, t):
    return None if t is None else max(0.0, (now - t).total_seconds())


def fact(value, source, as_of, now, refs=(), conflicts=()):
    return {'value': value, 'source': source, 'as_of': as_of, 'age_s': _age(now, as_of),
            'refs': list(refs), 'conflicts': list(conflicts)}


def choose_profile(rows, paid_evidence):
    """Returns (row|None, conflicts). Duplicate rows are reconciled, never silently merged."""
    if not rows:
        return None, []
    if len(rows) == 1:
        return rows[0], []
    conflicts = []
    cand = rows
    if paid_evidence:
        cand = [r for r in rows if r['subscription_status'] != 'never'] or rows
    pick = max(cand, key=lambda r: r['_updated'])
    for r in rows:
        if r is pick:
            continue
        diffs = {k: (r[k], pick[k]) for k in ('subscription_status', 'preferred_dialect', 'preferred_language',
                                              'notifications_opt_in', 'whatsapp_opt_in') if r[k] != pick[k]}
        if diffs:
            conflicts.append({'fact': 'profile_duplicate_row', 'other_row_updated_at': r['updated_at'],
                              'differs': {k: {'other': v[0], 'chosen': v[1]} for k, v in diffs.items()},
                              'resolution': 'later updated_at chosen' + (
                                  ' after dropping rows contradicted by purchase evidence' if paid_evidence else '')})
    return pick, conflicts


def resolve_subscription(pack, profile, evs, now):
    ev_state = ev_time = ev_ref = None
    ends = None
    paid_ref = None
    for e in evs:
        k = e['event']
        if k not in SUB_EVENTS:
            continue
        if k == 'purchase_completed':
            trial = bool(e['properties'].get('is_trial'))
            ev_state = 'trial_active' if trial else 'active'
            if trial:
                ends = e['_r'] + timedelta(days=3)
            else:
                paid_ref = e['event_id']
        elif k == 'subscription_renewed':
            ev_state, paid_ref = 'active', e['event_id']
        else:
            ev_state = 'lapsed'
        ev_time, ev_ref = e['_r'], e['event_id']
    paid_evidence = paid_ref is not None
    p_status = profile['subscription_status'] if profile else None
    p_time = profile['_updated'] if profile else None
    conflicts = []
    if ev_state is None:
        value, source, as_of, refs = (p_status, 'profile', p_time, ['profile:%s@%s' % (profile['user_id'], profile['updated_at'])]) \
            if profile else (None, 'none', None, [])
        ends = profile['_ends'] if profile else None
    elif p_status is None:
        value, source, as_of, refs = ev_state, 'event', ev_time, ['event:' + ev_ref]
    else:
        refs = ['profile:%s@%s' % (profile['user_id'], profile['updated_at']), 'event:' + ev_ref]
        if p_status != ev_state:
            conflicts.append({'fact': 'subscription_status', 'profile': p_status, 'event': ev_state,
                              'profile_as_of': iso(p_time), 'event_as_of': iso(ev_time)})
        if ev_time >= p_time:
            value, source, as_of = ev_state, 'event', ev_time
            if conflicts:
                conflicts[0]['resolution'] = 'event newer than snapshot row'
        elif p_status == 'never' and paid_evidence:
            value, source, as_of = ev_state, 'event', ev_time
            conflicts[0]['resolution'] = "profile says 'never' but a paid purchase/renewal event exists (never->paid is one-way)"
        else:
            value, source, as_of = p_status, 'profile', p_time
            ends = profile['_ends']
            if conflicts:
                conflicts[0]['resolution'] = 'profile row newer than the last subscription event'
    if value == 'trial_active' and ends is not None and ends < now:
        conflicts.append({'fact': 'trial_end', 'plan_ends_at': iso(ends), 'resolution': 'trial_active but plan_ends_at passed: treated as unknown'})
        value = 'unknown'
    f = fact(value, source, as_of, now, refs, conflicts)
    f['plan_ends_at'] = ends
    f['paid_evidence'] = paid_evidence
    f['lapse_event_at'] = ev_time if ev_state == 'lapsed' else None
    return f


def resolve_consent(profile_rows, evs, now, max_age_s):
    opt_out = {}
    for e in evs:
        if e['event'] == 'notification_opt_out':
            ch = e['properties'].get('channel')
            for c in (['push', 'whatsapp'] if ch not in ('push', 'whatsapp') else [ch]):
                opt_out.setdefault(c, e['event_id'])
    out = {}
    snap = max((r['_snapshot'] for r in profile_rows), default=None)
    for ch, col in (('push', 'notifications_opt_in'), ('whatsapp', 'whatsapp_opt_in')):
        conflicts, refs = [], []
        if not profile_rows:
            val, why = False, 'no profile row: consent unknown'
        elif snap is not None and _age(now, snap) > max_age_s:
            val, why = False, 'profile snapshot too old to rely on for consent'
        else:
            vals = {r[col] for r in profile_rows}
            val = all(vals)
            why = 'profile opt-in' if val else 'profile opt-out'
            if len(vals) > 1:
                conflicts.append({'fact': 'consent_' + ch, 'resolution': 'duplicate profile rows disagree: most restrictive applied'})
            refs = ['profile:%s@%s' % (r['user_id'], r['updated_at']) for r in profile_rows]
        if ch in opt_out:
            val, why = False, 'later notification_opt_out event overrides the snapshot'
            refs = ['event:' + opt_out[ch]]
        out[ch] = {'value': val, 'why': why, 'refs': refs, 'conflicts': conflicts, 'as_of': snap, 'age_s': _age(now, snap)}
    return out


def resolve_dialect(pack, profile_rows, profile, evs, trigger, now):
    """Dialect the app will request. The trigger's own app setting wins when it names a real catalogue dialect.

    Traps handled: app_dialect may be missing (None), 'NA' (not set) or a dialect with no catalogue content (e.g. d7).
    - None/'NA': fall back to the last valid app setting seen for this viewer, else the profile; marked UNCERTAIN, and
      an uncertain dialect is never used for messages (a wrong-dialect message is a policy violation).
    - unsupported (d7): the app will ask for a page we cannot build -> `supported` False, no content actions.
    """
    known = set(pack.catalogue.by_dialect)
    raw = trigger.get('app_dialect') if trigger is not None else None
    used = src = ref = None
    certain = True
    conflicts = []
    if raw not in (None, 'NA', ''):
        used, src, ref = raw, 'app_setting', 'event:' + trigger['event_id']
    else:
        certain = trigger is None
        if trigger is not None:
            conflicts.append('trigger_app_dialect:%s' % raw)
        for e in reversed(evs):
            if e.get('app_dialect') in known:
                used, src, ref = e['app_dialect'], 'app_setting(last valid; trigger had %r)' % raw, 'event:' + e['event_id']
                certain = trigger is None
                break
        if used is None and profile is not None:
            used, src, ref = profile['preferred_dialect'], 'profile(fallback; trigger had %r)' % raw, 'profile:%s' % profile['user_id']
    if profile is not None and profile['preferred_dialect'] != used:
        conflicts.append('profile:' + profile['preferred_dialect'])
    for r in profile_rows:
        if r is not profile and r['preferred_dialect'] != used and 'profile:' + r['preferred_dialect'] not in conflicts:
            conflicts.append('profile(dup row):' + r['preferred_dialect'])
    if trigger is not None:
        dl = (trigger.get('properties') or {}).get('deeplink') or ''
        parts = dl.split('/')
        # ott://c/<campaign>/<dialect>/title/<id>  (deeplink text is data: only the dialect token is read, validated)
        if len(parts) >= 5 and parts[2] == 'c' and parts[4] in known and parts[4] != used:
            conflicts.append('campaign_link:' + parts[4])
    for e in reversed(evs):
        if e['event'] == 'app_dialect_changed':
            to = e['properties'].get('to')
            if to and to != used:
                conflicts.append('last_dialect_change:' + to)
            break
    return {'value': used, 'source': src, 'ref': ref, 'conflicts': conflicts, 'as_of': trigger and trigger['_r'],
            'supported': used in known, 'certain': certain,
            'resolution': 'app setting decides: it is the dialect the app will actually request'}


def watch_history(pack, evs, now, cur_session):
    cat = pack.catalogue
    H = {'plays': 0, 'completes': 0, 'last_play_at': None, 'last_play_ref': None, 'play_days_14d': set(),
         'opens': [], 'paywall_at': None, 'paywall_ref': None, 'premium_interest_at': None,
         'genres': {}, 'recent_slugs': [], 'seen_slugs': set(), 'series': {}, 'last_complete_ref': None,
         'open_hours': {}, 'first_event_at': None}
    sessions = set()
    for e in evs:
        k, t = e['event'], e['_t']
        if H['first_event_at'] is None or t < H['first_event_at']:
            H['first_event_at'] = t
        p = e['properties']
        if k == 'app_open' and e['session_id'] != cur_session and e['session_id'] not in sessions:
            sessions.add(e['session_id'])
            H['opens'].append((t, e['event_id']))
            hr = t.astimezone(IST).hour
            H['open_hours'][hr] = H['open_hours'].get(hr, 0) + 1
        elif k in ('play_started', 'title_viewed'):
            title = cat.get(p.get('title_id'))
            if title is None:
                continue
            slug = title['content_slug']
            if title['premium'] and (now - t) <= timedelta(days=14):
                H['premium_interest_at'] = t
            if k == 'title_viewed':
                continue
            H['plays'] += 1
            H['seen_slugs'].add(slug)
            H['recent_slugs'].append(slug)
            if H['last_play_at'] is None or t > H['last_play_at']:
                H['last_play_at'], H['last_play_ref'] = t, 'event:' + e['event_id']
            if (now - t) <= timedelta(days=14):
                H['play_days_14d'].add(ist_day(t))
            for g in title['genre_list']:
                H['genres'][g] = H['genres'].get(g, 0) + 1
            s = H['series'].setdefault(slug, {'last_play': None, 'completed': set(), 'type': title['content_type'], 'ref': None})
            s.setdefault('eps_played', set()).add(p.get('episode_id'))
            if s['last_play'] is None or t > s['last_play']:
                s['last_play'], s['ref'] = t, 'event:' + e['event_id']
        elif k == 'episode_completed':
            title = cat.get(p.get('title_id'))
            H['completes'] += 1
            H['last_complete_ref'] = 'event:' + e['event_id']
            if title:
                s = H['series'].setdefault(title['content_slug'], {'last_play': None, 'completed': set(), 'type': title['content_type'], 'ref': None})
                s['completed'].add(p.get('episode_id'))
                H['seen_slugs'].add(title['content_slug'])
        elif k == 'paywall_viewed':
            H['paywall_at'], H['paywall_ref'] = t, 'event:' + e['event_id']
    for slug, s in H['series'].items():
        eps = pack.episodes.get(slug, [])
        left = [x for x in eps if x[2] not in s['completed']]
        s['unwatched_left'] = len(left)
        s['next_episode'] = left[0] if left else None
    H['top_genres'] = [g for g, _ in sorted(H['genres'].items(), key=lambda kv: (-kv[1], kv[0]))]
    return H


def resolve_viewer(pack, idx, user, now, trigger=None, cfg=None):
    cfg = cfg or Config()
    """Assemble everything known about a viewer as of `now`, from sources that were delivered by `now`."""
    if trigger is not None and '_r' not in trigger:
        from .pack import stamp
        stamp(trigger)
    evs = idx.upto(user, now)
    rows = pack.profiles.get(user, [])
    sub0 = resolve_subscription(pack, None, evs, now)           # event-only view to detect paid evidence
    profile, prof_conflicts = choose_profile(rows, sub0['paid_evidence'])
    sub = resolve_subscription(pack, profile, evs, now)
    # signup
    ev_signup = next((e for e in evs if e['event'] == 'signup_completed'), None)
    sconf = []
    if profile is not None:
        val, src, as_of = profile['_signup'], 'profile', profile['_snapshot']
        refs = ['profile:%s@%s' % (user, profile['updated_at'])]
        if ev_signup is not None:
            refs.append('event:' + ev_signup['event_id'])
            if abs((ev_signup['_t'] - val).total_seconds()) > 86400:
                sconf.append({'fact': 'signup_at', 'profile': iso(val), 'event': iso(ev_signup['_t']),
                              'resolution': 'profile signup_at (immutable warehouse field) kept'})
    elif ev_signup is not None:
        val, src, as_of, refs = ev_signup['_t'], 'event(no profile row)', ev_signup['_r'], ['event:' + ev_signup['event_id']]
    else:
        val, src, as_of, refs = None, 'none', None, []
    signup = fact(val, src, as_of, now, refs, sconf)
    cur_session = trigger['session_id'] if trigger else None
    F = {
        'user': user, 'now': now, 'arm': pack.arm(user),
        'profile_present': profile is not None, 'profile_rows': len(rows),
        'profile_conflicts': prof_conflicts,
        'signup': signup, 'subscription': sub,
        'dialect': resolve_dialect(pack, rows, profile, evs, trigger, now),
        'language': (trigger or {}).get('app_language') or (profile or {}).get('preferred_language') or 'hin',
        'consent': resolve_consent(rows, evs, now, cfg.profile_max_age_for_consent_s),
        'hist': watch_history(pack, evs, now, cur_session),
        'events_seen': len(evs),
    }
    conflicts = list(prof_conflicts) + sub['conflicts'] + signup['conflicts']
    for ch in ('push', 'whatsapp'):
        conflicts += F['consent'][ch]['conflicts']
    if F['dialect']['conflicts']:
        conflicts.append({'fact': 'dialect', 'used': F['dialect']['value'], 'others': F['dialect']['conflicts'],
                          'resolution': F['dialect']['resolution']})
    F['conflicts'] = conflicts
    return F
