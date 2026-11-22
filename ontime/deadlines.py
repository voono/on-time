"""Preparation reminders for one task, bounded by its civil deadline."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .dates import fa, instant, label
from .store import enqueue, message

DAY = timedelta(days=1)


def cutoff(item, zone):
    day = date.fromisoformat(item['due_day'])
    if item['due_clock']:
        return instant(day, item['due_clock'], zone)
    return instant(day + DAY, '00:00', zone)


def stop(db, i):
    db.execute("UPDATE jobs SET state='cancelled' WHERE kind='deadline' AND item_id=? AND state='pending'", (i,))
    db.execute('DELETE FROM deadline_plans WHERE item_id=?', (i,))


def schedule(db, i, now, *, after_day=None):
    row = db.execute("""SELECT p.*, i.due_day, i.due_clock FROM deadline_plans p JOIN items i
                       ON i.id=p.item_id WHERE i.id=? AND i.status='open'""", (i,)).fetchone()
    if not row:
        return None
    zone = ZoneInfo(row['zone'])
    end = cutoff(row, zone)
    if now.timestamp() >= end:
        return None
    due = date.fromisoformat(row['due_day'])
    mins = row['effort_minutes']
    if mins % 1440 == 0:
        first_day = due - DAY * (mins // 1440)
        hm = row['clock']
    else:
        anchor = datetime.combine(due, time.fromisoformat(row['due_clock'] or '23:59'))
        first = anchor - timedelta(minutes=mins)
        first_day, hm = first.date(), first.strftime('%H:%M')
    day = max(first_day, now.astimezone(zone).date())
    if after_day:
        day = max(day, date.fromisoformat(after_day) + DAY)
    while day <= due:
        at = max(instant(day, hm, zone), int(now.timestamp()))
        if at >= end:
            return None
        key = f'deadline:{i}:{day}'
        old = db.execute('SELECT state FROM jobs WHERE key=?', (key,)).fetchone()
        if not old or old['state'] in ('pending', 'cancelled'):
            enqueue(db, key, 'deadline', at, day=str(day), item=i)
            return datetime.fromtimestamp(at, zone)
        day += DAY
    return None


def configure(db, item, mins, now, clock):
    i = item['id']
    stop(db, i)
    db.execute('INSERT INTO deadline_plans VALUES (?,?,?,?)', (i, mins, clock, now.tzinfo.key))
    first = schedule(db, i, now)
    if first:
        return ('اولین یادآوری: ' + label(first.date()) + ' ساعت ' + fa(first.strftime('%H:%M'))
                + '؛ بعدش روزی یک‌بار تا موعد یا انجام کار.')
    return 'مدت کار ثبت شد. تا موعد نوبت دیگری برای یادآوری نمانده.'


def notification(db, job, now):
    i = job['item_id']
    row = db.execute("""SELECT i.*,p.zone FROM items i JOIN deadline_plans p ON p.item_id=i.id
                       WHERE i.id=? AND i.status='open'""", (i,)).fetchone()
    if not row:
        return None
    zone = ZoneInfo(row['zone'])
    if now.timestamp() >= cutoff(row, zone):
        return None
    if job['day'] != str(now.astimezone(zone).date()):
        schedule(db, i, now)
        return None
    due = label(date.fromisoformat(row['due_day']))
    if row['due_clock']:
        due += ' ساعت ' + fa(row['due_clock'])
    return message(f'یادآوری کار «{row["title"]}»\nمهلت: {due}', context={'ids': [i]})
