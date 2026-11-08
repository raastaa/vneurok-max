"""Запуск интерфейса и приём сообщений бота."""
import argparse
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
from .core import Store,DomainError,require
from .delivery import ensure_delivery_schema,delivery_summary,resolve_delivery
from .onboarding import create_invitation,redeem_invitation,list_invitations,revoke_invitation
from .readiness import inspect_config,valid_origin
from .max_api import validate_init_data,accept_webhook,process_bot_update,flush_bot_outbox,flush_outbox

ROOT=Path(__file__).resolve().parent.parent
MAX_BODY=32768


class Application:
    def __init__(self,db_path,demo=True,origin='http://127.0.0.1:8765',token='',webhook_secret='',web_app_username=''):
        self.store=Store(db_path); self.demo=demo; self.origin=origin.rstrip('/')
        self.token=token; self.webhook_secret=webhook_secret; self.sessions={}; self.lock=threading.Lock()
        self.login_hits={}
        self.bot_router=None
        parsed=urlsplit(self.origin)
        require(parsed.scheme in ('http','https') and parsed.netloc and parsed.path in ('','/'),'invalid_config','APP_ORIGIN должен быть адресом сервиса без пути.')
        if demo:
            require(parsed.hostname in ('127.0.0.1','localhost','::1'),'invalid_config','Демонстрация разрешена только на локальном компьютере.')
            self.store.seed_demo()
        else:
            require(valid_origin(self.origin) and bool(token) and bool(webhook_secret),'invalid_config','Для режима MAX нужны HTTPS APP_ORIGIN, MAX_BOT_TOKEN и MAX_WEBHOOK_SECRET.')
            from .bot import BotRouter
            self.bot_router=BotRouter(Path(db_path).parent/'bot-demo',self.store)
            require(not web_app_username or re.fullmatch(r'[A-Za-z0-9_]{5,64}',web_app_username),'invalid_config','Некорректное публичное имя бота.')
            self.bot_router.web_app_username=web_app_username
        ensure_delivery_schema(self.store)

    def login(self,user_id):
        user=self.store.user(user_id)
        token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32)
        with self.lock:
            now=time.time()
            self.sessions={k:v for k,v in self.sessions.items() if v['expires']>now}
            require(len(self.sessions)<1000,'busy','Слишком много сеансов. Повторите позже.',503)
            self.sessions[token]={'user_id':user_id,'csrf':csrf,'expires':now+3600,'access_version':user.get('access_version',1)}
        return token,{'user':user,'csrf':csrf}

    def session(self,cookie):
        try:
            parsed=SimpleCookie(); parsed.load(cookie or '')
            token=parsed['vneurok_session'].value
        except (KeyError,ValueError):
            raise DomainError('unauthorized','Выберите демо-пользователя или войдите из MAX.',401)
        with self.lock:
            session=self.sessions.get(token)
        require(session is not None and session['expires']>time.time(),'unauthorized','Войдите заново: сеанс истёк.',401)
        actor=self.store.user(session['user_id'])
        require(actor.get('access_version',1)==session.get('access_version',1),'unauthorized','Права изменились. Откройте приложение из MAX заново.',401)
        return session


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version='Vneurok/0.1'
        def log_message(self,format,*args):
            # Не записываем токены и сведения об учениках в журнал запросов.
            return

        def respond(self,status,payload,content_type='application/json; charset=utf-8',cookie=None):
            body=json.dumps(payload,ensure_ascii=False).encode() if content_type.startswith('application/json') else payload
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' https://st.max.ru/js/max-web-app.js; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self' https://web.max.ru https://max.ru")
            if cookie:
                secure='; Secure' if app.origin.startswith('https:') else ''
                same_site='None' if not app.demo else 'Lax'
                self.send_header('Set-Cookie',f'vneurok_session={cookie}; Path=/; Max-Age=3600; HttpOnly; SameSite={same_site}{secure}')
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            require(self.headers.get('Content-Type','').split(';')[0].strip()=='application/json','content_type','Нужен Content-Type application/json.',415)
            require('Transfer-Encoding' not in self.headers,'invalid_body','Потоковое тело не поддерживается.')
            try:
                length=int(self.headers.get('Content-Length','0'))
            except ValueError:
                raise DomainError('invalid_body','Некорректный размер запроса.')
            require(0<length<=MAX_BODY,'body_too_large','Размер запроса превышает лимит.',413)
            try:
                raw=json.loads(self.rfile.read(length))
            except (ValueError,UnicodeDecodeError):
                raise DomainError('invalid_json','Некорректный JSON.')
            require(isinstance(raw,dict),'invalid_json','Нужен JSON-объект.')
            return raw

        def dispatch(self):
            self.connection.settimeout(10)
            expected=urlsplit(app.origin).netloc
            require(self.headers.get('Host')==expected,'invalid_host','Неверный адрес сервиса.',400)
            path=urlsplit(self.path).path
            if self.command=='GET':
                if path=='/health':
                    return self.respond(200,{'ok':True,'demo':app.demo,'version':'0.2.0-local'})
                if path=='/api/config':
                    return self.respond(200,{'demo':app.demo})
                if path.startswith('/api/'):
                    session=app.session(self.headers.get('Cookie')); uid=session['user_id']
                    if path=='/api/bootstrap':
                        data=app.store.bootstrap(uid,app.demo); data['csrf']=session['csrf']
                        return self.respond(200,data)
                    if path=='/api/admin':
                        return self.respond(200,{**app.store.school_admin(uid),**list_invitations(app.store,uid)})
                    if path=='/api/delivery':
                        return self.respond(200,delivery_summary(app.store,uid))
                    match=re.fullmatch('/api/initiatives/([a-zA-Z0-9-]+)',path)
                    if match:
                        return self.respond(200,app.store.detail(match[1],uid))
                    raise DomainError('not_found','Адрес не найден.',404)
                files={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/style.css':'style.css'}
                require(path in files,'not_found','Страница не найдена.',404)
                file=ROOT/'web'/files[path]
                require(file.is_file(),'not_found','Интерфейс ещё не собран.',404)
                content_type={'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[file.suffix[1:]]
                body=file.read_bytes()
                if file.name=='index.html' and not app.demo:
                    body=body.replace(b'<script src="/app.js" defer></script>',b'<script src="https://st.max.ru/js/max-web-app.js" defer></script><script src="/app.js" defer></script>')
                return self.respond(200,body,content_type)
            require(self.command in ('POST','PATCH'),'method_not_allowed','Метод не поддерживается.',405)
            if path=='/api/max/webhook':
                require(not app.demo and self.command=='POST','not_found','Webhook отключён в демо.',404)
                return self.respond(200,process_bot_update(app.store,app.bot_router,self.body(),self.headers.get('X-Max-Bot-Api-Secret',''),app.webhook_secret))
            origin=self.headers.get('Origin')
            require(origin in (None,app.origin),'invalid_origin','Запрос с другого сайта запрещён.',403)
            require(self.headers.get('Sec-Fetch-Site') not in ('cross-site','same-site'),'invalid_origin','Запрос с другого сайта запрещён.',403)
            if path in ('/api/demo/login','/api/max/login','/api/max/enroll'):
                require(self.command=='POST','method_not_allowed','Для входа используйте POST.',405)
                now=time.time(); ip=self.client_address[0]
                with app.lock:
                    hits=[x for x in app.login_hits.get(ip,[]) if x>now-60]
                    require(len(hits)<30,'rate_limit','Слишком много попыток. Подождите минуту.',429)
                    app.login_hits[ip]=hits+[now]
                raw=self.body()
                if path=='/api/demo/login':
                    require(set(raw)<={'user_id'},'invalid_field','В запросе есть неизвестные поля.')
                    require(app.demo,'not_found','Демонстрационный вход выключен.',404)
                    require(raw.get('user_id') in ('student-author','student-helper','student-visitor','curator','admin'),'invalid_user','Выберите демо-персону.')
                    uid=raw['user_id']
                else:
                    require(not app.demo,'not_found','В демо вход MAX отключён.',404)
                    require(set(raw)<=({'init_data','invitation_code'} if path=='/api/max/enroll' else {'init_data'}),'invalid_field','В запросе есть неизвестные поля.')
                    max_id=validate_init_data(raw.get('init_data'),app.token)
                    uid=(redeem_invitation(app.store,max_id,raw.get('invitation_code')) if path=='/api/max/enroll' else app.store.max_user(max_id))['id']
                token,response=app.login(uid)
                return self.respond(200,response,cookie=token)
            session=app.session(self.headers.get('Cookie')); uid=session['user_id']
            require(hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']),'invalid_csrf','Обновите страницу перед изменением.',403)
            raw=self.body()
            if path=='/api/logout' and self.command=='POST':
                with app.lock:
                    app.sessions={k:v for k,v in app.sessions.items() if v is not session}
                return self.respond(200,{'ok':True},cookie='')
            if path=='/api/interests' and self.command=='POST':
                require(set(raw)<={'interest_id','enabled'},'invalid_field','В запросе есть неизвестные поля.')
                return self.respond(200,app.store.set_interest_subscription(uid,raw.get('interest_id'),raw.get('enabled')))
            if path=='/api/admin' and self.command=='POST':
                return self.respond(200,{**app.store.admin(uid,raw),**list_invitations(app.store,uid)})
            if path=='/api/admin/invitations' and self.command=='POST':
                return self.respond(201,create_invitation(app.store,uid,raw))
            if path=='/api/admin/invitations/revoke' and self.command=='POST':
                return self.respond(200,revoke_invitation(app.store,uid,raw.get('id')))
            if path=='/api/delivery/resolve' and self.command=='POST':
                return self.respond(200,resolve_delivery(app.store,uid,raw))
            if path=='/api/initiatives' and self.command=='POST':
                return self.respond(201,app.store.create(uid,raw,self.headers.get('Idempotency-Key','')))
            match=re.fullmatch('/api/initiatives/([a-zA-Z0-9-]+)(?:/(actions|tasks)(?:/([a-zA-Z0-9-]+))?)?',path)
            require(match is not None,'not_found','Адрес не найден.',404)
            iid,resource,task_id=match.groups()
            if resource is None and self.command=='PATCH':
                return self.respond(200,app.store.edit(uid,iid,raw))
            if resource=='actions' and task_id is None and self.command=='POST':
                return self.respond(200,app.store.action(uid,iid,raw))
            if resource=='tasks' and self.command=='POST':
                return self.respond(200,app.store.task(uid,iid,raw,task_id))
            raise DomainError('method_not_allowed','Метод не поддерживается.',405)

        def handle_request(self):
            try:
                self.dispatch()
            except DomainError as error:
                self.respond(error.status,{'error':{'code':error.code,'message':error.message}})
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
                pass
            except Exception:
                self.respond(500,{'error':{'code':'internal_error','message':'Не удалось выполнить действие. Изменения не подтверждены. Обновите карточку.'}})
        do_GET=do_POST=do_PATCH=handle_request
    return Handler


def main():
    parser=argparse.ArgumentParser(description='Локальный прототип «Внеурок»')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--db',default=os.getenv('VNEUROK_DB',str(ROOT/'data'/'vneurok.db')))
    parser.add_argument('--max-mode',action='store_true',help='Отключает демо, требует HTTPS и настройки MAX')
    parser.add_argument('--send-enabled',action='store_true',help='Разрешает реальные ответы и уведомления в MAX')
    parser.add_argument('--web-app-username',default='',help='Включить кнопку mini-app только после настройки приложения указанного бота')
    parser.add_argument('--env-file',help='Локальный файл настроек без выполнения команд оболочки')
    args=parser.parse_args()
    if args.env_file:
        if not all(c['ok'] for c in inspect_config(args.env_file)):
            parser.error('Файл настроек не прошёл безопасную проверку. Используйте приватный обычный файл с корректными ключами и HTTPS.')
        allowed={'APP_ORIGIN','MAX_BOT_TOKEN','MAX_WEBHOOK_SECRET'}
        for line in Path(args.env_file).read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            name,sep,value=line.partition('=')
            if not sep or name not in allowed:
                parser.error('Некорректная строка в файле настроек.')
            os.environ[name]=value
    if not args.max_mode and args.host not in ('127.0.0.1','::1','localhost'):
        parser.error('Демо можно запускать только на loopback, без публикации в сети.')
    Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    origin=os.getenv('APP_ORIGIN',f'http://{args.host}:{args.port}')
    app=Application(args.db,not args.max_mode,origin,os.getenv('MAX_BOT_TOKEN',''),os.getenv('MAX_WEBHOOK_SECRET',''),args.web_app_username)
    server=ThreadingHTTPServer((args.host,args.port),handler_for(app))
    stop=threading.Event()
    if args.send_enabled:
        if app.demo:
            parser.error('Отправка включается только в режиме MAX.')
        def delivery_loop():
            while not stop.wait(1):
                try:
                    flush_bot_outbox(app.store,app.token)
                    flush_outbox(app.store,app.token,True)
                except Exception:
                    print('Не удалось проверить очередь. Подробности проверьте локально.',flush=True)
        threading.Thread(target=delivery_loop,daemon=True).start()
    print(f'Внеурок: {origin} (демо: {app.demo}). Ctrl+C для остановки.',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__=='__main__':
    main()
