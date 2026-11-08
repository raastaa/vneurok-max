"""Real local subprocess startup/restart, synthetic credentials, no outbound send loop."""
from contextlib import closing, contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from vneurok.healthcheck import HealthError, check_health
from vneurok.school_setup import apply_setup

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = 'https://synthetic-startup.invalid'


class StartupSmokeTests(unittest.TestCase):
    @contextmanager
    def start_server(self, database):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
        env = {**os.environ, 'APP_ORIGIN': ORIGIN, 'MAX_BOT_TOKEN': 'synthetic-offline-token',
               'MAX_WEBHOOK_SECRET': 'synthetic-offline-webhook', 'PYTHONDONTWRITEBYTECODE': '1'}
        command = [sys.executable, '-W', 'error::ResourceWarning', '-m', 'vneurok.server', '--max-mode',
                   '--host', '127.0.0.1', '--port', str(port), '--db', str(database)]
        # No --send-enabled: even an inherited network connection cannot send MAX traffic.
        with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        out, err = process.communicate(); self.fail('Synthetic server exited: ' + out + err)
                    try:
                        check_health(port=port, origin=ORIGIN, timeout=.3)
                        break
                    except HealthError: time.sleep(.05)
                else: self.fail('Synthetic loopback server did not become healthy.')
                yield port
            finally:
                if process.poll() is None: process.terminate()
                try: process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate(timeout=5)

    def request(self, port, path, method='GET', raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        headers = {'Host': 'synthetic-startup.invalid'}
        if raw is not None: headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=json.dumps(raw) if raw is not None else None, headers=headers)
            response = connection.getresponse(); data = response.read()
            return response.status, json.loads(data) if response.getheader('Content-Type', '').startswith('application/json') else data
        finally: connection.close()

    def test_max_mode_startup_restart_and_database_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'school.db'
            initial = apply_setup(database, dict(school_id='synthetic-startup', school_name='Синтетическая школа запуска',
                                  admin_name='Синтетический взрослый', admin_max_id=900000101), confirm_access=True, confirm_adult=True)
            for _ in range(2):
                with self.start_server(database) as port:
                    self.assertFalse(check_health(port=port, origin=ORIGIN)['demo'])
                    self.assertEqual(self.request(port, '/api/config'), (200, {'demo': False}))
                    self.assertEqual(self.request(port, '/api/bootstrap')[0], 401)
                    self.assertEqual(self.request(port, '/api/demo/login', 'POST', {'user_id': 'admin'})[0], 404)
                    self.assertEqual(self.request(port, '/api/max/webhook', 'POST', {})[0], 401)
                    status, html = self.request(port, '/')
                    self.assertEqual(status, 200); self.assertIn(b'/app.js', html)
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute('SELECT id FROM users').fetchall(), [(initial['admin_id'],)])
                    self.assertEqual(db.execute('SELECT count(*) FROM school_setup_audit').fetchone()[0], 1)
                    self.assertEqual(db.execute('SELECT count(*) FROM bot_outbox').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
