"""Initial provisioning tests use only temporary databases and invented people."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from vneurok.core import DomainError, Store
from vneurok.school_setup import apply_setup, main, preview, validate_request


@contextlib.contextmanager
def database(path):
    db=sqlite3.connect(path)
    try:
        with db:yield db
    finally:db.close()


class SchoolSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'school.db'
        self.raw = dict(school_id='synthetic-school', school_name='Синтетическая школа',
                        admin_name='Синтетический взрослый', admin_max_id=900000001)

    def apply(self, **changes):
        return apply_setup(self.path, {**self.raw, **changes}, confirm_access=True, confirm_adult=True)

    def test_preview_missing_database_makes_no_files(self):
        before = list(Path(self.tmp.name).iterdir())
        result = preview(self.path, self.raw)
        self.assertFalse(result['applied']); self.assertEqual(result['operation'], 'create')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), before)

    def test_existing_preview_is_read_only(self):
        Store(self.path)
        before = self.path.read_bytes(); files=sorted(p.name for p in self.path.parent.iterdir())
        self.assertEqual(preview(self.path, self.raw)['operation'], 'create')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), files)
        with database(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 0)

    def test_explicit_access_and_adult_attestation_both_required(self):
        for access, adult in ((False, False), (True, False), (False, True)):
            with self.assertRaises(DomainError):
                apply_setup(self.path, self.raw, confirm_access=access, confirm_adult=adult)
            self.assertFalse(self.path.exists())

    def test_new_database_atomic_and_audited_no_demo_users(self):
        result = self.apply(); self.assertTrue(result['applied'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with database(self.path) as db:
            row = db.execute('SELECT name,role,class_id,max_id,school_id,is_admin,active FROM users').fetchone()
            self.assertEqual(row, (self.raw['admin_name'], 'curator', 'staff', str(self.raw['admin_max_id']), self.raw['school_id'], 1, 1))
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM school_setup_audit WHERE adult_confirmed=1 AND access_confirmed=1').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT action FROM admin_history').fetchone()[0], 'initial_admin_local_operator')
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
        self.assertFalse(list(self.path.parent.glob('.vneurok-school-setup-*')))

    def test_exact_active_administrator_only_is_idempotent(self):
        first = self.apply(); second = self.apply()
        self.assertEqual(first['admin_id'], second['admin_id']); self.assertFalse(second['applied'])
        self.assertEqual(second['operation'], 'noop')
        with database(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM school_setup_audit').fetchone()[0], 1)
        for changes in ({'admin_name': 'Другой взрослый'}, {'school_name': 'Другая школа'}, {'school_id': 'other-school'}):
            with self.assertRaises(DomainError): self.apply(**changes)

    def test_existing_student_or_revoked_admin_not_elevated(self):
        self.apply()
        for role, admin, active in (('student', 0, 1), ('curator', 0, 1), ('curator', 1, 0)):
            with database(self.path) as db:
                db.execute('UPDATE users SET role=?,is_admin=?,active=?', (role, admin, active))
            with self.assertRaises(DomainError): self.apply()
            with database(self.path) as db:
                self.assertEqual(db.execute('SELECT role,is_admin,active FROM users').fetchone(), (role, admin, active))

    def test_nonempty_database_does_not_get_another_administrator(self):
        store = Store(self.path); store.seed_demo()
        with self.assertRaises(DomainError): self.apply()
        with store.connection() as db:
            self.assertIsNone(db.execute('SELECT id FROM users WHERE max_id=?', (str(self.raw['admin_max_id']),)).fetchone())

    def test_failure_rolls_back_school_user_and_audit(self):
        Store(self.path)
        with database(self.path) as db:
            db.execute("CREATE TRIGGER forbid_setup_audit BEFORE INSERT ON admin_history BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.apply()
        with database(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 0)
            self.assertIsNone(db.execute('SELECT id FROM schools WHERE id=?', (self.raw['school_id'],)).fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='school_setup_audit'").fetchone())

    def test_staged_failure_leaves_no_target_or_temporary_files(self):
        with patch('vneurok.school_setup._write', side_effect=sqlite3.IntegrityError('synthetic')):
            with self.assertRaises(sqlite3.IntegrityError): self.apply()
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_concurrent_identical_setup_creates_only_one_admin(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.apply(), range(2)))
        self.assertEqual(len({r['admin_id'] for r in results}), 1)
        with database(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM school_setup_audit').fetchone()[0], 1)

    def test_validation_and_symlink_fail_closed(self):
        for change in ({'school_id': 'default'}, {'school_id': '../school'}, {'school_id': 'School'},
                       {'school_name': 'bad\nname'}, {'admin_name': ''}, {'admin_max_id': True},
                       {'admin_max_id': -1}, {'admin_max_id': 2**63}):
            with self.assertRaises(DomainError): validate_request(**{**self.raw, **change})
        target = self.path.parent / 'target.db'; Store(target); self.path.symlink_to(target)
        with self.assertRaises(DomainError): self.apply()

    def test_preview_refuses_live_wal_without_touching_it(self):
        Store(self.path)
        with database(self.path) as db:
            db.execute("UPDATE schools SET name='Синтетическая правка' WHERE id='default'");db.commit()
            wal=Path(str(self.path)+'-wal');self.assertTrue(wal.exists());before=wal.read_bytes()
            with self.assertRaises(DomainError):preview(self.path,self.raw)
            self.assertEqual(wal.read_bytes(),before)

    def test_zero_byte_existing_file_is_not_silently_initialized(self):
        self.path.touch()
        with self.assertRaises(DomainError):preview(self.path,self.raw)
        with self.assertRaises(DomainError):self.apply()
        self.assertEqual(self.path.read_bytes(),b'')

    def test_cli_without_apply_is_preview_even_with_confirmations(self):
        args = ['--db', str(self.path), '--school-id', self.raw['school_id'], '--school-name', self.raw['school_name'],
                '--admin-name', self.raw['admin_name'], '--admin-max-id', str(self.raw['admin_max_id']), '--confirm-access', '--confirm-adult']
        with contextlib.redirect_stdout(io.StringIO()) as output: code = main(args)
        self.assertEqual(code, 0); self.assertIn('"applied": false', output.getvalue()); self.assertFalse(self.path.exists())
        with contextlib.redirect_stdout(io.StringIO()) as output: code = main(args + ['--apply'])
        self.assertEqual(code, 0); self.assertTrue(self.path.exists())


if __name__ == '__main__': unittest.main()
