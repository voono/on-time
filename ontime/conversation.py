"""Resolve short replies against the Telegram message the user actually saw."""
import json
import re

from .dates import norm
from .intent import Intent, When

ORDINALS = ('اول', 'دوم', 'سوم', 'چهارم', 'پنجم', 'ششم', 'هفتم', 'هشتم', 'نهم', 'دهم', 'یازدهم', 'دوازدهم')
COMMANDS = {'سابقه یادآوری ها': '/history', 'سابقه یادآوری هام': '/history', 'سابقه': '/history',
            'همهٔ کارها': '/mytasks', 'همه کارها': '/mytasks', 'کارهای من': '/mytasks',
            'لیست کارها': '/mytasks', 'فهرست کارها': '/mytasks',
            'کارهای امروز': '/todaytasks', 'امروز چه کارهایی دارم': '/todaytasks',
            'سلام': '/start', 'راهنما': '/help', 'تنظیمات': '/settings',
            'وقت های آزادم': '/free', 'کارهای بدون موعدم رو نشون بده': '/tasks',
            'این هفته چه کارهایی دارم': '/week', 'بی خیال': '/cancel', 'بیخیال': '/cancel', 'انصراف': '/cancel'}
STATUS = {'انجام شد': 'done', 'انجامش دادم': 'done', 'تموم شد': 'done',
          'بذار برای بعد': 'later', 'امروز ردش کن': 'skip'}


LEADING_REMINDER_DAY = re.compile(
    r'^(پس ?فردا|فردا|امروز)[ ،,:]+(?:(ساعت [^،,]+?) )?یادم (?:بیار|بنداز)\b')
HOURS = ('یک', 'دو', 'سه', 'چهار', 'پنج', 'شش', 'هفت', 'هشت', 'نه', 'ده', 'یازده', 'دوازده')


def anchor_reminder(intent, text, today):
    remind = intent.remind
    if (remind is None and intent.action == 'create' and not intent.continuation
            and (intent.kind == 'reminder' or (intent.kind == 'task' and intent.wants_reminder))
            and not intent.due and not intent.daily and not intent.annual):
        match = LEADING_REMINDER_DAY.match(norm(text))
        if match:
            remind = When(day=match[1], at=reminder_clock_reply(match[2] or ''))
            if remind.at:
                intent = intent.model_copy(update={'question': ''})
    if remind:
        return intent.model_copy(update={'remind': remind.anchored(today)})
    return intent


def reminder_clock_reply(text):
    match = re.fullmatch(r'(?:ساعت )?([0-9]{1,2}):([0-5][0-9])', text)
    if match and int(match[1]) <= 23:
        return f'{int(match[1]):02}:{match[2]}'
    match = re.fullmatch(
        r'(?:ساعت )?([0-9]{1,2}|' + '|'.join(HOURS)
        + r')( و نیم)? (صبح|ظهر|بعد از ظهر|بعدازظهر|عصر|شب)', text)
    if not match:
        return None
    hour = int(match[1]) if match[1].isascii() else HOURS.index(match[1]) + 1
    period = match[3]
    if (not 1 <= hour <= 12 or (period == 'ظهر' and hour != 12)
            or (period == 'صبح' and hour == 12) or (period == 'شب' and 1 <= hour <= 5)):
        return None
    if period == 'شب' and hour == 12:
        hour = 0
    elif period != 'صبح' and hour < 12:
        hour += 12
    minute = 30 if match[2] else 0
    return f'{hour:02}:{minute:02}'


def conversation_context(db, msg):
    quoted = msg.get('reply_to_message')
    if quoted:
        row = db.execute('SELECT context FROM chat_messages WHERE message_id=?',
                         (quoted.get('message_id'),)).fetchone()
        return json.loads(row[0]) if row else {}
    row = db.execute('SELECT context FROM chat_messages WHERE message_id<? ORDER BY message_id DESC LIMIT 1',
                     (msg.get('message_id', 2**62),)).fetchone()
    return json.loads(row[0]) if row else {}


def shortcut(text, context, draft):
    value = norm(text).strip(' ؟?!.')
    if text.startswith('/'):
        return text
    if value in ('گزارش امروز', 'گزارش شبانه', 'جمع بندی امروز', 'جمع بندی امشب'):
        return '/report'
    if value in COMMANDS:
        return COMMANDS[value]
    if value.startswith('سابقه '):
        return '/history ' + value.removeprefix('سابقه ').removeprefix('یادآوری ')
    if text.rstrip().endswith(('؟', '?')):
        query = re.fullmatch(r'(?:آیا )?(دیروز|امروز) (.+?) (?:رو |را )?(?:خوردم|انجام دادم|ثبت کردم)', value)
        if query:
            return f'/history {query[1]} {query[2]}'
    if draft and draft.get('field') == 'effort':
        if value in ('نمی دونم', 'نمیدونم', 'فعلا نه', 'رد کن'):
            return '/cancel'
        match = re.fullmatch(r'(?:حدود\s*)?(\d{1,6}|یک|یه|دو|سه|چهار|پنج|شش|هفت|نیم)\s*(روز|هفته|ساعت|دقیقه)(?:ه| طول میکشه| طول می کشه)?', value)
        if match:
            qty, unit = match.groups()
            words = {'یک': 1, 'یه': 1, 'دو': 2, 'سه': 3, 'چهار': 4, 'پنج': 5, 'شش': 6, 'هفت': 7, 'نیم': 0.5}
            amount = int(qty) if qty.isdecimal() else words[qty]
            minutes = int(amount * {'روز': 1440, 'هفته': 10080, 'ساعت': 60, 'دقیقه': 1}[unit])
            if 1 <= minutes <= 5256000:
                return Intent(action='estimate', continuation=True, effort_minutes=minutes)
            return 'invalid_effort'
    if draft and draft.get('field') == 'remind_clock':
        at = reminder_clock_reply(value)
        if at:
            remind = {**draft['draft']['remind'], 'at': at}
            return Intent(action=draft['draft']['action'], continuation=True, remind=remind)
    ids = context.get('ids', [])
    kind = context.get('kind')
    if context.get('checkin'):
        if value in ('انجام دادم', 'انجامش دادم', 'انجام شد', 'خوردم', 'تموم شد'):
            return f"check:{context['checkin']}:done"
        if value in ('انجام ندادم', 'انجامش ندادم', 'نخوردم', 'هنوز نه'):
            return f"check:{context['checkin']}:missed"
    if value in STATUS:
        return Intent(action=STATUS[value], target='@' if ids else '')
    if value in ('همون ساعت خوبه', 'همین ساعت خوبه') and kind == 'morning':
        return Intent(action='accept_slot', target=str(ids[0]))
    if kind == 'list' and context.get('view'):
        if value in ('بعدی', 'صفحه بعد') and context['more']:
            return f"page:{context['view']}:{context['page']+1}"
        if value in ('قبلی', 'صفحه قبل') and context['page']:
            return f"page:{context['view']}:{context['page']-1}"
    if not ids or (draft and kind not in ('list', 'match')):
        return None
    action = None
    word = value
    if value.startswith('جزئیات '):
        action, word = 'detail', value.removeprefix('جزئیات ')
    else:
        for suffix, meaning in ((' رو برای امروز بذار', 'select'), (' رو امروز انجام میدم', 'select'),
                                (' انجام شد', 'done'), (' رو انجام دادم', 'done')):
            if value.endswith(suffix):
                action, word = meaning, value.removesuffix(suffix)
                break
    word = re.sub(r'^(?:شماره|گزینه)\s*', '', word).strip()
    if word.isdecimal() and len(word) < 3:
        index = int(word)
    elif word.removesuffix('ی') in ORDINALS:
        index = ORDINALS.index(word.removesuffix('ی')) + 1
    else:
        return None
    if not 1 <= index <= len(ids):
        return 'invalid_position'
    i = ids[index-1]
    if kind == 'match' and action is None:
        return f"match:{context['token']}:{i}"
    return Intent(action=action or ('select' if kind == 'morning' else 'detail'), target=str(i))
