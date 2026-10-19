"""Intervention planner: decides WHAT to change (home page, at most one message, or nothing). It only proposes;
nothing it returns is published until the independent verifier approves it.
"""
from datetime import timedelta

from .goals import series_candidate, new_release_slugs
from .pack import looks_like_injection
from .util import IST, at_ist_hour, in_quiet_hours, next_allowed_send

# which goals justify a message at all (proportionality: everything else gets the home page only)
MESSAGE_GOALS = {'G01', 'G03', 'G04', 'G06', 'G07', 'G12'}
MAX_ROWS, MIN_ROW, MAX_ROW = 6, 3, 10


def _pick(cat, slugs, language, now, through, banned):
    out = []
    for s in slugs:
        if s in banned:
            continue
        t = cat.variant(s, language, now, through)
        if t is not None:
            out.append(t)
    return out


def build_home_page(pack, F, goal, now, cfg, dialect=None, pick_hero=None):
    """A personalised page for ONE dialect. Every title is in that dialect, available for the page's whole life,
    not quarantined, with one content_slug per page. Rows with <3 titles are never emitted.
    `pick_hero(shortlist)` is an optional (model-backed) chooser; it can only return one of the shortlisted,
    already-validated titles, so a bad answer degrades to the rule-based hero."""
    cat = pack.catalogue
    dialect = dialect or F['dialect']['value']
    if dialect not in cat.by_dialect:
        return None          # e.g. d7: the app will ask for a page in a dialect with no content
    lang, H = F['language'], F['hist']
    through = now + cfg.page_availability_margin
    pop = pack.popularity or {}
    slugs = [t['content_slug'] for t in cat.by_dialect.get(dialect, [])]
    slugs = sorted(set(slugs), key=lambda s: (-pop.get(s, 0), s))
    used, rows, notes = set(), [], []
    gid = goal['goal_id'] if goal else None

    def unseen(ss):
        return [s for s in ss if s not in H['seen_slugs']]

    def add_row(rtype, titles):
        titles = [t for t in titles if t['content_slug'] not in used][:MAX_ROW]
        if len(titles) < MIN_ROW or len(rows) >= MAX_ROWS:
            notes.append('row %s dropped: %d valid titles' % (rtype, len(titles)))
            return
        for t in titles:
            used.add(t['content_slug'])
        rows.append({'row_type': rtype, 'title_ids': [t['title_id'] for t in titles]})

    # hero: the title that best serves the goal
    hero = None
    if gid == 'G07' and goal.get('slug'):
        hero = cat.variant(goal['slug'], lang, now, through)
    elif gid == 'G08':
        n = new_release_slugs(F, pack, now, dialect)
        hero = n[0][1] if n else None
    if hero is None:
        pool = unseen(slugs)
        if gid in ('G01', 'G02', 'G03'):  # first-play/first-completion goals: shortest content gets finished
            pool = sorted(pool, key=lambda s: (cat.by_slug[s][0]['content_type'] != 'micro_drama', -pop.get(s, 0), s))
        elif gid in ('G04', 'G05', 'G06'):
            pool = sorted(pool, key=lambda s: (not cat.by_slug[s][0]['premium'], -pop.get(s, 0), s))
        c = _pick(cat, pool[:40], lang, now, through, used)
        hero = c[0] if c else None
        if pick_hero is not None and len(c) > 1:
            chosen = pick_hero(c[:5])
            if chosen is not None and any(chosen['title_id'] == t['title_id'] for t in c[:5]):
                if chosen['title_id'] != hero['title_id']:
                    notes.append('hero chosen by model from a shortlist of %d' % len(c[:5]))
                hero = chosen
    if hero is not None:
        used.add(hero['content_slug'])

    # rows
    started = sorted([(s['last_play'], slug) for slug, s in H['series'].items()
                      if s['type'] in ('show', 'micro_drama') and s['unwatched_left']
                      and cat.by_slug.get(slug) and cat.by_slug[slug][0]['dialect'] == dialect],
                     reverse=True)
    next_ep = _pick(cat, [s for _, s in started], lang, now, through, set())
    recent = []
    for s in reversed(H['recent_slugs']):
        if s not in recent:
            recent.append(s)
    byw_genres = []
    for s in recent[:5]:
        for t in cat.by_slug.get(s, [])[:1]:
            byw_genres += t['genre_list']
    byw = _pick(cat, [s for s in unseen(slugs) if set(cat.by_slug[s][0]['genre_list']) & set(byw_genres)][:30], lang, now, through, used)
    new_rel = [v for _, v in new_release_slugs(F, pack, now, dialect)]
    genre_rows = []
    for g in H['top_genres'][:2]:
        genre_rows.append(_pick(cat, [s for s in unseen(slugs) if g in cat.by_slug[s][0]['genre_list']][:30], lang, now, through, used))
    popular = _pick(cat, unseen(slugs)[:40], lang, now, through, used)

    order = {'G07': ['next_episode', 'because_you_watched', 'popular_in_dialect', 'genre', 'new_releases'],
             'G08': ['new_releases', 'because_you_watched', 'next_episode', 'genre', 'popular_in_dialect']}.get(
        gid, ['next_episode', 'because_you_watched', 'genre', 'new_releases', 'popular_in_dialect'])
    for r in order:
        if r == 'next_episode':
            add_row(r, next_ep)
        elif r == 'because_you_watched':
            add_row(r, byw)
        elif r == 'genre':
            for g in genre_rows:
                add_row('genre', g)
        elif r == 'new_releases':
            add_row(r, new_rel)
        else:
            add_row(r, popular)
    # a generic (unpersonalised) page must still never contain a row that is not valid
    if not rows and hero is None:
        return None
    content = ([hero['title_id']] if hero else []) + [t for r in rows for t in r['title_ids']]
    return {'type': 'home_page', 'dialect': dialect, 'hero_title_id': hero['title_id'] if hero else None,
            'rows': rows, 'content': content, 'notes': notes}


def generic_safe_page(pack, dialect, language, now, cfg):
    """Fallback when viewer facts are unavailable: viewer-independent, dialect-correct, popularity only."""
    cat, pop = pack.catalogue, pack.popularity or {}
    slugs = sorted({t['content_slug'] for t in cat.by_dialect.get(dialect, [])}, key=lambda s: (-pop.get(s, 0), s))
    titles = _pick(cat, slugs[:40], language, now, now + cfg.page_availability_margin, set())
    if len(titles) < MIN_ROW + 1:
        return None
    hero, rest = titles[0], titles[1:MAX_ROW + 1]
    return {'type': 'home_page', 'dialect': dialect, 'hero_title_id': hero['title_id'],
            'rows': [{'row_type': 'popular_in_dialect', 'title_ids': [t['title_id'] for t in rest]}],
            'content': [hero['title_id']] + [t['title_id'] for t in rest], 'notes': ['generic fallback page']}


def _send_time(F, goal, now, cfg):
    """Choose when to send: the viewer's usual open hour if known, else the default, never in quiet hours, and
    inside the goal window. Returns None if no legal slot exists before the deadline."""
    hours = F['hist']['open_hours']
    pref = max(hours.items(), key=lambda kv: (kv[1], -kv[0]))[0] if sum(hours.values()) >= 3 else cfg.default_send_hour_ist
    if in_quiet_hours(at_ist_hour(now, pref)):
        pref = cfg.default_send_hour_ist
    t = at_ist_hour(now + cfg.min_send_delay, pref)
    t = next_allowed_send(t)
    if t >= goal['deadline'] - timedelta(hours=1):
        t = next_allowed_send(now + cfg.min_send_delay)
        if t >= goal['deadline'] - timedelta(minutes=30):
            return None
    return t


def body_ok(pack, body, allow_digits=''):
    """Pre-check model-written copy with the policy the verifier will apply (the verifier still decides)."""
    c = pack.interventions['push']['copy']
    if not body or len(body) > min(c['body_max_chars'], 100) or looks_like_injection(body):
        return False
    low = body.lower()
    if any(ph in low for ph in c['forbidden_phrases']):
        return False
    digits = ''.join(ch for ch in body if ch.isdigit())
    if digits and digits != allow_digits:
        return False
    return not any(ch in body for ch in '₹$@<>{}[]') and 'http' not in low and 'rs.' not in low


def _model_body(pack, write_body, text, gid, title, ep_no, trial_ending, channel):
    """Let the model rewrite the body only. The title stays the catalogue title, and anything price-related
    (trial ending) keeps the fixed, price-verified template text."""
    if write_body is None or trial_ending:
        return text
    body = write_body({'channel': channel, 'goal': gid, 'title': title['title'], 'content_type': title['content_type'],
                       'genres': title['genre_list'][:3], 'episode_number': ep_no, 'template_body': text['body']})
    if body and body_ok(pack, body, str(ep_no) if ep_no else ''):
        return dict(text, body=body)
    return text


def plan_message(pack, F, goal, page, now, cfg, slot_free=lambda send_at, typ: True, write_body=None):
    """At most one message. Returns (proposal|None, reason). `write_body(context)` is an optional model-backed
    copywriter for push/in-app bodies; its answer is used only if it passes `body_ok`, else the template text."""
    gid = goal['goal_id']
    if gid not in MESSAGE_GOALS:
        return None, 'goal %s is served by the home page only (proportionality)' % gid
    if goal.get('messages_sent', 0) >= cfg.max_messages_per_goal:
        return None, 'message limit for this goal reached'
    cat, lang, dialect = pack.catalogue, F['language'], F['dialect']['value']
    if not (F['dialect']['supported'] and F['dialect']['certain']):
        return None, 'viewer dialect is %s (%s): never message on an uncertain/unsupported dialect' % (
            dialect, 'unsupported' if not F['dialect']['supported'] else 'uncertain')
    send_at = _send_time(F, goal, now, cfg)
    if send_at is None:
        return None, 'no legal send slot before the goal deadline (quiet hours/deadline)'
    through = send_at + timedelta(hours=3)
    ep_no, title = None, None
    if gid == 'G07' and goal.get('slug'):
        title = cat.variant(goal['slug'], lang, send_at, through)
        s = F['hist']['series'].get(goal['slug'])
        ep_no = s['next_episode'][1] if s and s['next_episode'] else None
    elif page and page.get('hero_title_id'):
        t = cat.get(page['hero_title_id'])
        title = cat.variant(t['content_slug'], lang, send_at, through) if t else None
    if title is None or title['dialect'] != dialect:
        return None, 'no valid title for a message in the viewer dialect'
    consent = F['consent']
    sub = F['subscription']
    trial_ending = (gid == 'G04' and sub.get('plan_ends_at') and (sub['plan_ends_at'] - now) <= timedelta(hours=48))
    base = {'send_at': send_at, 'title_id': title['title_id'], 'goal_id': gid}
    if (consent['whatsapp']['value'] or consent['push']['value']) and not slot_free(send_at, 'push_or_whatsapp'):
        return None, 'daily push/WhatsApp cap already used for the IST day of the send slot'
    if consent['whatsapp']['value']:
        if trial_ending:
            return dict(base, type='whatsapp', template_id='wa_trial_ending_v1',
                        variables={'plan_price': pack.price_text('P_MONTH')}), 'whatsapp: trial ending'
        if gid == 'G07' and ep_no:
            return dict(base, type='whatsapp', template_id='wa_new_episode_v2',
                        variables={'title_name': title['title'], 'episode_number': str(ep_no)}), 'whatsapp: resume series'
        return dict(base, type='whatsapp', template_id='wa_recommend_title_v1',
                    variables={'title_name': title['title'], 'dialect_name': dialect}), 'whatsapp: recommend'
    if consent['push']['value']:
        if trial_ending:
            text = {'title': 'Aapka trial', 'body': 'Aapka trial jald khatam ho raha hai. %s mein dekhte rahiye.' % pack.price_text('P_MONTH')}
        elif gid == 'G07' and ep_no:
            text = {'title': title['title'][:40], 'body': 'Episode %s aapka intezaar kar raha hai. Abhi dekhiye.' % ep_no}
        else:
            text = {'title': title['title'][:40], 'body': 'Aapke liye chuna gaya. Abhi dekhiye.'}
        text = _model_body(pack, write_body, text, gid, title, ep_no, trial_ending, 'push')
        return dict(base, type='push', text=text, deeplink='ott://title/%s' % title['title_id']), 'push'
    if gid in ('G04', 'G07', 'G01'):
        if not slot_free(send_at, 'in_app_message'):
            return None, 'in-app message already queued for that IST day'
        # in-app message: no consent needed, shown at the next open (no send_at in quiet hours concern)
        text = {'title': title['title'][:40], 'body': ('Episode %s aapka intezaar kar raha hai.' % ep_no) if ep_no else 'Aapke liye chuna gaya.'}
        text = _model_body(pack, write_body, text, gid, title, ep_no, False, 'in_app_message')
        return dict(base, type='in_app_message', text=text, deeplink='ott://title/%s' % title['title_id']), 'in_app_message (no push/WhatsApp consent)'
    return None, 'no consented channel for this goal'
