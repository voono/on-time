"""The nightly report uses recorded task completions and reminder responses."""
from datetime import timedelta

from .deadlines import cutoff
from .dates import fa, instant, label
from .store import message, pref

DAY = timedelta(days=1)


def record_completion(db, item, now):
    on_time = int(now.timestamp() < cutoff(item, now.tzinfo)) if item['due_day'] else None
    db.execute('INSERT OR IGNORE INTO task_completions VALUES (?,?,?)',
               (item['id'], int(now.timestamp()), on_time))


def queue_report(db, now):
    if pref(db, 'night_enabled') != '1':
        return
    at = instant(now.date(), pref(db, 'night'), now.tzinfo)
    if now.timestamp() >= at:
        day = str(now.date())
        db.execute("INSERT OR IGNORE INTO jobs(key,kind,at,day) VALUES (?,'night',?,?)",
                   ('night:' + day, at, day))


def report(db, now):
    day = now.date()
    zone = now.tzinfo
    start = instant(day, '00:00', zone)
    end = instant(day + DAY, '00:00', zone)
    week_start = instant(day - 6 * DAY, '00:00', zone)
    completions = db.execute("""SELECT i.title,c.on_time,c.completed_at FROM task_completions c
        JOIN items i ON i.id=c.item_id WHERE c.completed_at>=? AND c.completed_at<?
        ORDER BY c.completed_at DESC,c.item_id DESC""", (week_start, end)).fetchall()
    today = [row for row in completions if row['completed_at'] >= start]
    lines = ['🌙 جمع‌بندی امروز · ' + label(day)]
    if today:
        titles = '، '.join(row['title'] for row in today[:2])
        more = ' و ' + fa(len(today) - 2) + ' کار دیگه' if len(today) > 2 else ''
        lines.append(f'✓ {fa(len(today))} کار انجام شد · {titles}{more}')
        dated = [row['on_time'] for row in today if row['on_time'] is not None]
        if dated:
            lines.append(f'⏱ {fa(sum(dated))} از {fa(len(dated))} کار موعددارِ انجام‌شده، به‌موقع بود.')
    else:
        lines.append('امروز هنوز انجام کاری ثبت نشده.')

    week = [row['on_time'] for row in completions if row['on_time'] is not None]
    if len(week) >= 2:
        lines.append(f'📈 هفت روز اخیر · {fa(sum(week))} از {fa(len(week))} کار موعددارِ انجام‌شده، به‌موقع بود.')

    counts = dict(db.execute("""SELECT c.outcome,COUNT(*) FROM jobs j JOIN checkins c ON c.job_key=j.key
        WHERE j.sent_at>=? AND j.sent_at<? GROUP BY c.outcome""", (start, end)))
    statuses = [('done', 'انجام شد'), ('missed', 'انجام نشد'), ('later', 'موکول شد'), (None, 'بی‌پاسخ')]
    answers = [f'{fa(counts[key])} {caption}' for key, caption in statuses if counts.get(key)]
    if answers:
        lines.append('🔔 یادآوری‌ها · ' + '، '.join(answers))

    upcoming = db.execute("""SELECT title,due_day,due_clock FROM items WHERE kind='task'
        AND status='open' AND due_day<=? ORDER BY due_day,COALESCE(due_clock,'23:59'),id""",
                          (str(day + DAY),)).fetchall()
    late = sum(now.timestamp() >= cutoff(row, zone) for row in upcoming)
    if late:
        lines.append(f'{fa(late)} کار باز از موعد گذشته.')
    if upcoming:
        lines.append(f'برای فردا · اول یه قدم کوچیک برای «{upcoming[0]["title"]}» بردار.')
    elif today:
        lines.append('برای امشب همین‌قدر کافیه. شب بخیر 🌙')
    else:
        lines.append('فردا دوباره از یه قدم کوچیک شروع کن. شب بخیر 🌙')
    return message('\n'.join(lines))
