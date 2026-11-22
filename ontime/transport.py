import errno
import json
import socket
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeliveryError(Exception):
    def __init__(self, state, delay=0):
        super().__init__(state)
        self.state, self.delay = state, delay


class Telegram:
    def __init__(self, token, owner):
        self.url = 'https://api.telegram.org/bot' + token + '/'
        self.owner = owner

    def call(self, method, payload, timeout=10):
        req = Request(self.url + method, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=timeout) as res:
                body = json.load(res)
        except HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except (ValueError, OSError):
                body = {}
            code = exc.code
        except URLError as exc:
            known = isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)) or getattr(
                exc.reason, 'errno', None) in (errno.ENETUNREACH, errno.EHOSTUNREACH)
            raise DeliveryError('pending' if known else 'uncertain') from None
        except (OSError, HTTPException, ValueError):
            raise DeliveryError('uncertain') from None
        else:
            if body.get('ok'):
                return body['result']
            code = body.get('error_code', 500)
        if method == 'sendMessage' and code >= 500 and body.get('ok') is not False:
            # A gateway error alone cannot prove Telegram rejected this message.
            raise DeliveryError('uncertain')
        if code == 429 or code >= 500 or code == 408:
            raise DeliveryError('pending', body.get('parameters', {}).get('retry_after', 0))
        raise DeliveryError('failed')

    def updates(self, offset):
        return self.call('getUpdates', {'offset': offset, 'timeout': 15,
                         'allowed_updates': ['message', 'callback_query']}, timeout=23)

    def send(self, payload):
        return self.call('sendMessage', {'chat_id': self.owner, **payload})

    def ack(self, callback_id):
        self.call('answerCallbackQuery', {'callback_query_id': callback_id})
