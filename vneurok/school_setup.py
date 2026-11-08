"""Explicit, local first-administrator provisioning. Preview is read-only.

This command cannot discover or prove a person's age or school authority. An
operator must independently verify both and attest before granting access.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from .core import DomainError, Store, TOPICS, now_iso, require


REQUIRED = {
    'schools': {'id', 'name'},
    'users': {'id', 'name', 'role', 'class_id', 'max_id', 'school_id', 'is_admin', 'active', 'access_version'},
    'interests': {'school_id', 'id', 'name'},
    'admin_history': {'school_id', 'actor_id', 'action', 'target_id', 'created_at'},
}


def _label(value, label, minimum=1, maximum=100):
    require(isinstance(value, str), 'invalid_setup', f'{label}: нужен текст.')
    value = value.strip()
    require(minimum <= len(value) <= maximum and value.isprintable(),
            'invalid_setup', f'{label}: от {minimum} до {maximum} символов без управляющих знаков.')
    return value


def validate_request(school_id, school_name, admin_name, admin_max_id):
    require(isinstance(school_id, str) and re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', school_id) is not None
            and school_id != 'default', 'invalid_school', 'Идентификатор школы: 3–63 строчные латинские буквы, цифры и дефисы, начинается с буквы; default зарезервирован.')
    require(type(admin_max_id) is int and 0 < admin_max_id <= 9223372036854775807,
            'invalid_max_id', 'MAX ID взрослого: положительное целое число int64.')
    return {'school_id': school_id, 'school_name': _label(school_name, 'Название школы', 3, 200),
            'admin_name': _label(admin_name, 'Имя взрослого администратора', 2, 100), 'admin_max_id': admin_max_id}


def _path(path):
    path = Path(path).expanduser().absolute()
    require(not path.is_symlink(), 'invalid_database', 'Не используйте символическую ссылку как путь базы.')
    require(path.parent.is_dir(), 'invalid_database', 'Сначала создайте защищённый каталог базы.')
    require(not path.exists() or path.is_file(), 'invalid_database', 'Путь базы должен указывать на обычный файл.')
    return path


def _schema(db):
    for table, required in REQUIRED.items():
        columns = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
        require(required <= columns, 'unsupported_schema', 'Существующая база не имеет текущей схемы. Этот CLI не выполняет миграции. Используйте новый путь или отдельно проверьте обновление базы.')


def _plan(db, raw):
    if db is None:
        return {'operation': 'create', 'admin_id': None}
    _schema(db)
    existing = db.execute('SELECT * FROM users WHERE max_id=?', (str(raw['admin_max_id']),)).fetchone()
    school = db.execute('SELECT * FROM schools WHERE id=?', (raw['school_id'],)).fetchone()
    if existing:
        identical = (existing['school_id'] == raw['school_id'] and school and school['name'] == raw['school_name']
                     and existing['name'] == raw['admin_name'] and existing['role'] == 'curator'
                     and existing['class_id'] == 'staff' and existing['is_admin'] == 1 and existing['active'] == 1)
        require(identical, 'existing_identity', 'MAX ID уже связан с другим или несовпадающим аккаунтом. CLI не меняет его школу, имя, роль или активность.')
        return {'operation': 'noop', 'admin_id': existing['id']}
    require(not school or school['name'] == raw['school_name'], 'school_conflict', 'Такой идентификатор школы уже имеет другое название. Перезапись запрещена.')
    for table in ('users', 'initiatives', 'spaces', 'classes'):
        require(db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0,
                'not_empty', 'Первичное подключение разрешено только в пустой производственной базе. Существующих пользователей этот CLI не повышает.')
    return {'operation': 'create', 'admin_id': None}


def _connection(path, mode):
    db = sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def preview(db_path, raw):
    raw = validate_request(**raw)
    path = _path(db_path)
    try:
        if path.exists():
            # SQLite mode=ro may still create WAL/SHM files. An immutable read
            # avoids all writes, but is allowed only without uncheckpointed WAL.
            # Preview is advisory; apply revalidates under BEGIN IMMEDIATE.
            wal=Path(str(path)+'-wal'); journal=Path(str(path)+'-journal')
            require(not journal.exists() and not (wal.exists() and wal.stat().st_size), 'database_busy', 'Перед просмотром остановите сервис и дождитесь штатного закрытия SQLite/WAL. Не удаляйте WAL вручную.')
            before=path.stat()
            with closing(_connection(path, 'ro&immutable=1')) as db:
                db.execute('PRAGMA query_only=ON')
                plan = _plan(db, raw)
            after=path.stat()
            require((before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns) and not journal.exists() and not (wal.exists() and wal.stat().st_size), 'database_busy', 'База менялась во время просмотра. Остановите сервис и повторите проверку.')
        else:
            plan = _plan(None, raw)
    except sqlite3.Error as exc:
        raise DomainError('invalid_database', 'База не читается как поддерживаемая SQLite. Файлы не изменены.') from exc
    return {**raw, **plan, 'database': str(path), 'applied': False,
            'notice': 'Только предварительный просмотр. MAX ID и взрослый статус должны быть проверены оператором. Роль после подтверждения: curator; администратор школы: да.'}


def _write(path, raw):
    with closing(_connection(path, 'rw')) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            plan = _plan(db, raw)
            if plan['operation'] == 'noop':
                db.rollback()
                return {**plan, 'applied': False}
            actor = uuid.uuid4().hex
            db.execute('INSERT OR IGNORE INTO schools(id,name) VALUES(?,?)', (raw['school_id'], raw['school_name']))
            db.executemany('INSERT OR IGNORE INTO interests(school_id,id,name) VALUES(?,?,?)',
                           [(raw['school_id'], topic, name) for topic, name in TOPICS])
            db.execute('INSERT INTO users(id,name,role,class_id,max_id,school_id,is_admin,active,access_version) VALUES(?,?,\'curator\',\'staff\',?,?,1,1,1)',
                       (actor, raw['admin_name'], str(raw['admin_max_id']), raw['school_id']))
            db.execute('''CREATE TABLE IF NOT EXISTS school_setup_audit (
                id INTEGER PRIMARY KEY, school_id TEXT NOT NULL REFERENCES schools(id),
                admin_id TEXT NOT NULL REFERENCES users(id), source TEXT NOT NULL,
                adult_confirmed INTEGER NOT NULL CHECK(adult_confirmed=1),
                access_confirmed INTEGER NOT NULL CHECK(access_confirmed=1), created_at TEXT NOT NULL)''')
            db.execute('INSERT INTO school_setup_audit(school_id,admin_id,source,adult_confirmed,access_confirmed,created_at) VALUES(?,?,\'local_operator_cli\',1,1,?)',
                       (raw['school_id'], actor, now_iso()))
            db.execute('INSERT INTO admin_history(school_id,actor_id,action,target_id,created_at) VALUES(?,?,\'initial_admin_local_operator\',?,?)',
                       (raw['school_id'], actor, actor, now_iso()))
            require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'integrity_error', 'Связи базы не прошли проверку. Все изменения отменены.')
            db.commit()
            return {'operation': 'create', 'admin_id': actor, 'applied': True}
        except BaseException:
            db.rollback()
            raise


def apply_setup(db_path, raw, *, confirm_access=False, confirm_adult=False):
    require(confirm_access is True and confirm_adult is True, 'confirmation_required',
            'Для записи нужны --apply --confirm-access --confirm-adult после проверки взрослого, его MAX ID и полномочий в школе.')
    raw = validate_request(**raw)
    path = _path(db_path)
    if path.exists():
        result = _write(path, raw)
    else:
        # Construct and validate off-path, then publish without replacing a file
        # another operator may have created concurrently. Both paths share a FS.
        descriptor, temporary = tempfile.mkstemp(prefix='.vneurok-school-setup-', suffix='.db', dir=path.parent)
        os.close(descriptor)
        staged = Path(temporary)
        try:
            Store(staged)
            result = _write(staged, raw)
            with closing(_connection(staged, 'rw')) as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute('PRAGMA journal_mode=DELETE')
            with staged.open('rb') as handle:
                os.fsync(handle.fileno())
            try:
                os.link(staged, path)
                directory=os.open(path.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
                try:os.fsync(directory)
                finally:os.close(directory)
            except FileExistsError:
                # A concurrent invocation never authorizes overwriting its work.
                result = _write(path, raw)
        finally:
            for candidate in (staged, Path(str(staged) + '-wal'), Path(str(staged) + '-shm'), Path(str(staged) + '-journal')):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
    return {**raw, **result, 'database': str(path),
            'notice': 'Локальный доступ подготовлен. Сообщения MAX не отправлялись. Повтор с теми же данными не изменяет совпадающего активного администратора.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Первый взрослый администратор пустой школы: без флагов только просмотр, без сети MAX.')
    parser.add_argument('--db', required=True)
    parser.add_argument('--school-id', required=True)
    parser.add_argument('--school-name', required=True)
    parser.add_argument('--admin-name', required=True)
    parser.add_argument('--admin-max-id', required=True, type=int)
    parser.add_argument('--apply', action='store_true', help='Записать только после отдельного подтверждения доступа и взрослого статуса.')
    parser.add_argument('--confirm-access', action='store_true', help='Оператор проверил право этого человека управлять этой школой и точный MAX ID.')
    parser.add_argument('--confirm-adult', action='store_true', help='Оператор независимо проверил, что назначаемый ответственный является взрослым.')
    args = parser.parse_args(argv)
    raw = {key: getattr(args, key) for key in ('school_id', 'school_name', 'admin_name', 'admin_max_id')}
    try:
        result = apply_setup(args.db, raw, confirm_access=args.confirm_access, confirm_adult=args.confirm_adult) if args.apply else preview(args.db, raw)
    except (DomainError, sqlite3.Error, OSError) as error:
        message = error.message if isinstance(error, DomainError) else 'Операция не завершена. Проверьте права, схему и свободное место; повтор не должен менять существующий аккаунт.'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False))
        return 2
    print(json.dumps({'ok': True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
