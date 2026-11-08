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


def flush_outbox(store,token=None,send=False,transport=None):
    """Проверяем очередь. Если доставка неясна, сообщение не отправляется повторно."""
    with store.connection() as db:
        pending=db.execute("SELECT o.id,o.payload,o.user_id,o.initiative_id,u.max_id FROM outbox o JOIN users u ON u.id=o.user_id WHERE o.state='pending' ORDER BY o.id").fetchall()
    if not send:
        return {'pending':len(pending),'deliverable':sum(bool(r['max_id']) for r in pending),'sent':0,'mode':'dry-run'}
    require(bool(token),'unconfigured','Не настроен токен MAX.')
    sent=failed=uncertain=0
    def network_send(recipient,payload):
        send_max_text(token,recipient,payload['text'])
    transport=transport or network_send
    for row in pending:
        if not row['max_id']:
            continue
        with store.connection(True) as db:
            payload=json.loads(row['payload'])
            if row['initiative_id']:
                card=store._row(db,row['initiative_id'])
                safe_notice=payload.get('restricted_notice') is True and card['kind']=='event' and bool(db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(card['id'],row['user_id'])).fetchone())
                if not store._can_view(db,card,store.user(row['user_id'],db)) and not safe_notice:
                    db.execute("UPDATE outbox SET state='cancelled',last_error='Доступ к карточке закрыт' WHERE id=? AND state='pending'",(row['id'],))
                    continue
            claimed=db.execute("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=? AND state='pending'",(row['id'],)).rowcount
        if not claimed:
            continue
        try:
            payload=json.loads(row['payload'])
            transport(row['max_id'],{'text':payload['text']})
            state,error='sent',''; sent+=1
        except HTTPError as e:
            if 400<=e.code<500:
                state,error='failed',f'HTTP {e.code}'; failed+=1
            else:
                state,error='uncertain',f'HTTP {e.code}; проверить доставку перед повтором'; uncertain+=1
        except Exception:
            state,error='uncertain','Ответ не подтверждён; проверить доставку перед повтором'; uncertain+=1
        with store.connection(True) as db:
            db.execute('UPDATE outbox SET state=?,last_error=?,sent_at=? WHERE id=?',(state,error,now_iso() if state=='sent' else None,row['id']))
    return {'sent':sent,'failed':failed,'uncertain':uncertain,'mode':'send'}


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
    reply=router.handle(uid,text,key)
    require(isinstance(reply,str) and 0<len(reply)<=4000,'invalid_reply','Не удалось подготовить ответ.',500)
    with store.connection(True) as db:
        db.execute('INSERT OR IGNORE INTO bot_outbox(event_key,max_user_id,text,created_at) VALUES(?,?,?,?)',(key,uid,reply,now_iso()))
    return {'ok':True,'queued':True}


_send_lock=threading.Lock()
_last_send={}


def send_max_text(token,user_id,text):
    # MAX разрешает не больше двух сообщений в секунду в один диалог.
    with _send_lock:
        key=str(user_id)
        wait=0.55-(time.monotonic()-_last_send.get(key,0))
        if wait>0:
            time.sleep(wait)
        _last_send[key]=time.monotonic()
    req=Request('https://platform-api2.max.ru/messages?'+urlencode({'user_id':user_id}),data=json.dumps({'text':text},ensure_ascii=False).encode(),headers={'Authorization':token,'Content-Type':'application/json'},method='POST')
    with build_opener(NoRedirect()).open(req,timeout=15) as response:
        parsed=json.loads(response.read(65536))
        if not isinstance(parsed,dict) or not parsed.get('message'):
            raise RuntimeError('Ответ без подтверждения сообщения')


def flush_bot_outbox(store,token,transport=None):
    """Отправляем только новые ответы. Неясную доставку повторять нельзя."""
    require(bool(token),'unconfigured','Не настроен токен MAX.')
    with store.connection() as db:
        rows=db.execute("SELECT * FROM bot_outbox WHERE state='pending' ORDER BY rowid LIMIT 20").fetchall()
    result={'sent':0,'failed':0,'uncertain':0}
    transport=transport or (lambda uid,text:send_max_text(token,uid,text))
    for row in rows:
        with store.connection(True) as db:
            if not db.execute("UPDATE bot_outbox SET state='sending',attempts=attempts+1 WHERE event_key=? AND state='pending'",(row['event_key'],)).rowcount:
                continue
        try:
            transport(row['max_user_id'],row['text']); state,error='sent',''
        except HTTPError as e:
            state='failed' if 400<=e.code<500 else 'uncertain'; error=f'HTTP {e.code}'
        except Exception:
            state,error='uncertain','Доставка не подтверждена. Перед повтором проверьте чат.'
        result[state]+=1
        with store.connection(True) as db:
            db.execute('UPDATE bot_outbox SET state=?,last_error=?,sent_at=? WHERE event_key=?',(state,error,now_iso() if state=='sent' else None,row['event_key']))
    return result
