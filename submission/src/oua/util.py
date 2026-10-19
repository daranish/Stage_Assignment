"""Time helpers. All internal times are timezone-aware UTC datetimes; IST (UTC+05:30) is used only for
quiet hours and "per day" rules, as the messaging policy requires."""
import json
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None


def iso(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z' if dt else None


def ist_day(dt):
    return dt.astimezone(IST).date()


def ist_hour(dt):
    return dt.astimezone(IST).hour


def in_quiet_hours(dt):
    h = ist_hour(dt)
    return h >= 22 or h < 8


def next_allowed_send(dt):
    """Earliest instant >= dt outside 22:00-08:00 IST."""
    if not in_quiet_hours(dt):
        return dt
    local = dt.astimezone(IST)
    target = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local.hour >= 22:
        target += timedelta(days=1)
    return target.astimezone(UTC)


def at_ist_hour(dt, hour, minute=0):
    """Next occurrence of hour:minute IST at or after dt."""
    local = dt.astimezone(IST)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target < local:
        target += timedelta(days=1)
    return target.astimezone(UTC)


def ist_midnight(d: date):
    return datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(UTC)


def jsonable(o):
    """json.dumps default: datetimes -> ISO, sets -> sorted lists."""
    if isinstance(o, datetime):
        return iso(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, timedelta):
        return o.total_seconds()
    raise TypeError('not serialisable: %r' % (type(o),))


def dumps(o, **kw):
    return json.dumps(o, default=jsonable, ensure_ascii=False, **kw)


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
