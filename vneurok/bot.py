"""Команды школьного бота. Ответ возвращается вызывающему коду."""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import json
import uuid
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from .core import Store, DomainError

ROLES = dict(author='student-author', helper='student-helper', participant='student-visitor', curator='curator')
CHITA = timezone(timedelta(hours=9), 'Asia/Chita')
STATUS = dict(draft='Черновик', submitted='На согласовании', needs_changes='Нужна доработка', approved='Согласовано', registration='Запись открыта', awaiting_confirmation='Итог ждёт подтверждения', completed='Завершено', cancelled='Отменено', deferred='Отложено', declined='Отклонено')
HERO = dict(author='Автор идеи', helper='Помощник', participant='Участник', curator='Педагог')
HELP = '''Внеурок: идея → команда → педагог → проведение → итог.
/demo — личный учебный пример; /school — школа по приглашению.
/new — коротко сохранить идею без дат и помещения.
/list — список с номерами; /my — мои идеи; /view 1 — открыть.
После открытия карточки ID не нужен: /menu — доступные действия.
/plan — заполнить условия; /invite — пригласить помощника; /task — задача.
/interests — темы и уведомления; /spaces — помещения и правила.
/resume — продолжить пошаговый ввод; /back — назад; /cancel — прервать ввод.
Даты в боте: Чита, Asia/Chita (UTC+9).
Старые команды тоже работают с ID или номером:
/new название | описание | event или team | помещение | ГГГГ-ММ-ДД ЧЧ:ММ | ГГГГ-ММ-ДД ЧЧ:ММ | мест
/submit /approve /changes /defer /decline /open /join /leave /follow /unfollow
/invite ID USER /remove ID USER /task ID название /claim ID TASK /done ID TASK /release ID TASK
/result ID итог /attend ID USER да|нет /confirm ID /cancel ID
/edit ID поле | значение (title, description, space_id, starts_at, ends_at, capacity)
/me — школьный код; /role author|helper|participant|curator — только демо.
/demo_end ID — учебное завершение, не настоящее событие.'''


def attendance_roster(row):
    return row.get('team_attendance',[]) if row.get('kind')=='team' else row.get('registrations',[])


class BotRouter:
    def __init__(self, demo_root, school_store=None):
        self.root = Path(demo_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.school_store = school_store
        self.path = self.root / 'router.sqlite3'
        self._reply_trace=threading.local()
        with self._db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS sessions(uid TEXT PRIMARY KEY, mode TEXT, role TEXT); CREATE TABLE IF NOT EXISTS inbox(uid TEXT,event TEXT,reply TEXT,PRIMARY KEY(uid,event)); CREATE TABLE IF NOT EXISTS guided(uid TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL); CREATE TABLE IF NOT EXISTS inbox_access(uid TEXT,event TEXT,scope TEXT NOT NULL,PRIMARY KEY(uid,event)); CREATE TABLE IF NOT EXISTS reply_cards(uid TEXT,event TEXT,cards TEXT NOT NULL,PRIMARY KEY(uid,event));')

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
        initial_scope=self._school_scope(uid)
        # Сохраняем начало обработки, чтобы после сбоя не выполнить действие дважды.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT reply FROM inbox WHERE uid=? AND event=?',(uid,event_key)).fetchone()
            if prior:
                access=db.execute('SELECT scope FROM inbox_access WHERE uid=? AND event=?',(uid,event_key)).fetchone()
                if (access and access[0] and access[0]!=initial_scope) or (not access and prior[0] is not None):
                    return 'Доступ изменился. Старый ответ скрыт; действие повторно не выполнено. /school'
                guard=db.execute('SELECT cards FROM reply_cards WHERE uid=? AND event=?',(uid,event_key)).fetchone()
                if guard and not self._cards_allowed(uid,json.loads(guard[0])):
                    return 'Карточка или доступ изменились. Старый ответ скрыт; действие повторно не выполнено. /list'
                return self._limit(prior[0]) if prior[0] is not None else 'Результат предыдущего действия уточняется. Нужна проверка руководителя.'
            if db.execute('SELECT 1 FROM inbox WHERE uid=? AND reply IS NULL',(uid,)).fetchone():
                return 'Предыдущее действие требует проверки руководителем. Новые действия пока остановлены.'
            db.execute('INSERT INTO inbox VALUES(?,?,NULL)',(uid,event_key))
        self._reply_trace.cards={}
        try:
            reply = self._route(uid,text.strip(),event_key)
        except DomainError as exc:
            reply = exc.message+('\nПрервите устаревший ввод: /cancel. Затем /view — обновить карточку, /menu — повторить выбор.' if exc.code=='stale_version' else '\nВ пошаговом вводе ответьте ещё раз: /resume. Команды: /help.')
        except (ValueError,IndexError):
            reply = 'Неверный формат. Исправьте команду и отправьте заново. /help'
        except Exception:
            return 'Не удалось закончить обработку. Руководителю нужно проверить результат перед повтором.'
        current_scope=self._school_scope(uid)
        cards=[{'id':iid,'version':version} for iid,version in self._reply_trace.cards.items()] if current_scope else []
        if initial_scope and current_scope and initial_scope!=current_scope:
            reply='Доступ изменился во время обработки. Ответ скрыт; проверьте результат с администратором. /school'
        if cards and not self._cards_allowed(uid,cards):
            reply='Карточка или доступ изменились во время обработки. Обновите данные: /list.'
        reply = self._limit(reply)
        with self._db() as db:
            db.execute('UPDATE inbox SET reply=? WHERE uid=? AND event=?',(reply,uid,event_key))
            db.execute('INSERT OR REPLACE INTO inbox_access VALUES(?,?,?)',(uid,event_key,current_scope))
            db.execute('INSERT OR REPLACE INTO reply_cards VALUES(?,?,?)',(uid,event_key,json.dumps(cards)))
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
        return datetime.fromisoformat(value).astimezone(CHITA).strftime('%d.%m.%Y %H:%M') if value else 'пока не выбрано'

    def _record_card(self,row):
        trace=getattr(self._reply_trace,'cards',None)
        if trace is not None:trace[row['id']]=row['version']

    def _cards_allowed(self,uid,cards):
        if not cards:return True
        if self.school_store is None:return False
        try:
            actor=self.school_store.max_user(uid)
            for card in cards:
                current=self.school_store.detail(card['id'],actor['id'])
                if current['version']!=card['version']:return False
            return True
        except DomainError:return False

    def reply_guard(self,max_user_id,event_key):
        """Exact school-card snapshots used by a reply, for delayed transport checks."""
        with self._db() as db:
            row=db.execute('SELECT cards FROM reply_cards WHERE uid=? AND event=?',(str(max_user_id),event_key)).fetchone()
            access=db.execute('SELECT scope FROM inbox_access WHERE uid=? AND event=?',(str(max_user_id),event_key)).fetchone()
        return {'cards':json.loads(row[0]) if row else [],'access':json.loads(access[0]) if access and access[0].startswith('{') else None}

    def _school_scope(self,uid):
        with self._db() as db:
            row=db.execute('SELECT mode FROM sessions WHERE uid=?',(uid,)).fetchone()
        if not row or row[0]!='school' or self.school_store is None:return ''
        try:
            actor=self.school_store.max_user(uid)
        except DomainError:return 'school:blocked'
        return json.dumps({k:actor.get(k) for k in ('id','school_id','role','class_id','is_admin','active','access_version')},sort_keys=True)

    def _identity(self, uid):
        with self._db() as db:
            session = db.execute('SELECT mode,role FROM sessions WHERE uid=?',(uid,)).fetchone()
        if not session:
            return None
        demo = session[0] == 'demo'
        if demo:
            store = Store(self.root/(hashlib.sha256(uid.encode()).hexdigest()+'.sqlite3'))
            store.seed_demo()
            actor = ROLES[session[1]]
        else:
            store = self.school_store
            if store is None:raise DomainError('school_unavailable','Школьный режим сейчас не подключён. Можно выбрать /demo.',503)
            actor = store.max_user(uid)['id']
        return store, actor, demo, session[0]+':'+actor+('' if demo else ':'+str(store.user(actor).get('access_version',1)))

    def _state(self, uid, scope):
        with self._db() as db:
            row = db.execute('SELECT state FROM guided WHERE uid=? AND scope=?',(uid,scope)).fetchone()
        return json.loads(row[0]) if row else {}

    def _save(self, uid, scope, state):
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO guided VALUES(?,?,?)',(uid,scope,json.dumps(state,ensure_ascii=False)))

    def _resolve(self, token, state):
        if not token or token == '.':
            if not state.get('current'):
                raise DomainError('choose_card','Сначала откройте карточку: /list и её номер.')
            return state['current'],state.get('version')
        if token.isdecimal():
            rows = state.get('cards',[])
            n = int(token)-1
            if not 0 <= n < len(rows):
                raise DomainError('choose_card','Такого номера нет. Обновите список: /list.')
            return rows[n]['id'],rows[n]['version']
        return token,None

    def _show_card(self, uid, scope, state, row, actor, store=None):
        state.update(current=row['id'],version=row['version'],selection='card')
        state.pop('menu',None)
        self._save(uid,scope,state)
        display=dict(row)
        if store is not None:
            data=store.bootstrap(actor['id'])
            display['interest_name']=next((t['name'] for t in data['interests'] if t['id']==row.get('interest_id')),'Общие интересы')
            display['audience_name']=next((c['name'] for c in data['classes'] if c['id']==row.get('audience_value')),row.get('audience_value',''))
        text = self._card(display,actor)
        # In the guided flow the current card is remembered. UUIDs remain accepted.
        text = '\n'.join(line for line in text.splitlines() if not line.startswith('ID: ')).replace(' '+row['id'],'')
        return text+'\n/menu — выбрать действие номером; /list — все идеи.'

    def _route(self, uid, text, key):
        parts=text.split(maxsplit=1)
        cmd=(parts[0].lower() if parts else '/help'); arg=parts[1] if len(parts)>1 else ''
        aliases={'новая идея':'/new','все идеи':'/list','моё участие':'/my','мои интересы':'/interests','помощь':'/help','идеи':'/list','мои идеи':'/my','назад':'/back','отмена':'/cancel','продолжить':'/resume','меню':'/menu'}
        cmd=aliases.get(text.lower(),cmd)
        if text.lower() in aliases: arg=''
        if cmd in ('/identity',):
            return f'Ваш MAX ID: {uid}. Передайте его администратору школы для именного приглашения. Это не регистрация и не назначение роли.'
        if cmd in ('/school','/join_school') and arg:
            if self.school_store is None: return 'Школьный режим ещё не подключён. Попробуйте /demo.'
            from .onboarding import redeem_invitation
            user=redeem_invitation(self.school_store,int(uid),arg.strip())
            self._session(uid,'school','')
            return 'Приглашение принято. '+user['name']+' · '+('педагог-куратор' if user['role']=='curator' else 'ученик')+' · класс '+user['class_id']+'. Права назначены школой.\n/list — идеи; /new — предложить свою; /interests — выбрать уведомления.'
        if cmd=='/school' and not arg and self.school_store is not None:
            try:
                self.school_store.max_user(uid)
            except DomainError as error:
                if error.code!='school_invitation_required': raise
                self._session(uid,'onboarding','')
                return f'Ваш MAX ID: {uid}. Аккаунт ещё не включён в список школы. Передайте ID администратору и получите личный код приглашения.\nПришлите код следующим сообщением или /school КОД. Роль и класс уже определяет школа. /cancel — выйти.'
        with self._db() as db:
            session=db.execute('SELECT mode FROM sessions WHERE uid=?',(uid,)).fetchone()
        if session and session[0]=='onboarding':
            if cmd=='/cancel':
                with self._db() as db: db.execute('DELETE FROM sessions WHERE uid=?',(uid,))
                return 'Подключение прервано. Ваши права не изменились. /demo или /school.'
            if not text.startswith('/'):
                return self._route(uid,'/school '+text,key)
            if cmd=='/me': return self._route(uid,'/identity',key)
        if cmd in ('/demo','/school','/role'):
            with self._db() as db: db.execute('DELETE FROM guided WHERE uid=?',(uid,))
        if cmd in ('/start','/help','/demo','/school','/role'):
            return self._legacy(uid,cmd+(' '+arg if arg else ''),key)
        identity=self._identity(uid)
        if not identity:
            if cmd=='/me': return self._route(uid,'/identity',key)
            return 'Сначала выберите /demo или /school. Ваш MAX ID: /identity.'
        store,actor,demo,scope=identity
        state=self._state(uid,scope)
        wizard=state.get('wizard')
        if cmd in ('/back','/resume') or (cmd=='/cancel' and not arg):
            if not wizard: return 'Пошаговый ввод не начат. /new — идея; /list — выбрать существующую.'
            if cmd=='/cancel':
                state.pop('wizard');self._save(uid,scope,state)
                return 'Ввод остановлен. Несохранённые ответы удалены, существующая карточка не изменена. /my'
            if cmd=='/back':
                history=wizard.get('history',[])
                if history:
                    old=history.pop();wizard.update(old);wizard['history']=history
                    self._save(uid,scope,state)
            return self._question(uid,scope,state,store,actor)
        if wizard and not text.startswith('/') and text.lower() not in aliases:
            return self._advance(uid,scope,state,text,store,actor,key)
        if wizard and cmd not in ('/help','/me','/spaces'):
            return 'Есть незавершённый ввод. /resume — продолжить; /back — назад; /cancel — выйти без изменения карточки.'
        if text.isdecimal():
            if state.get('selection')=='menu':
                n=int(text)-1; items=state.get('menu',[])
                if not 0<=n<len(items): return 'Выберите номер действия из меню или /menu.'
                return self._route(uid,items[n]['command'],key)
            if state.get('selection')=='interests':
                n=int(text)-1; topics=state.get('topics',[])
                if not 0<=n<len(topics): return 'Выберите номер темы из /interests.'
                subscribed=store.bootstrap(actor,demo)['interest_subscriptions']
                topic=topics[n]
                wizard={'flow':'interest','step':'confirm','interest_id':topic['id'],'name':topic['name'],'enabled':topic['id'] not in subscribed,'history':[]}
                state['wizard']=wizard;self._save(uid,scope,state)
                return self._question(uid,scope,state,store,actor)
            cmd,arg='/view',text
        if cmd in ('/list','/my','/review'):
            data=store.bootstrap(actor,demo)
            rows=[r for r in data['initiatives'] if cmd=='/list' or (cmd=='/review' and r['status']=='submitted') or (cmd=='/my' and (r['owner_id']==actor or r['my_registration'] or r['my_following'] or r['my_membership'] or any(t['assignee_id']==actor for t in r['tasks'])))]
            try: page=max(1,int(arg or 1))
            except ValueError: return 'Номер страницы: /list 2.'
            pages=max(1,(len(rows)+9)//10)
            if page>pages:return f'Всего страниц: {pages}. /list'
            visible=rows[(page-1)*10:page*10]
            for row in visible:self._record_card(row)
            state.update(cards=[dict(id=r['id'],version=r['version']) for r in visible],selection='cards')
            self._save(uid,scope,state)
            lines=[f'{n}. {r["title"]} [{STATUS.get(r["status"],"В работе")}]\n/view {n}' for n,r in enumerate(visible,1)]
            if len(rows)>page*10:lines.append(f'Ещё {len(rows)-page*10} инициатив. Следующая страница: {cmd} {page+1}')
            return '\n'.join(lines)+f'\nОтправьте номер или /view 1. Страница {page}/{pages}. /new — новая идея.' if lines else 'Пока ничего нет. /new — предложить идею пошагово.'
        if cmd=='/spaces':
            data=store.bootstrap(actor,demo);lines=['Пространства школы. Время: Чита UTC+9.']
            for n,space in enumerate(data['spaces'],1):
                lines.extend([f'{n}. {space["name"]} · до {space["capacity"]} человек', 'Оборудование: '+(', '.join(space.get('equipment',[])) or 'не указано'), 'Правила: '+(space.get('access_rules') or 'уточните у куратора'), 'Ответственный: '+(space.get('responsible_name') or space.get('responsible_id') or 'не назначен')])
                slots=[a for a in data['availability'] if a['space_id']==space['id']]
                lines.extend(self._local(a['starts_at'])+' — '+self._local(a['ends_at']) for a in slots[:3])
                if not slots:lines.append('Доступных интервалов пока нет.')
                busy=[a for a in data.get('busy_slots',[]) if a['space_id']==space['id']]
                lines.extend('Занято: '+self._local(a['starts_at'])+' — '+self._local(a['ends_at']) for a in busy[:3])
            return '\n'.join(lines)+'\nМожно провести инициативу без помещения. /plan — выбрать условия.'
        if cmd=='/interests':
            data=store.bootstrap(actor,demo);state['selection']='interests';state['topics']=data['interests'];self._save(uid,scope,state)
            lines=['Темы и уведомления. Подписка добровольная; номер темы включает или отключает её после подтверждения.']
            lines.extend(f'{n}. {t["name"]}: '+('включены' if t['id'] in data['interest_subscriptions'] else 'выключены') for n,t in enumerate(data['interests'],1))
            return '\n'.join(lines)+'\nУведомления о своих записях и команде сохраняются по вашей роли.'
        if cmd=='/new' and '|' not in arg:
            wizard={'flow':'create','step':'title','data':{'kind':'event','needs_space':True,'audience_type':'school','audience_value':''},'key':uuid.uuid4().hex,'history':[]}
            state['wizard']=wizard;self._save(uid,scope,state)
            if arg:return self._advance(uid,scope,state,arg,store,actor,key)
            return self._question(uid,scope,state,store,actor)
        args=arg.split(maxsplit=1)
        # No IDs are needed for actions on the last opened card.
        supported={'/view','/menu','/plan','/edit','/task','/claim','/done','/release','/submit','/approve','/changes','/defer','/decline','/open','/join','/leave','/cancel','/result','/attend','/confirm','/follow','/unfollow','/invite','/remove','/demo_end'}
        if cmd not in supported:
            return self._legacy(uid,cmd+(' '+arg if arg else ''),key)
        iid,version=self._resolve(args[0] if args else '',state)
        tail=args[1] if len(args)>1 else ''
        if cmd=='/unfollow':
            store.unsubscribe(actor,iid)
            return 'Подписка отключена. Сообщения по вашей роли в команде сохраняются. /list'
        row=store.detail(iid,actor)
        if cmd=='/view':return self._show_card(uid,scope,state,row,store.user(actor),store)
        if cmd=='/menu':return self._menu(uid,scope,state,row,store.user(actor))
        if cmd=='/plan' or (cmd=='/edit' and not tail):
            if row['owner_id']!=actor:raise DomainError('forbidden','Условия заполняет автор инициативы.',403)
            wizard={'flow':'plan','step':'audience','iid':iid,'version':version if version is not None else row['version'],'status':row['status'],'data':{},'history':[]}
            state['wizard']=wizard;self._save(uid,scope,state)
            return self._question(uid,scope,state,store,actor)
        if cmd=='/edit' and tail and row['status'] in ('submitted','approved','registration'):
            field,value=[v.strip() for v in tail.split('|',1)]
            if field not in ('title','description','space_id','starts_at','ends_at','capacity'):
                return 'Допустимые поля: title, description, space_id, starts_at, ends_at, capacity.'
            value=int(value) if field=='capacity' else self._time(value) if field in ('starts_at','ends_at') else value
            state['wizard']={'flow':'edit','step':'confirm','iid':iid,'version':version if version is not None else row['version'],'data':{field:value},'history':[]}
            self._save(uid,scope,state)
            return self._question(uid,scope,state,store,actor)
        dialog_steps={'/invite':'person','/remove':'person','/task':'text','/changes':'text','/defer':'text','/decline':'text','/result':'text','/attend':'person','/claim':'task','/done':'task','/release':'task'}
        if cmd in dialog_steps and not tail:
            state['wizard']={'flow':'action','step':dialog_steps[cmd],'command':cmd,'iid':iid,'version':version if version is not None else row['version'],'data':{},'history':[]}
            self._save(uid,scope,state)
            return self._question(uid,scope,state,store,actor)
        if cmd=='/cancel':
            state['wizard']={'flow':'action','step':'confirm','command':cmd,'iid':iid,'version':version if version is not None else row['version'],'data':{},'history':[]}
            self._save(uid,scope,state)
            return self._question(uid,scope,state,store,actor)
        # Legacy command syntax remains supported; numeric/current selection uses its displayed version.
        reply=self._legacy(uid,cmd+' '+iid+(' '+tail if tail else ''),key,version)
        if cmd=='/demo_end' or ('ID: '+iid not in reply):return reply
        try:
            fresh=store.detail(iid,actor)
            return self._show_card(uid,scope,state,fresh,store.user(actor),store)
        except DomainError:
            return reply

    def _menu(self,uid,scope,state,row,actor):
        self._record_card(row)
        own=row['owner_id']==actor['id'];adult=actor['role']=='curator';status=row['status']
        before=not row.get('starts_at') or datetime.fromisoformat(row['starts_at'])>datetime.now(timezone.utc)
        ended=bool(row.get('ends_at')) and datetime.fromisoformat(row['ends_at'])<=datetime.now(timezone.utc)
        items=[]
        def add(label,command):items.append(dict(label=label,command=command+' .'))
        if own and status in ('draft','needs_changes','deferred','approved','registration') and before:add('Заполнить условия','/plan')
        if own and status in ('draft','needs_changes','deferred'):add('Отправить педагогу','/submit')
        if own and status not in ('declined','cancelled','completed','awaiting_confirmation') and before:
            add('Пригласить помощника','/invite');add('Добавить задачу','/task')
        if status not in ('declined','cancelled','completed') and row.get('tasks') and not adult:
            add('Взять задачу','/claim');add('Завершить задачу','/done');add('Освободить задачу','/release')
        if adult and status=='submitted' and not own:
            add('Согласовать','/approve');add('Вернуть на доработку','/changes');add('Отложить с причиной','/defer');add('Отклонить с причиной','/decline')
        if own and row['kind']=='event' and status=='approved' and before:add('Открыть запись','/open')
        if not adult and row['kind']=='event' and status=='registration' and before:add('Отменить мою запись' if row.get('my_registration') else 'Записаться','/leave' if row.get('my_registration') else '/join')
        if not adult and row['kind']=='event' and status in ('approved','registration') and not row.get('my_registration'):add('Отключить подписку' if row.get('my_following') else 'Подписаться на изменения','/unfollow' if row.get('my_following') else '/follow')
        if own and status in ('approved','registration','awaiting_confirmation') and ended:add('Сохранить итог','/result')
        if adult and status in ('approved','registration','awaiting_confirmation') and not before and attendance_roster(row):add('Отметить пришёл / не пришёл','/attend')
        if adult and status=='awaiting_confirmation' and not own:add('Подтвердить проведение','/confirm')
        if (own or adult) and status not in ('declined','cancelled','completed'):add('Отменить инициативу','/cancel')
        state.update(current=row['id'],version=row['version'],menu=items,selection='menu');self._save(uid,scope,state)
        return row['title']+'\n'+('\n'.join(f'{n}. {v["label"]}' for n,v in enumerate(items,1)) or 'Сейчас действий нет. Ожидайте следующего этапа.')+'\nОтправьте номер. /view — карточка; /list — список.'

    def _question(self,uid,scope,state,store,actor):
        prompt=self._prompt(state['wizard'],store,actor)
        self._save(uid,scope,state)
        return prompt

    def _prompt(self,w,store,actor):
        flow,step=w['flow'],w['step'];data=w.get('data',{});footer='\n/back — назад; /cancel — выйти без сохранения; /resume — повторить вопрос.'
        if flow=='interest':
            return ('Включить' if w['enabled'] else 'Выключить')+' уведомления по теме «'+w['name']+'»? Пришлите «да» или «нет».'+footer
        if flow=='create':
            prompts={'title':'Как назовём идею? 3–100 символов. Даты и помещение можно выбрать позже.', 'description':'Коротко опишите, что хотите сделать. 10–2000 символов.', 'kind':'Формат: 1 — событие для записи; 2 — закрытая команда. Пришлите номер.', 'confirm':'Сохранить черновик?\n'+data.get('title','')+'\n'+data.get('description','')+'\nФормат: '+('закрытая команда' if data.get('kind')=='team' else 'событие')+'\nБез дат, помещения и публикации. После сохранения можно пригласить помощника. Пришлите «да».'}
            return prompts[step]+footer
        if flow=='edit':
            row=store.detail(w['iid'],actor);self._record_card(row)
            return 'Изменить условия «'+row['title']+'»?\n'+'\n'.join(k+': '+str(v) for k,v in data.items())+'\nПрежнее согласование и записи будут отменены. Потребуются новое согласование и повторная запись участников.\nПришлите «да» для сохранения.'+footer
        if flow=='plan':
            self._record_card(store.detail(w['iid'],actor))
            boot=store.bootstrap(actor);classes=boot.get('classes',[])
            if boot['user']['role']=='student':classes=[c for c in classes if c['id']==boot['user']['class_id']]
            if step=='audience':prompt='Кому предназначена идея? 1 — вся школа; 2 — параллель; 3 — класс.'
            elif step=='audience_value':
                values=sorted({str(c['parallel']) for c in classes}) if data.get('audience_type')=='parallel' else [str(c['id']) for c in classes]
                w['choices']=values
                labels=values if data.get('audience_type')=='parallel' else [str(c['name']) for c in classes]
                prompt='Выберите номер аудитории:\n'+'\n'.join(f'{n}. {v}' for n,v in enumerate(labels,1))
            elif step=='interest':
                w['choices']=boot['interests'];prompt='Тема инициативы:\n0. Общее\n'+'\n'.join(f'{n}. {t["name"]}' for n,t in enumerate(boot['interests'],1))
            elif step=='needs_space':prompt='Нужно помещение? 1 — да, выбрать; 2 — без помещения.'
            elif step=='space':
                w['choices']=boot['spaces'];prompt='Выберите номер помещения:\n'+'\n'.join(f'{n}. {s["name"]} · до {s["capacity"]}; '+(', '.join(s.get('equipment',[])) or 'оборудование не указано') for n,s in enumerate(boot['spaces'],1))+'\nПравила и интервалы: /spaces.'
            elif step=='start':
                slots=[a for a in boot['availability'] if a['space_id']==data.get('space_id')]
                prompt='Когда начнём? ГГГГ-ММ-ДД ЧЧ:ММ, Чита UTC+9.'
                if slots:prompt+='\nОкна: '+'; '.join(self._local(a['starts_at'])+' — '+self._local(a['ends_at']) for a in slots[:3])
            elif step=='end':prompt='Когда закончим? ГГГГ-ММ-ДД ЧЧ:ММ, Чита UTC+9; не позже чем через 6 часов.'
            elif step=='capacity':prompt='Сколько человек максимум? Для закрытой команды автор тоже занимает место. Для события в помещении оставьте места для команды организаторов и ответственного педагога.'
            else:
                space=next((s['name'] for s in boot['spaces'] if s['id']==data.get('space_id')),'Без помещения')
                prompt='Сохранить условия?\nАудитория: '+{'school':'вся школа','parallel':'параллель','class':'класс'}.get(data.get('audience_type'),'')+' '+data.get('audience_value','')+'\n'+space+'\n'+self._local(data.get('starts_at'))+' → '+self._local(data.get('ends_at'))+'\nМест: '+str(data.get('capacity',''))
                if w.get('status') in ('submitted','approved','registration'):prompt+='\nВнимание: прежнее согласование и записи отменятся. Потребуется новое согласование и повторная запись.'
                prompt+='\nПришлите «да» для сохранения.'
            return prompt+footer
        row=store.detail(w['iid'],actor);self._record_card(row);cmd=w['command']
        if step=='confirm':
            names={'/cancel':'Отменить инициативу и закрыть запись','/remove':'Убрать помощника из команды; он потеряет доступ, если нет другой роли','/invite':'Пригласить помощника: он увидит черновик и сможет взять задачу','/changes':'Вернуть автору на доработку','/defer':'Отложить инициативу','/decline':'Отклонить и закрыть инициативу','/result':'Передать итог педагогу','/attend':'Сохранить посещаемость','/task':'Добавить задачу','/claim':'Взять задачу','/done':'Завершить задачу','/release':'Освободить задачу'}
            details=[names.get(cmd,cmd)+'?',row['title']]
            if data.get('user_id'):details.append('Участник: '+(data.get('user_name') or data['user_id']))
            if 'attended' in data:details.append('Пришёл' if data['attended'] else 'Не пришёл')
            if data.get('text'):details.append(data['text'])
            if data.get('task_id'):details.append(next((t['title'] for t in row['tasks'] if t['id']==data['task_id']),data['task_id']))
            return '\n'.join(details)+'\nПришлите «да» для подтверждения.'+footer
        if step=='text':return {'/task':'Напишите название задачи (до 160 символов).','/changes':'Что нужно исправить? Напишите причину (3–1000 символов).','/defer':'Почему и до какого условия откладываем? 3–1000 символов.','/decline':'Почему отклоняем? 3–1000 символов. Решение закрывает инициативу.','/result':'Что получилось? Напишите итог (10–2000 символов).'}[cmd]+footer
        if step=='attendance':return 'Участник пришёл? Пришлите «да» или «нет». Отсутствие отмечается отдельно, не по умолчанию.'+footer
        if step=='task':
            w['choices']=row['tasks']
            return 'Выберите номер задачи:\n'+'\n'.join(f'{n}. {t["title"]}' for n,t in enumerate(row['tasks'],1))+footer
        if cmd=='/attend':people=attendance_roster(row)
        elif cmd=='/remove':people=row.get('members',[])
        else:people=row.get('helper_candidates',[u for u in store.bootstrap(actor)['users'] if u['id']!=actor and u['role']=='student'])
        w['choices']=people
        return ('Выберите участника' if cmd=='/attend' else 'Укажите школьный код помощника')+(' или номер из списка' if people else '')+':\n'+'\n'.join(f'{n}. {u.get("name",u.get("user_id",""))}' for n,u in enumerate(people,1))+footer

    def _advance(self,uid,scope,state,text,store,actor,key):
        w=state['wizard'];flow,step=w['flow'],w['step'];answer=text.strip();low=answer.lower()
        if step=='confirm':
            if low in ('нет','no'):
                state.pop('wizard');self._save(uid,scope,state);return 'Изменения не сохранены. /my'
            if low not in ('да','yes','сохранить'):
                return 'Для подтверждения пришлите «да»; /back — исправить; /cancel — выйти.'
            if flow=='interest':
                store.set_interest_subscription(actor,w['interest_id'],w['enabled'])
                state.pop('wizard');self._save(uid,scope,state)
                return 'Уведомления '+('включены' if w['enabled'] else 'выключены')+'. /interests'
            if flow=='create':row=store.create(actor,w['data'],w['key'])
            elif flow in ('plan','edit'):row=store.edit(actor,w['iid'],dict(w['data'],version=w['version']))
            else:return self._finish_action(uid,scope,state,store,actor)
            state.pop('wizard')
            return 'Сохранено.\n'+self._show_card(uid,scope,state,row,store.user(actor),store)
        snapshot={k:v for k,v in w.items() if k!='history'}
        snapshot=json.loads(json.dumps(snapshot))
        data=w.setdefault('data',{})
        def valid(condition,message):
            if not condition:raise DomainError('invalid_answer',message)
        def choice(values):
            valid(answer.isdecimal() and 1<=int(answer)<=len(values),'Выберите один из номеров в вопросе. /resume')
            return values[int(answer)-1]
        if flow=='create':
            if step=='title':valid(3<=len(answer)<=100,'Название: 3–100 символов.');data['title']=answer;w['step']='description'
            elif step=='description':valid(10<=len(answer)<=2000,'Описание: 10–2000 символов.');data['description']=answer;w['step']='kind'
            else:data['kind']=choice(['event','team']);w['step']='confirm'
        elif flow=='plan':
            boot=store.bootstrap(actor)
            if step=='audience':
                data['audience_type']=choice(['school','parallel','class']);data['audience_value']='';w['step']='interest' if data['audience_type']=='school' else 'audience_value'
            elif step=='audience_value':
                values=sorted({str(c['parallel']) for c in boot['classes']}) if data['audience_type']=='parallel' else [str(c['id']) for c in boot['classes']]
                data['audience_value']=choice(w.get('choices',values));w['step']='interest'
            elif step=='interest':
                data['interest_id']='general' if answer=='0' else choice(w['choices'])['id'];w['step']='needs_space'
            elif step=='needs_space':
                data['needs_space']=choice([True,False]);data['space_id']=None;w['step']='space' if data['needs_space'] else 'start'
            elif step=='space':data['space_id']=choice(w['choices'])['id'];w['step']='start'
            elif step in ('start','end'):
                value=self._time(answer);when=datetime.fromisoformat(value)
                if step=='start':valid(when>datetime.now(timezone.utc),'Начало должно быть в будущем.');data['starts_at']=value;w['step']='end'
                else:
                    start=datetime.fromisoformat(data['starts_at']);valid(start<when<=start+timedelta(hours=6),'Окончание позже начала, не больше 6 часов.');data['ends_at']=value;w['step']='capacity'
            else:
                cap=int(answer);space=next((s for s in boot['spaces'] if s['id']==data.get('space_id')),None)
                valid(1<=cap<=(space['capacity'] if space else 500),'Проверьте вместимость: минимум 1, максимум вместимость помещения (без помещения — 500).');data['capacity']=cap;w['step']='confirm'
        else:
            row=store.detail(w['iid'],actor);self._record_card(row);cmd=w['command']
            if step=='text':
                minimum,maximum=(10,2000) if cmd=='/result' else (1,160) if cmd=='/task' else (3,1000)
                valid(minimum<=len(answer)<=maximum,f'Текст: {minimum}–{maximum} символов.');data['text']=answer
                w['step']='confirm'
            elif step=='person':
                people=attendance_roster(row) if cmd=='/attend' else row.get('members',[]) if cmd=='/remove' else [u for u in store.bootstrap(actor)['users'] if u['id']!=actor and u['role']=='student']
                people=w.get('choices',people)
                person=choice(people) if answer.isdecimal() and people else None
                data['user_id']=(person.get('user_id') or person['id']) if person else answer
                data['user_name']=person.get('name','') if person else ''
                w['step']='attendance' if cmd=='/attend' else 'confirm'
            elif step=='attendance':
                valid(low in ('да','нет'),'Укажите «да» или «нет».');data['attended']=low=='да';w['step']='confirm'
            elif step=='task':data['task_id']=choice(w['choices'])['id'];w['step']='confirm'
        w.setdefault('history',[]).append(snapshot)
        self._save(uid,scope,state)
        return self._question(uid,scope,state,store,actor)

    def _finish_action(self,uid,scope,state,store,actor):
        w=state['wizard'];cmd=w['command'];data=w['data'];raw={'version':w['version']}
        if cmd in ('/task','/claim','/done','/release'):
            if cmd=='/task':raw['title']=data['text'];row=store.task(actor,w['iid'],raw)
            else:
                raw['action']={'/claim':'claim','/done':'complete','/release':'release'}[cmd]
                row=store.task(actor,w['iid'],raw,data['task_id'])
        else:
            raw['action']={'/invite':'invite','/remove':'remove_member','/changes':'request_changes','/defer':'defer','/decline':'decline','/result':'submit_result','/attend':'attendance','/cancel':'cancel'}[cmd]
            if 'user_id' in data:raw['user_id']=data['user_id']
            if cmd in ('/changes','/defer','/decline'):raw['note']=data['text']
            if cmd=='/result':raw['result']=data['text']
            if cmd=='/attend':raw['attended']=data['attended']
            row=store.action(actor,w['iid'],raw)
        state.pop('wizard')
        if row.get('removed_from_card'):
            self._save(uid,scope,state);return 'Действие сохранено. Карточка больше недоступна. /list'
        return self._show_card(uid,scope,state,row,store.user(actor),store)

    def _legacy(self,uid,text,key,expected_version=None):
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
        supported = {'/view','/edit','/task','/claim','/done','/submit','/approve','/changes','/open','/join','/leave','/cancel','/result','/attend','/demo_end','/confirm','/follow','/unfollow','/invite','/remove','/release','/defer','/decline'}
        if cmd not in supported: return 'Неизвестная команда. /help'
        if cmd=='/demo_end' and not demo: return 'Показ завершения доступен только в личном /demo.'
        args = arg.split(maxsplit=1)
        if not args: return 'Укажите ID из /list. Формат: /help'
        iid,tail = args[0],args[1] if len(args)>1 else ''
        if cmd=='/unfollow':
            store.unsubscribe(actor,iid)
            return 'Подписка отключена. Если вы в команде, сообщения по вашей роли сохраняются. /list'
        row = store.detail(iid,actor)
        raw = {'version':row['version'] if expected_version is None else expected_version}
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
            raw['action'] = {'/submit':'submit','/approve':'approve','/changes':'request_changes','/open':'open_registration','/join':'register','/leave':'unregister','/cancel':'cancel','/result':'submit_result','/attend':'attendance','/confirm':'complete','/follow':'follow','/unfollow':'unfollow','/invite':'invite','/remove':'remove_member','/defer':'defer','/decline':'decline'}[cmd]
            if cmd in ('/invite','/remove'): raw['user_id'] = tail.strip()
            if cmd in ('/changes','/defer','/decline'): raw['note'] = tail
            if cmd == '/result': raw['result'] = tail
            if cmd == '/attend':
                who,present = tail.split()
                if present not in ('да','нет'): raise ValueError()
                raw.update(user_id=who,attended=present=='да')
            row = store.action(actor,iid,raw)
        if row.get('removed_from_card'): return 'Действие сохранено. Карточка больше недоступна. /list'
        return self._card(row,store.user(actor))

    def _card(self,r,actor=None):
        self._record_card(r)
        iid=r['id'];status=r['status'];now=datetime.now(timezone.utc)
        own=bool(actor and r['owner_id']==actor['id']);adult=bool(actor and actor['role']=='curator')
        before=not r.get('starts_at') or datetime.fromisoformat(r['starts_at'])>now
        ended=bool(r.get('ends_at')) and datetime.fromisoformat(r['ends_at'])<=now
        missing=[]
        if not r.get('starts_at') or not r.get('ends_at'):missing.append('даты')
        if r.get('capacity') is None:missing.append('число участников')
        if r.get('needs_space',True) and not r.get('space_id'):missing.append('помещение')
        if status in ('draft','needs_changes','deferred'):
            if own or not actor:next_step=(f'/plan {iid} — заполнить: '+', '.join(missing)) if missing else f'/submit {iid} — отправить педагогу'
            else:next_step='Автор готовит заявку; помощник может взять задачу через /menu.'
        elif status=='submitted':next_step=f'/menu {iid} — согласовать, вернуть, отложить или отклонить с причиной' if adult else 'Ожидаем решения педагога.'
        elif status in ('approved','registration'):
            if ended:next_step=f'/result {iid} — описать итог' if own or not actor else 'Ожидаем итог автора; педагог отмечает каждого участника.'
            elif not before:next_step=f'/attend {iid} — отметить каждого участника' if adult else 'Событие уже началось; новая запись закрыта.'
            elif status=='registration':next_step='После начала отметьте каждого участника: /attend '+iid if adult else 'Готовьтесь к событию; запись подтверждена.' if r.get('my_registration') else f'/join {iid}'
            elif r['kind']=='team':next_step=f'/result {iid} — итог после окончания встречи команды' if own or not actor else 'Подготовка закрытой встречи команды.'
            else:next_step=f'/open {iid} — открыть запись' if own or not actor else 'Условия согласованы; автор готовит проведение.' if adult else f'/follow {iid} — получать изменения'
        elif status=='awaiting_confirmation':next_step=f'/attend {iid}, затем /confirm {iid} — проверить посещаемость и подтвердить проведение' if adult or not actor else 'Педагог проверяет посещаемость и итог.'
        else:next_step='/list — другие идеи'
        def short(value,maximum):
            value=str(value or '')
            return value if len(value)<=maximum else value[:maximum-1]+'…'
        capacity=r.get('capacity') or '?'
        room=(r.get('space_name') or 'Помещение') if r.get('space_id') else ('Помещение не требуется' if not r.get('needs_space',True) else 'Помещение пока не выбрано')
        lines=[r['title'],short(r['description'],500),f'ID: {iid}',f'Статус: {STATUS.get(status,"В работе")} · версия {r["version"]}',
               'Формат: '+('закрытая команда; видят только команда и кураторы' if r['kind']=='team' else 'событие'),
               'Аудитория: '+{'school':'вся школа','parallel':'параллель','class':'класс'}.get(r.get('audience_type'),'вся школа')+' '+str(r.get('audience_name',r.get('audience_value')) or ''),
               room+': '+self._local(r.get('starts_at'))+' — '+self._local(r.get('ends_at'))+' (Чита UTC+9)',
               f'Команда {r.get("team_count",1)}/{capacity}' if r['kind']=='team' else f'Записано {r.get("registration_count",0)}/{capacity}',
               'Комментарий: '+short(r.get('review_note') or 'нет',300),
               'Итог: '+short(r.get('result') or 'пока нет',500),'Задачи:']
        if r.get('interest_name'):lines.insert(6,'Тема: '+r['interest_name'])
        for n,t in enumerate(r.get('tasks',[])[:10],1):
            state='готово' if t['done'] else 'в работе' if t['assignee_id'] else 'свободно'
            lines.append(f'{n}. {short(t["title"],65)} [{state}]')
        if not r.get('tasks'):lines.append('нет')
        if len(r.get('tasks',[]))>10:lines.append(f'Ещё задач: {len(r["tasks"])-10}. Здесь показаны первые 10.')
        lines.append('Участники: '+short(', '.join((p.get('name') or p['user_id'])+' ('+('пришёл' if p['attended'] else 'не пришёл')+')' if p.get('attendance_recorded') else (p.get('name') or p['user_id'])+' (не отмечен)' for p in attendance_roster(r)) or 'нет',250))
        if r.get('my_following'):lines.append('Получаете изменения. Отписка без активной записи: /unfollow '+iid)
        if r.get('members'):lines.append('Приглашённая команда: '+', '.join(m['user_id'] for m in r['members']))
        if own and status in ('draft','needs_changes','deferred'):lines.append('Помощника можно пригласить до согласования: /invite '+iid)
        lines.extend(['Следующий шаг: '+next_step,'Все команды: /help'])
        return self._limit('\n'.join(lines))
