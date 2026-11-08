"""Проверка входа, получение событий и ответы MAX. Отправку нужно включить отдельно."""
import hashlib
import hmac
import json
import re
import time
import threading
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from .core import DomainError, require, now_iso
from .delivery import ensure_delivery_schema,flush_queue


def validate_init_data(raw, token, now=None, max_age=3600):
    require(isinstance(raw,str) and 0<len(raw)<=16384,'invalid_auth','Нет корректных данных входа MAX.',401)
    require(bool(token),'unconfigured','Вход MAX пока не настроен.',503)
    try:
        pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True)
    except ValueError:
        raise DomainError('invalid_auth','Некорректные данные входа MAX.',401)
    keys=[k for k,_ in pairs]
    require(len(keys)==len(set(keys)) and keys.count('hash')==1,'invalid_auth','Повторяющиеся параметры входа MAX.',401)
    values=dict(pairs); received=values.pop('hash')
    require(re.fullmatch('[a-fA-F0-9]{64}',received) is not None,'invalid_auth','Некорректная подпись MAX.',401)
    launch='\n'.join(f'{k}={v}' for k,v in sorted(values.items()))
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,launch.encode(),hashlib.sha256).hexdigest()
    require(hmac.compare_digest(expected,received.lower()),'invalid_auth','Подпись MAX не подтверждена.',401)
    now=time.time() if now is None else now
    try:
        auth_date=int(values['auth_date'])
        user=json.loads(values['user'])
        uid=user['id']
    except (ValueError,KeyError,TypeError):
        raise DomainError('invalid_auth','Некорректный пользователь MAX.',401)
    require(0<=now-auth_date<=max_age,'expired_auth','Откройте приложение из MAX заново: данные входа устарели.',401)
    require(type(uid) is int and uid>0,'invalid_auth','Некорректный пользователь MAX.',401)
    return uid


def accept_webhook(store,payload,provided_secret,expected_secret,app_url):
    require(bool(expected_secret) and isinstance(provided_secret,str) and hmac.compare_digest(provided_secret,expected_secret),'invalid_webhook','Неверная подпись webhook.',401)
    require(isinstance(payload,dict),'invalid_webhook','Некорректное событие MAX.')
    kind=payload.get('update_type')
    if kind not in ('bot_started','message_created'):
        return {'ok':True,'ignored':True}
    event_key=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    user=payload.get('user') if kind=='bot_started' else payload.get('message',{}).get('sender')
    require(isinstance(user,dict) and type(user.get('user_id')) is int,'invalid_webhook','Нет пользователя в событии MAX.')
    if kind=='message_created':
        message=payload.get('message',{})
        # Команды из общих чатов здесь не обрабатываются.
        if message.get('recipient',{}).get('chat_type') != 'dialog' or message.get('body',{}).get('text','').strip() not in ('/start','/help'):
            return {'ok':True,'ignored':True}
    with store.connection(True) as db:
        if db.execute('SELECT 1 FROM webhook_events WHERE event_key=?',(event_key,)).fetchone():
            return {'ok':True,'duplicate':True}
        db.execute('INSERT INTO webhook_events VALUES(?,?)',(event_key,now_iso()))
        row=db.execute('SELECT id FROM users WHERE max_id=?',(str(user['user_id']),)).fetchone()
        if not row:
            return {'ok':True,'ignored':True}
        text='Внеурок: от идеи до школьного события. Создайте инициативу, соберите команду и отправьте её куратору. '+app_url
        db.execute('INSERT INTO outbox(user_id,event,payload,created_at) VALUES(?,?,?,?)',(row['id'],'bot_welcome',json.dumps({'text':text},ensure_ascii=False),now_iso()))
    return {'ok':True,'queued':True}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def flush_outbox(store,token=None,send=False,transport=None,now=None):
    """Durable notifications; definite rejection may retry, uncertain sends don't."""
    if not send:
        with store.connection() as db:
            rows=db.execute("SELECT o.state,u.max_id FROM outbox o JOIN users u ON u.id=o.user_id WHERE o.state IN ('pending','blocked')").fetchall()
        return {'pending':len(rows),'deliverable':sum(bool(r['max_id']) for r in rows),'sent':0,'mode':'dry-run'}
    require(bool(token),'unconfigured','Не настроен токен MAX.')
    def deliver(recipient,row):
        payload=json.loads(row['payload'])
        if transport:return transport(recipient,{'text':payload['text']})
        return send_max_text(token,recipient,payload['text'])
    return {**flush_queue(store,'outbox',deliver,now=now),'mode':'send'}


def process_bot_update(store,router,payload,provided_secret,expected_secret):
    with store.bot_processing_lock:
        return _process_bot_update(store,router,payload,provided_secret,expected_secret)


def _process_bot_update(store,router,payload,provided_secret,expected_secret):
    """Проверяем сообщение MAX, готовим ответ и сохраняем его до отправки."""
    require(bool(expected_secret) and isinstance(provided_secret,str) and hmac.compare_digest(provided_secret,expected_secret),'invalid_webhook','Неверный секрет webhook.',401)
    require(isinstance(payload,dict),'invalid_webhook','Некорректное событие MAX.')
    kind=payload.get('update_type')
    if kind=='bot_started':
        user=payload.get('user'); text='/start'
    elif kind=='message_created':
        message=payload.get('message')
        require(isinstance(message,dict),'invalid_webhook','Не найдено сообщение.')
        if message.get('recipient',{}).get('chat_type')!='dialog':
            return {'ok':True,'ignored':True}
        user=message.get('sender'); text=message.get('body',{}).get('text','')
        require(isinstance(text,str) and len(text)<=4000,'invalid_webhook','Неверный текст сообщения.')
    else:
        return {'ok':True,'ignored':True}
    require(isinstance(user,dict) and type(user.get('user_id')) is int and user['user_id']>0,'invalid_webhook','Не найден отправитель.')
    uid=user['user_id']
    stable_id=payload.get('message',{}).get('body',{}).get('mid') if kind=='message_created' else payload.get('timestamp')
    require(isinstance(stable_id,(str,int)) and not isinstance(stable_id,bool),'invalid_webhook','Не найден номер события.')
    key=hashlib.sha256(f'{kind}:{uid}:{stable_id}'.encode()).hexdigest()
    with store.connection() as db:
        previous=db.execute('SELECT 1 FROM bot_outbox WHERE event_key=?',(key,)).fetchone()
    if previous:
        return {'ok':True,'duplicate':True}
    ensure_delivery_schema(store)
    reply=router.handle(uid,text,key)
    require(isinstance(reply,str) and 0<len(reply)<=4000,'invalid_reply','Не удалось подготовить ответ.',500)
    with store.connection(True) as db:
        guard=router.reply_guard(uid,key) if hasattr(router,'reply_guard') else None
        if isinstance(guard,dict):
            actor=guard.get('access');cards=guard.get('cards',[]);guarded=1
        else:
            mapped=db.execute('SELECT id FROM users WHERE max_id=?',(str(uid),)).fetchone()
            actor=store.user(mapped['id'],db) if mapped else None;cards=[];guarded=0
        attachments=navigation_keyboard(getattr(router,'web_app_username',''))
        db.execute('INSERT OR IGNORE INTO bot_outbox(event_key,max_user_id,text,created_at,attachments,source_user_id,source_school_id,source_access_version,source_card_guards,source_guarded) VALUES(?,?,?,?,?,?,?,?,?,?)',(key,uid,reply,now_iso(),json.dumps(attachments,ensure_ascii=False),actor['id'] if actor else None,actor.get('school_id') if actor else None,actor.get('access_version',1) if actor else None,json.dumps(cards),guarded))
    return {'ok':True,'queued':True}


_send_lock=threading.Lock()
_last_send={}


def navigation_keyboard(web_app_username=''):
    rows=[[{'type':'message','text':'Новая идея'},{'type':'message','text':'Все идеи'}],
          [{'type':'message','text':'Моё участие'},{'type':'message','text':'Мои интересы'}],
          [{'type':'message','text':'Помощь'}]]
    if web_app_username:
        require(re.fullmatch(r'[A-Za-z0-9_]{5,64}',web_app_username) is not None,'invalid_config','Некорректное публичное имя бота.')
        rows.insert(0,[{'type':'open_app','text':'Открыть Внеурок','web_app':web_app_username}])
    return [{'type':'inline_keyboard','payload':{'buttons':rows}}]


def send_max_message(token,user_id,payload):
    require(isinstance(payload,dict) and isinstance(payload.get('text'),str) and 0<len(payload['text'])<=4000,'invalid_reply','Некорректный ответ MAX.')
    body={'text':payload['text']}
    if payload.get('attachments'):body['attachments']=payload['attachments']
    # Official MAX limit: at most two messages/second per dialog.
    with _send_lock:
        key=str(user_id)
        wait=0.55-(time.monotonic()-_last_send.get(key,0))
        if wait>0:time.sleep(wait)
        _last_send[key]=time.monotonic()
    req=Request('https://platform-api2.max.ru/messages?'+urlencode({'user_id':user_id}),data=json.dumps(body,ensure_ascii=False).encode(),headers={'Authorization':token,'Content-Type':'application/json'},method='POST')
    with build_opener(NoRedirect()).open(req,timeout=15) as response:
        parsed=json.loads(response.read(65536))
        message=parsed.get('message') if isinstance(parsed,dict) else None
        mid=message.get('body',{}).get('mid') if isinstance(message,dict) else None
        if not isinstance(mid,str) or not mid:raise RuntimeError('Delivery acknowledgement has no message ID')
        return {'message_id':mid}


def send_max_text(token,user_id,text):
    return send_max_message(token,user_id,{'text':text})


def flush_bot_outbox(store,token,transport=None,now=None):
    require(bool(token),'unconfigured','Не настроен токен MAX.')
    def deliver(recipient,row):
        if transport:return transport(recipient,row['text'])
        return send_max_message(token,recipient,{'text':row['text'],'attachments':json.loads(row['attachments'])})
    return flush_queue(store,'bot_outbox',deliver,now=now)
