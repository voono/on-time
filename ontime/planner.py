import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import deadlines, nightly
from .dates import clock, fa, instant, label, occurrence
from .store import cancel, enqueue, item_by_id, message, pref

MINUTE = 60
DAY = timedelta(days=1)


def set_windows(db, keys, windows):
    ordered = sorted((clock(a), clock(b)) for a, b in windows)
    for idx, (a, b) in enumerate(ordered):
        if a >= b or (idx and a < ordered[idx - 1][1]):
            raise ValueError('هر بازه باید در همان روز تمام شود و بازه‌ها با هم تداخل نداشته باشند.')
    db.executemany('INSERT OR REPLACE INTO availability VALUES (?,?)',
                   [(k, json.dumps(ordered)) for k in keys])


def openings(db, now):
    day = now.date()
    row = db.execute("""SELECT windows FROM availability WHERE key IN (?,?)
        ORDER BY CASE key WHEN ? THEN 0 ELSE 1 END LIMIT 1""",
                     (str(day), str(day.weekday()), str(day))).fetchone()
    if not row:
        return []
    windows = [(max(instant(day, a, now.tzinfo), int(now.timestamp()) + (1 if now.second else 0)),
                instant(day, b, now.tzinfo)) for a, b in json.loads(row[0])]
    # Round up to an actual clock minute available to the user.
    windows = [((a + 59) // 60 * 60, b) for a, b in windows if a < b]
    for item in db.execute("SELECT * FROM items WHERE status='open' AND due_day IS NOT NULL"):
        # Include yesterday's timed appointments that extend past midnight.
        for d in (day - DAY, day):
            if occurrence(item, d) != d:
                continue
            if item['due_clock']:
                start = instant(d, item['due_clock'], now.tzinfo)
                if item['minutes']:
                    end = start + MINUTE * item['minutes']
                elif item['kind'] == 'event':
                    end = instant(d + DAY, '00:00', now.tzinfo)
                else:
                    end = start + MINUTE
            elif item['kind'] == 'event':
                start, end = instant(d, '00:00', now.tzinfo), instant(d + DAY, '00:00', now.tzinfo)
            else:
                continue
            parts = []
            for a, b in windows:
                if b <= start or end <= a:
                    parts.append((a, b))
                else:
                    if a < start:
                        parts.append((a, start))
                    if end < b:
                        parts.append((end, b))
            windows = parts
    return [(a, b) for a, b in windows if b > a]


def morning(db, now):
    day = now.date()
    lines = ['صبح بخیر 🌤']
    today, late, near = [], [], []
    for row in db.execute("SELECT * FROM items WHERE status='open' AND due_day IS NOT NULL ORDER BY due_day,due_clock,id"):
        d = occurrence(row, day)
        if d == day:
            today.append((row['due_clock'] or '', row['title']))
        elif d and d < day and row['kind'] == 'task' and row['important']:
            late.append(row['title'])
        elif d and day < d <= day + DAY * int(pref(db, 'near')) and row['kind'] == 'event':
            near.append((d, row['title']))
    for row in db.execute("SELECT * FROM items WHERE status='open' AND remind_at IS NOT NULL"):
        when = datetime.fromtimestamp(row['remind_at'], now.tzinfo)
        if when.date() == day and (row['due_day'] != str(day) or row['due_clock'] != when.strftime('%H:%M')):
            today.append((when.strftime('%H:%M'), 'یادآوری ' + row['title']))
    def summary(heading, values):
        if values:
            extra = fa(f' (+{len(values)-3})') if len(values) > 3 else ''
            lines.append(heading + '؛ '.join(values[:3]) + extra)
    summary('امروز: ', [f'{fa(t)} {v}'.strip() for t, v in sorted(today)])
    summary('مهم و عقب‌افتاده: ', late)
    summary('نزدیک: ', [f'{v} ({label(d)})' for d, v in sorted(near)])
    windows = openings(db, now)
    tasks = db.execute("""SELECT i.*, MAX(s.day) AS last_day FROM items i
        LEFT JOIN suggestions s ON s.item_id=i.id AND s.day < ?
        WHERE i.kind='task' AND i.status='open' AND i.due_day IS NULL
        GROUP BY i.id ORDER BY (COALESCE(last_day,'') >= ?) ASC,
        (i.important + MAX(i.short, COALESCE(i.minutes<=20,0))) DESC,
        i.important DESC, COALESCE(last_day,''), i.id""",
                       (str(day), str(day - 3 * DAY))).fetchall()
    slot = None
    chosen = []
    if windows:
        for item in tasks:
            length = item['minutes']
            for a, b in windows:
                if not length or length * MINUTE <= b - a:
                    slot = (a, a + length * MINUTE if length else min(b, a + 15 * MINUTE))
                    chosen.append(item)
                    break
            if slot:
                break
    if chosen:
        chosen.extend([t for t in tasks if t['id'] != chosen[0]['id']][:2])
    else:
        chosen = tasks[:1]
    db.execute('DELETE FROM suggestions WHERE day=?', (str(day),))
    rows = []
    if chosen:
        lines.append('اگر دوست داشتی امروز یکی رو بردار:')
    for index, item in enumerate(chosen):
        i = item['id']
        a, b = slot if index == 0 and slot else (None, None)
        db.execute('INSERT INTO suggestions VALUES (?,?,?,?)', (str(day), i, a, b))
        lines.append(f'{fa(index+1)}. ' + item['title'])
        if a is not None:
            span = ' تا '.join(fa(datetime.fromtimestamp(t, now.tzinfo).strftime('%H:%M')) for t in (a, b))
            lines.append(('فرصتی برای شروع: ' if not item['minutes'] else 'زمان پیشنهادی: ') + span)
    if chosen:
        first = chosen[0]['id']
        if slot:
            rows = [[('قبول همین ساعت برای گزینهٔ اول', f'slot:{day}:{first}')]]
        else:
            rows = [[('برای امروز انتخابش کن', f'pick:{day}:{first}')]]
        lines.append('برای انتخاب، شماره یا اسم کار رو بگو.')
    return message('\n'.join(lines), rows, context={'kind': 'morning', 'day': str(day),
                   'ids': [item['id'] for item in chosen]} if chosen else None)


def current_plan(db, day):
    return db.execute("SELECT * FROM plans WHERE day=? AND status='active'", (str(day),)).fetchone()


def plan_buttons(p):
    tail = f"{p['day']}:{p['revision']}"
    return [[('انجام شد', 'done:' + tail), ('بذار برای بعد', 'later:' + tail),
             ('امروز ردش کن', 'skip:' + tail)]]


def choose(db, now, item, accept=False):
    if not item or item['kind'] != 'task' or item['due_day'] or item['status'] != 'open':
        return message('این کار در فهرست کارهای بازِ بی‌موعد نیست.')
    day, i = str(now.date()), item['id']
    end = None
    if accept:
        s = db.execute('SELECT * FROM suggestions WHERE day=? AND item_id=?', (day, i)).fetchone()
        if not s or not s['start_at'] or not any(
            a <= s['start_at'] and s['end_at'] <= b for a, b in openings(db, now)
        ):
            return message('این بازه دیگر آزاد نیست. خود کار را برای امروز انتخاب می‌کنی؟',
                           [[('انتخاب برای امروز', f'pick:{day}:{i}')]])
        end = s['end_at']
    db.execute("""INSERT INTO plans VALUES (?,?,'active',?,1) ON CONFLICT(day) DO UPDATE SET
        item_id=excluded.item_id,status='active',end_at=excluded.end_at,revision=plans.revision+1""", (day, i, end))
    key = 'follow:' + day
    cancel(db, key)
    at = end or instant(now.date(), pref(db, 'follow'), now.tzinfo)
    if at > now.timestamp():
        enqueue(db, key, 'follow', at, day=day, item=i)
    p = current_plan(db, day)
    suffix = '؛ با همون زمان پیشنهادی' if accept else ''
    return message(f'برای امروز انتخاب شد: {item["title"]}{suffix}.',
                   context={'kind': 'plan', 'ids': [i], 'day': day, 'revision': p['revision']})


def close_plan(db, p, status):
    if p:
        db.execute('UPDATE plans SET status=? WHERE day=?', (status, p['day']))
        cancel(db, 'follow:' + p['day'])


def reminder_key(i, at):
    return f'remind:{i}:{at}'


def clear_reminder(db, item):
    if item['remind_at'] is not None:
        cancel(db, reminder_key(item['id'], item['remind_at']))
    db.execute('UPDATE items SET daily=0,remind_at=NULL,reminder_lead=NULL,reminder_clock=NULL,reminder_zone=NULL WHERE id=?',
               (item['id'],))


def next_daily(day, hm, zone, after):
    day = max(day, datetime.fromtimestamp(after, zone).date())
    at = instant(day, hm, zone)
    return at if at > after else instant(day + DAY, hm, zone)


def next_annual(item, after):
    zone = ZoneInfo(item['reminder_zone'])
    base = datetime.fromtimestamp(after, zone).date() + timedelta(days=item['reminder_lead'])
    event = occurrence(item, base)
    if event:
        def stamp(d):
            return instant(d - timedelta(days=item['reminder_lead']), item['reminder_clock'], zone)
        at = stamp(event)
        if at <= after:
            following = occurrence(item, event + DAY)
            return stamp(following) if following else None
        return at
    return None


def arm(db, i, at):
    db.execute('UPDATE items SET remind_at=? WHERE id=?', (at, i))
    if at is not None:
        enqueue(db, reminder_key(i, at), 'reminder', at, item=i)


def morning_due(db, now):
    day = str(now.date())
    key = 'morning:' + day
    if pref(db, 'enabled') == '1':
        at = instant(now.date(), pref(db, 'morning'), now.tzinfo)
        if at <= now.timestamp() < at + 2 * 3600:
            db.execute("""INSERT OR IGNORE INTO jobs(key,kind,at,day) VALUES (?,'morning',?,?)""", (key, at, day))


def scheduled_message(db, job, now):
    kind = job['kind']
    if kind == 'deadline':
        return deadlines.notification(db, job, now)
    if kind == 'reply':
        return json.loads(job['payload'])
    if kind == 'reminder':
        row = item_by_id(db, job['item_id'])
        if row and row['status'] == 'open' and row['remind_at'] is not None and job['key'] == reminder_key(row['id'], row['remind_at']):
            if row['daily']:
                zone = ZoneInfo(row['reminder_zone'])
                today = now.astimezone(zone).date()
                if datetime.fromtimestamp(row['remind_at'], zone).date() < today:
                    arm(db, row['id'], instant(today, row['reminder_clock'], zone))
                    return None
            return message('⏰ ' + row['title'] + ('\n' + row['details'] if row['details'] else ''), context={'ids': [row['id']]})
        return None
    if job['day'] != str(now.date()):
        return None
    if kind == 'night' and pref(db, 'night_enabled') == '1':
        return nightly.report(db, now)
    if kind == 'morning' and pref(db, 'enabled') == '1':
        at = instant(now.date(), pref(db, 'morning'), now.tzinfo)
        if at <= now.timestamp() < at + 2 * 3600:
            return morning(db, now)
    if kind == 'follow':
        plan = current_plan(db, now.date())
        row = item_by_id(db, job['item_id'])
        if plan and plan['item_id'] == job['item_id'] and row and row['status'] == 'open':
            return message('فرصت شد «' + row['title'] + '» رو انجام بدی؟', plan_buttons(plan),
                           context={'kind': 'plan', 'ids': [row['id']], 'day': plan['day'], 'revision': plan['revision']})
    return None
