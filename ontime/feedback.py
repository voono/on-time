"""Per-occurrence check-ins and short, local feedback based on explicit answers."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .dates import fa, label, norm
from .store import item_by_id, message

DAY = timedelta(days=1)


def keyboard(n, day, revision, outcome=None):
    buttons = [[(('✓ ' if outcome == key else '') + caption, f'check:{n}:{key}')
                for key, caption in (('done', 'انجام دادم'), ('missed', 'انجام ندادم'))]]
    if revision is not None:
        suffix = f'{day}:{revision}'
        buttons.append([('بذار برای بعد', 'later:' + suffix), ('امروز ردش کن', 'skip:' + suffix)])
    return message('', buttons)['reply_markup']


def prepare(db, job, payload, now):
    item = item_by_id(db, job['item_id'])
    ctx = payload['_context']
    zone = ZoneInfo(item['reminder_zone']) if item['reminder_zone'] else now.tzinfo
    day = job['day'] or str(datetime.fromtimestamp(job['at'], zone).date())
    routine = job['kind'] == 'reminder' and bool(item['daily'] or item['annual'])
    # A retry can follow an edit to an unsent daily selection.
    db.execute("""INSERT INTO checkins(job_key,item_id,day,routine,revision) VALUES (?,?,?,?,?)
        ON CONFLICT(job_key) DO UPDATE SET item_id=excluded.item_id,day=excluded.day,
        routine=excluded.routine,revision=excluded.revision""",
               (job['key'], item['id'], day, routine, ctx.get('revision')))
    checkin = db.execute('SELECT id FROM checkins WHERE job_key=?', (job['key'],)).fetchone()[0]
    ctx['checkin'] = checkin
    payload['reply_markup'] = keyboard(checkin, day, ctx.get('revision'))
    if routine and item['daily']:
        yesterday = str(now.astimezone(zone).date() - DAY)
        previous = db.execute("""SELECT outcome,answered_at FROM checkins WHERE item_id=?
            AND routine=1 AND day=? ORDER BY id DESC LIMIT 1""", (item['id'], yesterday)).fetchone()
        if previous and previous['outcome'] == 'missed':
            hint(db, payload, item['id'], now.date(), previous['answered_at'],
                 'برای نوبت دیروز «انجام ندادم» زدی. امروز هم که انجامش دادی، همین‌جا علامت بزن.')


def hint(db, payload, i, day, evidence, text):
    last = db.execute('SELECT * FROM checkin_hints WHERE item_id=?', (i,)).fetchone()
    if last and (last['day'] > str(day - 3 * DAY) or last['evidence'] >= evidence):
        return False
    payload['text'] += '\n' + text
    payload.setdefault('_context', {})['hint'] = [i, str(day), evidence]
    return True


def morning_hint(db, payload, now):
    week = (str(now.date() - 7 * DAY), str(now.date()))
    rows = db.execute("""WITH delays(item_id,day,evidence) AS (
          SELECT item_id,day,answered_at FROM checkins WHERE routine=0
            AND outcome IN ('missed','later') AND day>=? AND day<?
          UNION ALL
          SELECT item_id,day,CAST(strftime('%s',day) AS INTEGER) FROM plans
            WHERE status='later' AND day>=? AND day<?
        )
        SELECT d.item_id,i.title,COUNT(DISTINCT d.day) AS days,MAX(d.evidence) AS evidence
        FROM delays d JOIN items i ON i.id=d.item_id WHERE i.kind='task' AND i.status='open'
        GROUP BY d.item_id HAVING COUNT(DISTINCT d.day)>=2 ORDER BY days DESC,evidence DESC""", week + week)
    for row in rows:
        line = f'«{row["title"]}» توی {fa(row["days"])} روز هنوز انجام نشده بود. امروز فقط قدم اولش رو برداریم؟'
        if hint(db, payload, row['item_id'], now.date(), row['evidence'], line):
            break


def record(db, n, outcome, now):
    row = db.execute("""SELECT c.*, j.kind, j.state FROM checkins c JOIN jobs j ON j.key=c.job_key
                       WHERE c.id=?""", (n,)).fetchone()
    if not row or row['state'] not in ('sent', 'uncertain'):
        return message('این نوبت رو پیدا نکردم.'), None
    item = item_by_id(db, row['item_id'])
    ctx = {'ids': [item['id']], 'checkin': n}
    date_text = label(datetime.fromisoformat(row['day']).date())
    labels = {'done': 'انجام دادم', 'missed': 'انجام ندادم', 'later': 'بذار برای بعد'}
    if row['outcome'] == outcome:
        return {**message('همین پاسخ قبلاً ثبت شده.', context=ctx), '_mark_checkin': n}, None
    if row['revision'] is not None:
        plan = db.execute("SELECT * FROM plans WHERE day=? AND status='active'", (row['day'],)).fetchone()
        if not plan or row['day'] != str(now.date()) or plan['revision'] != row['revision']:
            return message('این پیگیری مربوط به انتخاب قبلیه.'), None
    if not row['routine'] and item['status'] != 'open':
        return message('این مورد قبلاً بسته شده.'), None
    db.execute('UPDATE checkins SET outcome=?,answered_at=? WHERE id=?', (outcome, int(now.timestamp()), n))
    finish = (outcome == 'done' and not row['routine'] and item['status'] == 'open' and
              (item['kind'] == 'task' or (item['kind'] == 'reminder' and item['remind_at'] is None)))
    result = message(f'{item["title"]} · {date_text}: «{labels[outcome]}» ثبت شد.', context=ctx)
    result['_mark_checkin'] = n
    return result, item if finish else None


def history(db, now, query=''):
    period, _, rest = norm(query).partition(' ')
    end = now.date()
    if period in ('امروز', 'دیروز'):
        if period == 'دیروز':
            end -= DAY
        start, target, heading = end, rest, 'سابقهٔ ' + period
    else:
        start, target, heading = end - 6 * DAY, norm(query), 'آخرین نوبت‌های هفت روز اخیر'
    rows = db.execute("""SELECT c.day,i.title,c.outcome FROM checkins c JOIN items i ON i.id=c.item_id
        JOIN jobs j ON j.key=c.job_key WHERE (j.state='sent' OR c.outcome IS NOT NULL)
        AND c.day BETWEEN ? AND ? ORDER BY c.day DESC,j.at DESC""", (str(start), str(end)))
    labels = {None: 'بی‌پاسخ', 'done': 'انجام دادم', 'missed': 'انجام ندادم', 'later': 'برای بعد'}
    lines = []
    for row in rows:
        title = norm(row['title'])
        if not all(w in title or (len(w) > 3 and w.endswith('م') and w[:-1] in title)
                   for w in target.split()):
            continue
        lines.append(f'{label(datetime.fromisoformat(row["day"]).date())} · {row["title"]} · {labels[row["outcome"]]}')
        if len(lines) == 10:
            break
    if not lines:
        return message(heading + '؛ برای این بازه نوبتی با این مشخصات ثبت نشده.')
    return message(heading + '\n' + '\n'.join(lines))
