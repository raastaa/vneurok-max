import json
import unittest
from unittest.mock import patch
from vneurok.healthcheck import HealthError, check_health


class FakeResponse:
    status = 200
    body = json.dumps({'ok': True, 'demo': False, 'version': 'synthetic'}).encode()
    content_type = 'application/json; charset=utf-8'
    def getheader(self, key, default=''): return self.content_type
    def read(self, maximum): return self.body[:maximum]


class FakeConnection:
    def __init__(self, response): self.response = response; self.requests = []; self.closed = False
    def request(self, method, path, **kwargs): self.requests.append((method, path, kwargs))
    def getresponse(self): return self.response
    def close(self): self.closed = True


class HealthcheckTests(unittest.TestCase):
    def test_only_loopback_correct_host_no_auth_or_proxy(self):
        connection = FakeConnection(FakeResponse())
        with patch('vneurok.healthcheck.http.client.HTTPConnection', return_value=connection) as factory, patch.dict('os.environ', {'HTTP_PROXY': 'http://never-contact.invalid', 'APP_ORIGIN': 'https://synthetic-school.invalid'}):
            self.assertTrue(check_health()['ok'])
        factory.assert_called_once_with('127.0.0.1', 8765, timeout=3)
        method, path, options = connection.requests[0]
        self.assertEqual((method, path), ('GET', '/health'))
        self.assertEqual(options['headers'], {'Host': 'synthetic-school.invalid', 'Accept': 'application/json'})
        self.assertTrue(connection.closed)

    def test_redirect_is_never_followed(self):
        response = FakeResponse(); response.status = 302; connection = FakeConnection(response)
        with patch('vneurok.healthcheck.http.client.HTTPConnection', return_value=connection):
            with self.assertRaises(HealthError): check_health(origin='https://synthetic-school.invalid')
        self.assertEqual(len(connection.requests), 1); self.assertTrue(connection.closed)

    def test_demo_rejected_unless_explicit(self):
        response = FakeResponse(); response.body = b'{"ok":true,"demo":true,"version":"synthetic"}'
        with patch('vneurok.healthcheck.http.client.HTTPConnection', side_effect=lambda *a, **k: FakeConnection(response)):
            with self.assertRaises(HealthError): check_health(origin='http://127.0.0.1:8765')
            self.assertTrue(check_health(origin='http://127.0.0.1:8765', allow_demo=True)['demo'])

    def test_bad_or_large_response_fails(self):
        for body in (b'not JSON', b'{}', b'{"ok":1,"demo":false,"version":"x"}', b'x'*9000):
            response = FakeResponse(); response.body = body
            with patch('vneurok.healthcheck.http.client.HTTPConnection', return_value=FakeConnection(response)):
                with self.assertRaises(HealthError): check_health(origin='https://synthetic-school.invalid')

    def test_invalid_origin_or_port_never_connects(self):
        with patch('vneurok.healthcheck.http.client.HTTPConnection') as factory:
            for origin in ('https://user:secret@synthetic.invalid', 'https://synthetic.invalid/path', 'http://a\r\nX: y', 'https://synthetic.invalid/#secret'):
                with self.assertRaises(HealthError): check_health(origin=origin)
            for port in (0, -1, 65536, True):
                with self.assertRaises(HealthError): check_health(port=port)
            factory.assert_not_called()


if __name__ == '__main__': unittest.main()
