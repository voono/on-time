import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ontime.dates import solar_date, solar_parts
from ontime.intent import Intent
from ontime.service import Service
from ontime.store import connect, item_by_id, pending, setup
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
        self.sent, self.failures = [], []

    def send(self, payload):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(payload)

    def ack(self, _):
        pass


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

    def send(self, text, result=None, *, button=False, actor=OWNER, chat='private', drain=True):
        if result:
            self.router.answers[text] = result
        self.uid += 1
        msg = {'from': {'id': actor}, 'chat': {'id': actor, 'type': chat}, 'text': text}
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
        self.assertEqual(3, len(m['reply_markup']['inline_keyboard']))
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
        old = self.tg.sent[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
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
        data = self.tg.sent[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.send(data,button=True)
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
