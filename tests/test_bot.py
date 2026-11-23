import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ontime.dates import solar_date, solar_parts
from ontime.intent import Intent
from ontime.service import Service
from ontime.store import connect, item_by_id, pending, put, setup
from ontime.transport import DeliveryError

ZONE = ZoneInfo('Asia/Tehran')
OWNER = 41


def when(day, at=None, calendar='jalali'):
    return dict(day=day, calendar=calendar, at=at)


class FakeRouter:
    def __init__(self):
        self.answers = {}
        self.calls = []

    def parse(self, text, now, ctx, candidates):
        self.calls.append((text, ctx))
        return Intent.model_validate(self.answers[text])


class FakeTelegram:
    def __init__(self):
        self.sent, self.failures, self.acks = [], [], []

    def send(self, payload):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(payload)
        return {'message_id': len(self.sent)}

    def ack(self, _, text=''):
        self.acks.append(text)

    def mark_checkin(self, message_id, markup):
        self.marked_message_id = message_id
        self.selected = markup


class BotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.db'
        self.now = datetime(2026, 11, 21, 7, 0, tzinfo=ZONE)  # Saturday
        self.router, self.tg = FakeRouter(), FakeTelegram()
        self.uid = 0
        self.restart()
        self.addCleanup(lambda: self.db.close())

    def restart(self):
        if hasattr(self, 'db'):
            self.db.close()
        setup(self.path, ZONE.key, OWNER)
        self.db = connect(self.path)
        self.bot = Service(self.path, OWNER, self.tg, self.router, lambda: self.now)

    def send(self, text, result=None, *, button=False, actor=OWNER, chat='private', drain=True, reply_to=None):
        if result:
            self.router.answers[text] = result
        self.uid += 1
        msg = {'from': {'id': actor}, 'chat': {'id': actor, 'type': chat}, 'text': text}
        if reply_to is not None:
            msg['reply_to_message'] = {'message_id': reply_to}
        body = {'callback_query': {'id': str(self.uid), 'from': msg['from'], 'message': msg, 'data': text}} if button else {'message': msg}
        self.last_update = {'update_id': self.uid, **body}
        self.bot.handle(self.db, self.last_update)
        if drain:
            self.drain()
        return self.tg.sent[-1]['text'] if self.tg.sent else ''

    def drain(self):
        for _ in range(100):
            if not self.bot.tick(self.db):
                return
        self.fail('delivery loop failed to quiesce')

    def task(self, title, **fields):
        self.send(title, dict(action='create', kind='task', title=title, **fields))
        return self.db.execute('SELECT MAX(id) FROM items').fetchone()[0]

    def at(self, h, m=0):
        self.now = self.now.replace(hour=h, minute=m)
        self.drain()

    def pick(self, i, slot=False):
        return self.send(f'{"slot" if slot else "pick"}:{self.now.date()}:{i}', button=True)

    def follows(self):
        return [m for m in self.tg.sent if m['text'].startswith('فرصت شد')]

    def test_inbox_and_notes_read_from_database(self):
        self.task('زنگ به دندون‌پزشک')
        self.send('ایدهٔ سفر به شیراز', dict(action='create', kind='note', title='ایدهٔ سفر به شیراز'))
        self.restart()
        tasks = self.send('کارهای بدون موعدم رو نشون بده')
        self.assertIn('دندون', tasks)
        self.assertNotIn('شیراز', tasks)
        self.assertIn('شیراز', self.send('/notes'))
        self.assertIsNone(item_by_id(self.db, 1)['due_day'])
        self.assertIsNone(item_by_id(self.db, 2)['remind_at'])
        self.assertIsNone(pending(self.db))

    def test_clarification_after_restart_and_independent_reminder(self):
        self.send('فردا قبض رو یادم بنداز', dict(action='create', kind='reminder', title='پرداخت قبض',
                  wants_reminder=True, remind=when('+1')))
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM items').fetchone()[0])
        self.assertIn('ساعت', self.tg.sent[-1]['text'])
        self.restart()
        self.send('ساعت ۹ صبح', dict(action='create', continuation=True, remind=when('+1','09:00')))
        self.assertEqual('فردا قبض رو یادم بنداز', self.router.calls[-1][1]['original'])
        self.assertIsNone(item_by_id(self.db, 1)['due_day'])
        self.assertEqual('پرداخت قبض', item_by_id(self.db, 1)['title'])
        self.now += timedelta(days=1, hours=2)
        self.restart()
        self.router.answers.clear()
        self.drain()
        self.restart()
        self.drain()
        self.assertEqual(1, sum(m['text'] == '⏰ پرداخت قبض' for m in self.tg.sent))

    def test_morning_empty_and_no_unscheduled_delivery(self):
        self.at(8, 29)
        self.assertFalse(self.tg.sent)
        self.at(8, 30)
        self.assertEqual([{'text':'صبح بخیر 🌤'}], self.tg.sent)
        self.restart()
        self.drain()
        self.assertEqual(1, len(self.tg.sent))

    def test_no_free_time_single_optional_and_no_notes(self):
        self.task('خرید')
        self.task('تماس', important=True)
        self.send('ایده', dict(action='create', kind='note', title='ایده'))
        self.at(8, 30)
        m = self.tg.sent[-1]
        self.assertIn('تماس', m['text'])
        self.assertNotIn('خرید', m['text'])
        self.assertNotIn('ایده', m['text'])
        self.assertEqual(1, len(m['reply_markup']['inline_keyboard'][0]))
        self.assertNotIn('زمان', m['text'])

    def test_windows_conflicts_and_accepted_slot(self):
        for name in ['نامه', 'خرید', 'تمیزکاری', 'تمرین']:
            self.task(name)
        i = self.task('تماس', minutes=20, important=True)
        self.send('شنبه تا چهارشنبه ۵ تا ۶ عصر وقتم آزاده', dict(action='availability', weekdays=[5,6,0,1,2],
                  windows=[{'start':'17:00','end':'18:00'}]))
        self.send('امروز ۳ تا ۴ آزادم', dict(action='availability', due=when('0'),
                  windows=[{'start':'15:00','end':'16:00'}]))
        self.send('جلسه', dict(action='create', kind='event', title='جلسه', due=when('0','15:00'), minutes=30))
        self.at(8, 30)
        m = self.tg.sent[-1]
        self.assertIn('جلسه', m['text'])
        self.assertIn('۱۵:۳۰ تا ۱۵:۵۰', m['text'])
        self.assertEqual(1, len(m['reply_markup']['inline_keyboard']))
        self.assertIn('۳.', m['text'])
        self.pick(i, slot=True)
        self.assertIsNone(item_by_id(self.db, i)['due_day'])
        self.at(15, 49)
        self.assertFalse(self.follows())
        self.at(15, 50)
        self.assertEqual(1, len(self.follows()))
        self.restart()
        self.at(19)
        self.assertEqual(1, len(self.follows()))
        self.assertEqual('open', item_by_id(self.db, i)['status'])

    def test_replacement_and_daily_cap(self):
        a, b = self.task('تماس'), self.task('مطالعه')
        self.send('امروز سعی می‌کنم تماس بگیرم', dict(action='select', target='تماس'))
        old = f'done:{self.now.date()}:1'  # A callback from a previously displayed plan.
        self.pick(b)
        self.send(old, button=True)
        self.assertEqual('open', item_by_id(self.db, a)['status'])
        self.assertIn('مطالعه', self.send('/today'))
        self.at(19)
        self.assertEqual(1, len(self.follows()))
        self.assertIn('مطالعه', self.follows()[0]['text'])
        self.send('/followup 21:00')
        self.pick(a)
        self.at(21)
        self.assertEqual(1, len(self.follows()))

    def test_done_before_follow_and_late_pick_and_skip(self):
        a, b = self.task('الف'), self.task('ب')
        self.pick(a)
        self.send('انجام شد')
        self.at(19, 5)
        self.assertFalse(self.follows())
        self.pick(b)
        self.at(21)
        self.assertFalse(self.follows())
        self.send('امروز ردش کن')
        self.assertIn('ب', self.send('/tasks'))
        self.assertEqual('done', item_by_id(self.db, a)['status'])

    def test_defer_then_set_new_explicit_reminder(self):
        i = self.task('تماس')
        self.pick(i)
        self.send('بذار برای بعد')
        self.send('فردا ۱۰', dict(action='reschedule', continuation=True, remind=when('+1','10:00')))
        self.assertIsNotNone(item_by_id(self.db, i)['remind_at'])
        self.assertIsNone(item_by_id(self.db, i)['due_day'])
        self.at(19)
        self.assertFalse(self.follows())

    def test_transient_send_retries_after_restart(self):
        self.send('دارو', dict(action='create', title='دارو', kind='reminder', remind=when('0','07:30')))
        self.tg.failures = [DeliveryError('pending', 5)]
        self.at(7,30)
        self.restart()
        self.now += timedelta(seconds=5)
        self.drain()
        self.drain()
        self.assertEqual(1, sum(m['text']=='⏰ دارو' for m in self.tg.sent))

    def test_privacy_and_duplicate_update(self):
        self.send('secret', actor=999)
        self.send('secret', chat='group')
        self.assertFalse(self.router.calls or self.tg.sent)
        self.task('خرید')
        self.restart()
        self.bot.handle(self.db, self.last_update)
        self.drain()
        self.assertEqual(1, len(self.router.calls))
        self.assertEqual(1, len(self.tg.sent))

    def test_annual_jalali_leap_and_queries(self):
        self.send('تولد مریم', dict(action='create', title='تولد مریم', kind='event', annual=True,
                  due=when('1403-12-30')))
        answer = self.send('تولد مریم کیه؟', dict(action='list', scope='events', target='مریم'))
        self.assertIn('۳۰ اسفند ۱۴۰۸', answer)
        self.assertIn('پیدا نکردم', self.send('تولد سارا کیه؟', dict(action='list', scope='events', target='سارا')))
        for y,m,d,g in [(1403,1,1,date(2024,3,20)),(1404,1,1,date(2025,3,21)),(1405,8,30,date(2026,11,21))]:
            self.assertEqual(solar_date(y,m,d),g)
            self.assertEqual(solar_parts(g), (y,m,d))
        with self.assertRaises(ValueError):
            solar_date(1404,12,30)

    def test_edit_due_and_cancel_delivery(self):
        i = self.task('تحویل پروژه', due=when('+1','16:00'), wants_reminder=True, remind=when('0','17:00'))
        original = item_by_id(self.db, i)
        self.assertNotEqual(original['created_at'], original['remind_at'])
        self.send(f'editdue:{i}',button=True)
        self.send('پس‌فردا',dict(action='reschedule', continuation=True, due=when('+2')))
        self.assertEqual(original['remind_at'], item_by_id(self.db,i)['remind_at'])
        self.send(f'unrem:{i}',button=True)
        self.at(17)
        self.assertFalse(any(m['text'].startswith('⏰') for m in self.tg.sent))
        self.assertIn('تحویل پروژه', self.send('این هفته چه کارهایی دارم؟'))

    def test_morning_switch_near_overdue_and_rotating_suggestions(self):
        for name in ['کار یک', 'کار دو', 'کار سه', 'کار چهار']:
            self.task(name)
        self.task('ضروری', important=True, due=when('-1'))
        self.send('دیدار',dict(action='create',kind='event',title='دیدار',due=when('+3')))
        self.send('/near 2')
        self.send('/morning off')
        self.at(8,30)
        self.assertFalse(any(m['text'].startswith('صبح بخیر') for m in self.tg.sent))
        self.send('/morning 09:00')
        self.at(9)
        self.assertIn('ضروری', self.tg.sent[-1]['text'])
        self.assertNotIn('دیدار', self.tg.sent[-1]['text'])
        self.assertIn('کار یک', self.tg.sent[-1]['text'])
        self.now += timedelta(days=1)
        self.drain()
        self.assertIn('دیدار', self.tg.sent[-1]['text'])
        self.assertIn('کار دو', self.tg.sent[-1]['text'])

    def test_new_conflict_invalidates_old_offer(self):
        i = self.task('مطالعه')
        self.send('وقت آزاد', dict(action='availability', weekdays=[5], windows=[{'start':'12:00','end':'13:00'}]))
        self.at(8,30)
        self.assertIn('فرصتی برای شروع', self.tg.sent[-1]['text'])
        self.send('جلسه', dict(action='create',kind='event',title='جلسه',due=when('0','11:30'),minutes=90))
        self.assertIn('دیگر آزاد نیست', self.pick(i,slot=True))
        self.assertIsNone(self.db.execute('SELECT * FROM plans').fetchone())

    def test_clear_daily_override_and_all_day_busy(self):
        self.task('تماس')
        self.send('بازه', dict(action='availability', weekdays=[5,6],windows=[{'start':'12:00','end':'14:00'}]))
        self.send('امروز وقت ندارم', dict(action='availability', due=when('0'),windows=[]))
        self.at(8,30)
        self.assertNotIn('فرصتی برای شروع',self.tg.sent[-1]['text'])
        self.send('سفر', dict(action='create',kind='event',title='سفر',due=when('+1')))
        self.now += timedelta(days=1)
        self.drain()
        self.assertNotIn('فرصتی برای شروع',self.tg.sent[-1]['text'])

    def test_annual_reminder_keeps_solar_month_day(self):
        self.send('نوروز', dict(action='create',kind='event',annual=True,title='نوروز',
                  due=when('1406-01-01'),remind=when('1405-12-29','10:00')))
        original = item_by_id(self.db,1)
        self.now = datetime.fromtimestamp(original['remind_at'], ZONE)
        self.drain()
        after = item_by_id(self.db,1)
        self.assertGreater(after['remind_at'], original['remind_at'])
        self.assertEqual(solar_parts(datetime.fromtimestamp(after['remind_at'],ZONE).date()), (1406,12,29))
        self.assertEqual(after['calendar'],'jalali')

    def test_ambiguous_transport_result_never_duplicates_daily_follow(self):
        self.pick(self.task('تماس'))
        self.tg.failures.append(DeliveryError('uncertain'))
        self.at(19)
        self.restart()
        self.send('/followup 20:00')
        self.pick(self.task('خرید'))
        self.at(20)
        self.assertFalse(self.follows())
        self.assertEqual('uncertain',self.db.execute("SELECT state FROM jobs WHERE kind='follow'").fetchone()[0])

    def test_pagination_and_disambiguation(self):
        for x in range(8):
            self.task(f'خرید {x}')
        self.send('/tasks')
        self.assertIn('بعدی',str(self.tg.sent[-1]['reply_markup']))
        self.send('page:1:1',button=True)
        self.assertIn('خرید 0', self.tg.sent[-1]['text'])
        self.send('خرید انجام شد',dict(action='done',target='خرید'))
        self.assertNotIn('reply_markup', self.tg.sent[-1])
        data = f"match:{pending(self.db)['token']}:{pending(self.db)['ids'][0]}"
        self.send('اولی')
        self.assertEqual(1,self.db.execute("SELECT COUNT(*) FROM items WHERE status='done'").fetchone()[0])
        self.assertIn('این دکمه', self.send(data,button=True))

    def test_interrupt_in_send_keeps_one_delivery_and_next_year(self):
        self.send('تولد', dict(action='create',kind='event',annual=True,title='تولد',
                  due=when('1405-08-30'),remind=when('1405-08-30','08:00')))
        with self.db:
            self.db.execute("UPDATE jobs SET state='sending' WHERE kind='reminder'")
        self.now = self.now.replace(hour=8)
        self.restart()
        self.drain()
        self.assertFalse(any(m['text'].startswith('⏰') for m in self.tg.sent))
        self.assertEqual('uncertain',self.db.execute("SELECT state FROM jobs WHERE kind='reminder' ORDER BY at").fetchone()[0])
        self.assertGreater(item_by_id(self.db,1)['remind_at'],self.now.timestamp())

    def test_birthday_with_birth_year_and_current_reminder(self):
        self.send('تولد مریم', dict(action='create',kind='event',title='تولد مریم',annual=True,
                  due=when('1380-09-01'),remind=when('1405-08-30','10:00')))
        item = item_by_id(self.db,1)
        self.assertEqual(item['reminder_lead'],1)
        self.at(10)
        next_due = datetime.fromtimestamp(item_by_id(self.db,1)['remind_at'],ZONE)
        self.assertEqual(solar_parts(next_due.date()),(1406,8,30))

    def test_dst_gap_does_not_stop_daily_scheduler(self):
        self.send('/tz America/New_York')
        self.send('/morning 02:30')
        self.now = datetime(2027,3,14,3,30,tzinfo=ZoneInfo('America/New_York'))
        self.drain()
        self.assertEqual(self.tg.sent[-1]['text'],'صبح بخیر 🌤')

    def test_daily_clock_delivery_restart_edit_and_cancel(self):
        confirmation = self.send('هر روز ساعت ۹ شب یادم بنداز قرصم رو بخورم',
                                 dict(action='create', kind='reminder', title='خوردن قرص',
                                      daily=True, remind=when('0', '21:00')))
        self.assertIn('هر روز ساعت ۲۱:۰۰', confirmation)
        self.assertIn('هر روز', self.send('/list'))
        self.at(20, 59)
        alarm = '⏰ خوردن قرص'
        self.assertNotIn(alarm, [m['text'] for m in self.tg.sent])
        self.at(21)
        self.restart()
        self.drain()
        self.now += timedelta(days=1)
        self.drain()
        self.assertEqual([m['text'] for m in self.tg.sent].count(alarm), 2)
        self.send('editrem:1', button=True)
        self.send('از فردا ساعت ۸ صبح', dict(action='reschedule', continuation=True, remind=when('+1', '08:00')))
        self.now = (self.now + timedelta(days=1)).replace(hour=8)
        self.drain()
        self.assertEqual([m['text'] for m in self.tg.sent].count(alarm), 3)
        self.assertEqual(item_by_id(self.db, 1)['remind_at'], (self.now + timedelta(days=1)).timestamp())
        self.send('unrem:1', button=True)
        self.restart()
        self.now += timedelta(days=1)
        self.drain()
        self.assertEqual([m['text'] for m in self.tg.sent].count(alarm), 3)
        self.assertIsNone(item_by_id(self.db, 1)['remind_at'])

    def test_daily_clarification_after_clock_and_missed_days(self):
        question = self.send('هر روز یادم بنداز تمرین کنم',
                             dict(action='create', kind='reminder', title='تمرین', daily=True))
        self.assertIn('هر روز چه ساعتی', question)
        self.restart()
        self.send('شش صبح', dict(action='create', continuation=True, remind=when('0', '06:00')))
        tomorrow = (self.now + timedelta(days=1)).replace(hour=6)
        self.assertEqual(item_by_id(self.db, 1)['remind_at'], tomorrow.timestamp())
        self.now = tomorrow + timedelta(days=4, minutes=-1)
        self.restart()
        self.drain()
        self.assertNotIn('⏰ تمرین', [m['text'] for m in self.tg.sent])
        self.now += timedelta(minutes=1)
        self.drain()
        self.assertEqual([m['text'] for m in self.tg.sent].count('⏰ تمرین'), 1)

    def test_daily_explicit_start_and_dst(self):
        self.send('/tz America/New_York')
        zone = ZoneInfo('America/New_York')
        self.now = datetime(2027, 3, 12, 7, 0, tzinfo=zone)
        self.send('از فردا هر صبح ساعت ۹ پیاده‌روی', dict(action='create', kind='reminder',
                  title='پیاده‌روی', daily=True, remind=when('+1', '09:00')))
        self.at(9)
        self.assertFalse(any(m['text'].startswith('⏰') for m in self.tg.sent))
        self.now += timedelta(days=1)
        self.drain()
        next_day = datetime.fromtimestamp(item_by_id(self.db, 1)['remind_at'], zone)
        self.assertEqual(next_day, self.now + timedelta(days=1))
        self.assertEqual(next_day.timestamp() - self.now.timestamp(), 23 * 3600)

    def test_upgrade_preserves_one_time_reminder(self):
        self.send('فقط امروز قرص', dict(action='create', kind='reminder', title='قرص',
                                      remind=when('0', '09:00')))
        with self.db:
            self.db.execute('ALTER TABLE items DROP COLUMN daily')
        self.restart()
        self.assertEqual(item_by_id(self.db, 1)['daily'], 0)
        self.at(9)
        self.assertEqual([m['text'] for m in self.tg.sent].count('⏰ قرص'), 1)
        self.assertIsNone(item_by_id(self.db, 1)['remind_at'])

    def test_text_list_choice_and_completion_without_model(self):
        first = self.task('نوشتن نامه')
        self.task('خرید شیر')
        self.send('/tasks')
        self.assertIn('۱. خرید شیر', self.tg.sent[-1]['text'])
        self.assertNotIn('reply_markup', self.tg.sent[-1])
        self.restart()
        self.assertIn('نوشتن نامه', self.send('دومی رو برای امروز بذار'))
        self.assertNotIn('reply_markup', self.tg.sent[-1])
        self.send('انجامش دادم')
        self.assertEqual(item_by_id(self.db, first)['status'], 'done')
        self.assertEqual(len(self.router.calls), 2)

    def test_edit_quoted_reminder_and_continue_in_text(self):
        self.send('دارو', dict(action='create', kind='reminder', title='قرص', daily=True,
                              remind=when('0','21:00')))
        confirmation = len(self.tg.sent)
        self.task('مطالعه')
        self.restart()
        self.send('ساعتش رو بکن ده شب', dict(action='reschedule', target='@',
                  remind=when('0','22:00')), reply_to=confirmation)
        self.assertEqual(item_by_id(self.db, 1)['reminder_clock'], '22:00')
        self.assertIsNone(item_by_id(self.db, 2)['remind_at'])
        self.send('یادآوریش رو لغو کن', dict(action='cancel_reminder', target='@'))
        self.assertIsNone(item_by_id(self.db, 1)['remind_at'])

    def test_chat_accepts_time_and_rejects_stale_plan_reply(self):
        a = self.task('مطالعه')
        self.task('نامه')
        self.send('وقت آزاد', dict(action='availability', weekdays=[5],
                                      windows=[{'start':'12:00','end':'13:00'}]))
        self.at(8, 30)
        self.send('همون ساعت خوبه')
        self.assertIn('مطالعه', self.tg.sent[-1]['text'])
        self.assertIsNotNone(self.db.execute('SELECT end_at FROM plans').fetchone()[0])
        previous_plan = len(self.tg.sent)
        self.pick(2)
        self.send('انجامش دادم', reply_to=previous_plan)
        self.assertEqual(item_by_id(self.db, a)['status'], 'open')
        self.assertEqual(item_by_id(self.db, 2)['status'], 'open')

    def test_ambiguous_pronoun_then_number_and_settings(self):
        self.task('خرید شیر')
        self.task('خرید نان')
        self.send('/tasks')
        self.send('لغوش کن', dict(action='cancel_item', target='@'))
        self.assertIn('کدوم',self.tg.sent[-1]['text'])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items WHERE status='open'").fetchone()[0], 2)
        self.send('شماره ۲')
        self.assertEqual(item_by_id(self.db, 1)['status'], 'cancelled')
        self.assertIn('۰۹:۰۰', self.send('صبح‌بخیر رو ساعت ۹ بفرست',
                      dict(action='setting', setting='morning', value='09:00')))
        self.assertIn('۰۹:۰۰', self.send('تنظیمات'))

    def test_task_button_lists_dated_and_undated_without_router(self):
        self.task('تحویل گزارش', due=when('+4'))
        self.task('تماس')
        self.send('یادداشت سفر', dict(action='create', kind='note', title='یادداشت سفر'))
        self.send('/start')
        self.assertEqual(self.tg.sent[-1]['reply_markup']['keyboard'],
                         [[{'text': 'همهٔ کارها'}, {'text': 'کارهای امروز'}]])
        calls = len(self.router.calls)
        listing = self.send('همهٔ کارها')
        self.assertIn('تحویل گزارش', listing)
        self.assertIn('تماس', listing)
        self.assertNotIn('یادداشت سفر', listing)
        self.assertIn('تحویل گزارش', self.send('اولی'))
        self.assertEqual(len(self.router.calls), calls)

    def test_today_button_refreshes_old_keyboard_and_lists_only_todays_open_tasks(self):
        due_today = self.task('تحویل امروز', due=when('0', '17:00'))
        picked = self.task('تماس انتخاب‌شده')
        self.task('تمرین فردا', due=when('+1'))
        self.task('خرید بی‌موعد')
        self.send('دیدار خانوادگی', dict(action='create', kind='event',
                                        title='دیدار خانوادگی', due=when('0')))
        self.pick(picked)
        with self.db:
            put(self.db, 'tasks_keyboard', '1')
        self.restart()
        calls = len(self.router.calls)
        listing = self.send('کارهای امروز')
        self.assertIn('تحویل امروز', listing)
        self.assertIn('تماس انتخاب‌شده', listing)
        for title in ('تمرین فردا', 'خرید بی‌موعد', 'دیدار خانوادگی'):
            self.assertNotIn(title, listing)
        self.assertEqual(self.tg.sent[-1]['reply_markup']['keyboard'],
                         [[{'text': 'همهٔ کارها'}, {'text': 'کارهای امروز'}]])
        self.send(f'finish:{due_today}', button=True)
        self.send(f'finish:{picked}', button=True)
        self.assertEqual(self.send('کارهای امروز'), 'فعلاً کاری برای امروز نداری.')
        self.assertEqual(len(self.router.calls), calls)

    def test_two_days_before_deadline_repeats_until_done(self):
        text = 'تا قبل چهارشنبه باید گزارش رو تحویل بدم'
        prompt = self.send(text, dict(action='create', kind='task', title='گزارش', due=when('+4','00:00')))
        self.assertIn('چقدر زمان می‌بره', prompt)
        self.assertEqual(item_by_id(self.db,1)['due_day'], str(self.now.date() + timedelta(days=4)))
        self.restart()
        self.assertIn('روزی یک‌بار', self.send('۲ روز'))
        self.assertEqual(len(self.router.calls),1)
        self.now += timedelta(days=2, hours=1, minutes=29)
        self.drain()
        alerts = lambda: [m for m in self.tg.sent if m['text'].startswith('یادآوری کار «')]
        self.assertFalse(alerts())
        self.now += timedelta(minutes=1)
        self.drain()
        self.assertEqual(len(alerts()),1)
        self.restart()
        self.drain()
        self.assertEqual(len(alerts()),1)
        self.now += timedelta(days=1)
        self.drain()
        self.assertEqual(len(alerts()),2)
        self.send('انجام شد')
        self.assertEqual(item_by_id(self.db,1)['status'],'done')
        self.now += timedelta(days=2)
        self.drain()
        self.assertEqual(len(alerts()),2)

    def test_deadline_change_retry_and_expiry(self):
        self.send('/morning off')
        self.task('فرم', due=when('+3','14:00'), effort_minutes=2880)
        self.send('تا پنجشنبه وقت دارم', dict(action='reschedule',target='فرم',due=when('+5','14:00')))
        self.now += timedelta(days=2, hours=2)
        self.drain()
        self.assertFalse(any(m['text'].startswith('یادآوری کار «') for m in self.tg.sent))
        self.now += timedelta(days=2)
        self.restart()
        self.tg.failures.append(DeliveryError('pending',5))
        self.drain()
        self.now += timedelta(seconds=5)
        self.drain()
        self.assertEqual(sum(m['text'].startswith('یادآوری کار «') for m in self.tg.sent),1)
        self.now += timedelta(days=1, hours=6)
        self.drain()
        self.assertEqual(sum(m['text'].startswith('یادآوری کار «') for m in self.tg.sent),1)
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs WHERE kind='deadline' AND state='pending'").fetchone())

    def test_skip_effort_and_cancel_without_altering_deadline(self):
        i = self.task('کار اول',due=when('+5'))
        self.send('فعلا نه')
        self.assertIsNone(pending(self.db))
        self.assertEqual(item_by_id(self.db,i)['status'],'open')
        i = self.task('کار دوم',due=when('+5'),remind=when('+1','14:00'))
        self.send('دو روز')
        self.assertIsNotNone(item_by_id(self.db,i)['remind_at'])
        self.send('یادآوریش رو لغو کن', dict(action='cancel_reminder',target=str(i)))
        self.assertIsNone(item_by_id(self.db,i)['remind_at'])
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs WHERE kind='deadline' AND state='pending'").fetchone())
        self.assertIsNotNone(item_by_id(self.db,i)['due_day'])

    def test_finish_early_stops_preparation_and_preserves_other_tasks(self):
        self.task('گزارش', due=when('+4'), effort_minutes=2880)
        self.task('تمرین', due=when('+4'), effort_minutes=2880)
        self.now += timedelta(days=2, hours=2)
        self.drain()
        self.send('گزارش رو انجام دادم', dict(action='done', target='گزارش'))
        self.restart()
        self.now += timedelta(days=1)
        self.drain()
        self.now += timedelta(days=1)
        self.drain()
        alerts = [m['text'] for m in self.tg.sent if m['text'].startswith('یادآوری کار «')]
        self.assertEqual(sum('«گزارش»' in text for text in alerts), 1)
        self.assertEqual(sum('«تمرین»' in text for text in alerts), 3)
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs WHERE kind='deadline' AND state='pending'").fetchone())

    def test_daily_checkins_are_per_occurrence_and_offline(self):
        self.send('قرص هر روز', dict(action='create', kind='reminder', daily=True,
                  title='قرص', remind=when('0', '10:00')))
        self.at(10)
        first = self.tg.sent[-1]['reply_markup']['inline_keyboard'][0]
        self.assertEqual([b['text'] for b in first], ['انجام دادم', 'انجام ندادم'])
        before = len(self.tg.sent)
        self.send(first[1]['callback_data'], button=True)
        self.send(first[1]['callback_data'], button=True)
        self.assertEqual(len(self.tg.sent), before)
        self.assertIn('قبلاً', self.tg.acks[-1])
        self.restart()
        self.now += timedelta(days=1)
        self.drain()
        alarm = self.tg.sent[-1]
        self.assertIn('برای نوبت دیروز', alarm['text'])
        second = alarm['reply_markup']['inline_keyboard'][0]
        self.assertNotEqual(first[0]['callback_data'],second[0]['callback_data'])
        self.send(second[0]['callback_data'],button=True)
        self.send(first[0]['callback_data'],button=True)
        self.assertEqual(item_by_id(self.db,1)['status'],'open')
        self.assertGreater(item_by_id(self.db,1)['remind_at'],self.now.timestamp())
        self.assertEqual(self.send('سابقه یادآوری‌ها').count('انجام دادم'),2)
        self.assertEqual(len(self.router.calls),1)

    def test_unanswered_is_unknown_and_hints_have_cooldown(self):
        self.send('تمرین', dict(action='create',kind='reminder',title='تمرین',daily=True,remind=when('0','11:00')))
        self.at(11)
        self.now += timedelta(days=1)
        self.drain()
        self.assertEqual(self.tg.sent[-1]['text'],'⏰ تمرین')
        self.send('انجام ندادم')
        self.assertIn('بی‌پاسخ',self.send('/history'))
        self.now += timedelta(days=1)
        self.drain()
        self.assertIn('دیروز',self.tg.sent[-1]['text'])
        self.send('انجام ندادم')
        self.now += timedelta(days=1)
        self.drain()
        self.assertEqual(self.tg.sent[-1]['text'],'⏰ تمرین')

    def test_task_misses_give_one_morning_hint_and_done_stops_deadline(self):
        self.task('گزارش',due=when('+4'),effort_minutes=5760)
        self.at(8,30)
        for _ in range(2):
            btn = self.tg.sent[-1]['reply_markup']['inline_keyboard'][0][1]
            self.send(btn['callback_data'],button=True)
            self.now += timedelta(days=1)
            self.drain()
        hints = [m['text'] for m in self.tg.sent if m['text'].startswith('صبح بخیر') and 'قدم اول' in m['text']]
        self.assertEqual(len(hints),1)
        self.assertIn('گزارش', hints[0])
        self.assertIn('۲ روز', hints[0])
        btn = self.tg.sent[-1]['reply_markup']['inline_keyboard'][0][0]
        self.send(btn['callback_data'],button=True)
        self.assertEqual(item_by_id(self.db,1)['status'],'done')
        self.assertFalse(self.db.execute("SELECT 1 FROM jobs WHERE kind='deadline' AND state='pending'").fetchone())

    def test_checkin_from_replaced_plan_cannot_finish_new_task(self):
        a,b = self.task('نامه'), self.task('خرید')
        self.pick(a)
        self.at(19)
        button = self.follows()[0]['reply_markup']['inline_keyboard'][0][0]
        self.pick(b)
        self.send(button['callback_data'],button=True)
        self.assertIn('قبلی',self.tg.acks[-1])
        self.assertEqual(item_by_id(self.db,a)['status'],'open')
        self.assertEqual(item_by_id(self.db,b)['status'],'open')
        self.assertIsNone(self.db.execute('SELECT outcome FROM checkins').fetchone()[0])

    def test_local_yesterday_query_and_correcting_the_original_message(self):
        self.send('قرص', dict(action='create', kind='reminder', title='خوردن قرص',
                              daily=True, remind=when('0','09:00')))
        self.at(9)
        original = len(self.tg.sent)
        self.send('نخوردم', reply_to=original)
        self.now += timedelta(days=1)
        self.drain()
        self.assertIn('انجام ندادم', self.send('دیروز قرصم رو ثبت کردم؟'))
        self.send('خوردم', reply_to=original)
        self.assertEqual(self.tg.marked_message_id, original)
        self.assertIn('✓ انجام دادم', str(self.tg.selected))
        self.assertIn('انجام دادم', self.send('دیروز قرصم رو خوردم؟'))
        self.assertIn('بی‌پاسخ', self.send('امروز قرصم رو ثبت کردم؟'))
        self.assertNotIn('خوردن قرص', self.send('سابقه باشگاه'))
        self.assertEqual(len(self.router.calls), 1)

    def test_nightly_report_counts_recorded_work_and_answers_without_model(self):
        self.send('/morning off')
        early = self.task('ارسال فرم', due=when('0', '12:00'))
        self.send(f'finish:{early}', button=True)
        self.task('تحویل گزارش', due=when('0', '12:00'))
        self.task('تماس')
        self.send('تمرین', dict(action='create', kind='reminder', daily=True, title='تمرین',
                              remind=when('0', '13:00')))
        self.send('مطالعه', dict(action='create', kind='reminder', daily=True, title='مطالعه',
                               remind=when('0', '14:00')))
        self.at(13)
        self.send('انجام ندادم')
        self.at(14)
        self.send('گزارش انجام شد', dict(action='done', target='گزارش'))
        self.send('تماس انجام شد', dict(action='done', target='تماس'))
        self.task('فرستادن پروژه', due=when('+1'))
        calls = len(self.router.calls)
        self.at(22, 29)
        self.assertFalse(any(m['text'].startswith('🌙 جمع‌بندی') for m in self.tg.sent))
        self.at(22, 30)
        report = self.tg.sent[-1]['text']
        self.assertIn('۳ کار انجام شد', report)
        self.assertIn('۱ از ۲ کار موعددار', report)
        self.assertIn('هفت روز اخیر', report)
        self.assertIn('۱ انجام نشد، ۱ بی‌پاسخ', report)
        self.assertIn('فرستادن پروژه', report)
        self.assertEqual(report, self.send('گزارش امروز'))
        self.assertEqual(len(self.router.calls), calls)

    def test_nightly_schedule_uses_local_clock_and_persists_after_restart(self):
        self.now = datetime(2026, 11, 21, 8, 0, tzinfo=ZoneInfo('America/New_York'))
        self.send('/morning off')
        self.send('/tz America/New_York')
        self.send('/night 21:45')
        self.now = datetime(2026, 11, 21, 21, 44, tzinfo=ZoneInfo('America/New_York'))
        self.drain()
        reports = lambda: [m for m in self.tg.sent if m['text'].startswith('🌙 جمع‌بندی')]
        self.assertEqual(len(reports()), 0)
        self.tg.failures.append(DeliveryError('pending', 5))
        self.now += timedelta(minutes=1)
        self.drain()
        self.restart()
        self.now += timedelta(seconds=5)
        self.drain()
        self.restart()
        self.drain()
        self.assertEqual(len(reports()), 1)
        self.assertIn('هنوز انجام کاری ثبت نشده', reports()[0]['text'])
        self.send('/night off')
        self.now += timedelta(days=1)
        self.drain()
        self.assertEqual(len(reports()), 1)
        self.send('/night on')
        self.assertEqual(len(reports()), 2)

    def test_completion_from_deadline_notification_is_recorded_once(self):
        i = self.task('پروژه', due=when('+1'), effort_minutes=1440)
        self.at(8, 30)
        reminder = next(m for m in self.tg.sent if m['text'].startswith('یادآوری کار'))
        done = reminder['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.send(done, button=True)
        self.send(done, button=True)
        rows = self.db.execute('SELECT item_id,on_time FROM task_completions').fetchall()
        self.assertEqual([tuple(row) for row in rows], [(i,1)])
        self.now += timedelta(days=1)
        self.drain()
        self.assertIn('امروز هنوز انجام کاری ثبت نشده', self.send('/report'))
