import sqlite3
from pathlib import Path
import tempfile
import unittest
from vneurok.bot import BotRouter


class BotConnectionTests(unittest.TestCase):
    def test_connection_commits_and_closes(self):
        with tempfile.TemporaryDirectory() as d:
            bot = BotRouter(Path(d)/"demo")
            with bot._db() as db:
                db.execute("INSERT INTO sessions VALUES('1','demo','author')")
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
            with bot._db() as check:
                self.assertEqual(check.execute("SELECT role FROM sessions WHERE uid='1'").fetchone()[0], "author")

    def test_connection_rolls_back_and_closes(self):
        with tempfile.TemporaryDirectory() as d:
            bot = BotRouter(Path(d)/"demo")
            with self.assertRaises(RuntimeError):
                with bot._db() as db:
                    db.execute("INSERT INTO sessions VALUES('1','demo','author')")
                    raise RuntimeError("test rollback")
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
            with bot._db() as check:
                self.assertEqual(check.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
