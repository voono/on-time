import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, details TEXT NOT NULL,
 created_at INTEGER NOT NULL, due_day TEXT, due_clock TEXT, calendar TEXT NOT NULL,
 annual INTEGER NOT NULL, important INTEGER NOT NULL, short INTEGER NOT NULL, minutes INTEGER,
 remind_at INTEGER, reminder_lead INTEGER, reminder_clock TEXT, reminder_zone TEXT,
 daily INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS deadline_plans (
 item_id INTEGER PRIMARY KEY REFERENCES items, effort_minutes INTEGER NOT NULL,
 clock TEXT NOT NULL, zone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_completions (
 item_id INTEGER PRIMARY KEY REFERENCES items, completed_at INTEGER NOT NULL,
 on_time INTEGER CHECK(on_time IN (0,1))
);
CREATE INDEX IF NOT EXISTS task_completions_time ON task_completions(completed_at);
CREATE TABLE IF NOT EXISTS availability (key TEXT PRIMARY KEY, windows TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans (
 day TEXT PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items, status TEXT NOT NULL,
 end_at INTEGER, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestions (
 day TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items, start_at INTEGER, end_at INTEGER,
 PRIMARY KEY(day,item_id)
);
CREATE TABLE IF NOT EXISTS jobs (
 key TEXT PRIMARY KEY, kind TEXT NOT NULL, at INTEGER NOT NULL, day TEXT, item_id INTEGER,
 payload TEXT, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 retry_at INTEGER, sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,at);
CREATE INDEX IF NOT EXISTS jobs_sent_at ON jobs(sent_at) WHERE sent_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS checkins (
 id INTEGER PRIMARY KEY, job_key TEXT NOT NULL UNIQUE REFERENCES jobs(key), item_id INTEGER NOT NULL REFERENCES items,
 day TEXT NOT NULL, routine INTEGER NOT NULL, revision INTEGER, message_id INTEGER,
 outcome TEXT CHECK(outcome IN ('done','missed','later')), answered_at INTEGER
);
CREATE INDEX IF NOT EXISTS checkins_item_day ON checkins(item_id,day);
CREATE INDEX IF NOT EXISTS checkins_day ON checkins(day);
CREATE TABLE IF NOT EXISTS checkin_hints (
 item_id INTEGER PRIMARY KEY REFERENCES items, day TEXT NOT NULL, evidence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (message_id INTEGER PRIMARY KEY, context TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS views (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, target TEXT NOT NULL);
"""


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def setup(path, zone, owner):
    ZoneInfo(zone)
    Path(path).parent.mkdir(exist_ok=True, parents=True)
    db = connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(SCHEMA)
    with db:
        if 'daily' not in {r['name'] for r in db.execute('PRAGMA table_info(items)')}:
            db.execute('ALTER TABLE items ADD COLUMN daily INTEGER NOT NULL DEFAULT 0')
        for key, value in {'zone': zone, 'owner': str(owner), 'morning': '08:30', 'enabled': '1',
                           'follow': '19:00', 'near': '7', 'offset': '0',
                           'night': '22:30', 'night_enabled': '1'}.items():
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, value))
        if pref(db, 'owner') != str(owner):
            raise ValueError('شناسهٔ مالک این پایگاه داده با تنظیمات متفاوت است.')
    db.close()


def pref(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))


def message(text, rows=(), *, context=None):
    result = {'text': text}
    if context:
        result['_context'] = context
    if rows:
        result['reply_markup'] = {'inline_keyboard': [
            [{'text': label, 'callback_data': data} for label, data in row] for row in rows]}
    return result


def enqueue(db, key, kind, at, *, day=None, item=None, payload=None):
    db.execute("""INSERT INTO jobs(key,kind,at,day,item_id,payload) VALUES (?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET at=excluded.at, item_id=excluded.item_id,
        payload=excluded.payload, attempts=0, retry_at=NULL, state='pending'
        WHERE jobs.state IN ('pending','cancelled')""",
               (key, kind, at, day, item, json.dumps(payload) if payload else None))


def cancel(db, key):
    db.execute("UPDATE jobs SET state='cancelled' WHERE key=? AND state='pending'", (key,))


def item_by_id(db, i):
    return db.execute('SELECT * FROM items WHERE id=?', (i,)).fetchone()


def pending(db):
    return json.loads(pref(db, 'pending', 'null'))


def set_pending(db, data):
    put(db, 'pending', json.dumps(data, ensure_ascii=False))


def tasks_keyboard():
    return {'keyboard': [[{'text': 'کارهای من'}]], 'resize_keyboard': True, 'is_persistent': True}
