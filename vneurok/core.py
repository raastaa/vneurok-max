"""Правила работы с идеями, задачами, помещениями и записью."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import threading
import uuid

UTC = timezone.utc

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

    def seed_demo(self):
        with self.connection(True) as db:
            if db.execute('SELECT count(*) FROM users').fetchone()[0]:
                return
            db.executemany('INSERT INTO users(id,name,role,class_id) VALUES(?,?,?,?)', [
                ('student-author','Автор идеи','student','7А'), ('student-helper','Помощник','student','7А'),
                ('student-visitor','Участник','student','8Б'), ('curator','Педагог-куратор','curator','staff')])
            db.executemany('INSERT INTO spaces VALUES(?,?,?)', [('computer','Компьютерный класс',12),('media','Медиастудия',6),('classroom','Проектная мастерская',20)])
            tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            for i in range(30):
                day = tomorrow + timedelta(days=i)
                for space in ('computer','media','classroom'):
                    db.execute('INSERT INTO availability(space_id,starts_at,ends_at) VALUES(?,?,?)', (space,(day+timedelta(hours=5)).isoformat(timespec='seconds'),(day+timedelta(hours=11)).isoformat(timespec='seconds')))
            db.execute('INSERT INTO initiatives(id,owner_id,title,description,kind,status,space_id,starts_at,ends_at,capacity,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('demo-game-workshop','student-author','Создаём свою первую игру','Вместе придумаем героя и соберём простой игровой уровень. Нужны помощники с афишей и подготовкой компьютеров.','event','draft','computer',(tomorrow+timedelta(hours=7)).isoformat(timespec='seconds'),(tomorrow+timedelta(hours=8)).isoformat(timespec='seconds'),10,now_iso(),now_iso()))
            db.execute('INSERT INTO tasks(id,initiative_id,title) VALUES(?,?,?)', ('demo-poster','demo-game-workshop','Подготовить афишу мастерской'))

    def user(self, user_id, db=None):
        if db is None:
            with self.connection() as cx:
                return self.user(user_id,cx)
        row = db.execute('SELECT id,name,role,class_id FROM users WHERE id=?',(user_id,)).fetchone()
        require(row is not None,'unauthorized','Войдите через приглашение школы.',401)
        return dict(row)

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

    def _detail(self, db, initiative_id, actor):
        row = self._row(db,initiative_id)
        require(self._can_view(db,row,actor),'forbidden','Эта карточка пока доступна только её команде и куратору.',403)
        row['owner_name'] = self.user(row['owner_id'],db)['name']
        row['space_name'] = db.execute('SELECT name FROM spaces WHERE id=?',(row['space_id'],)).fetchone()['name']
        row['tasks'] = [dict(t) for t in db.execute('SELECT t.*,u.name AS assignee_name FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id WHERE initiative_id=? ORDER BY t.rowid',(initiative_id,))]
        registrations = [dict(t) for t in db.execute('SELECT r.user_id,u.name,r.attended,r.attendance_recorded FROM registrations r JOIN users u ON u.id=r.user_id WHERE initiative_id=? ORDER BY r.created_at',(initiative_id,))]
        row['registration_count'] = len(registrations)
        row['team_count'] = len(self._team_ids(db,row))
        row['my_registration'] = any(r['user_id']==actor['id'] for r in registrations)
        row['my_following'] = bool(db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(initiative_id,actor['id'])).fetchone())
        row['my_membership'] = bool(db.execute('SELECT 1 FROM initiative_members WHERE initiative_id=? AND user_id=?',(initiative_id,actor['id'])).fetchone())
        row['attendance_pending'] = sum(not r['attendance_recorded'] for r in registrations) if actor['role']=='curator' or row['owner_id']==actor['id'] else None
        row['members'] = [dict(m) for m in db.execute('SELECT m.user_id,u.name FROM initiative_members m JOIN users u ON u.id=m.user_id WHERE initiative_id=?',(initiative_id,))] if actor['role']=='curator' or row['owner_id']==actor['id'] else []
        # Другим ученикам не показываем чужие записи и посещаемость.
        row['registrations'] = registrations if actor['role']=='curator' or row['owner_id']==actor['id'] else [r for r in registrations if r['user_id']==actor['id']]
        row['history'] = [dict(h) for h in db.execute('SELECT action,note,created_at FROM history WHERE initiative_id=? ORDER BY id DESC',(initiative_id,))]
        return row

    def detail(self, initiative_id, user_id):
        with self.connection() as db:
            return self._detail(db,initiative_id,self.user(user_id,db))

    def bootstrap(self,user_id,demo=False):
        with self.connection() as db:
            actor=self.user(user_id,db)
            result={'user':actor,'demo':demo,'spaces':[dict(s) for s in db.execute('SELECT * FROM spaces')],
                'users':[dict(u) for u in db.execute('SELECT id,name,role FROM users')] if demo else [actor],
                'initiatives':[],'availability':[dict(a) for a in db.execute('SELECT space_id,starts_at,ends_at FROM availability WHERE ends_at>? ORDER BY starts_at',(now_iso(),))]}
            for r in db.execute('SELECT * FROM initiatives ORDER BY updated_at DESC').fetchall():
                if self._can_view(db,r,actor):
                    result['initiatives'].append(self._detail(db,r['id'],actor))
            return result

    def _conditions(self,db,raw):
        allowed={'title','description','kind','space_id','starts_at','ends_at','capacity'}
        require(set(raw)<=allowed,'invalid_field','В запросе есть неизвестные поля.')
        fields={
            'title':text_field(raw.get('title'),'Название',3,100),
            'description':text_field(raw.get('description'),'Описание',10,2000),
            'kind':raw.get('kind'),'space_id':raw.get('space_id')}
        require(fields['kind'] in ('event','team'),'invalid_kind','Выберите событие или встречу команды.')
        space=db.execute('SELECT * FROM spaces WHERE id=?',(fields['space_id'],)).fetchone()
        require(space is not None,'invalid_space','Выберите доступное пространство.')
        cap=raw.get('capacity')
        require(isinstance(cap,int) and not isinstance(cap,bool) and 1<=cap<=space['capacity'],'invalid_capacity',f'Вместимость помещения: от 1 до {space["capacity"]}.')
        start,end=utc_time(raw.get('starts_at')),utc_time(raw.get('ends_at'))
        require(start>datetime.now(UTC),'past_time','Выберите будущее время.')
        require(start<end and end-start<=timedelta(hours=6),'invalid_time','Продолжительность: больше нуля, не более 6 часов.')
        fields.update(starts_at=start.isoformat(timespec='seconds'),ends_at=end.isoformat(timespec='seconds'),capacity=cap)
        require(db.execute('SELECT 1 FROM availability WHERE space_id=? AND starts_at<=? AND ends_at>=?',(fields['space_id'],fields['starts_at'],fields['ends_at'])).fetchone() is not None,'unavailable','Это время не входит в часы доступности помещения.')
        return fields

    def _version(self,row,version):
        require(type(version) is int and version==row['version'],'stale_version','Карточка уже изменилась. Обновите её и повторите действие.',409)

    def _history(self,db,row,actor,action,note=''):
        db.execute('INSERT INTO history(initiative_id,actor_id,action,note,created_at) VALUES(?,?,?,?,?)',(row['id'],actor['id'],action,note,now_iso()))
        db.execute('UPDATE initiatives SET version=version+1,updated_at=? WHERE id=?',(now_iso(),row['id']))

    def _notify(self,db,row,event,recipients):
        # Saved in the same transaction as the decision; transport runs separately.
        room=db.execute('SELECT name FROM spaces WHERE id=?',(row['space_id'],)).fetchone()['name']
        local=timezone(timedelta(hours=9))
        start=utc_time(row['starts_at']).astimezone(local).strftime('%d.%m.%Y %H:%M')
        end=utc_time(row['ends_at']).astimezone(local).strftime('%H:%M')
        text=f'«{row["title"]}»: {event}.\n{room}, {start}–{end} (Чита, UTC+9).\nШкола: /school, затем /view {row["id"]}'
        for uid in set(recipients):
            can_view=self._can_view(db,row,self.user(uid,db))
            notice=not can_view and row['kind']=='event' and row['published'] and bool(db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(row['id'],uid)).fetchone())
            if not can_view and not notice:
                continue
            safe_event=('Ранее выбранная инициатива отменена. Участия не будет.' if row['status']=='cancelled' else 'У ранее выбранной инициативы изменились условия. Прежняя запись отменена. Карточка на повторной проверке; сообщим, когда запись откроется.')
            safe_text=(safe_event+'\n'
                       f'ID: {row["id"]}\nШкола: /school. Отключить подписку: /unfollow {row["id"]}') if notice else text
            payload=json.dumps({'text':safe_text,'initiative_id':row['id'],'restricted_notice':bool(notice)},ensure_ascii=False)
            db.execute('INSERT INTO outbox(user_id,initiative_id,event,payload,created_at) VALUES(?,?,?,?,?)',(uid,row['id'],event,payload,now_iso()))

    def _audience(self,db,row):
        ids={row['owner_id']} | {r[0] for r in db.execute('SELECT user_id FROM registrations WHERE initiative_id=?',(row['id'],))}
        for table,column in (('tasks','assignee_id'),('initiative_members','user_id'),('initiative_followers','user_id')):
            ids.update(r[0] for r in db.execute(f'SELECT {column} FROM {table} WHERE initiative_id=? AND {column} IS NOT NULL',(row['id'],)))
        if row['approved_by']:
            ids.add(row['approved_by'])
        return ids

    def create(self,user_id,raw,key):
        key=text_field(key,'Ключ повторного запроса',8,128)
        fingerprint=hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.connection(True) as db:
            actor=self.user(user_id,db)
            prior=db.execute('SELECT * FROM idempotency WHERE actor_id=? AND key=?',(user_id,key)).fetchone()
            if prior:
                require(prior['fingerprint']==fingerprint,'idempotency_conflict','Этот ключ уже использован для другой заявки.',409)
                return self._detail(db,prior['initiative_id'],actor)
            values=self._conditions(db,raw)
            iid=uuid.uuid4().hex
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
            require(row['status'] not in ('completed','cancelled','awaiting_confirmation'),'invalid_state','Завершённую или отменённую инициативу менять нельзя.',409)
            require(utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала условия менять нельзя: нужно сохранить записи и посещаемость.',409)
            data={k:row[k] for k in ('title','description','kind','space_id','starts_at','ends_at','capacity')}
            data.update({k:v for k,v in raw.items() if k!='version'})
            values=self._conditions(db,data)
            require(values['kind']==row['kind'],'immutable_kind','Формат после создания не меняется. Создайте отдельную инициативу.')
            changed=any(row[k]!=v for k,v in values.items())
            if not changed:
                return self._detail(db,iid,actor)
            recipients=self._audience(db,row)
            # После изменения условий нужно снова согласовать событие и записаться.
            new_status='needs_changes' if row['status'] in ('submitted','approved','registration') else row['status']
            db.execute(f'UPDATE initiatives SET {",".join(k+"=?" for k in values)},status=?,approved_by=NULL WHERE id=?', [*values.values(),new_status,iid])
            db.execute('DELETE FROM registrations WHERE initiative_id=?',(iid,))
            self._history(db,row,actor,'edited','Условия изменены. Нужны новое согласование и повторная запись.')
            self._notify(db,self._row(db,iid),'условия изменены; прежняя запись отменена. Дождитесь согласования и запишитесь заново',recipients)
            return self._detail(db,iid,actor)

    def unsubscribe(self,user_id,iid):
        # An opt-out must work even while a revised card is private.
        with self.connection(True) as db:
            actor=self.user(user_id,db); row=self._row(db,iid)
            require(not db.execute('SELECT 1 FROM registrations WHERE initiative_id=? AND user_id=?',(iid,user_id)).fetchone(),'still_registered','Сначала отмените запись, чтобы не пропустить важные изменения.')
            removed=db.execute('DELETE FROM initiative_followers WHERE initiative_id=? AND user_id=?',(iid,user_id)).rowcount
            if removed:
                db.execute("UPDATE outbox SET state='cancelled',last_error='Пользователь отписался' WHERE initiative_id=? AND user_id=? AND state='pending'",(iid,user_id))
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
                require(status in ('draft','needs_changes'),'invalid_state','Карточка уже отправлена или закрыта.',409)
                self._conditions(db,{k:row[k] for k in ('title','description','kind','space_id','starts_at','ends_at','capacity')})
                new_status='submitted'
                self._notify(db,row,'нужно согласование',[u[0] for u in db.execute("SELECT id FROM users WHERE role='curator'")])
            elif action in ('invite','remove_member'):
                require(owner,'forbidden','Команду приглашает и изменяет автор.',403)
                require(status not in ('completed','cancelled','awaiting_confirmation'),'invalid_state','Команда уже закрыта.',409)
                target=self.user(raw.get('user_id'),db)
                require(target['role']=='student' and target['id']!=user_id,'invalid_member','Пригласите другого ученика своей школы.')
                if action=='invite':
                    require(utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала встречи состав команды не меняется.',409)
                    require(row['kind']!='team' or len(self._team_ids(db,row)|{target['id']})<=row['capacity'],'team_full','Вместимость закрытой встречи исчерпана; автор тоже занимает место.',409)
                    changed=db.execute('INSERT OR IGNORE INTO initiative_members VALUES(?,?)',(iid,target['id'])).rowcount
                    if not changed:
                        return self._detail(db,iid,actor)
                    self._notify(db,row,'вас пригласили в команду; можно взять задачу',[target['id']])
                else:
                    require(not db.execute('SELECT 1 FROM tasks WHERE initiative_id=? AND assignee_id=?',(iid,target['id'])).fetchone(),'member_has_tasks','Сначала освободите задачи участника.')
                    db.execute('DELETE FROM initiative_members WHERE initiative_id=? AND user_id=?',(iid,target['id']))
                    if not self._can_view(db,row,target):
                        db.execute("UPDATE outbox SET state='cancelled',last_error='Доступ к карточке закрыт' WHERE initiative_id=? AND user_id=? AND state='pending'",(iid,target['id']))
            elif action=='follow':
                require(actor['role']=='student' and row['kind']=='event','forbidden','Подписка доступна ученикам для событий.',403)
                require(status in ('approved','registration'),'invalid_state','Можно подписаться на согласованное событие.',409)
                if db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(iid,user_id)).fetchone():
                    return self._detail(db,iid,actor)
                db.execute('INSERT INTO initiative_followers VALUES(?,?)',(iid,user_id))
            elif action in ('approve','request_changes'):
                require(curator and not owner,'forbidden','Согласовать может другой пользователь с ролью куратора.',403)
                require(status=='submitted','invalid_state','На проверке нет актуальной заявки.',409)
                if action=='request_changes':
                    note=text_field(raw.get('note'),'Комментарий куратора',3,1000); new_status='needs_changes'
                    db.execute('UPDATE initiatives SET review_note=?,approved_by=NULL WHERE id=?',(note,iid))
                else:
                    self._conditions(db,{k:row[k] for k in ('title','description','kind','space_id','starts_at','ends_at','capacity')})
                    require(row['kind']!='team' or len(self._team_ids(db,row))<=row['capacity'],'team_full','В команде больше людей, чем разрешено условиями встречи.',409)
                    conflict=db.execute("SELECT id FROM initiatives WHERE id<>? AND space_id=? AND status IN ('approved','registration','awaiting_confirmation','completed') AND starts_at<? AND ends_at>?",(iid,row['space_id'],row['ends_at'],row['starts_at'])).fetchone()
                    require(conflict is None,'booking_conflict','Это помещение на выбранное время уже согласовано другой команде.',409)
                    db.execute("UPDATE initiatives SET approved_by=?,review_note='',published=1 WHERE id=?",(user_id,iid)); new_status='approved'
                self._notify(db,{**self._row(db,iid),'status':new_status},'согласовано' if action=='approve' else 'нужна доработка: '+note,self._audience(db,row))
            elif action=='open_registration':
                require(owner,'forbidden','Запись открывает автор.',403)
                require(status=='approved' and row['kind']=='event','invalid_state','Запись открывается только для согласованного события.',409)
                require(utc_time(row['starts_at'])>datetime.now(UTC),'past_time','Событие уже началось.',409)
                new_status='registration'
                self._notify(db,row,'запись открыта; подтвердите участие командой /join '+iid,self._audience(db,row))
            elif action=='register':
                require(actor['role']=='student','forbidden','Этот сценарий записи предназначен для учеников.',403)
                require(status=='registration' and row['kind']=='event','invalid_state','Запись ещё не открыта или уже закрыта.',409)
                require(utc_time(row['starts_at'])>datetime.now(UTC),'past_time','Событие уже началось.',409)
                count=db.execute('SELECT count(*) FROM registrations WHERE initiative_id=?',(iid,)).fetchone()[0]
                require(count<row['capacity'],'full','Свободных мест больше нет.',409)
                conflict=db.execute("SELECT 1 FROM registrations r JOIN initiatives i ON i.id=r.initiative_id WHERE r.user_id=? AND i.id<>? AND i.status='registration' AND i.starts_at<? AND i.ends_at>?",(user_id,iid,row['ends_at'],row['starts_at'])).fetchone()
                require(conflict is None,'schedule_conflict','У вас уже есть запись на пересекающееся событие.',409)
                db.execute('INSERT INTO registrations(initiative_id,user_id,created_at) VALUES(?,?,?)',(iid,user_id,now_iso()))
                db.execute('INSERT OR IGNORE INTO initiative_followers VALUES(?,?)',(iid,user_id))
                self._notify(db,row,'запись подтверждена; вы получите важные изменения',[user_id,row['owner_id']])
            elif action=='unregister':
                require(status not in ('completed','cancelled'),'invalid_state','Событие уже закрыто.',409)
                require(utc_time(row['starts_at'])>datetime.now(UTC),'already_started','После начала события запись меняет только педагог, чтобы сохранить посещаемость.',409)
                db.execute('DELETE FROM registrations WHERE initiative_id=? AND user_id=?',(iid,user_id))
                db.execute('DELETE FROM initiative_followers WHERE initiative_id=? AND user_id=?',(iid,user_id))
                self._notify(db,row,'запись участника отменена',[user_id,row['owner_id']])
            elif action=='cancel':
                require(owner or curator,'forbidden','Отменить может автор или куратор.',403)
                require(status not in ('completed','cancelled'),'invalid_state','Карточка уже закрыта.',409)
                new_status='cancelled'
                self._notify(db,{**row,'status':'cancelled'},'отменено',self._audience(db,row))
            elif action=='attendance':
                require(curator,'forbidden','Присутствие подтверждает педагог.',403)
                require(status in ('approved','registration','awaiting_confirmation') and utc_time(row['starts_at'])<=datetime.now(UTC),'invalid_state','Присутствие отмечают после начала согласованного события.',409)
                require(type(raw.get('attended')) is bool,'invalid_field','Укажите присутствие: да или нет.')
                changed=db.execute('UPDATE registrations SET attended=?,attendance_recorded=1 WHERE initiative_id=? AND user_id=?',(int(raw['attended']),iid,raw.get('user_id'))).rowcount
                require(changed==1,'not_registered','Ученик не записан на это событие.',404)
            elif action=='submit_result':
                require(owner,'forbidden','Итог описывает автор инициативы.',403)
                require(status in ('approved','registration','awaiting_confirmation') and utc_time(row['ends_at'])<=datetime.now(UTC),'invalid_state','Итог сохраняют после окончания согласованного события.',409)
                note=text_field(raw.get('result'),'Итог',10,2000)
                db.execute('UPDATE initiatives SET result=? WHERE id=?',(note,iid)); new_status='awaiting_confirmation'
                recipients={u[0] for u in db.execute("SELECT id FROM users WHERE role='curator'")}
                self._notify(db,row,'итог сохранён; педагог должен проверить посещаемость и подтвердить проведение',recipients | {row['owner_id']})
            elif action=='complete':
                require(curator and not owner,'forbidden','Проведение подтверждает педагог, который не является автором.',403)
                require(status=='awaiting_confirmation' and utc_time(row['ends_at'])<=datetime.now(UTC),'invalid_state','Сначала сохраните итог после окончания события.',409)
                require(not db.execute('SELECT 1 FROM registrations WHERE initiative_id=? AND attendance_recorded=0',(iid,)).fetchone(),'attendance_required','Отметьте для каждого участника: пришёл или не пришёл.',409)
                require(len(row['result'].strip())>=10,'result_required','Сначала сохраните итог.')
                new_status='completed'
                db.execute('UPDATE initiatives SET confirmed_by=?,confirmed_at=? WHERE id=?',(user_id,now_iso(),iid))
                self._notify(db,row,'педагог подтвердил проведение; итог доступен',self._audience(db,row))
            else:
                raise DomainError('unknown_action','Неизвестное действие.')
            if new_status:
                db.execute('UPDATE initiatives SET status=? WHERE id=?',(new_status,iid))
            self._history(db,row,actor,action,note)
            if not self._can_view(db,self._row(db,iid),actor):
                return {'id':iid,'removed_from_card':True}
            return self._detail(db,iid,actor)

    def task(self,user_id,iid,raw,task_id=None):
        with self.connection(True) as db:
            actor=self.user(user_id,db); row=self._row(db,iid)
            require(self._can_view(db,row,actor),'forbidden','Нет доступа к карточке.',403)
            self._version(row,raw.get('version'))
            require(row['status'] not in ('completed','cancelled'),'invalid_state','Карточка уже закрыта.',409)
            owner=row['owner_id']==user_id
            if task_id is None:
                require(owner,'forbidden','Задачи добавляет автор.',403)
                title=text_field(raw.get('title'),'Задача',3,160)
                assignee=raw.get('assignee_id')
                if assignee:
                    require(self.user(assignee,db)['role']=='student','invalid_assignee','Исполнитель задачи должен быть учеником.')
                    require(row['kind']!='team' or len(self._team_ids(db,row)|{assignee})<=row['capacity'],'team_full','Вместимость закрытой встречи исчерпана.',409)
                db.execute('INSERT INTO tasks(id,initiative_id,title,assignee_id) VALUES(?,?,?,?)',(uuid.uuid4().hex,iid,title,assignee))
                event='task_created'
            else:
                task=db.execute('SELECT * FROM tasks WHERE id=? AND initiative_id=?',(task_id,iid)).fetchone()
                require(task is not None,'not_found','Задача не найдена.',404)
                event=raw.get('action')
                if event=='claim':
                    require(actor['role']=='student','forbidden','Задачу берёт ученик.',403)
                    require(task['assignee_id'] in (None,user_id),'task_taken','Задачу уже взял другой помощник.',409)
                    db.execute('UPDATE tasks SET assignee_id=? WHERE id=?',(user_id,task_id))
                elif event in ('release','complete','reopen'):
                    require(owner or task['assignee_id']==user_id,'forbidden','Изменить задачу может автор или её исполнитель.',403)
                    require(task['assignee_id'] is not None,'unassigned','Сначала назначьте исполнителя.',409)
                    if event=='release':
                        db.execute('UPDATE tasks SET assignee_id=NULL,done=0 WHERE id=?',(task_id,))
                    else:
                        db.execute('UPDATE tasks SET done=? WHERE id=?',(int(event=='complete'),task_id))
                else:
                    raise DomainError('unknown_action','Неизвестное действие с задачей.')
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
