"""Resolve short replies against the Telegram message the user actually saw."""
import json
import re

from .dates import norm
from .intent import Intent

ORDINALS = ('اول', 'دوم', 'سوم', 'چهارم', 'پنجم', 'ششم', 'هفتم', 'هشتم', 'نهم', 'دهم', 'یازدهم', 'دوازدهم')
COMMANDS = {'سلام': '/start', 'راهنما': '/help', 'تنظیمات': '/settings',
            'وقت های آزادم': '/free', 'کارهای بدون موعدم رو نشون بده': '/tasks',
            'این هفته چه کارهایی دارم': '/week', 'بی خیال': '/cancel', 'بیخیال': '/cancel', 'انصراف': '/cancel'}
STATUS = {'انجام شد': 'done', 'انجامش دادم': 'done', 'تموم شد': 'done',
          'بذار برای بعد': 'later', 'امروز ردش کن': 'skip'}


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
    if value in COMMANDS:
        return COMMANDS[value]
    ids = context.get('ids', [])
    kind = context.get('kind')
    if value in STATUS:
        return Intent(action=STATUS[value], target='@' if ids else '')
    if value in ('همون ساعت خوبه', 'همین ساعت خوبه') and kind == 'morning':
        return Intent(action='accept_slot', target=str(ids[0]))
    if kind == 'list' and context.get('view'):
        if value in ('بعدی', 'صفحه بعد') and context['more']:
            return f"page:{context['view']}:{context['page']+1}"
        if value in ('قبلی', 'صفحه قبل') and context['page']:
            return f"page:{context['view']}:{context['page']-1}"
    if not ids or (draft and kind != 'match'):
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
