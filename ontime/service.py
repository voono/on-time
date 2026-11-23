import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import deadlines, feedback, nightly
from .conversation import anchor_reminder, conversation_context, shortcut
from .intent import Intent
from .organizer import callback, candidate_titles, interpret, local_command
from .planner import arm, current_plan, morning_due, next_annual, next_daily, reminder_key, scheduled_message
from .store import TASKS_KEYBOARD_VERSION, connect, enqueue, item_by_id, message, pending, pref, put, tasks_keyboard
from .transport import DeliveryError

LOG = logging.getLogger(__name__)


def incoming(update, owner):
    cb = update.get('callback_query')
    msg = cb.get('message', {}) if cb else update.get('message', {})
    actor = (cb or msg).get('from', {}).get('id')
    chat = msg.get('chat', {})
    if chat.get('type') != 'private' or actor != owner or chat.get('id') != owner:
        return None
    return (cb, msg) if cb or msg.get('text') else None


def replied_plan(msg):
    keyboard = msg.get('reply_to_message', {}).get('reply_markup', {}).get('inline_keyboard', [])
    for row in keyboard:
        for btn in row:
            data = btn.get('callback_data', '')
            if data.startswith('done:'):
                return data.split(':')[1:]
    return None


class Service:
    def __init__(self, path, owner, tg, router, utc_now=None):
        self.path, self.owner, self.tg, self.router = path, owner, tg, router
        self.utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.recovered = False

    def local(self, db):
        return self.utc_now().astimezone(ZoneInfo(pref(db, 'zone')))

    def handle(self, db, update):
        uid = update['update_id']
        if uid < int(pref(db, 'offset')):
            return
        parsed = incoming(update, self.owner)
        intent = None
        parse_error = False
        check_button = False
        mark_checkin = None
        if parsed:
            cb, msg = parsed
            text = msg.get('text', '').strip()
            ctx = pending(db)
            focus = conversation_context(db, msg)
            stale_question = False
            if msg.get('reply_to_message'):
                if focus.get('kind') in ('question', 'match'):
                    field = 'token' if focus['kind'] == 'match' else 'question_token'
                    stale_question = not ctx or focus[field] != ctx.get(field)
                else:
                    ctx = None
            route = None if cb else shortcut(text, focus, ctx)
            check_button = bool(cb and cb.get('data', '').startswith('check:'))
            if cb and not check_button:
                try:
                    self.tg.ack(cb['id'])
                except DeliveryError:
                    pass
            elif not cb and route is None and not stale_question:
                try:
                    received = self.local(db)
                    intent = self.router.parse(text, received, ctx, candidate_titles(db, text, received, focus))
                except Exception as exc:
                    LOG.warning('Intent unavailable (%s)', type(exc).__name__)
                    parse_error = True
        with self.lock, db:
            db.execute('BEGIN IMMEDIATE')
            if parsed:
                now = self.local(db)
                db.execute('SAVEPOINT input')
                try:
                    if stale_question and not cb and not text.startswith('/'):
                        out = message('این سؤال بسته شده. بگو الان می‌خوای کدوم کار رو تغییر بدی.')
                    elif parse_error:
                        out = message('الان نتونستم پیام رو بخونم. لطفاً دوباره بفرست. فهرست با /list در دسترسه.', context=focus)
                    elif cb:
                        out = callback(db, cb.get('data', ''), now)
                    elif isinstance(route, str):
                        if route == 'invalid_effort':
                            out = message('مدت رو با یک عدد مثبت بنویس، مثلاً «۲ روز».', context=focus)
                        elif route == 'invalid_position':
                            out = message('شماره‌ای از همین فهرست بگو.', context=focus)
                        elif route.startswith('/'):
                            out = local_command(db, route, now)
                        else:
                            out = callback(db, route, now)
                    else:
                        if isinstance(route, Intent):
                            intent = route
                        else:
                            intent = anchor_reminder(intent, text, received.date())
                        ref = replied_plan(msg)
                        if focus.get('kind') == 'plan':
                            ref = [focus['day'], str(focus['revision'])]
                        p = current_plan(db, now.date())
                        if intent.action == 'checkin':
                            if focus.get('checkin') and intent.value in ('done', 'missed'):
                                out = callback(db, f"check:{focus['checkin']}:{intent.value}", now)
                            else:
                                out = message('روی یادآوری مدنظرت جواب بده تا برای همون نوبت ثبت کنم.')
                        elif ref and intent.action in ('skip', 'later', 'done') and (
                                not p or ref != [p['day'], str(p['revision'])]):
                            out = message('این پاسخ مربوط به انتخاب قبلیه.')
                        elif (focus.get('kind') == 'morning' and focus['day'] != str(now.date())
                              and intent.action in ('select', 'accept_slot')):
                            out = message('این پیشنهاد برای امروز نیست. بگو امروز چه کاری مدنظرت هست.')
                        else:
                            out = interpret(db, text, intent, ctx, now, focus.get('ids', []))
                except (ValueError, OverflowError) as exc:
                    db.execute('ROLLBACK TO input')
                    LOG.info('Input rejected (%s)', type(exc).__name__)
                    hint = str(exc)
                    out = message(hint if hint and len(hint) < 200 and any('\u0600' <= c <= '\u06ff' for c in hint)
                                  else 'این تاریخ یا مقدار معتبر نیست. دوباره بنویس.')
                mark_checkin = out.pop('_mark_checkin', None)
                if not check_button:
                    enqueue(db, f"reply:{pref(db, 'epoch', '0')}:{uid}", 'reply', int(now.timestamp()), payload=out)
                db.execute('RELEASE input')
            put(db, 'offset', str(uid + 1))
            put(db, 'last_update', str(int(self.utc_now().timestamp())))

        if check_button:
            try:
                self.tg.ack(cb['id'], out['text'][:200])
            except DeliveryError:
                pass
        if mark_checkin is not None:
            row = db.execute('SELECT * FROM checkins WHERE id=?', (mark_checkin,)).fetchone()
            message_id = row['message_id'] or (msg.get('message_id') if check_button else None)
            if message_id:
                try:
                    self.tg.mark_checkin(message_id, feedback.keyboard(mark_checkin, row['day'], row['revision'], row['outcome']))
                except DeliveryError:
                    pass

    def tick(self, db):
        with self.lock:
            now = self.local(db)
            stamp = int(now.timestamp())
            with db:
                if not self.recovered:
                    interrupted = db.execute("SELECT * FROM jobs WHERE state='sending'").fetchall()
                    for old in interrupted:
                        LOG.warning('Interrupted delivery %s (uncertain)', old['key'])
                        self.finish(db, old, stamp, 'uncertain')
                    self.recovered = True
                morning_due(db, now)
                nightly.queue_report(db, now)
                job = db.execute("""SELECT * FROM jobs WHERE state='pending' AND at<=?
                    AND (retry_at IS NULL OR retry_at<=?)
                    ORDER BY at, CASE kind WHEN 'morning' THEN 0 WHEN 'night' THEN 2 ELSE 1 END,
                    rowid LIMIT 1""", (stamp,stamp)).fetchone()
                if not job:
                    return False
                payload = scheduled_message(db, job, now)
                if payload and job['kind'] in ('deadline', 'reminder', 'follow'):
                    feedback.prepare(db, job, payload, now)
                elif payload and job['kind'] == 'morning':
                    feedback.morning_hint(db, payload, now)
                db.execute("UPDATE jobs SET state=? WHERE key=?",
                           ('sending' if payload else 'cancelled', job['key']))
            if not payload:
                return True
            try:
                focus = payload.pop('_context', {})
                if (job['kind'] == 'reply' and 'reply_markup' not in payload
                        and pref(db, 'tasks_keyboard') != TASKS_KEYBOARD_VERSION):
                    payload['reply_markup'] = tasks_keyboard()
                sent = self.tg.send(payload)
            except DeliveryError as exc:
                delay = max(exc.delay, min(300, 2 ** min(8, job['attempts'] + 1)))
                with db:
                    if exc.state == 'pending':
                        db.execute("UPDATE jobs SET state='pending',retry_at=?,attempts=attempts+1 WHERE key=?",
                                   (stamp + delay, job['key']))
                    else:
                        self.finish(db, job, stamp, exc.state)
                LOG.warning('Delivery %s (%s)', job['key'], exc.state)
            else:
                with db:
                    if 'checkin' in focus and job['kind'] in ('deadline', 'reminder', 'follow'):
                        db.execute('UPDATE checkins SET message_id=? WHERE id=?', (sent['message_id'], focus['checkin']))
                    if 'hint' in focus:
                        db.execute('INSERT OR REPLACE INTO checkin_hints VALUES (?,?,?)', focus.pop('hint'))
                    if 'keyboard' in payload.get('reply_markup', {}):
                        put(db, 'tasks_keyboard', TASKS_KEYBOARD_VERSION)
                    db.execute('INSERT OR REPLACE INTO chat_messages VALUES (?,?)',
                               (sent['message_id'], json.dumps(focus)))
                    self.finish(db, job, stamp, 'sent')
            return True

    @staticmethod
    def advance_reminder(db, job, stamp):
        item = item_by_id(db, job['item_id'])
        if item and item['remind_at'] is not None and job['key'] == reminder_key(item['id'], item['remind_at']):
            at = None
            if item['daily']:
                zone = ZoneInfo(item['reminder_zone'])
                last_day = datetime.fromtimestamp(item['remind_at'], zone).date()
                at = next_daily(last_day + timedelta(days=1), item['reminder_clock'], zone, stamp)
            elif item['annual'] and item['reminder_zone']:
                at = next_annual(item, stamp)
            arm(db, item['id'], at)

    def finish(self, db, job, stamp, state):
        db.execute('UPDATE jobs SET state=?,sent_at=? WHERE key=?',
                   (state, int(self.utc_now().timestamp()) if state == 'sent' else None, job['key']))
        if job['kind'] == 'deadline':
            deadlines.schedule(db, job['item_id'], self.local(db), after_day=job['day'])
        elif job['kind'] == 'reminder':
            self.advance_reminder(db, job, stamp)

    def run(self, stop):
        def sender():
            db = connect(self.path)
            try:
                while not stop.is_set():
                    if not self.tick(db):
                        stop.wait(0.5)
            except Exception:
                LOG.exception('Scheduler stopped')
                stop.set()
                raise
            finally:
                db.close()
        worker = threading.Thread(target=sender, name='scheduler')
        worker.start()
        db = connect(self.path)
        try:
            while not stop.is_set():
                try:
                    offset = int(pref(db, 'offset'))
                    if offset and self.utc_now().timestamp() - int(pref(db, 'last_update', '0')) >= 6 * 86400:
                        # Telegram may start a new ID sequence after a week of inactivity.
                        with self.lock, db:
                            put(db, 'offset', '0')
                            put(db, 'epoch', str(int(self.utc_now().timestamp())))
                        offset = 0
                    updates = self.tg.updates(offset)
                except DeliveryError as exc:
                    LOG.warning('Polling unavailable (%s)', exc.state)
                    stop.wait(max(exc.delay, 3))
                    continue
                for update in updates:
                    self.handle(db, update)
        finally:
            stop.set()
            worker.join()
            db.close()

