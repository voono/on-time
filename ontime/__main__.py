import fcntl
import logging
import os
from pathlib import Path
import signal
import threading

from .intent import Router
from .service import Service
from .store import setup
from .transport import Telegram


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    required = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_USER_ID', 'OPENROUTER_API_KEY')
    missing = [k for k in required if not os.environ.get(k, '').strip()]
    if missing:
        raise SystemExit('متغیرهای لازم: ' + ', '.join(missing))
    raw_id = os.environ['TELEGRAM_USER_ID']
    if not raw_id.isascii() or not raw_id.isdecimal() or int(raw_id) <= 0:
        raise SystemExit('TELEGRAM_USER_ID باید شناسهٔ عددی مثبت باشد.')
    owner = int(raw_id)
    path = Path(os.getenv('BOT_DATABASE', 'data/ontime.sqlite3')).expanduser().resolve()
    os.umask(0o077)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('یک نسخه از ربات با همین پایگاه داده در حال اجراست.') from None
        model = os.getenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini')
        setup(path, os.getenv('BOT_TIMEZONE', 'Asia/Tehran'), owner, default_model=model)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        router = Router(os.environ['OPENROUTER_API_KEY'], model)
        Service(path, owner, Telegram(os.environ['TELEGRAM_BOT_TOKEN'], owner), router).run(stop)


if __name__ == '__main__':
    main()
