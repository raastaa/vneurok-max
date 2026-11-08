"""Команды школьного бота. Ответ возвращается вызывающему коду."""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from .core import Store, DomainError

ROLES = dict(author='student-author', helper='student-helper', participant='student-visitor', curator='curator')
CHITA = timezone(timedelta(hours=9), 'Asia/Chita')
STATUS = dict(draft='Черновик', submitted='На согласовании', needs_changes='Нужна доработка', approved='Согласовано', registration='Запись открыта', awaiting_confirmation='Итог ждёт подтверждения', completed='Завершено', cancelled='Отменено')
HERO = dict(author='Автор идеи', helper='Помощник', participant='Участник', curator='Педагог')
HELP = '''Внеурок: идея → команда → педагог → помещение → запись → итог.
/demo — личный учебный пример; /school — школа по приглашению.
/role author|helper|participant|curator — сменить героя только в демо.
/list /my /view ID /spaces /me
/new название | описание | event или team | помещение | ГГГГ-ММ-ДД ЧЧ:ММ | ГГГГ-ММ-ДД ЧЧ:ММ | мест
Даты и время: Чита, Asia/Chita (UTC+9).
/submit ID /approve ID /changes ID комментарий /open ID
/join ID /leave ID /follow ID /unfollow ID /cancel ID
/invite ID USER /remove ID USER
/task ID название /claim ID TASK /done ID TASK /release ID TASK
/result ID итог /attend ID USER да|нет /confirm ID (педагог)
/edit ID поле | значение (title, description, space_id, starts_at, ends_at, capacity)
/demo_end ID — показать завершение на учебном примере.
Используйте полные ID из карточек. Начните с /demo и /list.'''

class BotRouter:
    def __init__(self, demo_root, school_store=None):
        self.root = Path(demo_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.school_store = school_store
        self.path = self.root / 'router.sqlite3'
        with self._db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS sessions(uid TEXT PRIMARY KEY, mode TEXT, role TEXT); CREATE TABLE IF NOT EXISTS inbox(uid TEXT,event TEXT,reply TEXT,PRIMARY KEY(uid,event));')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def handle(self, max_user_id, text, event_key):
        if type(max_user_id) is not int or max_user_id <= 0:
            return 'Не удалось определить пользователя.'
        if not isinstance(text,str) or len(text)>8000 or not isinstance(event_key,str) or not event_key or len(event_key)>512:
            return 'Не удалось прочитать сообщение.'
        uid = str(max_user_id)
        # Сохраняем начало обработки, чтобы после сбоя не выполнить действие дважды.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT reply FROM inbox WHERE uid=? AND event=?',(uid,event_key)).fetchone()
            if prior:
                return self._limit(prior[0]) if prior[0] is not None else 'Результат предыдущего действия уточняется. Нужна проверка руководителя.'
            if db.execute('SELECT 1 FROM inbox WHERE uid=? AND reply IS NULL',(uid,)).fetchone():
                return 'Предыдущее действие требует проверки руководителем. Новые действия пока остановлены.'
            db.execute('INSERT INTO inbox VALUES(?,?,NULL)',(uid,event_key))
        try:
            reply = self._route(uid,text.strip(),event_key)
        except DomainError as exc:
            reply = exc.message+'\nИсправьте команду и отправьте новым сообщением. /help'
        except (ValueError,IndexError):
            reply = 'Неверный формат. Исправьте команду и отправьте заново. /help'
        except Exception:
            return 'Не удалось закончить обработку. Руководителю нужно проверить результат перед повтором.'
        reply = self._limit(reply)
        with self._db() as db:
            db.execute('UPDATE inbox SET reply=? WHERE uid=? AND event=?',(reply,uid,event_key))
        return reply

    @staticmethod
    def _limit(text):
        suffix = '\n…Показана сокращённая версия. Команды: /help'
        return text if len(text)<=3900 else text[:3900-len(suffix)]+suffix

    def _session(self,uid,mode,role):
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)',(uid,mode,role))

    @staticmethod
    def _time(value):
        value = datetime.fromisoformat(value.strip())
        return (value.replace(tzinfo=CHITA) if value.tzinfo is None else value).isoformat()

    @staticmethod
    def _local(value):
        return datetime.fromisoformat(value).astimezone(CHITA).strftime('%d.%m.%Y %H:%M')

    def _route(self,uid,text,key):
        parts = text.split(maxsplit=1)
        cmd, arg = (parts[0].lower() if parts else '/help'), (parts[1] if len(parts)>1 else '')
        if cmd == '/start': return 'Привет! Это Внеурок. Здесь можно предложить школьное событие, собрать команду и договориться с педагогом.\nПопробуйте /demo: пройдите путь от идеи до итога на личном учебном примере. Настоящих школьных данных в нём нет.\nДля своей школы: /school. Все команды: /help'
        if cmd == '/help': return HELP
        if cmd == '/demo':
            self._session(uid,'demo','author')
            return 'Учебный пример только для вас, настоящих школьных данных здесь нет. Вы автор. /list\nСмена героя: /role helper, /role participant, /role curator'
        if cmd == '/school':
            if self.school_store is None: return 'Школьный режим ещё не подключён. Попробуйте /demo'
            self.school_store.max_user(uid)
            self._session(uid,'school','')
            return 'Школьный режим. Права назначает школа. /list'
        with self._db() as db:
            session = db.execute('SELECT mode,role FROM sessions WHERE uid=?',(uid,)).fetchone()
        if not session: return 'Сначала выберите /demo или /school.'
        demo, role = session[0]=='demo', session[1]
        if cmd == '/role':
            if not demo: return 'В школе роли назначает администратор. Смена героя доступна только в /demo.'
            if arg not in ROLES: return 'Выберите /role author|helper|participant|curator'
            self._session(uid,'demo',arg)
            return f'Учебный герой: {HERO[arg]}. /list'
        if demo:
            store = Store(self.root/(hashlib.sha256(uid.encode()).hexdigest()+'.sqlite3'))
            store.seed_demo()
            actor = ROLES[role]
        else:
            store = self.school_store
            actor = store.max_user(uid)['id']
        if cmd=='/me': return 'Ваш школьный код для приглашения в команду: '+actor
        if cmd in ('/list','/my','/spaces'):
            data = store.bootstrap(actor,demo=demo)
            if cmd == '/spaces':
                return '\n'.join(f'{s["id"]}: {s["name"]}, мест {s["capacity"]}' for s in data['spaces'])+'\nБлижайшие часы (Чита UTC+9):\n'+'\n'.join(f'{a["space_id"]}: {self._local(a["starts_at"])} — {self._local(a["ends_at"])}' for a in data['availability'][:9])
            rows = [r for r in data['initiatives'] if cmd=='/list' or r['owner_id']==actor or r['my_registration'] or r['my_following'] or r['my_membership'] or any(t['assignee_id']==actor for t in r['tasks'])]
            page = '\n'.join(f'{r["title"]} [{STATUS.get(r["status"], "В работе")}]\n/view {r["id"]}' for r in rows[:10])
            if len(rows)>10: page += f'\nЕщё {len(rows)-10} инициатив не показано. Свои записи: /my. Знакомую карточку можно открыть через /view ID.'
            return page or 'Пока ничего нет. /new — формат в /help'
        if cmd == '/new':
            fields = [p.strip() for p in arg.split('|')]
            if len(fields)!=7: return 'Создайте идею одной командой по образцу из /help. Помещения и часы: /spaces'
            title,description,kind,space,start,end,cap = fields
            row = store.create(actor,dict(title=title,description=description,kind=kind,space_id=space,starts_at=self._time(start),ends_at=self._time(end),capacity=int(cap)),hashlib.sha256((uid+key).encode()).hexdigest())
            return self._card(row,store.user(actor))
        supported = {'/view','/edit','/task','/claim','/done','/submit','/approve','/changes','/open','/join','/leave','/cancel','/result','/attend','/demo_end','/confirm','/follow','/unfollow','/invite','/remove','/release'}
        if cmd not in supported: return 'Неизвестная команда. /help'
        if cmd=='/demo_end' and not demo: return 'Показ завершения доступен только в личном /demo.'
        args = arg.split(maxsplit=1)
        if not args: return 'Укажите ID из /list. Формат: /help'
        iid,tail = args[0],args[1] if len(args)>1 else ''
        if cmd=='/unfollow':
            store.unsubscribe(actor,iid)
            return 'Подписка отключена. Если вы в команде, сообщения по вашей роли сохраняются. /list'
        row = store.detail(iid,actor)
        raw = {'version':row['version']}
        if cmd == '/view': return self._card(row,store.user(actor))
        if cmd == '/demo_end':
            if row['status'] not in ('approved','registration'): return 'Сначала согласуйте учебный пример и откройте запись.'
            now = datetime.now(timezone.utc)
            with store.connection(True) as db:
                db.execute('UPDATE initiatives SET starts_at=?,ends_at=? WHERE id=?',((now-timedelta(hours=2)).isoformat(timespec='seconds'),(now-timedelta(hours=1)).isoformat(timespec='seconds'),iid))
                store._history(db,row,store.user(actor,db),'demo_time_shift','Показ завершения на учебном примере. Это не настоящее проведённое событие.')
            return 'Только учебный пример: время перенесено для показа завершения. Это не настоящее проведённое событие.\n'+self._card(store.detail(iid,actor))+'\nТеперь педагог: /attend '+iid+' student-visitor да\nАвтор: /result '+iid+' Подвели итоги учебного примера.'
        if cmd == '/edit':
            field,value = [p.strip() for p in tail.split('|',1)]
            if field not in ('title','description','space_id','starts_at','ends_at','capacity'): return 'Допустимые поля: /help'
            raw[field] = int(value) if field=='capacity' else self._time(value) if field in ('starts_at','ends_at') else value
            row = store.edit(actor,iid,raw)
        elif cmd in ('/task','/claim','/done','/release'):
            if cmd == '/task':
                raw['title'] = tail
                row = store.task(actor,iid,raw)
            else:
                raw['action'] = {'/claim':'claim','/done':'complete','/release':'release'}[cmd]
                row = store.task(actor,iid,raw,tail.strip())
        else:
            raw['action'] = {'/submit':'submit','/approve':'approve','/changes':'request_changes','/open':'open_registration','/join':'register','/leave':'unregister','/cancel':'cancel','/result':'submit_result','/attend':'attendance','/confirm':'complete','/follow':'follow','/unfollow':'unfollow','/invite':'invite','/remove':'remove_member'}[cmd]
            if cmd in ('/invite','/remove'): raw['user_id'] = tail.strip()
            if cmd == '/changes': raw['note'] = tail
            if cmd == '/result': raw['result'] = tail
            if cmd == '/attend':
                who,present = tail.split()
                if present not in ('да','нет'): raise ValueError()
                raw.update(user_id=who,attended=present=='да')
            row = store.action(actor,iid,raw)
        if row.get('removed_from_card'): return 'Действие сохранено. Карточка больше недоступна. /list'
        return self._card(row,store.user(actor))

    def _card(self,r,actor=None):
        iid = r['id']
        steps = {'draft':f'/submit {iid}', 'needs_changes':f'/edit {iid} description | исправленное описание', 'submitted':f'/approve {iid} (педагог)', 'approved':f'/open {iid} (автор)', 'registration':f'/join {iid}', 'awaiting_confirmation':f'/confirm {iid} (педагог, после отметки каждого участника)', 'completed':'/list', 'cancelled':'/list'}
        if r['status']=='approved' and r['kind']=='team':
            steps['approved'] = f'/result {iid} ваш итог (после окончания встречи команды)'
        if actor:
            own=r['owner_id']==actor['id']; adult=actor['role']=='curator'
            if r['status'] in ('draft','needs_changes') and not own:
                steps[r['status']]=f'/claim {iid} TASK — взять свободную задачу'
            if r['status']=='submitted' and not adult:
                steps['submitted']='Ожидаем решения педагога'
            if r['status']=='approved' and not own:
                steps['approved']='Условия согласованы; автор готовит проведение' if adult else f'/follow {iid} — получать изменения' if r['kind']=='event' else 'Подготовка встречи команды'
            if r['status']=='registration':
                steps['registration']='После начала отметьте каждого участника: /attend '+iid+' USER да|нет' if adult else 'Готовьтесь к событию; запись уже подтверждена' if r['my_registration'] else f'/join {iid}'
            if r['status'] in ('approved','registration') and datetime.fromisoformat(r['ends_at'])<=datetime.now(timezone.utc):
                steps[r['status']]=f'/result {iid} ваш итог' if own else 'Ожидаем итог автора; педагог проверяет посещаемость'
            elif r['status']=='registration' and datetime.fromisoformat(r['starts_at'])<=datetime.now(timezone.utc) and not adult:
                steps['registration']='Событие уже началось; новая запись закрыта'
            if r['status']=='awaiting_confirmation' and not adult:
                steps['awaiting_confirmation']='Педагог проверяет посещаемость и итог'
        def short(value, maximum):
            return value if len(value)<=maximum else value[:maximum-1]+'…'
        lines = [r['title'], short(r['description'],500), f'ID: {iid}',
                 f'Статус: {STATUS.get(r["status"], "В работе")}',
                 f'{r["space_name"]}: {self._local(r["starts_at"])} — {self._local(r["ends_at"])} (Чита UTC+9)',
                 f'Команда {r.get("team_count",1)}/{r["capacity"]}' if r['kind']=='team' else f'Записано {r["registration_count"]}/{r["capacity"]}',
                 'Комментарий: '+short(r['review_note'] or 'нет',300),
                 'Итог: '+short(r['result'] or 'пока нет',500), 'Задачи:']
        for t in r['tasks'][:10]:
            state = 'готово' if t['done'] else 'в работе' if t['assignee_id'] else 'свободно'
            lines.append(f'{t["id"]}: {short(t["title"],65)} [{state}]')
        if not r['tasks']: lines.append('нет')
        if len(r['tasks'])>10: lines.append(f'Ещё задач: {len(r["tasks"])-10}. Здесь показаны первые 10.')
        lines.append('Участники: '+short(', '.join(p['user_id']+' ('+('пришёл' if p['attended'] else 'не пришёл')+')' if p.get('attendance_recorded') else p['user_id']+' (не отмечен)' for p in r['registrations']) or 'нет',250))
        if r.get('my_following'): lines.append('Получаете изменения. Отписка без активной записи: /unfollow '+iid)
        if r.get('members'): lines.append('Приглашённая команда: '+', '.join(m['user_id'] for m in r['members']))
        lines.extend([f'Следующий шаг: {steps.get(r["status"],"/list")}', 'Все команды: /help'])
        return self._limit('\n'.join(lines))
