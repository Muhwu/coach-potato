"""Calendar dates <-> ms timestamps in the USER's timezone.

The server only ever binds to 127.0.0.1 (run.sh, desktop.py), so the browser
showing the dates and this process run on the same machine: the OS's local
zone IS the user's zone. Every "which day was this game" / "where does this
day start" question goes through here, never through UTC — a UTC day ends at
4-8 pm in North America, which silently dropped evening games from a
"to = today" filter and pushed them into tomorrow's trend bucket.

`TZ = None` means the OS local zone (DST-aware via the C library); tests pin
it to a fixed `zoneinfo.ZoneInfo` so results don't depend on the machine.
"""
from datetime import date, datetime, timedelta

TZ = None


def day_start_ms(date_str):
    """Local midnight at the start of YYYY-MM-DD, in ms."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if TZ is not None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp() * 1000)  # naive = local time (mktime, DST-aware)


def add_days(date_str, days):
    return (date.fromisoformat(date_str) + timedelta(days=days)).isoformat()


def day_end_ms(date_str):
    """Last ms of YYYY-MM-DD in local time (inclusive end; a DST day is 23/25 h)."""
    return day_start_ms(add_days(date_str, 1)) - 1


def local_datetime(ms):
    return datetime.fromtimestamp(ms / 1000, TZ)


def local_date(ms):
    return local_datetime(ms).date()


def today():
    return datetime.now(TZ).date() if TZ is not None else date.today()


def bucket_label(ms, bucket):
    """Calendar bucket of a timestamp: day 'YYYY-MM-DD', week = its Monday's
    date, month 'YYYY-MM'."""
    d = local_date(ms)
    if bucket == "day":
        return d.isoformat()
    if bucket == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if bucket == "month":
        return d.strftime("%Y-%m")
    raise ValueError(f"unknown bucket {bucket!r}")


def register_sql_functions(conn):
    """`local_bucket(ms, 'day'|'week'|'month')` for GROUP BY in SQLite, whose
    own 'localtime' modifier can't be pinned for tests."""
    conn.create_function("local_bucket", 2, bucket_label, deterministic=True)
