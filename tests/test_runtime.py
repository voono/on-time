"""Run the production entry point with only remote HTTP responses substituted."""
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from ontime.__main__ import main
from ontime.intent import Intent, Router
from ontime.planner import arm
from ontime.store import connect, put, setup


def response(body):
    return io.BytesIO(json.dumps(body).encode())


class RuntimeTest(unittest.TestCase):
    def test_poll_parse_schedule_and_send_via_production_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bot.db'
            setup(path, 'Asia/Tehran', 42)
            db = connect(path)
            with db:
                put(db, 'enabled', '0')
                put(db, 'night_enabled', '0')
                i = db.execute("""INSERT INTO items(kind,title,details,created_at,calendar,annual,important,short)
                    VALUES ('reminder','قرار قبلی','',0,'jalali',0,0,0)""").lastrowid
                arm(db, i, int(time.time()))
            db.close()
            parsing, alarm, replied = threading.Event(), threading.Event(), threading.Event()
            signals, sent, offsets = {}, [], []
            def telegram(req, timeout):
                data = json.loads(req.data)
                if req.full_url.endswith('/getUpdates'):
                    offsets.append(data['offset'])
                    if len(offsets) == 1:
                        return response({'ok':True,'result':[{'update_id':12,'message':{
                            'from':{'id':42},'chat':{'type':'private','id':42},'text':'ایدهٔ سفر به شیراز'}}]})
                    if not replied.wait(4):
                        raise AssertionError('No confirmation delivered')
                    signals[signal.SIGTERM](None,None)
                    return response({'ok':True,'result':[]})
                self.assertTrue(req.full_url.endswith('/sendMessage'))
                self.assertEqual(data['chat_id'], 42)
                sent.append(data['text'])
                if data['text'].startswith('⏰'):
                    self.assertTrue(parsing.wait(4))
                    alarm.set()
                else:
                    replied.set()
                return response({'ok':True,'result':{'message_id':len(sent)}})

            def router(req, timeout):
                parsing.set()
                # The sender must keep running while the model call is blocked.
                self.assertTrue(alarm.wait(4))
                data = json.loads(req.data)
                self.assertTrue(data['response_format']['json_schema']['strict'])
                self.assertEqual(data['model'], 'test-model')
                result = Intent(action='create', kind='note', title='سفر به شیراز').model_dump_json()
                return response({'choices':[{'message':{'content':result}}]})

            env = {'TELEGRAM_BOT_TOKEN':'test-token', 'TELEGRAM_USER_ID':'42', 'OPENROUTER_API_KEY':'test-key',
                   'BOT_DATABASE':str(path), 'OPENROUTER_MODEL':'test-model', 'BOT_TIMEZONE':'Asia/Tehran'}
            old_mask = os.umask(0o077)
            try:
                with patch.dict(os.environ, env), patch('ontime.intent.urlopen',router), \
                     patch('ontime.transport.urlopen',telegram), \
                     patch('ontime.__main__.signal.signal',side_effect=lambda s,f: signals.update({s:f})):
                    main()
            finally:
                os.umask(old_mask)
            self.assertEqual(offsets, [0,13])
            self.assertEqual(sent, ['⏰ قرار قبلی','یادداشت ثبت شد: سفر به شیراز'])
            db = connect(path)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state='sent'").fetchone()[0],2)
            db.close()

    def test_model_rejects_invalid_shape_and_time(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        api = Router('test','test')
        now = datetime.now(ZoneInfo('Asia/Tehran'))
        for bad in [{'action':'run_sql'}, {'action':'create','unexpected':True},
                    {'action':'create','remind':{'day':'0','at':'25:00'}},
                    {'action':'create','important':'yes'}]:
            body = {'choices':[{'message':{'content':json.dumps(bad)}}]}
            with patch('ontime.intent.urlopen',return_value=response(body)), self.assertRaises(ValidationError):
                api.parse('پیام',now,None)

    def test_telegram_retry_classification(self):
        from urllib.error import HTTPError
        from ontime.transport import DeliveryError, Telegram
        tg = Telegram('test',42)
        for code, body, expected, wait in [
            (429, b'{"ok":false,"parameters":{"retry_after":8}}','pending',8),
            (503, b'{"ok":false,"error_code":503}','pending',0),
            (502, b'bad gateway','uncertain',0),
            (403, b'{"ok":false}','failed',0),
        ]:
            err = HTTPError('https://example.invalid',code,'test',{},io.BytesIO(body))
            with self.subTest(code=code), patch('ontime.transport.urlopen',side_effect=err):
                with self.assertRaises(DeliveryError) as caught:
                    tg.send({'text':'test'})
                self.assertEqual((caught.exception.state,caught.exception.delay),(expected,wait))
