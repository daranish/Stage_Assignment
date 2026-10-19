"""Durable per-viewer state and the action ledger.

One JSON document per viewer (viewer_state/<id>.json): facts with provenance, the single active goal and plan, every
decision/action/outcome and what was learned. Writes are atomic (tmp + rename). If the data store is unavailable
the write stays queued (dirty) and is flushed when it recovers - state is never silently dropped.
"""
import os
from collections import defaultdict
from datetime import timedelta

from .util import dumps, iso, ist_day


def new_state(user, arm):
    return {'viewer_id': user, 'arm': arm, 'schema': 1,
            'facts': None,                 # latest resolved facts (with provenance + freshness)
            'active_goal': None,           # exactly one at a time: ownership
            'plan': None,
            'goal_history': [],            # every goal ever committed, with its judgement
            'decisions': [],               # compact index of decisions (full records are in decisions.jsonl)
            'actions': [],
            'outcomes': [],
            'learnings': [],
            'goal_stats': {},              # goal_id -> {attempts, succeeded, failed, inconclusive}
            'seen_triggers': {},           # idempotency: trigger event_id -> decision_id
            'rule_migrations': [],
            'notes': []}


class StateStore:
    def __init__(self, out_dir=None):
        self.dir = out_dir
        self.states = {}
        self.dirty = set()
        self.deferred_writes = 0
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

    def get(self, user, arm):
        st = self.states.get(user)
        if st is None:
            st = self.states[user] = new_state(user, arm)
        return st

    def save(self, user, healthy=True):
        if healthy:
            self._write(user)
        else:
            self.dirty.add(user)
            self.deferred_writes += 1

    def flush(self):
        n = len(self.dirty)
        for u in list(self.dirty):
            self._write(u)
        self.dirty.clear()
        return n

    def _write(self, user):
        if not self.dir:
            return
        p = os.path.join(self.dir, user + '.json')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(dumps(self.states[user], indent=1))
        os.replace(tmp, p)


class Ledger:
    """Append-only record of every action the system published, with cancellation state."""

    def __init__(self):
        self.actions = []
        self.by_user = defaultdict(list)
        self.keys = set()
        self.log = []          # lifecycle events: published / cancelled / blocked

    def has_key(self, key):
        return key in self.keys

    def add(self, action, key):
        self.keys.add(key)
        action['status'] = 'PUBLISHED'
        self.actions.append(action)
        self.by_user[action['user_id']].append(action)
        self.log.append({'event': 'published', 'action_id': action['action_id'], 'type': action['type'],
                         'user_id': action['user_id'], 'at': action['published_at']})

    def live(self, user):
        return [a for a in self.by_user[user] if a['status'] == 'PUBLISHED']

    def messages_on_day(self, user, day, types=('push', 'whatsapp')):
        out = []
        for a in self.live(user):
            if a['type'] in types:
                when = a.get('send_at') or a['published_at']
                if ist_day(when) == day:
                    out.append(a)
        return out

    def live_page(self, user, now, ttl):
        pages = [a for a in self.live(user) if a['type'] == 'home_page' and a['published_at'] <= now < a['published_at'] + ttl]
        return pages[-1] if pages else None

    def pending(self, user, now):
        """Scheduled messages not yet sent."""
        return [a for a in self.live(user) if a['type'] in ('push', 'whatsapp', 'in_app_message')
                and (a.get('send_at') or a['published_at']) > now]

    def cancel(self, action, at, reason):
        action['status'] = 'CANCELLED'
        action['cancelled_at'] = at
        action['cancel_reason'] = reason
        self.log.append({'event': 'cancelled', 'action_id': action['action_id'], 'type': action['type'],
                         'user_id': action['user_id'], 'at': at, 'reason': reason})

    def supersede(self, action, at, reason):
        """A page that was really shown stays in the exported actions; it is just replaced by a newer page."""
        action['status'] = 'SUPERSEDED'
        action['superseded_at'] = at
        action['supersede_reason'] = reason
        self.log.append({'event': 'superseded', 'action_id': action['action_id'], 'type': action['type'],
                         'user_id': action['user_id'], 'at': at, 'reason': reason})

    def titles_in(self, action):
        if action['type'] == 'home_page':
            return ([action['hero_title_id']] if action.get('hero_title_id') else []) + \
                [t for r in action['rows'] for t in r['title_ids']]
        return [action['title_id']] if action.get('title_id') else []

    def affected_by_title(self, slugs_of, now, ttl):
        """Actions still live/pending at `now` that include a title whose content is in slugs_of(title_id)."""
        out = []
        for a in self.actions:
            if a['status'] != 'PUBLISHED':
                continue
            if a['type'] == 'home_page':
                if not (a['published_at'] <= now < a['published_at'] + ttl):
                    continue
            elif (a.get('send_at') or a['published_at']) <= now:
                continue
            if any(slugs_of(t) for t in self.titles_in(a)):
                out.append(a)
        return out
