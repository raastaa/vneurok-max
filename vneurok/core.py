"""Правила работы с идеями, задачами, помещениями и записью."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import threading
import uuid

UTC = timezone.utc

INITIATIVE_FIELDS = ('title','description','kind','space_id','starts_at','ends_at','capacity','needs_space','audience_type','audience_value','interest_id')
CLOSED_STATUSES = ('completed','cancelled','declined')
TOPICS = (('general','Общие интересы'),('technology','Технологии'),('science','Наука'),('art','Творчество'),('sport','Спорт'),('volunteering','Добрые дела'))

class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def require(condition, code, message, status=400):
    if not condition:
        raise DomainError(code, message, status)


def now_iso():
    return datetime.now(UTC).isoformat(timespec='seconds')


def text_field(value, label, minimum=1, maximum=1000):
    require(isinstance(value, str), 'invalid_field', f'Поле «{label}» должно быть текстом.')
    value = value.strip()
    require(minimum <= len(value) <= maximum, 'invalid_field', f'Поле «{label}»: от {minimum} до {maximum} символов.')
    return value


def utc_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, 'invalid_time', 'Укажите часовой пояс.')
        return parsed.astimezone(UTC)
    except (ValueError, AttributeError, TypeError):
        raise DomainError('invalid_time', 'Неверная дата или время.')


# Keep the original baseline readable; Store applies additive and nullable-field
# migrations below before exposing this database to callers.
SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('student','curator')),
 class_id TEXT NOT NULL DEFAULT '7А', max_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, name TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity>0));
CREATE TABLE IF NOT EXISTS availability (
 id INTEGER PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), starts_at TEXT NOT NULL, ends_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS initiatives (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL, description TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('event','team')), status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,
 space_id TEXT NOT NULL REFERENCES spaces(id), starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, capacity INTEGER NOT NULL,
 published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
 confirmed_by TEXT REFERENCES users(id), confirmed_at TEXT,
 approved_by TEXT REFERENCES users(id), review_note TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, initiative_id TEXT NOT NULL REFERENCES initiatives(id), title TEXT NOT NULL,
 assignee_id TEXT REFERENCES users(id), done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)));
CREATE TABLE IF NOT EXISTS registrations (
 initiative_id TEXT NOT NULL REFERENCES initiatives(id), user_id TEXT NOT NULL REFERENCES users(id),
 attended INTEGER NOT NULL DEFAULT 0 CHECK(attended IN (0,1)),
 attendance_recorded INTEGER NOT NULL DEFAULT 0 CHECK(attendance_recorded IN (0,1)), created_at TEXT NOT NULL,
 PRIMARY KEY(initiative_id,user_id));
CREATE TABLE IF NOT EXISTS initiative_members (
 initiative_id TEXT NOT NULL REFERENCES initiatives(id), user_id TEXT NOT NULL REFERENCES users(id),
 PRIMARY KEY(initiative_id,user_id));
CREATE TABLE IF NOT EXISTS initiative_followers (
 initiative_id TEXT NOT NULL REFERENCES initiatives(id), user_id TEXT NOT NULL REFERENCES users(id),
 PRIMARY KEY(initiative_id,user_id));
CREATE TABLE IF NOT EXISTS history (
 id INTEGER PRIMARY KEY, initiative_id TEXT NOT NULL REFERENCES initiatives(id), actor_id TEXT NOT NULL REFERENCES users(id),
 action TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), initiative_id TEXT REFERENCES initiatives(id),
 event TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, sent_at TEXT, last_error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS idempotency (
 actor_id TEXT NOT NULL REFERENCES users(id), key TEXT NOT NULL, fingerprint TEXT NOT NULL, initiative_id TEXT NOT NULL REFERENCES initiatives(id),
 PRIMARY KEY(actor_id,key));
CREATE TABLE IF NOT EXISTS webhook_events (event_key TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bot_outbox (
 event_key TEXT PRIMARY KEY, max_user_id INTEGER NOT NULL, text TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, sent_at TEXT, last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS initiative_space_time ON initiatives(space_id,starts_at,ends_at,status);
CREATE INDEX IF NOT EXISTS task_initiative ON tasks(initiative_id);
CREATE INDEX IF NOT EXISTS history_initiative ON history(initiative_id);
'''


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.bot_processing_lock = threading.RLock()
        with self.connection() as db:
            db.executescript(SCHEMA)
            db.execute('PRAGMA journal_mode=WAL')
        # Additive migration: old unchecked false values do not mean confirmed absence.
        with self.connection(True) as db:
            columns={r['name'] for r in db.execute('PRAGMA table_info(registrations)')}
            if 'attendance_recorded' not in columns:
                db.execute('ALTER TABLE registrations ADD COLUMN attendance_recorded INTEGER NOT NULL DEFAULT 0')
                db.execute('UPDATE registrations SET attendance_recorded=1 WHERE attended=1')
            columns={r['name'] for r in db.execute('PRAGMA table_info(initiatives)')}
            if 'published' not in columns:
                db.execute('ALTER TABLE initiatives ADD COLUMN published INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE initiatives SET published=1 WHERE approved_by IS NOT NULL OR status IN ('approved','registration','awaiting_confirmation','completed') OR id IN (SELECT initiative_id FROM history WHERE action='approve')")
            if 'confirmed_by' not in columns:
                db.execute('ALTER TABLE initiatives ADD COLUMN confirmed_by TEXT REFERENCES users(id)')
                db.execute('ALTER TABLE initiatives ADD COLUMN confirmed_at TEXT')
                db.execute("UPDATE history SET action='task_complete' WHERE action='complete' AND note=''")
            db.execute("INSERT OR IGNORE INTO initiative_followers SELECT r.initiative_id,r.user_id FROM registrations r JOIN initiatives i ON i.id=r.initiative_id WHERE i.kind='event'")
        self._migrate_school_model()

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _migrate_school_model(self):
        # Rebuild only the constrained parent table, inside one transaction with FKs
        # checked before commit. Keep IDs, children, audit history and prior decisions.
        with self.connection() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute("CREATE TABLE IF NOT EXISTS schools(id TEXT PRIMARY KEY,name TEXT NOT NULL,retention_days INTEGER NOT NULL DEFAULT 365)")
                db.execute("INSERT OR IGNORE INTO schools(id,name) VALUES('default','Школа')")
                additions = {
                    'users': [('school_id',"TEXT NOT NULL DEFAULT 'default' REFERENCES schools(id)"),('is_admin','INTEGER NOT NULL DEFAULT 0'),('active','INTEGER NOT NULL DEFAULT 1'),('access_version','INTEGER NOT NULL DEFAULT 1')],
                    'spaces': [('school_id',"TEXT NOT NULL DEFAULT 'default' REFERENCES schools(id)"),('equipment',"TEXT NOT NULL DEFAULT '[]'"),('access_rules',"TEXT NOT NULL DEFAULT ''"),('responsible_id','TEXT REFERENCES users(id)'),('active','INTEGER NOT NULL DEFAULT 1')],
                    'initiatives': [('school_id',"TEXT NOT NULL DEFAULT 'default' REFERENCES schools(id)"),('needs_space','INTEGER NOT NULL DEFAULT 1'),('audience_type',"TEXT NOT NULL DEFAULT 'school'"),('audience_value',"TEXT NOT NULL DEFAULT ''"),('interest_id',"TEXT NOT NULL DEFAULT 'general'"),('booking_status',"TEXT NOT NULL DEFAULT 'draft'"),('conditions_revision','INTEGER NOT NULL DEFAULT 1')],
                }
                changed = set()
                for table, columns in additions.items():
                    existing={r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
                    for name, declaration in columns:
                        if name not in existing:
                            db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+declaration)
                            changed.add((table,name))
                if ('initiatives','school_id') in changed:
                    db.execute('UPDATE initiatives SET school_id=(SELECT school_id FROM users WHERE users.id=initiatives.owner_id)')
                if ('initiatives','booking_status') in changed:
                    db.execute("UPDATE initiatives SET booking_status=CASE WHEN status IN ('approved','registration','awaiting_confirmation','completed') THEN 'approved' WHEN status='submitted' THEN 'pending' WHEN status IN ('cancelled','declined') THEN 'released' ELSE 'draft' END")
                columns=list(db.execute('PRAGMA table_info(initiatives)'))
                if next(c for c in columns if c['name']=='space_id')['notnull']:
                    dependent_sql=[r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='initiatives' AND type IN ('index','trigger') AND sql IS NOT NULL")]
                    sql=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='initiatives'").fetchone()[0]
                    sql=sql.replace('CREATE TABLE initiatives','CREATE TABLE initiatives_new',1).replace('space_id TEXT NOT NULL','space_id TEXT').replace('capacity INTEGER NOT NULL','capacity INTEGER')
                    db.execute(sql)
                    names=','.join(c['name'] for c in columns)
                    db.execute('INSERT INTO initiatives_new('+names+') SELECT '+names+' FROM initiatives')
                    db.execute('DROP TABLE initiatives')
                    db.execute('ALTER TABLE initiatives_new RENAME TO initiatives')
                    for definition in dependent_sql:
                        db.execute(definition)
                db.execute('CREATE INDEX IF NOT EXISTS initiative_space_time ON initiatives(space_id,starts_at,ends_at,status)')
                db.execute('CREATE INDEX IF NOT EXISTS initiative_school ON initiatives(school_id,updated_at)')
                db.execute('CREATE TABLE IF NOT EXISTS classes(school_id TEXT NOT NULL REFERENCES schools(id),id TEXT NOT NULL,name TEXT NOT NULL,parallel TEXT NOT NULL,PRIMARY KEY(school_id,id))')
                db.execute('CREATE TABLE IF NOT EXISTS interests(school_id TEXT NOT NULL REFERENCES schools(id),id TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(school_id,id))')
                db.execute('CREATE TABLE IF NOT EXISTS interest_subscriptions(user_id TEXT NOT NULL REFERENCES users(id),school_id TEXT NOT NULL,interest_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(user_id,interest_id),FOREIGN KEY(school_id,interest_id) REFERENCES interests(school_id,id))')
                db.execute('CREATE TABLE IF NOT EXISTS team_attendance(initiative_id TEXT NOT NULL REFERENCES initiatives(id),user_id TEXT NOT NULL REFERENCES users(id),attended INTEGER NOT NULL DEFAULT 0 CHECK(attended IN (0,1)),attendance_recorded INTEGER NOT NULL DEFAULT 0 CHECK(attendance_recorded IN (0,1)),recorded_by TEXT REFERENCES users(id),recorded_at TEXT,PRIMARY KEY(initiative_id,user_id))')
                db.execute('CREATE TABLE IF NOT EXISTS participation_cancellations(initiative_id TEXT NOT NULL REFERENCES initiatives(id),user_id TEXT NOT NULL REFERENCES users(id),school_id TEXT NOT NULL REFERENCES schools(id),conditions_revision INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,PRIMARY KEY(initiative_id,user_id))')
                db.execute('CREATE TABLE IF NOT EXISTS admin_history(id INTEGER PRIMARY KEY,school_id TEXT NOT NULL REFERENCES schools(id),actor_id TEXT NOT NULL REFERENCES users(id),action TEXT NOT NULL,target_id TEXT NOT NULL,created_at TEXT NOT NULL)')
                for school in db.execute('SELECT id FROM schools').fetchall():
                    sid=school['id']
                    db.executemany('INSERT OR IGNORE INTO interests VALUES(?,?,?)',[(sid,i,n) for i,n in TOPICS])
                    for user in db.execute("SELECT DISTINCT class_id FROM users WHERE school_id=? AND role='student'",(sid,)).fetchall():
                        cid=user['class_id']; match=re.match(r'\d+',cid)
                        db.execute('INSERT OR IGNORE INTO classes VALUES(?,?,?,?)',(sid,cid,cid,match[0] if match else cid))
                if ('spaces','responsible_id') in changed:
                    db.execute("UPDATE spaces SET responsible_id=(SELECT id FROM users WHERE role='curator' AND school_id=spaces.school_id ORDER BY id LIMIT 1)")
                # Legacy team records retain unknown attendance; never infer that
                # a prior completion means each listed participant was present.
                for original in db.execute("SELECT * FROM initiatives WHERE kind='team' AND status IN ('approved','awaiting_confirmation','completed')").fetchall():
                    row=dict(original)
                    for uid in self._team_ids(db,row):
                        if db.execute('SELECT 1 FROM users WHERE id=? AND school_id=?',(uid,row['school_id'])).fetchone():
                            db.execute('INSERT OR IGNORE INTO team_attendance(initiative_id,user_id) VALUES(?,?)',(row['id'],uid))
                require(not db.execute('PRAGMA foreign_key_check').fetchall(),'migration_integrity','Не удалось безопасно обновить базу: нарушены связи.',500)
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.execute('PRAGMA foreign_keys=ON')

    def seed_demo(self):
        with self.connection(True) as db:
            if db.execute('SELECT count(*) FROM users').fetchone()[0]:
                return
            db.execute("UPDATE schools SET name='Учебная школа (вымышленные данные)' WHERE id='default'")
            db.executemany('INSERT OR IGNORE INTO classes VALUES(?,?,?,?)',[('default','7А','7А','7'),('default','8Б','8Б','8')])
            db.executemany('INSERT INTO users(id,name,role,class_id,is_admin) VALUES(?,?,?,?,?)', [
                ('student-author','Автор идеи','student','7А',0), ('student-helper','Помощник','student','7А',0),
                ('student-visitor','Участник','student','8Б',0), ('curator','Педагог-куратор','curator','staff',0),
                ('admin','Администратор учебной школы','curator','staff',1)])
            db.executemany('INSERT INTO spaces(id,name,capacity,equipment,access_rules,responsible_id) VALUES(?,?,?,?,?,?)', [
                ('computer','Компьютерный класс',14,json.dumps(['Компьютеры','Проектор'],ensure_ascii=False),'Работа с педагогом. После встречи выключить компьютеры.','curator'),
                ('media','Медиастудия',6,json.dumps(['Камера','Микрофон'],ensure_ascii=False),'Получить оборудование у ответственного педагога.','curator'),
                ('classroom','Проектная мастерская',20,json.dumps(['Доска','Столы'],ensure_ascii=False),'Оставить помещение в порядке.','curator')])
            tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            for i in range(30):
                day = tomorrow + timedelta(days=i)
                for space in ('computer','media','classroom'):
                    db.execute('INSERT INTO availability(space_id,starts_at,ends_at) VALUES(?,?,?)', (space,(day+timedelta(hours=5)).isoformat(timespec='seconds'),(day+timedelta(hours=11)).isoformat(timespec='seconds')))
            db.execute('INSERT INTO initiatives(id,owner_id,title,description,kind,status,space_id,starts_at,ends_at,capacity,interest_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('demo-game-workshop','student-author','Создаём свою первую игру','Вместе придумаем героя и соберём простой игровой уровень. Нужны помощники с афишей и подготовкой компьютеров.','event','draft','computer',(tomorrow+timedelta(hours=7)).isoformat(timespec='seconds'),(tomorrow+timedelta(hours=8)).isoformat(timespec='seconds'),10,'technology',now_iso(),now_iso()))
            db.execute('INSERT INTO tasks(id,initiative_id,title) VALUES(?,?,?)', ('demo-poster','demo-game-workshop','Подготовить афишу мастерской'))

    def user(self, user_id, db=None):
        if db is None:
            with self.connection() as cx:
                return self.user(user_id,cx)
        require(isinstance(user_id,str),'unauthorized','Некорректный пользователь.',401)
        row = db.execute('SELECT id,name,role,class_id,school_id,is_admin,active,access_version FROM users WHERE id=?',(user_id,)).fetchone()
        require(row is not None and row['active'],'unauthorized','Войдите через приглашение школы.',401)
        return dict(row)

    def _same_school(self, row, actor):
        require(row['school_id']==actor['school_id'],'forbidden','Нет доступа к данным другой школы.',403)

    def _in_audience(self, db, row, actor):
        if row['school_id']!=actor['school_id'] or not actor['active']:
            return False
        if actor['role']=='curator' or row['audience_type']=='school':
            return True
        if row['audience_type']=='class':
            return actor['class_id']==row['audience_value']
        cls=db.execute('SELECT parallel FROM classes WHERE school_id=? AND id=?',(actor['school_id'],actor['class_id'])).fetchone()
        return bool(cls and cls['parallel']==row['audience_value'])

    def max_user(self, max_id):
        with self.connection() as db:
            row = db.execute('SELECT id FROM users WHERE max_id=?',(str(max_id),)).fetchone()
            require(row is not None,'school_invitation_required','Аккаунт MAX ещё не включён в список вашей школы. Обратитесь к куратору.',403)
            return self.user(row['id'],db)

    def _row(self, db, initiative_id):
        row = db.execute('SELECT * FROM initiatives WHERE id=?',(initiative_id,)).fetchone()
        require(row is not None,'not_found','Инициатива не найдена.',404)
        return dict(row)

    def _can_view(self, db, row, actor):
        if not self._in_audience(db,row,actor):
            return False
        uid=actor['id']
        if row['owner_id']==uid or actor['role']=='curator':
            return True
        if db.execute('SELECT 1 FROM initiative_members WHERE initiative_id=? AND user_id=?',(row['id'],uid)).fetchone():
            return True
        if db.execute('SELECT 1 FROM tasks WHERE initiative_id=? AND assignee_id=?',(row['id'],uid)).fetchone():
            return True
        # Private teams never become public merely by changing workflow state.
        if row['kind']=='team':
            return False
        return bool(row['published'] and row['approved_by'] and row['status'] in ('approved','registration','awaiting_confirmation','completed','cancelled'))

    def _team_ids(self,db,row):
        return {row['owner_id']} | {r[0] for r in db.execute('SELECT user_id FROM initiative_members WHERE initiative_id=?',(row['id'],))} | {r[0] for r in db.execute('SELECT assignee_id FROM tasks WHERE initiative_id=? AND assignee_id IS NOT NULL',(row['id'],))}

    def _sync_team_attendance(self,db,row):
        if row['kind']!='team' or row['status']!='approved':
            return
        require(row['starts_at'] and utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала состав команды и история посещения не меняются.',409)
        ids=self._team_ids(db,row)
        for uid in ids:
            db.execute('INSERT OR IGNORE INTO team_attendance(initiative_id,user_id) VALUES(?,?)',(row['id'],uid))
        for participant in db.execute('SELECT user_id,attendance_recorded FROM team_attendance WHERE initiative_id=?',(row['id'],)).fetchall():
            if participant['user_id'] not in ids:
                require(not participant['attendance_recorded'],'attendance_locked','Нельзя удалить подтверждённое посещение.',409)
                db.execute('DELETE FROM team_attendance WHERE initiative_id=? AND user_id=?',(row['id'],participant['user_id']))

    def _detail(self, db, initiative_id, actor):
        row = self._row(db,initiative_id)
        require(self._can_view(db,row,actor),'forbidden','Эта карточка пока доступна только её команде и куратору.',403)
        row['owner_name'] = db.execute('SELECT name FROM users WHERE id=? AND school_id=?',(row['owner_id'],row['school_id'])).fetchone()['name']
        space=db.execute('SELECT name,responsible_id FROM spaces WHERE id=? AND school_id=?',(row['space_id'],row['school_id'])).fetchone()
        row['space_name'] = space['name'] if space else 'Без помещения'
        row['responsible_id'] = space['responsible_id'] if space else None
        row['tasks'] = [dict(t) for t in db.execute('SELECT t.*,u.name AS assignee_name FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id WHERE initiative_id=? ORDER BY t.rowid',(initiative_id,))]
        registrations = [dict(t) for t in db.execute('SELECT r.user_id,u.name,r.attended,r.attendance_recorded FROM registrations r JOIN users u ON u.id=r.user_id WHERE initiative_id=? ORDER BY r.created_at',(initiative_id,))]
        row['registration_count'] = len(registrations)
        row['team_count'] = len(self._team_ids(db,row))
        row['my_registration'] = any(r['user_id']==actor['id'] for r in registrations)
        row['my_following'] = bool(db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(initiative_id,actor['id'])).fetchone())
        row['my_membership'] = bool(db.execute('SELECT 1 FROM initiative_members WHERE initiative_id=? AND user_id=?',(initiative_id,actor['id'])).fetchone())
        team_attendance=[dict(r) for r in db.execute('SELECT a.user_id,u.name,a.attended,a.attendance_recorded,a.recorded_by,a.recorded_at FROM team_attendance a JOIN users u ON u.id=a.user_id WHERE a.initiative_id=? AND u.school_id=? ORDER BY u.name,u.id',(initiative_id,row['school_id']))] if row['kind']=='team' else []
        attendance=team_attendance if row['kind']=='team' else registrations
        row['attendance_pending'] = sum(not r['attendance_recorded'] for r in attendance) if actor['role']=='curator' or row['owner_id']==actor['id'] else None
        row['team_attendance'] = team_attendance if actor['role']=='curator' or row['owner_id']==actor['id'] else [r for r in team_attendance if r['user_id']==actor['id']]
        row['attendance_legacy_unknown'] = bool(row['kind']=='team' and row['status']=='completed' and any(not r['attendance_recorded'] for r in team_attendance))
        row['members'] = [dict(m) for m in db.execute('SELECT m.user_id,u.name FROM initiative_members m JOIN users u ON u.id=m.user_id WHERE initiative_id=?',(initiative_id,))] if actor['role']=='curator' or row['owner_id']==actor['id'] else []
        # Другим ученикам не показываем чужие записи и посещаемость.
        row['registrations'] = registrations if actor['role']=='curator' or row['owner_id']==actor['id'] else [r for r in registrations if r['user_id']==actor['id']]
        row['helper_candidates'] = []
        if row['owner_id']==actor['id'] and row['status'] not in (*CLOSED_STATUSES,'awaiting_confirmation'):
            members={m['user_id'] for m in row['members']}
            for candidate in db.execute("SELECT id,name,role,class_id,school_id,active FROM users WHERE school_id=? AND role='student' AND active=1 AND id<>? ORDER BY name,id",(row['school_id'],actor['id'])):
                if candidate['id'] not in members and self._in_audience(db,row,candidate):
                    row['helper_candidates'].append({'id':candidate['id'],'name':candidate['name']})
        row['history'] = [dict(h) for h in db.execute('SELECT action,note,created_at FROM history WHERE initiative_id=? ORDER BY id DESC',(initiative_id,))]
        if actor['role']!='curator' and row['owner_id']!=actor['id']:
            for entry in row['history']:
                if entry['action']=='attendance':
                    entry['note']='Посещаемость проверена педагогом.'
        return row

    def detail(self, initiative_id, user_id):
        with self.connection() as db:
            return self._detail(db,initiative_id,self.user(user_id,db))

    def _space_dict(self, row, db):
        result=dict(row)
        result['equipment']=json.loads(result['equipment'])
        responsible=db.execute('SELECT name FROM users WHERE id=? AND school_id=?',(row['responsible_id'],row['school_id'])).fetchone()
        result['responsible_name']=responsible['name'] if responsible else ''
        return result

    def bootstrap(self,user_id,demo=False):
        with self.connection() as db:
            actor=self.user(user_id,db); sid=actor['school_id']
            result={'user':actor,'demo':demo,
                'school':dict(db.execute('SELECT * FROM schools WHERE id=?',(sid,)).fetchone()),
                'capabilities':{'manage_school':bool(actor['is_admin']), 'review':actor['role']=='curator'},
                'spaces':[self._space_dict(s,db) for s in db.execute('SELECT * FROM spaces WHERE school_id=? AND active=1',(sid,))],
                'classes':[dict(c) for c in db.execute('SELECT id,name,parallel FROM classes WHERE school_id=? ORDER BY id',(sid,))],
                'interests':[dict(i) for i in db.execute('SELECT id,name FROM interests WHERE school_id=? ORDER BY id',(sid,))],
                'interest_subscriptions':[r[0] for r in db.execute('SELECT interest_id FROM interest_subscriptions WHERE user_id=? AND school_id=?',(user_id,sid))],
                'users':[dict(u) for u in db.execute('SELECT id,name,role,class_id,school_id,is_admin,active FROM users WHERE school_id=? AND active=1',(sid,))] if demo else [actor],
                'initiatives':[],
                'availability':[dict(a) for a in db.execute('SELECT a.space_id,a.starts_at,a.ends_at FROM availability a JOIN spaces s ON s.id=a.space_id WHERE s.school_id=? AND s.active=1 AND a.ends_at>? ORDER BY a.starts_at',(sid,now_iso()))],
                'busy_slots':[dict(b) for b in db.execute("SELECT space_id,starts_at,ends_at FROM initiatives WHERE school_id=? AND needs_space=1 AND booking_status='approved' AND ends_at>? ORDER BY starts_at",(sid,now_iso()))]}
            for r in db.execute('SELECT * FROM initiatives WHERE school_id=? ORDER BY updated_at DESC',(sid,)).fetchall():
                if self._can_view(db,r,actor):
                    result['initiatives'].append(self._detail(db,r['id'],actor))
            return result

    def _conditions(self,db,raw,actor,complete=True):
        require(isinstance(raw,dict) and set(raw)<=set(INITIATIVE_FIELDS),'invalid_field','В запросе есть неизвестные поля.')
        fields={
            'title':text_field(raw.get('title',''),'Название',3 if complete else 0,100),
            'description':text_field(raw.get('description',''),'Описание',10 if complete else 0,2000),
            'kind':raw.get('kind','event'),'space_id':raw.get('space_id') or None,
            'starts_at':raw.get('starts_at') or '', 'ends_at':raw.get('ends_at') or '',
            'capacity':raw.get('capacity'), 'needs_space':raw.get('needs_space',True),
            'audience_type':raw.get('audience_type','school'),'audience_value':raw.get('audience_value',''),
            'interest_id':raw.get('interest_id','general')}
        require(fields['kind'] in ('event','team'),'invalid_kind','Выберите событие или встречу команды.')
        require(type(fields['needs_space']) in (bool,int) and fields['needs_space'] in (0,1),'invalid_field','Укажите, нужно ли помещение.')
        fields['needs_space']=int(fields['needs_space'])
        require(fields['audience_type'] in ('school','parallel','class') and isinstance(fields['audience_value'],str),'invalid_audience','Выберите аудиторию: класс, параллель или школа.')
        sid=actor['school_id']
        if fields['audience_type']=='school':
            require(not fields['audience_value'],'invalid_audience','Для всей школы не нужно указывать класс.')
        elif fields['audience_type']=='class':
            require(db.execute('SELECT 1 FROM classes WHERE school_id=? AND id=?',(sid,fields['audience_value'])).fetchone(),'invalid_audience','Класс не найден в вашей школе.')
        else:
            require(db.execute('SELECT 1 FROM classes WHERE school_id=? AND parallel=?',(sid,fields['audience_value'])).fetchone(),'invalid_audience','Параллель не найдена в вашей школе.')
        require(self._in_audience(db,{**fields,'school_id':sid},actor),'invalid_audience','Выберите аудиторию, в которую входит ваш класс.')
        require(isinstance(fields['interest_id'],str),'invalid_interest','Укажите код темы.')
        require(db.execute('SELECT 1 FROM interests WHERE school_id=? AND id=?',(sid,fields['interest_id'])).fetchone(),'invalid_interest','Выберите тему интереса вашей школы.')
        require(fields['space_id'] is None or isinstance(fields['space_id'],str),'invalid_space','Укажите код пространства.')
        space=None
        if not fields['needs_space']:
            require(fields['space_id'] is None,'invalid_space','Для встречи без помещения уберите выбранное пространство.')
        elif fields['space_id']:
            space=db.execute('SELECT * FROM spaces WHERE id=? AND school_id=? AND active=1',(fields['space_id'],sid)).fetchone()
            require(space is not None,'invalid_space','Выберите доступное пространство своей школы.')
        elif complete:
            raise DomainError('invalid_space','Выберите помещение или сценарий без помещения.')
        cap=fields['capacity']
        if cap is not None or complete:
            limit=space['capacity'] if space else 1000
            require(type(cap) is int and 1<=cap<=limit,'invalid_capacity',f'Вместимость: от 1 до {limit}.')
        if fields['starts_at']:
            start=utc_time(fields['starts_at']); require(start>datetime.now(UTC),'past_time','Выберите будущее время.')
            fields['starts_at']=start.isoformat(timespec='seconds')
        if fields['ends_at']:
            fields['ends_at']=utc_time(fields['ends_at']).isoformat(timespec='seconds')
        if complete:
            require(bool(fields['starts_at'] and fields['ends_at']),'invalid_time','Укажите начало и окончание.')
        if fields['starts_at'] and fields['ends_at']:
            start,end=utc_time(fields['starts_at']),utc_time(fields['ends_at'])
            require(start<end and end-start<=timedelta(hours=6),'invalid_time','Продолжительность: больше нуля, не более 6 часов.')
            if space:
                require(db.execute('SELECT 1 FROM availability WHERE space_id=? AND starts_at<=? AND ends_at>=?',(space['id'],fields['starts_at'],fields['ends_at'])).fetchone(),'unavailable','Это время не входит в разрешённые интервалы помещения.')
        return fields

    def _version(self,row,version):
        require(type(version) is int and version==row['version'],'stale_version','Карточка уже изменилась. Обновите её и повторите действие.',409)

    def _history(self,db,row,actor,action,note=''):
        db.execute('INSERT INTO history(initiative_id,actor_id,action,note,created_at) VALUES(?,?,?,?,?)',(row['id'],actor['id'],action,note,now_iso()))
        db.execute('UPDATE initiatives SET version=version+1,updated_at=? WHERE id=?',(now_iso(),row['id']))

    def _invalidate_notifications(self,db,reason,initiative_id=None,user_id=None,interest_id=None,payload_match=None):
        # A message can be waiting for account linkage or operator action as well
        # as pending. Preserve uncertain/sending audit states, but revoke retry.
        clauses=["state IN ('pending','blocked','failed','uncertain','sending')"]
        values=[]
        if initiative_id is not None:
            clauses.append('initiative_id=?');values.append(initiative_id)
        if user_id is not None:
            clauses.append('user_id=?');values.append(user_id)
        for item in db.execute('SELECT id,state,payload FROM outbox WHERE '+' AND '.join(clauses),values).fetchall():
            try:
                payload=json.loads(item['payload'])
            except (ValueError,TypeError):
                payload={}
            if not isinstance(payload,dict):
                payload={}
            if interest_id is not None and payload.get('interest_id')!=interest_id:
                continue
            if payload_match and any(payload.get(k)!=v for k,v in payload_match.items()):
                continue
            payload['obsolete']=True
            payload['obsolete_reason']=reason
            state='cancelled' if item['state'] in ('pending','blocked','failed') else item['state']
            db.execute("UPDATE outbox SET state=?,payload=?,last_error=CASE WHEN ?='cancelled' THEN ? ELSE last_error END WHERE id=?",(state,json.dumps(payload,ensure_ascii=False),state,reason,item['id']))

    def _record_cancelled_participation(self,db,row,user_ids):
        for uid in user_ids:
            if db.execute('SELECT 1 FROM users WHERE id=? AND school_id=?',(uid,row['school_id'])).fetchone():
                db.execute('INSERT INTO participation_cancellations VALUES(?,?,?,?,0,?) ON CONFLICT(initiative_id,user_id) DO UPDATE SET conditions_revision=excluded.conditions_revision,revoked=0,created_at=excluded.created_at',(row['id'],uid,row['school_id'],row['conditions_revision'],now_iso()))

    def notification_allowed(self,db,row,actor,payload):
        # Conditions revisions do not change when other users sign up or take a
        # task. An old agreement can never be revived by reapproval or a retry.
        if not isinstance(payload,dict):
            return False
        if payload.get('obsolete') is True or not actor['active'] or row['school_id']!=actor['school_id']:
            return False
        if payload.get('conditions_revision')!=row['conditions_revision']:
            return False
        if payload.get('source_access_version')!=actor['access_version']:
            return False
        if payload.get('valid_statuses') and row['status'] not in payload['valid_statuses']:
            return False
        if payload.get('registration_user_id'):
            registered=bool(db.execute('SELECT 1 FROM registrations WHERE initiative_id=? AND user_id=?',(row['id'],payload['registration_user_id'])).fetchone())
            if registered is not payload.get('registration_present'):
                return False
        if payload.get('membership_user_id') and not db.execute('SELECT 1 FROM initiative_members WHERE initiative_id=? AND user_id=?',(row['id'],payload['membership_user_id'])).fetchone():
            return False
        if payload.get('participation_cancelled') is True:
            if payload.get('recipient_id')!=actor['id'] or payload.get('interest_id') or row['kind']!='event':
                return False
            receipt=db.execute('SELECT 1 FROM participation_cancellations WHERE initiative_id=? AND user_id=? AND school_id=? AND conditions_revision=? AND revoked=0',(row['id'],actor['id'],actor['school_id'],row['conditions_revision'])).fetchone()
            registered=db.execute('SELECT 1 FROM registrations WHERE initiative_id=? AND user_id=?',(row['id'],actor['id'])).fetchone()
            return bool(receipt and not registered)
        if not self._in_audience(db,row,actor):
            return False
        interest=payload.get('interest_id')
        if interest and (row['kind']!='event' or interest!=row['interest_id'] or not db.execute('SELECT 1 FROM interest_subscriptions WHERE user_id=? AND school_id=? AND interest_id=?',(actor['id'],actor['school_id'],interest)).fetchone()):
            return False
        if self._can_view(db,row,actor):
            return True
        return bool(payload.get('restricted_notice') is True and row['kind']=='event' and row['published'] and db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(row['id'],actor['id'])).fetchone())

    def _notify(self,db,row,event,recipients,include_interests=False,allow_former_notice=False,guards=None):
        # Enqueue transactionally. Topics are opt-in, and never broaden audience.
        direct=set(recipients); recipients=set(direct)
        if include_interests and row['kind']=='event':
            recipients.update(r[0] for r in db.execute('SELECT user_id FROM interest_subscriptions WHERE school_id=? AND interest_id=?',(row['school_id'],row['interest_id'])))
        room=db.execute('SELECT name FROM spaces WHERE id=? AND school_id=?',(row['space_id'],row['school_id'])).fetchone()
        where=room['name'] if room else 'Без помещения' if not row['needs_space'] else 'Помещение пока не выбрано'
        local=timezone(timedelta(hours=9))
        start=utc_time(row['starts_at']).astimezone(local).strftime('%d.%m.%Y %H:%M') if row['starts_at'] else 'Дата пока не выбрана'
        end=utc_time(row['ends_at']).astimezone(local).strftime('%H:%M') if row['ends_at'] else ''
        text=f'«{row["title"] or "Черновик идеи"}»: {event}.\n{where}, {start}–{end} (Чита, UTC+9).\nШкола: /school, затем /view {row["id"]}'
        for uid in recipients:
            user=db.execute('SELECT id FROM users WHERE id=? AND active=1 AND school_id=?',(uid,row['school_id'])).fetchone()
            if not user:
                continue
            actor=self.user(uid,db)
            if not self._in_audience(db,row,actor):
                # A former participant gets only a loss-of-participation receipt,
                # never the revised title, time, place, audience or a view grant.
                receipt=allow_former_notice and row['kind']=='event' and uid in direct and db.execute('SELECT 1 FROM participation_cancellations WHERE initiative_id=? AND user_id=? AND school_id=? AND revoked=0',(row['id'],uid,row['school_id'])).fetchone()
                if receipt and not db.execute('SELECT 1 FROM registrations WHERE initiative_id=? AND user_id=?',(row['id'],uid)).fetchone():
                    db.execute('UPDATE participation_cancellations SET conditions_revision=? WHERE initiative_id=? AND user_id=?',(row['conditions_revision'],row['id'],uid))
                    payload={'text':'Ваша прежняя запись отменена. Не приходите по прежним условиям. Карточка сейчас недоступна.\nID: '+row['id']+'\nОтключить подписку: /unfollow '+row['id'],
                        'initiative_id':row['id'],'participation_cancelled':True,'recipient_id':uid,'conditions_revision':row['conditions_revision'],'source_access_version':actor['access_version']}
                    db.execute('INSERT INTO outbox(user_id,initiative_id,event,payload,created_at) VALUES(?,?,?,?,?)',(uid,row['id'],'participation_cancelled',json.dumps(payload,ensure_ascii=False),now_iso()))
                continue
            can_view=self._can_view(db,row,actor)
            notice=not can_view and row['kind']=='event' and row['published'] and bool(db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(row['id'],uid)).fetchone())
            if not can_view and not notice:
                continue
            safe_event=('Ранее выбранная инициатива отменена. Участия не будет.' if row['status'] in ('cancelled','declined') else 'У ранее выбранной инициативы изменились условия. Прежняя запись отменена. Карточка на повторной проверке; сообщим, когда запись откроется.')
            safe_text=(safe_event+'\n'+f'ID: {row["id"]}\nШкола: /school. Отключить подписку: /unfollow {row["id"]}') if notice else text
            payload={'text':safe_text,'initiative_id':row['id'],'restricted_notice':bool(notice),'conditions_revision':row['conditions_revision'],'source_access_version':actor['access_version']}
            payload.update(guards or {})
            if uid not in direct:
                payload['interest_id']=row['interest_id']
            db.execute('INSERT INTO outbox(user_id,initiative_id,event,payload,created_at) VALUES(?,?,?,?,?)',(uid,row['id'],event,json.dumps(payload,ensure_ascii=False),now_iso()))

    def _audience(self,db,row):
        ids={row['owner_id']} | {r[0] for r in db.execute('SELECT user_id FROM registrations WHERE initiative_id=?',(row['id'],))}
        for table,column in (('tasks','assignee_id'),('initiative_members','user_id'),('initiative_followers','user_id')):
            ids.update(r[0] for r in db.execute(f'SELECT {column} FROM {table} WHERE initiative_id=? AND {column} IS NOT NULL',(row['id'],)))
        if row['approved_by']:
            ids.add(row['approved_by'])
        return {uid for uid in ids if db.execute('SELECT 1 FROM users WHERE id=? AND school_id=? AND active=1',(uid,row['school_id'])).fetchone()}

    def _check_participants(self,db,row,extra=None):
        team=self._team_ids(db,row) | set(extra or ())
        for uid in team:
            member=self.user(uid,db)
            require(self._in_audience(db,row,member),'invalid_member','Все участники команды должны входить в аудиторию своей школы.')
        if row['capacity'] is not None:
            require(row['kind']!='team' or len(team)<=row['capacity'],'team_full','Вместимость закрытой встречи исчерпана; автор тоже занимает место.',409)
        if row['needs_space'] and row['space_id']:
            space=db.execute('SELECT * FROM spaces WHERE id=? AND school_id=? AND active=1',(row['space_id'],row['school_id'])).fetchone()
            require(space,'invalid_space','Пространство недоступно.')
            responsible=db.execute("SELECT id FROM users WHERE id=? AND school_id=? AND role='curator' AND active=1",(space['responsible_id'],row['school_id'])).fetchone()
            require(responsible,'responsible_required','У пространства должен быть активный ответственный педагог.')
            people=team|{responsible['id']}
            # Reserve the promised public seats plus organizers and responsible adult.
            required=len(people)+(row['capacity'] or 0) if row['kind']=='event' else max(len(people),(row['capacity'] or 0)+int(responsible['id'] not in team))
            require(required<=space['capacity'],'space_capacity','Недостаточно мест для участников, организаторов и ответственного педагога.',409)

    def _curators(self,db,row):
        return {r[0] for r in db.execute("SELECT id FROM users WHERE role='curator' AND active=1 AND school_id=?",(row['school_id'],))}

    def create(self,user_id,raw,key):
        key=text_field(key,'Ключ повторного запроса',8,128)
        fingerprint=hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.connection(True) as db:
            actor=self.user(user_id,db)
            prior=db.execute('SELECT * FROM idempotency WHERE actor_id=? AND key=?',(user_id,key)).fetchone()
            if prior:
                require(prior['fingerprint']==fingerprint,'idempotency_conflict','Этот ключ уже использован для другой заявки.',409)
                return self._detail(db,prior['initiative_id'],actor)
            values=self._conditions(db,raw,actor,complete=False)
            iid=uuid.uuid4().hex
            values['school_id']=actor['school_id']
            values['booking_status']='draft' if values['needs_space'] else 'not_required'
            keys=list(values)
            db.execute(f'INSERT INTO initiatives(id,owner_id,{",".join(keys)},created_at,updated_at) VALUES({",".join("?" for _ in range(len(keys)+4))})',[iid,user_id,*[values[k] for k in keys],now_iso(),now_iso()])
            db.execute('INSERT INTO idempotency VALUES(?,?,?,?)',(user_id,key,fingerprint,iid))
            db.execute('INSERT INTO history(initiative_id,actor_id,action,created_at) VALUES(?,?,?,?)',(iid,user_id,'created',now_iso()))
            return self._detail(db,iid,actor)

    def edit(self,user_id,iid,raw):
        with self.connection(True) as db:
            actor=self.user(user_id,db); row=self._row(db,iid)
            require(row['owner_id']==user_id,'forbidden','Изменять инициативу может только её автор.',403)
            self._version(row,raw.get('version'))
            self._same_school(row,actor)
            require(row['status'] not in (*CLOSED_STATUSES,'awaiting_confirmation'),'invalid_state','Завершённую или отменённую инициативу менять нельзя.',409)
            require(not row['starts_at'] or utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала условия менять нельзя: нужно сохранить записи и посещаемость.',409)
            data={k:row[k] for k in INITIATIVE_FIELDS}
            data.update({k:v for k,v in raw.items() if k!='version'})
            values=self._conditions(db,data,actor,complete=False)
            require(values['kind']==row['kind'],'immutable_kind','Формат после создания не меняется. Создайте отдельную инициативу.')
            changed=any(row[k]!=v for k,v in values.items())
            if not changed:
                return self._detail(db,iid,actor)
            self._check_participants(db,{**row,**values})
            recipients=self._audience(db,row)
            former={r[0] for r in db.execute('SELECT user_id FROM registrations WHERE initiative_id=?',(iid,))}
            # После изменения условий нужно снова согласовать событие и записаться.
            new_status='needs_changes' if row['status'] in ('submitted','approved','registration') else row['status']
            booking='draft' if values['needs_space'] else 'not_required'
            db.execute(f'UPDATE initiatives SET {",".join(k+"=?" for k in values)},status=?,booking_status=?,approved_by=NULL,conditions_revision=conditions_revision+1 WHERE id=?', [*values.values(),new_status,booking,iid])
            db.execute('DELETE FROM registrations WHERE initiative_id=?',(iid,))
            db.execute('DELETE FROM team_attendance WHERE initiative_id=?',(iid,))
            self._invalidate_notifications(db,'Условия инициативы изменены',initiative_id=iid)
            self._record_cancelled_participation(db,self._row(db,iid),former)
            self._history(db,row,actor,'edited','Условия изменены. Нужны новое согласование и повторная запись.')
            self._notify(db,self._row(db,iid),'условия изменены; прежняя запись отменена. Дождитесь согласования и запишитесь заново',recipients,allow_former_notice=True)
            return self._detail(db,iid,actor)

    def unsubscribe(self,user_id,iid):
        # An opt-out must work even while a revised card is private.
        with self.connection(True) as db:
            actor=self.user(user_id,db); row=self._row(db,iid)
            self._same_school(row,actor)
            require(not db.execute('SELECT 1 FROM registrations WHERE initiative_id=? AND user_id=?',(iid,user_id)).fetchone(),'still_registered','Сначала отмените запись, чтобы не пропустить важные изменения.')
            removed=db.execute('DELETE FROM initiative_followers WHERE initiative_id=? AND user_id=?',(iid,user_id)).rowcount
            if removed:
                self._invalidate_notifications(db,'Пользователь отписался',initiative_id=iid,user_id=user_id)
                db.execute('UPDATE participation_cancellations SET revoked=1 WHERE initiative_id=? AND user_id=?',(iid,user_id))
                self._history(db,row,actor,'unfollow')
            return {'id':iid,'removed_from_card':True}

    def action(self,user_id,iid,raw):
        if raw.get('action')=='unfollow':
            return self.unsubscribe(user_id,iid)
        with self.connection(True) as db:
            actor=self.user(user_id,db); row=self._row(db,iid)
            require(self._can_view(db,row,actor),'forbidden','Нет доступа к карточке.',403)
            action=raw.get('action'); status=row['status']; owner=row['owner_id']==user_id
            curator=actor['role']=='curator'
            # Повторное нажатие не добавит человека второй раз.
            existing=db.execute('SELECT 1 FROM registrations WHERE initiative_id=? AND user_id=?',(iid,user_id)).fetchone()
            if action=='register' and existing:
                return self._detail(db,iid,actor)
            if action=='unregister' and not existing:
                return self._detail(db,iid,actor)
            self._version(row,raw.get('version'))
            note=''; new_status=None
            if action=='submit':
                require(owner,'forbidden','На согласование отправляет автор.',403)
                require(status in ('draft','needs_changes','deferred'),'invalid_state','Карточка уже отправлена или закрыта.',409)
                self._conditions(db,{k:row[k] for k in INITIATIVE_FIELDS},self.user(row['owner_id'],db))
                self._check_participants(db,row)
                new_status='submitted'
                db.execute('UPDATE initiatives SET booking_status=? WHERE id=?',('pending' if row['needs_space'] else 'not_required',iid))
                self._notify(db,row,'нужно согласование',self._curators(db,row),guards={'valid_statuses':['submitted']})
            elif action in ('invite','remove_member'):
                require(owner,'forbidden','Команду приглашает и изменяет автор.',403)
                require(status not in (*CLOSED_STATUSES,'awaiting_confirmation'),'invalid_state','Команда уже закрыта.',409)
                require(not row['starts_at'] or utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала встречи состав команды не меняется.',409)
                if action=='invite':
                    target=self.user(raw.get('user_id'),db)
                    require(target['role']=='student' and target['id']!=user_id and self._in_audience(db,row,target),'invalid_member','Пригласите другого ученика своей школы.')
                else:
                    target_id=raw.get('user_id')
                    require(isinstance(target_id,str),'invalid_member','Укажите участника своей школы.')
                    target=db.execute('SELECT id,name,role,class_id,school_id,active FROM users WHERE id=? AND school_id=?',(target_id,row['school_id'])).fetchone()
                    require(target is not None and target_id!=user_id,'invalid_member','Укажите участника своей школы.')
                if action=='invite':
                    require(not row['starts_at'] or utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала встречи состав команды не меняется.',409)
                    self._check_participants(db,row,{target['id']})
                    changed=db.execute('INSERT OR IGNORE INTO initiative_members VALUES(?,?)',(iid,target['id'])).rowcount
                    if not changed:
                        return self._detail(db,iid,actor)
                    self._notify(db,row,'вас пригласили в команду; можно взять задачу',[target['id']],guards={'membership_user_id':target['id']})
                else:
                    require(not db.execute('SELECT 1 FROM tasks WHERE initiative_id=? AND assignee_id=?',(iid,target['id'])).fetchone(),'member_has_tasks','Сначала освободите задачи участника.')
                    self._invalidate_notifications(db,'Приглашение отозвано',initiative_id=iid,payload_match={'membership_user_id':target['id']})
                    db.execute('DELETE FROM initiative_members WHERE initiative_id=? AND user_id=?',(iid,target['id']))
                    if not self._can_view(db,row,target):
                        self._invalidate_notifications(db,'Доступ к карточке закрыт',initiative_id=iid,user_id=target['id'])
            elif action=='follow':
                require(actor['role']=='student' and row['kind']=='event','forbidden','Подписка доступна ученикам для событий.',403)
                require(status in ('approved','registration'),'invalid_state','Можно подписаться на согласованное событие.',409)
                if db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(iid,user_id)).fetchone():
                    return self._detail(db,iid,actor)
                db.execute('INSERT INTO initiative_followers VALUES(?,?)',(iid,user_id))
            elif action in ('approve','request_changes','defer','decline'):
                require(curator and not owner,'forbidden','Согласовать может другой пользователь с ролью куратора.',403)
                require(status=='submitted','invalid_state','На проверке нет актуальной заявки.',409)
                if action!='approve':
                    note=text_field(raw.get('note'),'Комментарий куратора',3,1000)
                    new_status={'request_changes':'needs_changes','defer':'deferred','decline':'declined'}[action]
                    db.execute('UPDATE initiatives SET review_note=?,approved_by=NULL,booking_status=? WHERE id=?',(note,'released' if row['needs_space'] else 'not_required',iid))
                else:
                    self._conditions(db,{k:row[k] for k in INITIATIVE_FIELDS},self.user(row['owner_id'],db))
                    self._check_participants(db,row)
                    if row['needs_space']:
                        responsible=db.execute('SELECT responsible_id FROM spaces WHERE id=? AND school_id=?',(row['space_id'],row['school_id'])).fetchone()
                        require(responsible and responsible['responsible_id']==user_id,'not_responsible','Помещение согласует только назначенный ответственный педагог.',403)
                    conflict=db.execute("SELECT id FROM initiatives WHERE id<>? AND school_id=? AND space_id=? AND booking_status='approved' AND starts_at<? AND ends_at>?",(iid,row['school_id'],row['space_id'],row['ends_at'],row['starts_at'])).fetchone() if row['needs_space'] else None
                    require(conflict is None,'booking_conflict','Это помещение на выбранное время уже согласовано другой команде.',409)
                    db.execute("UPDATE initiatives SET approved_by=?,review_note='',published=1,booking_status=? WHERE id=?",(user_id,'approved' if row['needs_space'] else 'not_required',iid)); new_status='approved'
                decision={'approve':'согласовано','request_changes':'нужна доработка','defer':'решение отложено','decline':'отказано'}[action]
                self._notify(db,{**self._row(db,iid),'status':new_status},decision+(': '+note if note else ''),self._audience(db,row),include_interests=action=='approve',guards={'valid_statuses':['approved','registration'] if action=='approve' else [new_status]})
            elif action=='open_registration':
                require(owner,'forbidden','Запись открывает автор.',403)
                require(status=='approved' and row['kind']=='event','invalid_state','Запись открывается только для согласованного события.',409)
                require(utc_time(row['starts_at'])>datetime.now(UTC),'past_time','Событие уже началось.',409)
                new_status='registration'
                self._notify(db,{**row,'status':'registration'},'запись открыта; подтвердите участие командой /join '+iid,self._audience(db,row),include_interests=True,guards={'valid_statuses':['registration']})
            elif action=='register':
                require(actor['role']=='student','forbidden','Этот сценарий записи предназначен для учеников.',403)
                require(status=='registration' and row['kind']=='event','invalid_state','Запись ещё не открыта или уже закрыта.',409)
                require(utc_time(row['starts_at'])>datetime.now(UTC),'past_time','Событие уже началось.',409)
                count=db.execute('SELECT count(*) FROM registrations WHERE initiative_id=?',(iid,)).fetchone()[0]
                require(count<row['capacity'],'full','Свободных мест больше нет.',409)
                conflict=db.execute("SELECT 1 FROM registrations r JOIN initiatives i ON i.id=r.initiative_id WHERE r.user_id=? AND i.id<>? AND i.status IN ('approved','registration','awaiting_confirmation','completed') AND i.starts_at<? AND i.ends_at>?",(user_id,iid,row['ends_at'],row['starts_at'])).fetchone()
                require(conflict is None,'schedule_conflict','У вас уже есть запись на пересекающееся событие.',409)
                self._invalidate_notifications(db,'Состояние записи изменилось',initiative_id=iid,payload_match={'registration_user_id':user_id})
                db.execute('INSERT INTO registrations(initiative_id,user_id,created_at) VALUES(?,?,?)',(iid,user_id,now_iso()))
                db.execute('UPDATE participation_cancellations SET revoked=1 WHERE initiative_id=? AND user_id=?',(iid,user_id))
                db.execute('INSERT OR IGNORE INTO initiative_followers VALUES(?,?)',(iid,user_id))
                self._notify(db,row,'запись подтверждена; вы получите важные изменения',[user_id,row['owner_id']],guards={'registration_user_id':user_id,'registration_present':True})
            elif action=='unregister':
                require(status not in CLOSED_STATUSES,'invalid_state','Событие уже закрыто.',409)
                require(utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала события запись меняет только педагог, чтобы сохранить посещаемость.',409)
                self._invalidate_notifications(db,'Запись участника отменена',initiative_id=iid,user_id=user_id)
                self._invalidate_notifications(db,'Запись участника отменена',initiative_id=iid,payload_match={'registration_user_id':user_id})
                db.execute('UPDATE participation_cancellations SET revoked=1 WHERE initiative_id=? AND user_id=?',(iid,user_id))
                db.execute('DELETE FROM registrations WHERE initiative_id=? AND user_id=?',(iid,user_id))
                db.execute('DELETE FROM initiative_followers WHERE initiative_id=? AND user_id=?',(iid,user_id))
                self._notify(db,row,'запись участника отменена',[user_id,row['owner_id']],guards={'registration_user_id':user_id,'registration_present':False})
            elif action=='cancel':
                require(owner or curator,'forbidden','Отменить может автор или куратор.',403)
                require(status not in CLOSED_STATUSES,'invalid_state','Карточка уже закрыта.',409)
                new_status='cancelled'
                self._invalidate_notifications(db,'Инициатива отменена',initiative_id=iid)
                db.execute('UPDATE initiatives SET conditions_revision=conditions_revision+1 WHERE id=?',(iid,))
                db.execute('UPDATE initiatives SET booking_status=? WHERE id=?',('released' if row['needs_space'] else 'not_required',iid))
                self._notify(db,{**self._row(db,iid),'status':'cancelled'},'отменено',self._audience(db,row),allow_former_notice=True)
            elif action=='attendance':
                require(curator,'forbidden','Присутствие подтверждает педагог.',403)
                require(status in ('approved','registration','awaiting_confirmation') and utc_time(row['starts_at'])<=datetime.now(UTC),'invalid_state','Присутствие отмечают после начала согласованного события.',409)
                require(type(raw.get('attended')) is bool,'invalid_field','Укажите присутствие: да или нет.')
                target=raw.get('user_id')
                require(isinstance(target,str),'invalid_field','Укажите участника.')
                if row['kind']=='team':
                    changed=db.execute('UPDATE team_attendance SET attended=?,attendance_recorded=1,recorded_by=?,recorded_at=? WHERE initiative_id=? AND user_id=?',(int(raw['attended']),user_id,now_iso(),iid,target)).rowcount
                    require(changed==1,'not_team_member','Этот пользователь не включён в согласованный состав команды.',404)
                else:
                    changed=db.execute('UPDATE registrations SET attended=?,attendance_recorded=1 WHERE initiative_id=? AND user_id=?',(int(raw['attended']),iid,target)).rowcount
                    require(changed==1,'not_registered','Ученик не записан на это событие.',404)
                note=target+(': присутствовал' if raw['attended'] else ': отсутствовал')
            elif action=='submit_result':
                require(owner,'forbidden','Итог описывает автор инициативы.',403)
                require(status in ('approved','registration','awaiting_confirmation') and utc_time(row['ends_at'])<=datetime.now(UTC),'invalid_state','Итог сохраняют после окончания согласованного события.',409)
                note=text_field(raw.get('result'),'Итог',10,2000)
                db.execute('UPDATE initiatives SET result=? WHERE id=?',(note,iid)); new_status='awaiting_confirmation'
                recipients=self._curators(db,row)
                self._notify(db,row,'итог сохранён; педагог должен проверить посещаемость и подтвердить проведение',recipients | {row['owner_id']},guards={'valid_statuses':['awaiting_confirmation']})
            elif action=='complete':
                require(curator and not owner,'forbidden','Проведение подтверждает педагог, который не является автором.',403)
                require(status=='awaiting_confirmation' and utc_time(row['ends_at'])<=datetime.now(UTC),'invalid_state','Сначала сохраните итог после окончания события.',409)
                attendance_table='team_attendance' if row['kind']=='team' else 'registrations'
                if row['kind']=='team':
                    require(db.execute('SELECT 1 FROM team_attendance WHERE initiative_id=?',(iid,)).fetchone(),'attendance_required','Сначала проверьте состав и присутствие команды.',409)
                require(not db.execute('SELECT 1 FROM '+attendance_table+' WHERE initiative_id=? AND attendance_recorded=0',(iid,)).fetchone(),'attendance_required','Отметьте для каждого участника: пришёл или не пришёл.',409)
                require(len(row['result'].strip())>=10,'result_required','Сначала сохраните итог.')
                new_status='completed'
                db.execute('UPDATE initiatives SET confirmed_by=?,confirmed_at=? WHERE id=?',(user_id,now_iso(),iid))
                self._notify(db,row,'педагог подтвердил проведение; итог доступен',self._audience(db,row),guards={'valid_statuses':['completed']})
            else:
                raise DomainError('unknown_action','Неизвестное действие.')
            if new_status:
                db.execute('UPDATE initiatives SET status=? WHERE id=?',(new_status,iid))
            updated=self._row(db,iid)
            if updated['kind']=='team' and updated['status']=='approved' and (action in ('approve','invite','remove_member')):
                self._sync_team_attendance(db,updated)
            self._history(db,row,actor,action,note)
            if not self._can_view(db,self._row(db,iid),actor):
                return {'id':iid,'removed_from_card':True}
            return self._detail(db,iid,actor)

    def task(self,user_id,iid,raw,task_id=None):
        with self.connection(True) as db:
            actor=self.user(user_id,db); row=self._row(db,iid)
            require(self._can_view(db,row,actor),'forbidden','Нет доступа к карточке.',403)
            self._version(row,raw.get('version'))
            require(row['status'] not in (*CLOSED_STATUSES,'awaiting_confirmation'),'invalid_state','Карточка уже закрыта.',409)
            owner=row['owner_id']==user_id
            if task_id is None:
                require(owner,'forbidden','Задачи добавляет автор.',403)
                title=text_field(raw.get('title'),'Задача',3,160)
                assignee=raw.get('assignee_id')
                if assignee:
                    target=self.user(assignee,db)
                    require(target['role']=='student' and self._in_audience(db,row,target),'invalid_assignee','Исполнитель должен быть учеником аудитории вашей школы.')
                    require(row['kind']!='team' or not row['starts_at'] or utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала состав команды не меняется.',409)
                    self._check_participants(db,row,{assignee})
                db.execute('INSERT INTO tasks(id,initiative_id,title,assignee_id) VALUES(?,?,?,?)',(uuid.uuid4().hex,iid,title,assignee))
                event='task_created'
            else:
                task=db.execute('SELECT * FROM tasks WHERE id=? AND initiative_id=?',(task_id,iid)).fetchone()
                require(task is not None,'not_found','Задача не найдена.',404)
                event=raw.get('action')
                if event=='claim':
                    require(actor['role']=='student','forbidden','Задачу берёт ученик.',403)
                    require(task['assignee_id'] in (None,user_id),'task_taken','Задачу уже взял другой помощник.',409)
                    require(row['kind']!='team' or not row['starts_at'] or utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала состав команды не меняется.',409)
                    self._check_participants(db,row,{user_id})
                    db.execute('UPDATE tasks SET assignee_id=? WHERE id=?',(user_id,task_id))
                elif event in ('release','complete','reopen'):
                    require(owner or task['assignee_id']==user_id,'forbidden','Изменить задачу может автор или её исполнитель.',403)
                    require(task['assignee_id'] is not None,'unassigned','Сначала назначьте исполнителя.',409)
                    if event=='release':
                        require(row['kind']!='team' or not row['starts_at'] or utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала состав команды не меняется.',409)
                        db.execute('UPDATE tasks SET assignee_id=NULL,done=0 WHERE id=?',(task_id,))
                    else:
                        db.execute('UPDATE tasks SET done=? WHERE id=?',(int(event=='complete'),task_id))
                else:
                    raise DomainError('unknown_action','Неизвестное действие с задачей.')
            if row['kind']=='team' and row['status']=='approved' and (event in ('claim','release') or (event=='task_created' and assignee)):
                self._sync_team_attendance(db,row)
            self._history(db,row,actor,event if event=='task_created' else 'task_'+event)
            names={'task_created':'добавлена задача','claim':'помощник взял задачу','release':'задача освобождена','complete':'задача выполнена','reopen':'задача снова открыта'}
            recipients={row['owner_id']}
            if row['approved_by']:
                recipients.add(row['approved_by'])
            if task_id is None and assignee:
                recipients.add(assignee)
            elif task_id is not None and task['assignee_id']:
                recipients.add(task['assignee_id'])
            task_title=title if task_id is None else task['title']
            self._notify(db,row,names[event]+': '+task_title,recipients)
            if not self._can_view(db,self._row(db,iid),actor):
                return {'id':iid,'removed_from_card':True}
            return self._detail(db,iid,actor)

    def set_interest_subscription(self,user_id,interest_id,enabled):
        require(type(enabled) is bool,'invalid_field','Укажите, включить ли подписку.')
        with self.connection(True) as db:
            actor=self.user(user_id,db); sid=actor['school_id']
            require(isinstance(interest_id,str),'invalid_interest','Укажите код темы.')
            require(db.execute('SELECT 1 FROM interests WHERE school_id=? AND id=?',(sid,interest_id)).fetchone(),'invalid_interest','Тема не найдена в вашей школе.')
            if enabled:
                db.execute('INSERT OR IGNORE INTO interest_subscriptions VALUES(?,?,?,?)',(user_id,sid,interest_id,now_iso()))
            else:
                db.execute('DELETE FROM interest_subscriptions WHERE user_id=? AND school_id=? AND interest_id=?',(user_id,sid,interest_id))
                self._invalidate_notifications(db,'Подписка на тему отключена',user_id=user_id,interest_id=interest_id)
            return {'interest_subscriptions':[r[0] for r in db.execute('SELECT interest_id FROM interest_subscriptions WHERE user_id=? AND school_id=? ORDER BY interest_id',(user_id,sid))]}

    def _admin_actor(self,db,user_id):
        actor=self.user(user_id,db)
        require(actor['role']=='curator' and actor['is_admin'],'forbidden','Это действие доступно администратору школы.',403)
        return actor

    def school_admin(self,user_id,db=None):
        if db is None:
            with self.connection() as cx:
                return self.school_admin(user_id,cx)
        actor=self._admin_actor(db,user_id); sid=actor['school_id']
        return {
            'school':dict(db.execute('SELECT * FROM schools WHERE id=?',(sid,)).fetchone()),
            'classes':[dict(r) for r in db.execute('SELECT id,name,parallel FROM classes WHERE school_id=? ORDER BY id',(sid,))],
            'users':[dict(r) for r in db.execute('SELECT id,name,role,class_id,max_id,school_id,is_admin,active,access_version FROM users WHERE school_id=? ORDER BY name',(sid,))],
            'spaces':[self._space_dict(r,db) for r in db.execute('SELECT * FROM spaces WHERE school_id=? ORDER BY name',(sid,))],
            'availability':[dict(r) for r in db.execute('SELECT a.* FROM availability a JOIN spaces s ON s.id=a.space_id WHERE s.school_id=? ORDER BY starts_at',(sid,))],
            'interests':[dict(r) for r in db.execute('SELECT id,name FROM interests WHERE school_id=? ORDER BY id',(sid,))],
            'retention_policy':'Срок хранения задаёт школа. Удаление и экспорт выполняет уполномоченный администратор по школьному регламенту; автоматического удаления истории нет.',
        }

    def _invalidate_space(self,db,space_id,actor):
        # A changed room agreement must never leave an old approval valid.
        rows=db.execute("SELECT * FROM initiatives WHERE space_id=? AND school_id=? AND status IN ('submitted','approved','registration','awaiting_confirmation')",(space_id,actor['school_id'])).fetchall()
        for original in rows:
            row=dict(original)
            require(not row['starts_at'] or utc_time(row['starts_at'])>datetime.now(UTC),'space_in_use','Нельзя менять условия пространства начавшегося события до его закрытия.',409)
            recipients=self._audience(db,row)
            former={r[0] for r in db.execute('SELECT user_id FROM registrations WHERE initiative_id=?',(row['id'],))}
            db.execute("UPDATE initiatives SET status='needs_changes',booking_status='draft',approved_by=NULL,conditions_revision=conditions_revision+1,review_note='Изменились условия пространства. Уточните условия и отправьте снова.' WHERE id=?",(row['id'],))
            db.execute('DELETE FROM registrations WHERE initiative_id=?',(row['id'],))
            db.execute('DELETE FROM team_attendance WHERE initiative_id=?',(row['id'],))
            self._invalidate_notifications(db,'Условия пространства изменены',initiative_id=row['id'])
            self._record_cancelled_participation(db,self._row(db,row['id']),former)
            self._history(db,row,actor,'space_changed','Изменились условия пространства; нужны повторное согласование и запись.')
            self._notify(db,self._row(db,row['id']),'изменились условия пространства; прежняя запись отменена',recipients,allow_former_notice=True)

    def _revoke_ineligible_future_registrations(self,db,user_id,administrator):
        target=dict(db.execute('SELECT id,name,role,class_id,school_id,active,access_version FROM users WHERE id=?',(user_id,)).fetchone())
        upcoming=db.execute("SELECT i.* FROM initiatives i JOIN registrations r ON r.initiative_id=i.id WHERE r.user_id=? AND i.school_id=? AND i.starts_at>? AND i.status NOT IN ('completed','cancelled','declined')",(user_id,target['school_id'],now_iso())).fetchall()
        for original in upcoming:
            row=dict(original)
            if target['role']=='student' and self._in_audience(db,row,target):
                continue
            self._invalidate_notifications(db,'Участие отменено после изменения школьного доступа',initiative_id=row['id'],payload_match={'registration_user_id':user_id})
            db.execute('DELETE FROM registrations WHERE initiative_id=? AND user_id=?',(row['id'],user_id))
            self._record_cancelled_participation(db,row,{user_id})
            self._history(db,row,administrator,'participation_access_changed','Будущая запись отменена после изменения доступа школьного пользователя.')
            self._notify(db,row,'ваша прежняя запись отменена после изменения школьного доступа',{user_id},allow_former_notice=True)

    def admin(self,user_id,raw):
        require(isinstance(raw,dict),'invalid_field','Ожидается объект с действием.')
        with self.connection(True) as db:
            actor=self._admin_actor(db,user_id); sid=actor['school_id']; action=raw.get('action')
            allowed={
                'upsert_class':{'action','id','name','parallel'},
                'upsert_user':{'action','id','name','role','class_id','max_id','is_admin','active'},
                'upsert_space':{'action','id','name','capacity','equipment','access_rules','responsible_id','active'},
                'set_availability':{'action','space_id','intervals'},
                'set_retention':{'action','retention_days'},
            }
            require(action in allowed,'unknown_action','Неизвестное действие администратора.')
            require(set(raw)<=allowed[action],'invalid_field','В запросе есть неизвестные поля.')
            target=''
            if action=='upsert_class':
                target=text_field(raw.get('id'),'Код класса',1,40)
                name=text_field(raw.get('name',target),'Класс',1,80)
                parallel=text_field(raw.get('parallel'),'Параллель',1,30)
                old=db.execute('SELECT * FROM classes WHERE school_id=? AND id=?',(sid,target)).fetchone()
                if old and old['parallel']!=parallel:
                    require(not db.execute("SELECT 1 FROM initiatives WHERE school_id=? AND audience_type IN ('class','parallel') AND status NOT IN ('completed','cancelled','declined')",(sid,)).fetchone(),'class_in_use','Сначала закройте инициативы класса и параллели, затем измените состав параллели.',409)
                    db.execute('UPDATE users SET access_version=access_version+1 WHERE school_id=? AND class_id=?',(sid,target))
                db.execute('INSERT INTO classes VALUES(?,?,?,?) ON CONFLICT(school_id,id) DO UPDATE SET name=excluded.name,parallel=excluded.parallel',(sid,target,name,parallel))
            elif action=='upsert_user':
                target=text_field(raw.get('id'),'Код пользователя',1,80)
                old=db.execute('SELECT * FROM users WHERE id=?',(target,)).fetchone()
                if old:
                    self._same_school(old,actor)
                name=text_field(raw.get('name'),'Имя',1,100); role=raw.get('role'); cid=raw.get('class_id')
                require(role in ('student','curator'),'invalid_role','Выберите ученика или педагога.')
                require(isinstance(cid,str) and 1<=len(cid)<=40,'invalid_class','Укажите класс.')
                require(role=='curator' or db.execute('SELECT 1 FROM classes WHERE school_id=? AND id=?',(sid,cid)).fetchone(),'invalid_class','Класс не найден в вашей школе.')
                active=raw.get('active',bool(old['active']) if old else True)
                is_admin=raw.get('is_admin',bool(old['is_admin']) if old else False)
                require(type(active) is bool and type(is_admin) is bool,'invalid_field','Флаги активности и администратора должны быть логическими.')
                require(not is_admin or role=='curator','invalid_role','Ученик не может быть администратором.')
                require(target!=user_id or (active and is_admin),'last_admin','Нельзя отозвать собственный доступ администратора.')
                max_id=raw.get('max_id',old['max_id'] if old else None)
                max_id=None if max_id in ('',None) else str(max_id)
                require(max_id is None or (re.fullmatch(r'[1-9][0-9]{0,19}',max_id) is not None),'invalid_max_id','Укажите числовой код MAX.')
                require(not max_id or not db.execute('SELECT 1 FROM users WHERE max_id=? AND id<>?',(max_id,target)).fetchone(),'max_id_in_use','Этот аккаунт MAX уже привязан.',409)
                if old and (role!='curator' or not active):
                    require(not db.execute('SELECT 1 FROM spaces WHERE responsible_id=? AND active=1',(target,)).fetchone(),'responsible_in_use','Сначала назначьте другого ответственного для его пространств.',409)
                scope=(role,cid,int(is_admin),int(active),max_id)
                old_scope=(old['role'],old['class_id'],old['is_admin'],old['active'],old['max_id']) if old else None
                if old:
                    db.execute('UPDATE users SET name=?,role=?,class_id=?,is_admin=?,active=?,max_id=?,access_version=access_version+? WHERE id=?',(name,*scope[:4],max_id,int(old_scope!=scope),target))
                    if old_scope!=scope:
                        self._invalidate_notifications(db,'Права или аккаунт получателя изменены',user_id=target)
                        self._revoke_ineligible_future_registrations(db,target,actor)
                else:
                    db.execute('INSERT INTO users(id,name,role,class_id,school_id,is_admin,active,max_id) VALUES(?,?,?,?,?,?,?,?)',(target,name,role,cid,sid,int(is_admin),int(active),max_id))
            elif action=='upsert_space':
                target=text_field(raw.get('id'),'Код пространства',1,80)
                old=db.execute('SELECT * FROM spaces WHERE id=?',(target,)).fetchone()
                if old:
                    self._same_school(old,actor)
                name=text_field(raw.get('name'),'Название пространства',1,100)
                cap=raw.get('capacity'); require(type(cap) is int and 1<=cap<=1000,'invalid_capacity','Вместимость пространства: от 1 до 1000.')
                equipment=raw.get('equipment',json.loads(old['equipment']) if old else [])
                require(isinstance(equipment,list) and len(equipment)<=50,'invalid_equipment','Оборудование должно быть списком до 50 пунктов.')
                equipment=[text_field(x,'Оборудование',1,100) for x in equipment]
                rules=text_field(raw.get('access_rules',old['access_rules'] if old else ''),'Условия доступа',0,2000)
                responsible=raw.get('responsible_id',old['responsible_id'] if old else None)
                require(db.execute("SELECT 1 FROM users WHERE id=? AND school_id=? AND role='curator' AND active=1",(responsible,sid)).fetchone(),'invalid_responsible','Назначьте активного педагога вашей школы ответственным.')
                active=raw.get('active',bool(old['active']) if old else True)
                require(type(active) is bool,'invalid_field','Активность должна быть логическим значением.')
                vals=(name,cap,json.dumps(equipment,ensure_ascii=False),rules,responsible,int(active))
                if old:
                    if vals!=tuple(old[k] for k in ('name','capacity','equipment','access_rules','responsible_id','active')):
                        self._invalidate_space(db,target,actor)
                    db.execute('UPDATE spaces SET name=?,capacity=?,equipment=?,access_rules=?,responsible_id=?,active=? WHERE id=?',(*vals,target))
                else:
                    db.execute('INSERT INTO spaces(id,school_id,name,capacity,equipment,access_rules,responsible_id,active) VALUES(?,?,?,?,?,?,?,?)',(target,sid,*vals))
            elif action=='set_availability':
                target=raw.get('space_id'); space=db.execute('SELECT * FROM spaces WHERE id=?',(target,)).fetchone()
                require(space,'invalid_space','Пространство не найдено.'); self._same_school(space,actor)
                intervals=raw.get('intervals'); require(isinstance(intervals,list) and len(intervals)<=366,'invalid_intervals','Укажите до 366 разрешённых интервалов.')
                values=[]
                for interval in intervals:
                    require(isinstance(interval,dict) and set(interval)=={'starts_at','ends_at'},'invalid_intervals','Каждый интервал должен содержать начало и окончание.')
                    start,end=utc_time(interval['starts_at']),utc_time(interval['ends_at'])
                    require(start<end and end>datetime.now(UTC),'invalid_time','Проверьте даты разрешённого интервала.')
                    values.append((start.isoformat(timespec='seconds'),end.isoformat(timespec='seconds')))
                values.sort()
                require(all(values[i-1][1]<=values[i][0] for i in range(1,len(values))),'overlapping_intervals','Разрешённые интервалы не должны пересекаться.')
                approved=db.execute("SELECT * FROM initiatives WHERE space_id=? AND booking_status='approved' AND ends_at>?",(target,now_iso())).fetchall()
                require(all(any(a<=r['starts_at'] and b>=r['ends_at'] for a,b in values) for r in approved),'bookings_outside_intervals','Новые интервалы исключают действующую бронь. Сначала измените или отмените инициативу.',409)
                db.execute('DELETE FROM availability WHERE space_id=?',(target,))
                db.executemany('INSERT INTO availability(space_id,starts_at,ends_at) VALUES(?,?,?)',[(target,a,b) for a,b in values])
            else:
                days=raw.get('retention_days'); require(type(days) is int and 30<=days<=3650,'invalid_retention','Срок хранения: от 30 до 3650 дней.')
                target=sid; db.execute('UPDATE schools SET retention_days=? WHERE id=?',(days,sid))
            db.execute('INSERT INTO admin_history(school_id,actor_id,action,target_id,created_at) VALUES(?,?,?,?,?)',(sid,user_id,action,target,now_iso()))
            return self.school_admin(user_id,db)
