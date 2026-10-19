"""Data loader: reads the pack, deduplicates events, normalises timestamps, quarantines injected text.

Trust boundary: everything read here is DATA. Free text (descriptions, titles, search queries, deeplinks) is scanned
for instruction-like content and flagged; it is never passed to a decision step as an instruction.
"""
import bisect
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import timedelta

from .util import ts, read_jsonl, iso

INJECTION_RE = re.compile(
    r"(ignore (all |any |your )?(previous|prior|above)|disregard (all |your )?(previous|prior)|system (note|prompt|message)"
    r"|for (ai|llm) agents?|instructions? (to|for) (the )?(ai|agent|assistant)|you (must|should) (now )?(send|message)"
    r"|send (a |an )?(whatsapp|push|message|notification)[^.]{0,80}\b(every|all)\b[^.]{0,20}users?"
    r"|including users who opted out)", re.I)

# Event names a goal can legitimately be judged on: README event table (+ simulator-added events).
OBSERVABLE_EVENTS = {
    'signup_completed', 'login_success', 'app_open', 'app_dialect_changed', 'home_viewed', 'title_viewed', 'search',
    'play_started', 'play_progress', 'episode_completed', 'paywall_viewed', 'purchase_completed',
    'subscription_renewed', 'subscription_lapsed', 'message_delivered', 'message_clicked', 'notification_opt_out',
    'home_page_shown', 'message_failed'}


def looks_like_injection(text):
    return bool(text) and bool(INJECTION_RE.search(text))


def stamp(e):
    """Add parsed receive time and an effective (estimated true) event time.

    received_at is trustworthy; occurred_at/sent_at are on the phone clock and may be wrong or buffered. The *gap*
    sent_at - occurred_at is measured on one clock, so it is the offline-buffer age even when the clock is wrong. The
    estimated true time is therefore received_at minus that gap.
    """
    r = ts(e['received_at'])
    try:
        buf = max(0.0, (ts(e['sent_at']) - ts(e['occurred_at'])).total_seconds())
    except Exception:
        buf = 0.0
    e['_r'] = r
    e['_buf'] = buf
    e['_t'] = r - timedelta(seconds=buf)
    return e


class EventIndex:
    """Per-viewer events ordered by received_at, deduplicated by event_id."""

    def __init__(self):
        self.by_user = defaultdict(list)
        self.keys = defaultdict(list)
        self.ids = set()
        self.dups = 0

    def add(self, e):
        if e['event_id'] in self.ids:
            self.dups += 1
            return False
        self.ids.add(e['event_id'])
        if '_r' not in e:
            stamp(e)
        lst, keys = self.by_user[e['user_id']], self.keys[e['user_id']]
        k = e['_r']
        if not keys or k >= keys[-1]:
            lst.append(e)
            keys.append(k)
        else:
            i = bisect.bisect_right(keys, k)
            lst.insert(i, e)
            keys.insert(i, k)
        return True

    def upto(self, user, now):
        keys = self.keys.get(user)
        if not keys:
            return []
        return self.by_user[user][:bisect.bisect_right(keys, now)]


class Catalogue:
    def __init__(self, rows):
        self.titles = {}
        self.by_slug = defaultdict(list)
        self.by_dialect = defaultdict(list)
        self.flagged = {}          # title_id -> reason  (instruction-like text; quarantined)
        self.withdrawn = {}        # content_slug -> datetime from which it must not be shown
        for r in rows:
            t = dict(r)
            t['available_from_dt'] = ts(r['available_from'])
            t['until_dt'] = ts(r['available_until']) if r['available_until'] else None
            t['release_dt'] = ts(r['release_date'] + 'T00:00:00Z') if r['release_date'] else None
            t['genre_list'] = [g for g in r['genres'].split('|') if g]
            t['premium'] = r['is_premium'] == 'true'
            self.titles[t['title_id']] = t
            self.by_slug[t['content_slug']].append(t)
            self.by_dialect[t['dialect']].append(t)
            if looks_like_injection(r.get('description', '')) or looks_like_injection(r.get('title', '')):
                self.flagged[t['title_id']] = 'instruction-like text in catalogue field (treated as data, not obeyed)'

    def get(self, tid):
        return self.titles.get(tid)

    def withdraw(self, title_id, at):
        t = self.titles.get(title_id)
        if t:  # conservative: the whole content (all language variants) stops being shown
            self.withdrawn[t['content_slug']] = min(at, self.withdrawn.get(t['content_slug'], at))

    def is_available(self, tid, at, through=None):
        """Available at `at` (and, if `through` is given, for the whole of [at, through])."""
        t = self.titles.get(tid)
        if t is None or t['available_from_dt'] > at:
            return False
        end = through or at
        if t['until_dt'] is not None and (t['until_dt'] <= at or t['until_dt'] < end):
            return False
        w = self.withdrawn.get(t['content_slug'])
        if w is not None and w <= end:
            return False
        return True

    def variant(self, slug, language, at, through=None):
        """Pick the title_id for a slug: language match first, any available variant otherwise."""
        vs = sorted(self.by_slug.get(slug, []), key=lambda t: (t['language'] != language, t['title_id']))
        for t in vs:
            if t['title_id'] in self.flagged:
                continue
            if self.is_available(t['title_id'], at, through):
                return t
        return None


class Pack:
    def __init__(self, root, replay_dir=None, arms_path=None):
        self.root = root
        self.replay_dir = replay_dir or os.path.join(root, 'replay')
        p = lambda *a: os.path.join(root, *a)
        self.goals_cfg = json.load(open(p('config', 'goals.json'), encoding='utf-8'))
        self.interventions = json.load(open(p('config', 'interventions.json'), encoding='utf-8'))
        self.pricing = json.load(open(p('config', 'pricing.json'), encoding='utf-8'))
        with open(p('data', 'catalogue.csv'), encoding='utf-8') as f:
            self.catalogue = Catalogue(list(csv.DictReader(f)))
        self.episodes = defaultdict(list)
        with open(p('data', 'episodes.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.episodes[r['content_slug']].append((int(r['season']), int(r['episode']), r['episode_id']))
        for v in self.episodes.values():
            v.sort()
        self.profiles = defaultdict(list)
        with open(p('data', 'profiles_snapshot.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                r['_updated'] = ts(r['updated_at'])
                r['_snapshot'] = ts(r['snapshot_at'])
                r['_signup'] = ts(r['signup_at'])
                r['_ends'] = ts(r['plan_ends_at']) if r.get('plan_ends_at') else None
                r['notifications_opt_in'] = r['notifications_opt_in'] == 'true'
                r['whatsapp_opt_in'] = r['whatsapp_opt_in'] == 'true'
                self.profiles[r['user_id']].append(r)
        self.arms = {}
        with open(arms_path or os.path.join(self.replay_dir, 'arms.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.arms[r['user_id']] = r['arm']
        self.triggers = list(read_jsonl(os.path.join(self.replay_dir, 'triggers.jsonl')))
        self.live = list(read_jsonl(os.path.join(self.replay_dir, 'live_events.jsonl')))
        self.control = list(read_jsonl(os.path.join(self.replay_dir, 'control_events.jsonl')))
        fp = os.path.join(self.replay_dir, 'model_faults.jsonl')
        self.faults = {f_['trigger_event_id']: f_ for f_ in read_jsonl(fp)} if os.path.exists(fp) else {}
        self.history = None
        self.popularity = None
        self.data_issues = Counter()

    def load_history(self):
        idx = EventIndex()
        n = 0
        for e in read_jsonl(os.path.join(self.root, 'data', 'events.jsonl')):
            n += 1
            idx.add(e)
        self.history = idx
        self.data_issues['history_lines'] = n
        self.data_issues['history_duplicate_event_ids'] = idx.dups
        # popularity per title, from deduplicated history only (no live leakage)
        pop = Counter()
        flagged_queries = 0
        for evs in idx.by_user.values():
            for e in evs:
                if e['event'] == 'play_started':
                    t = self.catalogue.get(e['properties'].get('title_id'))
                    if t:
                        pop[t['content_slug']] += 1
                elif e['event'] == 'search' and looks_like_injection(e['properties'].get('query', '')):
                    flagged_queries += 1
        self.popularity = pop
        self.data_issues['injection_like_search_queries'] = flagged_queries
        self.data_issues['injection_like_catalogue_rows'] = len(self.catalogue.flagged)
        return idx

    def arm(self, user):
        # unknown arm -> safest reading: treat as holdout (no actions)
        return self.arms.get(user, 'holdout')

    def price_text(self, plan_id='P_MONTH'):
        for p_ in self.pricing['plans']:
            if p_['plan_id'] == plan_id:
                return '₹%d' % p_['price_inr']
        raise KeyError(plan_id)

    def valid_prices(self):
        return {p_['price_inr'] for p_ in self.pricing['plans']}
