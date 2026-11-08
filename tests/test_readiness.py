import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from vneurok.readiness import inspect_config, main, valid_origin


class ReadinessTests(unittest.TestCase):
    def test_origin_accepts_https_default_port(self):
        for origin in ("https://school.example", "https://school.example:443"):
            self.assertTrue(valid_origin(origin))

    def test_origin_rejects_unsafe_or_unsupported_values(self):
        for origin in ("http://school.example", "https://school.example:8443", "https://user:password@school.example", "https://school.example/path", "https://school.example?x=1", "https://school.example#x", "https://school.example\n", "https://localhost", "https://127.0.0.1", "https://[::1]", "https://school.example:bad", "https://bad host"):
            self.assertFalse(valid_origin(origin), origin)

    def test_missing_config_is_not_ready(self):
        self.assertTrue(any(not c["ok"] for c in inspect_config(environ={})))

    def test_valid_config_does_not_expose_values(self):
        values = {"APP_ORIGIN": "https://school.example", "MAX_BOT_TOKEN": "fake-private-token", "MAX_WEBHOOK_SECRET": "fake-private-hook"}
        result = inspect_config(environ=values)
        self.assertTrue(all(c["ok"] for c in result))
        self.assertNotIn("fake-private", json.dumps(result))

    def test_rejects_invalid_secret(self):
        values = {"APP_ORIGIN": "https://school.example", "MAX_BOT_TOKEN": "fake", "MAX_WEBHOOK_SECRET": "bad!"}
        self.assertFalse(inspect_config(environ=values)[-1]["ok"])

    def test_private_file_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"settings"
            p.write_text("APP_ORIGIN=https://school.example\nMAX_BOT_TOKEN=fake\nMAX_WEBHOOK_SECRET=fake-hook\n")
            p.chmod(0o600)
            self.assertTrue(all(c["ok"] for c in inspect_config(p, {})))
            p.write_text(p.read_text()+"MAX_BOT_TOKEN=second\n")
            self.assertFalse(inspect_config(p, {})[-1]["ok"])

    def test_does_not_follow_symlinks_or_execute_shell(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"settings"
            p.write_text("MAX_BOT_TOKEN=$(touch should-never-exist)\n")
            p.chmod(0o600)
            link = Path(d)/"link"
            link.symlink_to(p)
            self.assertFalse(inspect_config(link, {})[0]["ok"])
            self.assertFalse(any(c["check"]=="bot_token_present" and c["ok"] for c in inspect_config(p, {})))
            self.assertFalse((Path(d)/"should-never-exist").exists())

    def test_missing_and_public_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"settings"
            self.assertFalse(inspect_config(p, {})[-1]["ok"])
            p.write_text("")
            p.chmod(0o644)
            result = inspect_config(p, {})
            self.assertFalse(next(c["ok"] for c in result if c["check"]=="env_private_permissions"))

    def test_cli_never_claims_live_bot(self):
        values = {"APP_ORIGIN": "https://school.example", "MAX_BOT_TOKEN": "fake-private-token", "MAX_WEBHOOK_SECRET": "fake-private-hook"}
        out = io.StringIO()
        with patch.dict(os.environ, values, clear=True), patch("sys.argv", ["readiness"]), contextlib.redirect_stdout(out):
            self.assertEqual(main(), 0)
        report=json.loads(out.getvalue())
        self.assertTrue(report["local_config_ok"])
        self.assertFalse(report["network_checked"])
        self.assertFalse(report["bot_live_confirmed"])
        self.assertNotIn("fake-private", out.getvalue())
