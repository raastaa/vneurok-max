"""Durable MAX delivery with bounded safe retries and explicit reconciliation.

HTTP 429 and proven pre-connection failures may retry. A timeout after a request,
5xx response or process crash after claiming a send is uncertain, never replayed
without an operator checking the destination. No credentials or message bodies
are copied into the delivery audit log.
"""
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import json
import socket
import sqlite3
import ssl
import time
from urllib.error import HTTPError,URLError
from .core import DomainError,require,now_iso

QUEUES={'outbox':'id','bot_outbox':'event_key'}
MAX_ATTEMPTS=8
SEND_LEASE_SECONDS=120


def ensure_delivery_schema(store):
    with store.connection(True) as db:
        for table in QUEUES:
            columns={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            additions={'next_attempt_at':'REAL NOT NULL DEFAULT 0','claimed_at':'REAL',
                       'message_id':"TEXT NOT NULL DEFAULT ''",'last_http_status':'INTEGER',
                       'resolution_note':"TEXT NOT NULL DEFAULT ''"}
            if table=='bot_outbox':
                additions.update(attachments="TEXT NOT NULL DEFAULT '[]'",source_user_id='TEXT',
                                 source_school_id='TEXT',source_access_version='INTEGER',source_card_guards="TEXT NOT NULL DEFAULT '[]'",source_guarded='INTEGER NOT NULL DEFAULT 0')
            for name,definition in additions.items():
                if name not in columns:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        db.executescript('''CREATE TABLE IF NOT EXISTS delivery_attempts (
            id INTEGER PRIMARY KEY, queue_name TEXT NOT NULL, message_key TEXT NOT NULL,
            attempt INTEGER NOT NULL, outcome TEXT NOT NULL, http_status INTEGER,
            occurred_at TEXT NOT NULL, retry_at REAL, error_code TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS delivery_decisions (
            id INTEGER PRIMARY KEY, queue_name TEXT NOT NULL, message_key TEXT NOT NULL,
            actor_id TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL,
            occurred_at TEXT NOT NULL);''')


def _retry_after(error,now,attempt):
    value=error.headers.get('Retry-After') if getattr(error,'headers',None) else None
    delay=None
    if value:
        try:delay=float(value)
        except (TypeError,ValueError):
            try:delay=parsedate_to_datetime(value).timestamp()-now
            except (TypeError,ValueError,OverflowError):pass
    if delay is None or not 0<=delay<=86400:delay=min(300,2**min(attempt,9))
    return now+max(1,delay)


def classify_failure(error,attempt,now):
    """Return state/error/status/retry_at without retaining unsafe error text."""
    if isinstance(error,HTTPError):
        if error.code==429:
            if attempt>=MAX_ATTEMPTS:return 'failed','retry_exhausted',429,0
            return 'pending','rate_limited',429,_retry_after(error,now,attempt)
        if 400<=error.code<500 and error.code!=408:
            return 'failed','http_rejected',error.code,0
        return 'uncertain','http_outcome_uncertain',error.code,0
    reason=error.reason if isinstance(error,URLError) else error
    if isinstance(reason,ssl.SSLCertVerificationError):
        return 'failed','tls_verification_failed',None,0
    if isinstance(reason,(ConnectionRefusedError,socket.gaierror)):
        if attempt>=MAX_ATTEMPTS:return 'failed','retry_exhausted',None,0
        return 'pending','not_connected',None,now+min(300,2**min(attempt,9))
    return 'uncertain','delivery_not_confirmed',None,0


def recover_stale_sending(store,now=None):
    ensure_delivery_schema(store);now=time.time() if now is None else now
    counts={}
    with store.connection(True) as db:
        for table,key in QUEUES.items():
            rows=db.execute(f"SELECT {key},attempts FROM {table} WHERE state='sending' AND (claimed_at IS NULL OR claimed_at<=?)",(now-SEND_LEASE_SECONDS,)).fetchall()
            for row in rows:
                db.execute(f"UPDATE {table} SET state='uncertain',last_error='interrupted_send_requires_check',claimed_at=NULL WHERE {key}=? AND state='sending'",(row[key],))
                db.execute('INSERT INTO delivery_attempts(queue_name,message_key,attempt,outcome,occurred_at,error_code) VALUES(?,?,?,?,?,?)',(table,str(row[key]),row['attempts'],'uncertain',now_iso(),'interrupted_send_requires_check'))
            counts[table]=len(rows)
    return counts


def _permission(store,db,table,row):
    if table=='outbox':
        try:
            actor=store.user(row['user_id'],db)
            if not actor.get('active',1):return False
            payload=json.loads(row['payload'])
            if not isinstance(payload,dict) or payload.get('obsolete') is True:return False
            if 'source_access_version' in payload and payload['source_access_version']!=actor.get('access_version',1):return False
            if row['initiative_id']:
                card=store._row(db,row['initiative_id'])
                if hasattr(store,'notification_allowed'):
                    return store.notification_allowed(db,card,actor,payload)
                safe=payload.get('restricted_notice') is True and card['kind']=='event' and bool(db.execute('SELECT 1 FROM initiative_followers WHERE initiative_id=? AND user_id=?',(card['id'],actor['id'])).fetchone())
                return store._can_view(db,card,actor) or safe
            return True
        except (DomainError,ValueError,TypeError):return False
    # Old replies prepared before access-version tracking must not replay private
    # school content after a role change. Newly prepared replies are bound below.
    if row['source_user_id']:
        try:actor=store.user(row['source_user_id'],db)
        except DomainError:return False
        if not (bool(actor.get('active',1)) and
                actor.get('school_id')==row['source_school_id'] and
                actor.get('access_version',1)==row['source_access_version'] and
                str(db.execute('SELECT max_id FROM users WHERE id=?',(actor['id'],)).fetchone()['max_id'])==str(row['max_user_id'])):
            return False
        try:
            guards=json.loads(row['source_card_guards'])
            if not isinstance(guards,list) or len(guards)>100:return False
            for guard in guards:
                card=store._row(db,guard['id'])
                if card['version']!=guard['version'] or not store._can_view(db,card,actor):return False
        except (DomainError,ValueError,TypeError,KeyError):return False
        return True
    if row['source_guarded']:
        return row['source_card_guards']=='[]'
    mapped=db.execute('SELECT id FROM users WHERE max_id=?',(str(row['max_user_id']),)).fetchone()
    # Legacy bot replies to known school accounts cannot establish their original
    # permissions. A new command is safe; silently replaying the old snapshot isn't.
    return mapped is None


def flush_queue(store,table,transport,now=None,limit=20):
    require(table in QUEUES,'invalid_queue','Неизвестная очередь.')
    ensure_delivery_schema(store);now=time.time() if now is None else now
    recover_stale_sending(store,now)
    key=QUEUES[table];result={'sent':0,'failed':0,'uncertain':0,'retried':0,'cancelled':0,'blocked':0}
    with store.connection(True) as db:
        if table=='outbox':
            db.execute("UPDATE outbox SET state='pending',last_error='' WHERE state='blocked' AND user_id IN (SELECT id FROM users WHERE max_id IS NOT NULL AND max_id<>'')")
            rows=db.execute("SELECT o.*,u.max_id FROM outbox o JOIN users u ON u.id=o.user_id WHERE o.state='pending' AND o.next_attempt_at<=? ORDER BY o.id LIMIT ?",(now,limit)).fetchall()
        else:rows=db.execute("SELECT * FROM bot_outbox WHERE state='pending' AND next_attempt_at<=? ORDER BY rowid LIMIT ?",(now,limit)).fetchall()
    for snapshot in rows:
        with store.connection(True) as db:
            row=db.execute(f'SELECT * FROM {table} WHERE {key}=?',(snapshot[key],)).fetchone()
            if row is None or row['state']!='pending' or row['next_attempt_at']>now:continue
            if not _permission(store,db,table,row):
                db.execute(f"UPDATE {table} SET state='cancelled',last_error='access_changed' WHERE {key}=?",(row[key],));result['cancelled']+=1;continue
            recipient=db.execute('SELECT max_id FROM users WHERE id=?',(row['user_id'],)).fetchone()['max_id'] if table=='outbox' else row['max_user_id']
            if not recipient:
                db.execute(f"UPDATE {table} SET state='blocked',last_error='account_not_linked' WHERE {key}=?",(row[key],));result['blocked']+=1;continue
            claimed=db.execute(f"UPDATE {table} SET state='sending',attempts=attempts+1,claimed_at=? WHERE {key}=? AND state='pending'",(now,row[key])).rowcount
            attempt=row['attempts']+1
        if not claimed:continue
        try:
            confirmation=transport(recipient,row)
            message_id=confirmation.get('message_id','') if isinstance(confirmation,dict) else confirmation if isinstance(confirmation,str) else ''
            state,error,status,retry_at='sent','',None,0
        except Exception as exc:
            state,error,status,retry_at=classify_failure(exc,attempt,now);message_id=''
            if isinstance(exc,HTTPError):exc.close()
        result['retried' if state=='pending' else state]+=1
        with store.connection(True) as db:
            db.execute(f'UPDATE {table} SET state=?,last_error=?,last_http_status=?,next_attempt_at=?,claimed_at=NULL,sent_at=?,message_id=? WHERE {key}=? AND state=\'sending\'',(state,error,status,retry_at,now_iso() if state=='sent' else None,message_id,row[key]))
            db.execute('INSERT INTO delivery_attempts(queue_name,message_key,attempt,outcome,http_status,occurred_at,retry_at,error_code) VALUES(?,?,?,?,?,?,?,?)',(table,str(row[key]),attempt,state,status,now_iso(),retry_at or None,error))
    return result


def _admin(store,user_id):
    actor=store.user(user_id)
    require(actor.get('is_admin') is True or actor.get('is_admin')==1,'forbidden','Доступно только администратору школы.',403)
    return actor


def _owned_row(db,table,key,school_id):
    if table=='outbox':
        return db.execute('SELECT o.* FROM outbox o JOIN users u ON u.id=o.user_id WHERE o.id=? AND u.school_id=?',(key,school_id)).fetchone()
    return db.execute('SELECT b.* FROM bot_outbox b JOIN users u ON CAST(u.max_id AS TEXT)=CAST(b.max_user_id AS TEXT) WHERE b.event_key=? AND u.school_id=? AND (b.source_school_id IS NULL OR b.source_school_id=?)',(key,school_id,school_id)).fetchone()


def delivery_summary(store,user_id):
    actor=_admin(store,user_id);ensure_delivery_schema(store)
    rows=[];counts={}
    with store.connection() as db:
        for table,key in QUEUES.items():
            if table=='outbox':
                records=db.execute('SELECT o.*,u.name AS recipient_name FROM outbox o JOIN users u ON u.id=o.user_id WHERE u.school_id=? ORDER BY o.id DESC LIMIT 100',(actor['school_id'],)).fetchall()
            else:
                records=db.execute('SELECT b.*,u.name AS recipient_name FROM bot_outbox b JOIN users u ON CAST(u.max_id AS TEXT)=CAST(b.max_user_id AS TEXT) WHERE u.school_id=? AND (b.source_school_id IS NULL OR b.source_school_id=?) ORDER BY b.rowid DESC LIMIT 100',(actor['school_id'],actor['school_id'])).fetchall()
            for r in records:
                counts[r['state']]=counts.get(r['state'],0)+1
                rows.append({'queue':table,'key':str(r[key]),'recipient_name':r['recipient_name'],'state':r['state'],'attempts':r['attempts'],'created_at':r['created_at'],'next_attempt_at':r['next_attempt_at'],'last_error':r['last_error'],'http_status':r['last_http_status'],'message_id':r['message_id']})
    return {'counts':counts,'items':rows,'limited_to':100,'uncertain_policy':'Проверьте чат MAX перед повтором. Тайм-аут не доказывает отсутствие сообщения.'}


def resolve_delivery(store,user_id,raw):
    actor=_admin(store,user_id);ensure_delivery_schema(store)
    table=raw.get('queue');require(table in QUEUES,'invalid_queue','Неизвестная очередь.')
    decision=raw.get('decision');require(decision in ('mark_delivered','retry_not_delivered','cancel'),'invalid_resolution','Выберите решение.')
    note=raw.get('note');require(isinstance(note,str) and 10<=len(note.strip())<=1000,'resolution_note','Опишите проверенный результат, от 10 до 1000 символов.')
    with store.connection(True) as db:
        row=_owned_row(db,table,raw.get('key'),actor['school_id'])
        require(row is not None,'not_found','Сообщение не найдено.',404)
        require(row['state'] in ('uncertain','failed','blocked'),'invalid_state','Это сообщение не требует ручного решения.',409)
        if decision=='retry_not_delivered':
            require(raw.get('confirmed_not_delivered') is True,'confirmation_required','Сначала проверьте чат и явно подтвердите, что сообщение не доставлено.')
            require(_permission(store,db,table,row),'forbidden','Доступ получателя изменился; старое сообщение нельзя отправить.',403)
            state='pending';mid=''
        elif decision=='mark_delivered':
            mid=raw.get('message_id');require(isinstance(mid,str) and 1<=len(mid)<=200,'message_id_required','Укажите ID найденного сообщения MAX.')
            state='sent'
        else:state='cancelled';mid=row['message_id']
        db.execute(f'UPDATE {table} SET state=?,next_attempt_at=0,claimed_at=NULL,message_id=?,resolution_note=?,sent_at=?,last_error=? WHERE {QUEUES[table]}=?',(state,mid,note.strip(),now_iso() if state=='sent' else None,'operator_'+decision,row[QUEUES[table]]))
        db.execute('INSERT INTO delivery_decisions(queue_name,message_key,actor_id,decision,note,occurred_at) VALUES(?,?,?,?,?,?)',(table,str(row[QUEUES[table]]),user_id,decision,note.strip(),now_iso()))
    return delivery_summary(store,user_id)
