import json
import re
from datetime import datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import deadlines, feedback, nightly
from .dates import WEEKDAYS, clock, fa, instant, label, norm, occurrence, relative_label, resolve
from .intent import Intent
from .planner import (arm, choose, clear_reminder, close_plan, current_plan, next_annual, next_daily,
                      set_windows)
from .store import cancel, enqueue, item_by_id, message, pending, pref, put, set_pending, tasks_keyboard

KINDS = {'task': 'کار', 'note': 'یادداشت', 'event': 'مناسبت', 'reminder': 'یادآوری'}
HELP_INTRO = """هرچی توی ذهنت هست بنویس. مثلاً «یه وقت به دندون‌پزشک زنگ بزنم» یا «فردا ساعت ۱۰ یادم بنداز قبض رو بدم».
بعدش هم می‌تونی بگی «ساعتش رو عوض کن» یا بپرسی «امروز چه کارهایی دارم؟»"""


def matches(row, target):
    if not target:
        return True
    t = norm(target).strip('# ')
    if t.isdecimal():
        return row['id'] == int(t)
    def words(s):
        return re.findall(r'[^\W_]+', norm(s).replace('ٔ', ''), re.UNICODE)
    title = ''.join(words(row['title']))
    return all(w in title for w in words(t)) and bool(words(t))


def active(db):
    return db.execute("SELECT * FROM items WHERE status='open' ORDER BY id DESC").fetchall()


def candidate_titles(db, text, now, context):
    visible = context.get('ids', [])
    words = {w for w in re.findall(r'[^\W_]+', norm(text)) if len(w) > 2}
    plan = current_plan(db, now.date())
    offered = {r[0] for r in db.execute('SELECT item_id FROM suggestions WHERE day=?', (str(now.date()),))}
    candidates = []
    for row in db.execute("SELECT id,title,kind,due_day,due_clock,remind_at,daily FROM items WHERE status='open'"):
        title = norm(row['title'])
        score = sum(len(w) for w in words if w in title)
        selected = bool(plan and plan['item_id'] == row['id'])
        if score or selected or row['id'] in offered or row['id'] in visible:
            position = visible.index(row['id']) + 1 if row['id'] in visible else None
            candidate = dict(row)
            if candidate['remind_at'] is not None:
                candidate['remind_at'] = datetime.fromtimestamp(row['remind_at'], now.tzinfo).isoformat()
            candidate.update(selected_today=selected, position=position, checkin=bool(position and context.get('checkin')))
            candidates.append((bool(position), score, row['id'], candidate))
    return [data for _, _, _, data in sorted(candidates, reverse=True)[:12]]


def list_page(db, scope, target, page, now, view=None):
    today = now.date()
    week = today - timedelta(days=(today.weekday() - 5) % 7)
    plan = current_plan(db, today) if scope in ('today', 'today_tasks') else None
    selected_id = plan['item_id'] if plan else None
    found = []
    for item in active(db):
        if not matches(item, target):
            continue
        d = occurrence(item, week if scope == 'week' else today)
        has_reminder = item['remind_at'] is not None
        keep = {'today': d == today, 'week': bool(d and week <= d < week + timedelta(days=7)),
                'undated': item['kind'] == 'task' and d is None, 'notes': item['kind'] == 'note',
                'events': item['kind'] == 'event', 'reminders': has_reminder,
                'tasks': item['kind'] == 'task' or (item['kind'] == 'reminder' and has_reminder),
                'today_tasks': item['kind'] == 'task' and (d == today or item['id'] == selected_id),
                'all': True}[scope]
        # Explicit reminders also belong in date-based agenda views.
        rday = datetime.fromtimestamp(item['remind_at'], now.tzinfo).date() if item['remind_at'] else None
        if keep or (scope == 'today' and rday == today) or (scope == 'week' and rday and week <= rday < week + timedelta(days=7)):
            found.append((item, d))
    header = ['کارهای باز و یادآوری‌های فعال'] if scope == 'tasks' and found else []
    if scope == 'today_tasks' and found:
        header = ['کارهای امروز']
    if scope == 'today' and not target and plan:
        selected = item_by_id(db, selected_id)
        header = ['انتخاب امروز: ' + selected['title']]
    if not found and header:
        return message(header[0], context={'ids': [selected_id]})
    if not found:
        empty = {'tasks': 'فعلاً کار باز یا یادآوری فعالی نداری.', 'today_tasks': 'فعلاً کاری برای امروز نداری.'}
        return message(empty.get(scope if not target else '', 'چیزی با این مشخصات پیدا نکردم.'))
    found.sort(key=lambda x: (x[1] is None, str(x[1]), x[0]['due_clock'] or '', -x[0]['id']))
    size = 6
    page = min(max(0, page), (len(found) - 1) // size)
    rows, lines, ids = [], header, []
    for item, d in found[page * size:(page + 1) * size]:
        desc = label(d) + (' ' + fa(item['due_clock']) if item['due_clock'] else '') if d else ''
        if scope == 'today_tasks' and item['id'] == selected_id and not desc:
            desc = 'انتخاب امروز'
        if item['remind_at']:
            desc += (' · ' if desc else '') + 'یادآوری ' + reminder_label(item, now.tzinfo)
        ids.append(item['id'])
        lines.append(f'{fa(len(ids))}. {item["title"]}' + (' — ' + desc if desc else ''))

    if len(found) > size:
        if view is None:
            view = db.execute('INSERT INTO views(scope,target) VALUES (?,?)', (scope, target)).lastrowid
        nav = []
        if page:
            nav.append(('قبلی', f'page:{view}:{page-1}'))
        if (page + 1) * size < len(found):
            nav.append(('بعدی', f'page:{view}:{page+1}'))
        rows.append(nav)
    lines.append('شماره یا اسمش رو بگو، مثلاً «جزئیات اولی».')
    return message('\n'.join(lines), rows, context={'kind': 'list', 'ids': ids, 'view': view, 'page': page,
                                                     'more': (page + 1) * size < len(found)})


def reminder_label(item, zone):
    dt = datetime.fromtimestamp(item['remind_at'], zone)
    upcoming = label(dt.date()) + ' ' + fa(dt.strftime('%H:%M'))
    if item['daily']:
        schedule = 'هر روز ساعت ' + fa(item['reminder_clock'])
        if item['reminder_zone'] != zone.key:
            schedule += ' به وقت ' + item['reminder_zone']
        return schedule + ' · نوبت بعد ' + upcoming
    return upcoming


def detail(db, item, now):
    if not item or item['status'] != 'open':
        return message('این مورد دیگر باز نیست.')
    i = item['id']
    lines = [KINDS[item['kind']] + ' · ' + item['title']]
    if item['details']:
        lines.append(item['details'])
    d = occurrence(item, now.date())
    if d:
        lines.append('موعد: ' + label(d) + (' ' + fa(item['due_clock']) if item['due_clock'] else ''))
    job = db.execute("SELECT at FROM jobs WHERE kind='deadline' AND item_id=? AND state='pending'", (i,)).fetchone()
    if job:
        at = datetime.fromtimestamp(job['at'], now.tzinfo)
        lines.append('یادآوری بعدیِ کار: ' + label(at.date()) + ' ساعت ' + fa(at.strftime('%H:%M')))
    if item['annual']:
        lines.append('هر سال در تقویم ' + ('شمسی' if item['calendar'] == 'jalali' else 'میلادی'))
    if item['remind_at']:
        lines.append('یادآوری: ' + reminder_label(item, now.tzinfo))
    return message('\n'.join(lines), context={'ids': [i]})


def help_command(_db, _now, _arg):
    lines = [HELP_INTRO, '', 'فرمان‌های بات']
    lines.extend(f'{command}{usage} — {description}' for command, (usage, description, _) in COMMANDS.items())
    return {**message('\n'.join(lines)), 'reply_markup': tasks_keyboard()}


def list_command(db, now, _arg, *, scope):
    return list_page(db, scope, '', 0, now)


def cancel_command(db, _now, _arg):
    set_pending(db, None)
    return message('سؤال بسته شد.')


def settings_command(db, now, arg, *, cmd):
    if cmd == '/settings':
        return message(f"صبح‌بخیر: {fa(pref(db,'morning'))} ({'روشن' if pref(db,'enabled')=='1' else 'خاموش'})"
                       f"\nجمع‌بندی شب: {fa(pref(db,'night'))} ({'روشن' if pref(db,'night_enabled')=='1' else 'خاموش'})"
                       f"\nپیگیری: {fa(pref(db,'follow'))}\nمناسبت‌های نزدیک: {fa(pref(db,'near'))} روز"
                       f"\nمنطقه: {pref(db,'zone')}\nبرای تغییرش بگو «صبح‌بخیر رو ساعت ۹ بفرست».")
    if cmd == '/night':
        if arg in ('off', 'خاموش'):
            put(db, 'night_enabled', '0')
        else:
            if arg not in ('on', 'روشن'):
                put(db, 'night', clock(arg))
            put(db, 'night_enabled', '1')
        db.execute("DELETE FROM jobs WHERE kind='night' AND state IN ('pending','cancelled')")
        if pref(db, 'night_enabled') == '0':
            return message('جمع‌بندی شبانه خاموش شد.')
        return message('جمع‌بندی شب رو ساعت ' + fa(pref(db, 'night')) + ' می‌فرستم.')
    if cmd == '/near':
        if not arg.isdecimal() or not 0 <= int(arg) <= 365:
            raise ValueError('تعداد روز را بین ۰ و ۳۶۵ بنویس.')
        put(db, 'near', str(int(arg)))
    elif cmd == '/morning':
        if arg in ('off', 'خاموش'):
            put(db, 'enabled', '0')
        else:
            if arg not in ('on', 'روشن'):
                put(db, 'morning', clock(arg))
            put(db, 'enabled', '1')
        db.execute("DELETE FROM jobs WHERE kind='morning' AND state IN ('pending','cancelled')")
    elif cmd in ('/followup', '/tz'):
        if cmd == '/tz':
            # IANA names are case-sensitive.
            name = arg
            try:
                zone = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('نام منطقه را مثل Asia/Tehran بنویس.') from None
            put(db, 'zone', name)
            now = now.astimezone(zone)
            db.execute("DELETE FROM jobs WHERE kind IN ('morning','night') AND state IN ('pending','cancelled')")
        else:
            put(db, 'follow', clock(arg))
        p = current_plan(db, now.date())
        if p and not p['end_at']:
            key = 'follow:' + p['day']
            cancel(db, key)
            at = instant(now.date(), pref(db, 'follow'), now.tzinfo)
            if at > now.timestamp():
                enqueue(db, key, 'follow', at, day=p['day'], item=p['item_id'])
    elif cmd == '/free':
        lines = ['بازه‌های آزاد', 'مثلاً «شنبه تا چهارشنبه ۵ تا ۶ عصر وقتم آزاده» یا «امروز ۳ تا ۴ بعدازظهر وقت دارم».']
        for row in db.execute('SELECT * FROM availability ORDER BY key'):
            k = row['key']
            if len(k) > 1 and k < str(now.date()):
                continue
            name = WEEKDAYS[int(k)] if len(k) == 1 else label(datetime.fromisoformat(k).date())
            spans = '، '.join(f'{fa(a)} تا {fa(b)}' for a, b in json.loads(row['windows']))
            lines.append(name + ': ' + (spans or 'وقت آزاد ندارم'))
        # Weekly pattern + upcoming exceptions fit one message in normal use; allow access to all via date.
        return message('\n'.join(lines)[:3900])
    if cmd == '/morning':
        if pref(db, 'enabled') == '0':
            return message('پیام صبح‌بخیر خاموش شد.')
        return message('صبح‌بخیر رو ساعت ' + fa(pref(db, 'morning')) + ' می‌فرستم.')
    if cmd == '/followup':
        return message('ساعت پیگیری شد ' + fa(pref(db, 'follow')) + '.')
    if cmd == '/tz':
        return message('منطقهٔ زمانی شد ' + pref(db, 'zone') + '.')
    return message('مناسبت‌های ' + fa(pref(db, 'near')) + ' روز آینده رو نشون می‌دم.')


COMMANDS = {
    '/start': ('', 'شروع و نمایش دکمه‌های فهرست', help_command),
    '/help': ('', 'نمایش این راهنما', help_command),
    '/mytasks': ('', 'کارهای باز و یادآوری‌های فعال', partial(list_command, scope='tasks')),
    '/todaytasks': ('', 'کارهای موعددار یا انتخاب‌شده برای امروز', partial(list_command, scope='today_tasks')),
    '/tasks': ('', 'کارهای بازِ بی‌موعد', partial(list_command, scope='undated')),
    '/list': ('', 'همهٔ موارد باز', partial(list_command, scope='all')),
    '/today': ('', 'موعدها و یادآوری‌های امروز', partial(list_command, scope='today')),
    '/week': ('', 'موعدها و یادآوری‌های هفتهٔ جاری', partial(list_command, scope='week')),
    '/notes': ('', 'یادداشت‌ها', partial(list_command, scope='notes')),
    '/history': (' [امروز یا دیروز] [عنوان]', 'سابقهٔ ثبت نتیجهٔ یادآوری‌ها', feedback.history),
    '/report': ('', 'جمع‌بندی امروز', lambda db, now, _: nightly.report(db, now)),
    '/cancel': ('', 'بستن سؤال در جریان', cancel_command),
    '/settings': ('', 'نمایش تنظیمات فعلی', partial(settings_command, cmd='/settings')),
    '/morning': (' 08:30 | on | off', 'تنظیم ساعت یا روشن و خاموش کردن پیام صبح', partial(settings_command, cmd='/morning')),
    '/night': (' 22:30 | on | off', 'تنظیم ساعت یا روشن و خاموش کردن جمع‌بندی شب',
               partial(settings_command, cmd='/night')),
    '/followup': (' 19:00', 'تنظیم ساعت پیگیری', partial(settings_command, cmd='/followup')),
    '/near': (' 7', 'تعداد روزهای نمایش مناسبت‌های نزدیک', partial(settings_command, cmd='/near')),
    '/tz': (' Asia/Tehran', 'تغییر منطقهٔ زمانی', partial(settings_command, cmd='/tz')),
    '/free': ('', 'نمایش بازه‌های آزاد ثبت‌شده', partial(settings_command, cmd='/free')),
}


def local_command(db, text, now):
    cmd, _, arg = norm(text).partition(' ')
    command = COMMANDS.get(cmd)
    if command is None:
        return message('این دستور را نمی‌شناسم. /help')
    if cmd == '/tz':
        parts = text.strip().split(maxsplit=1)
        arg = parts[1] if len(parts) > 1 else ''
    return command[2](db, now, arg)


def ask(db, text, original, intent, now, **extra):
    draft = intent.model_dump()
    for field in ('due', 'remind'):
        spec = getattr(intent, field)
        if spec:
            draft[field]['day'] = spec.anchored(now.date()).day
    seq = int(pref(db, 'question_seq', '0')) + 1
    put(db, 'question_seq', str(seq))
    set_pending(db, {'original': original, 'at': now.isoformat(), 'draft': draft, 'question_token': seq,
                     'question': text, **extra})
    context = {'kind': 'question', 'question_token': seq}
    if extra.get('edit_id'):
        context['ids'] = [extra['edit_id']]
    return message(text, context=context)


def save_item(db, intent, original, now, item=None):
    creating = item is None
    kind = intent.kind if creating else item['kind']
    if creating and not intent.title.strip():
        return ask(db, 'چی رو ثبت کنم؟', original, intent, now)
    due, remind = intent.due, intent.remind
    if kind == 'note' and creating:
        due = None
    due_day = resolve(due, now.date())
    if due_day:
        label(due_day)
    daily = intent.daily if intent.daily is not None else bool(item and item['daily'])
    needs_time = intent.daily is True or intent.wants_reminder or remind is not None or (creating and kind == 'reminder')
    extra = {'edit_id': item['id']} if item else {}
    if intent.question or (needs_time and (not remind or not remind.at)):
        question = intent.question or 'چه روزی و چه ساعتی یادت بندازم؟'
        if daily and (not remind or not remind.at):
            question = 'هر روز چه ساعتی یادت بندازم؟'
        elif remind and not remind.at:
            day = resolve(remind, now.date())
            relative = relative_label((day - now.date()).days)
            question = (f'{relative} چه ساعتی یادآوری کنم؟' if relative
                        else f'برای {label(day)} چه ساعتی یادت بندازم؟')
            extra['field'] = 'remind_clock'
        return ask(db, question, original, intent, now, **extra)
    if not creating and not due and not remind:
        return ask(db, 'زمان تازه رو می‌گی؟', original, intent, now, **extra)
    annual = intent.annual if creating else bool(item['annual'])
    if annual and (kind != 'event' or not (due_day or (item and item['due_day']))):
        return ask(db, 'این مناسبت هر سال چه تاریخی است؟', original, intent, now, **extra)
    stamp, lead = None, None
    if remind:
        rem_day = resolve(remind, now.date())
        label(rem_day)
        if daily:
            stamp = next_daily(rem_day, remind.at, now.tzinfo, now.timestamp())
        else:
            stamp = instant(rem_day, remind.at, now.tzinfo, exact=True)
        if annual and not daily:
            event = ({'due_day': str(due_day), 'annual': True, 'calendar': due.calendar}
                     if due_day else item)
            original_day = datetime.fromisoformat(event['due_day']).date()
            # A birthday may have been entered with the actual birth year.
            # Match the nearby anniversary to the alert before computing the annual offset.
            event_day = occurrence(event, rem_day - timedelta(days=183))
            lead = ((event_day or original_day) - rem_day).days
        if stamp <= now.timestamp() and not annual:
            return ask(db, 'اون زمان گذشته؛ چه روز و ساعتی یادت بندازم؟', original, intent, now, **extra)
    if creating:
        i = db.execute("""INSERT INTO items(kind,title,details,created_at,due_day,due_clock,calendar,
            annual,important,short,minutes) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (kind, intent.title.strip(), intent.details, int(now.timestamp()), str(due_day) if due_day else None,
                        due.at if due else None, due.calendar if due else 'jalali',
                        annual, intent.important, intent.short, intent.minutes)).lastrowid
    else:
        i = item['id']
        if due:
            db.execute('UPDATE items SET due_day=?,due_clock=?,calendar=? WHERE id=?',
                       (str(due_day), due.at, due.calendar, i))
            p = current_plan(db, now.date())
            if p and p['item_id'] == i:
                close_plan(db, p, 'rescheduled')
    if remind:
        clear_reminder(db, item_by_id(db, i))
        if annual or daily:
            db.execute('UPDATE items SET daily=?,reminder_lead=?,reminder_clock=?,reminder_zone=? WHERE id=?',
                       (daily, lead, remind.at, now.tzinfo.key, i))
            if annual and not daily and stamp <= now.timestamp():
                stamp = next_annual(item_by_id(db, i), now.timestamp())
        arm(db, i, stamp)
    set_pending(db, None)
    saved = item_by_id(db, i)
    text = f'{KINDS[kind]} ثبت شد: {saved["title"]}' if creating else f'زمان «{saved["title"]}» تغییر کرد.'
    if due_day:
        text += '\nموعد: ' + label(occurrence(saved, now.date())) + (' ' + fa(due.at) if due.at else '')
    elif creating and kind == 'task':
        text += ' (بی‌موعد)'
    if stamp:
        text += '\nیادآوری: ' + reminder_label(saved, now.tzinfo)
    if due_day and kind == 'task':
        plan = db.execute('SELECT * FROM deadline_plans WHERE item_id=?', (i,)).fetchone()
        minutes = intent.effort_minutes or (plan['effort_minutes'] if plan else saved['minutes'])
        if minutes:
            hm = plan['clock'] if plan else pref(db, 'morning')
            text += '\n' + deadlines.configure(db, saved, minutes, now, hm)
        elif deadlines.cutoff(saved, now.tzinfo) > now.timestamp():
            return ask(db, text + '\nفکر می‌کنی چقدر زمان می‌بره؟ مثلاً «۲ روز».',
                       original, Intent(action='estimate', target=str(i)), now, field='effort', edit_id=i)
    return message(text, context={'ids': [i]})


def apply_action(db, intent, original, now, item=None, visible=()):
    a = intent.action
    if a == 'clarify' or (intent.question and a not in ('create', 'reschedule')):
        return ask(db, intent.question or 'کدوم کار یا زمان رو می‌گی؟', original, intent, now)
    if a == 'create':
        return save_item(db, intent, original, now)
    if a == 'setting':
        if not intent.setting:
            return local_command(db, '/settings', now)
        response = local_command(db, '/' + intent.setting + ' ' + intent.value, now)
        set_pending(db, None)
        return response
    if a == 'history':
        period = {'yesterday': 'دیروز', 'today': 'امروز'}.get(intent.value, '')
        return feedback.history(db, now, (period + ' ' + intent.target).strip())
    if a == 'list':
        set_pending(db, None)
        return list_page(db, intent.scope, intent.target, 0, now)
    if a == 'availability':
        keys = [str(d) for d in intent.weekdays]
        if intent.due:
            if keys:
                raise ValueError('بازه را برای یک تاریخ یا روزهای هفته مشخص کن.')
            keys = [str(resolve(intent.due, now.date()))]
        if not keys:
            return ask(db, 'این بازه برای چه روزیه؟', original, intent, now)
        set_windows(db, keys, [(w.start, w.end) for w in intent.windows])
        set_pending(db, None)
        return message('وقت آزادت رو ثبت کردم.')
    p = current_plan(db, now.date())
    if item is None:
        if not intent.target and a in ('done', 'skip', 'later') and p:
            item = item_by_id(db, p['item_id'])
        elif not intent.target and a == 'accept_slot':
            s = db.execute('SELECT item_id FROM suggestions WHERE day=? AND start_at IS NOT NULL',
                           (str(now.date()),)).fetchone()
            item = item_by_id(db, s[0]) if s else None
        else:
            if intent.target == '@':
                candidates = [r for i in visible if (r := item_by_id(db, i)) and r['status'] == 'open']
            else:
                candidates = [r for r in active(db) if intent.target and matches(r, intent.target)]
            if a in ('select', 'done', 'skip', 'later'):
                candidates = [r for r in candidates if r['kind'] == 'task']
            if len(candidates) > 1:
                # Use a persisted request id, so old buttons cannot operate on a different request.
                token = int(pref(db, 'choice_seq', '0')) + 1
                put(db, 'choice_seq', str(token))
                ids = [r['id'] for r in candidates[:12]]
                set_pending(db, {'original': original, 'draft': intent.model_dump(), 'token': token, 'ids': ids})
                options = [f'{fa(n)}. {r["title"]}' for n, r in enumerate(candidates[:12], 1)]
                return message('کدوم رو می‌گی؟ شماره یا اسمش رو بگو.\n' + '\n'.join(options),
                               context={'kind': 'match', 'ids': ids, 'token': token})
            item = candidates[0] if candidates else None
    if not item or item['status'] != 'open':
        return message('مورد بازی با این مشخصات پیدا نکردم.')
    i = item['id']
    set_pending(db, None)
    if a in ('select', 'accept_slot'):
        return choose(db, now, item, a == 'accept_slot')
    if a == 'detail':
        return detail(db, item, now)
    if a == 'reschedule':
        return save_item(db, intent, original, now, item)
    if a == 'estimate':
        if item['kind'] != 'task' or not item['due_day']:
            return message('برای یادآوری شروع کار، اول موعدش رو مشخص کن.')
        minutes = intent.effort_minutes or intent.minutes
        if not minutes:
            return ask(db, 'چقدر زمان می‌بره؟ مثلاً «۲ روز».', original, intent, now, field='effort', edit_id=i)
        return message(f'برای «{item["title"]}» تنظیم شد.\n' +
                       deadlines.configure(db, item, minutes, now, pref(db, 'morning')), context={'ids': [i]})
    if a == 'cancel_reminder':
        deadlines.stop(db, i)
        clear_reminder(db, item)
        return message('یادآوری «' + item['title'] + '» لغو شد.')
    if a in ('done', 'cancel_item'):
        if a == 'done' and item['kind'] != 'task':
            return message('این مورد کار نیست.')
        status = 'done' if a == 'done' else 'cancelled'
        close_item(db, item, status, now)
        return message('«' + item['title'] + ('» انجام شد.' if a == 'done' else '» لغو شد.'))
    if not p or p['item_id'] != i:
        return message('این کار برای امروز انتخاب نشده.')
    if a == 'later':
        checkin = db.execute('SELECT id FROM checkins WHERE revision=? AND job_key=?',
                             (p['revision'], 'follow:' + p['day'])).fetchone()
        if checkin:
            feedback.record(db, checkin['id'], 'later', now)
    close_plan(db, p, a)
    if a == 'later':
        edit = Intent(action='reschedule', target=str(i), wants_reminder=True, remind=intent.remind)
        if edit.remind:
            return save_item(db, edit, original, now, item)
        return ask(db, 'کار باز موند. اگر یادآوری می‌خوای، چه روز و ساعتی؟',
                   'تنظیم یادآوری برای ' + item['title'], edit, now, edit_id=i, field='remind')
    return message('باشه، امروز ردش کردیم. «' + item['title'] + '» توی کارهای بی‌موعد موند.')


def close_item(db, item, status, now):
    db.execute('UPDATE items SET status=? WHERE id=?', (status, item['id']))
    if status == 'done' and item['kind'] == 'task':
        nightly.record_completion(db, item, now)
    deadlines.stop(db, item['id'])
    clear_reminder(db, item)
    p = current_plan(db, now.date())
    if p and p['item_id'] == item['id']:
        close_plan(db, p, status)


def callback(db, data, now):
    parts = data.split(':')
    action = parts[0]
    expired = message('این دکمه برای انتخاب فعلی نیست. /today')
    if action == 'check' and len(parts) == 3 and re.fullmatch(r'[0-9]{1,18}', parts[1]) and parts[2] in ('done', 'missed'):
        out, item = feedback.record(db, int(parts[1]), parts[2], now)
        if item:
            close_item(db, item, 'done', now)
        return out
    if action in ('done', 'skip', 'later'):
        p = current_plan(db, now.date())
        if not p or parts[1:] != [p['day'], str(p['revision'])]:
            return expired
        return apply_action(db, Intent(action=action), '', now, item_by_id(db, p['item_id']))
    if action in ('pick', 'slot') and len(parts) == 3 and parts[2].isdecimal():
        if parts[1] != str(now.date()):
            return expired
        set_pending(db, None)
        return choose(db, now, item_by_id(db, int(parts[2])), action == 'slot')
    if action == 'page' and len(parts) == 3 and all(x.isdecimal() for x in parts[1:]):
        v = db.execute('SELECT * FROM views WHERE id=?', (parts[1],)).fetchone()
        if v:
            return list_page(db, v['scope'], v['target'], int(parts[2]), now, v['id'])
        return expired
    if action == 'match' and len(parts) == 3 and parts[2].isdecimal():
        ctx = pending(db)
        i = int(parts[2])
        if ctx and str(ctx.get('token')) == parts[1] and i in ctx.get('ids', []):
            item = item_by_id(db, i)
            if item and item['status'] == 'open':
                return apply_action(db, Intent.model_validate(ctx['draft']), ctx['original'], now, item)
        return expired
    if len(parts) != 2 or not parts[1].isdecimal():
        return expired
    item = item_by_id(db, int(parts[1]))
    if action == 'item':
        return detail(db, item, now)
    if not item or item['status'] != 'open':
        return expired
    if action in ('editrem', 'editdue'):
        field = 'remind' if action == 'editrem' else 'due'
        prompt = 'چه روز و ساعتی یادت بندازم؟' if field == 'remind' else 'موعد تازه چه روزیه؟'
        if field == 'remind' and item['daily']:
            prompt = 'از این به بعد هر روز چه ساعتی یادت بندازم؟'
        return ask(db, prompt, ('تنظیم یادآوری ' if field == 'remind' else 'تغییر موعد ') + item['title'],
                   Intent(action='reschedule', target=str(item['id']), wants_reminder=field == 'remind'),
                   now, edit_id=item['id'], field=field)
    actions = {'finish': 'done', 'unrem': 'cancel_reminder', 'drop': 'cancel_item'}
    if action in actions:
        return apply_action(db, Intent(action=actions[action]), '', now, item)
    return expired


def interpret(db, text, intent, ctx, now, visible=()):
    if not ctx or not intent.continuation:
        return apply_action(db, intent, text, now, visible=visible)
    if ctx.get('field') == 'effort':
        item = item_by_id(db, ctx['edit_id'])
        if not item or item['status'] != 'open':
            set_pending(db, None)
            return message('این کار دیگه باز نیست.')
        if intent.action in ('cancel_reminder', 'done', 'cancel_item'):
            return apply_action(db, intent, text, now, item)
        estimate = Intent(action='estimate', effort_minutes=intent.effort_minutes or intent.minutes)
        return apply_action(db, estimate, text, now, item)
    old = Intent.model_validate(ctx['draft'])
    if 'token' in ctx:
        selected = [i for i in ctx['ids'] if (r := item_by_id(db, i)) and
                    (matches(r, intent.target or text) or str(i) == norm(text))]
        if len(selected) == 1:
            return callback(db, f'match:{ctx["token"]}:{selected[0]}', now)
        return message('شماره یا اسم موردی که منظورت بود رو بگو.')
    if old.action not in ('create', 'reschedule'):
        return apply_action(db, intent, ctx['original'], now)
    # The earlier turn owns the item and action; the reply supplies missing information.
    values = old.model_dump()
    values.update(question=intent.question, title=old.title or intent.title,
                  daily=old.daily if intent.daily is None else intent.daily,
                  wants_reminder=old.wants_reminder or intent.wants_reminder)
    field = ctx.get('field')
    if field == 'remind_clock':
        field = 'remind'
    for name in ('due', 'remind'):
        if field and name != field:
            continue
        spec = getattr(intent, name)
        if field and spec is None:
            spec = intent.remind or intent.due
        if spec:
            values[name] = spec.model_dump()
    if ctx.get('edit_id'):
        item = item_by_id(db, ctx['edit_id'])
        if not item or item['status'] != 'open':
            set_pending(db, None)
            return message('این مورد دیگر باز نیست.')
    else:
        item = None
    return save_item(db, Intent.model_validate(values), ctx['original'], now, item)
