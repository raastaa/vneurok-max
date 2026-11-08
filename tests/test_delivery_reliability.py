"""No-network delivery reliability, privacy and reconciliation acceptance cases."""
import concurrent.futures
from datetime import datetime,timezone
from email.message import Message
from email.utils import format_datetime
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError,URLError
from vneurok.core import Store,DomainError,now_iso
from vneurok.delivery import ensure_delivery_schema,recover_stale_sending,delivery_summary,resolve_delivery,MAX_ATTEMPTS
from vneurok.max_api import flush_outbox,flush_bot_outbox,send_max_message,navigation_keyboard,process_bot_update

class DeliveryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Store(Path(self.tmp.name)/'queue.db');self.s.seed_demo();ensure_delivery_schema(self.s)
        with self.s.connection(True) as db:
            db.execute("UPDATE users SET max_id='1001' WHERE id='student-author'")
            db.execute("INSERT INTO outbox(user_id,event,payload,created_at) VALUES('student-author','test',?,?)",(json.dumps({'text':'Тестовое уведомление'}),now_iso()))
            self.key=db.execute('SELECT max(id) FROM outbox').fetchone()[0]
    def row(self,table='outbox',key=None):
        with self.s.connection() as db:return dict(db.execute(f"SELECT * FROM {table} WHERE {'id' if table=='outbox' else 'event_key'}=?",(self.key if key is None else key,)).fetchone())
    def error(self,code,fn):
        with self.assertRaises(DomainError) as got:fn()
        self.assertEqual(got.exception.code,code)
    def fail(self,error):
        def f(*a):raise error
        return f
    def http(self,code,retry=None):
        h=Message()
        if retry is not None:h['Retry-After']=str(retry)
        return HTTPError('https://platform-api2.max.ru/messages',code,'test',h,None)
    def admin(self):return 'admin'

    def test_429_is_durable_delayed_and_not_replayed_early(self):
        r=flush_outbox(self.s,'fake',True,self.fail(self.http(429,10)),now=1000)
        self.assertEqual(r['retried'],1);self.assertEqual(self.row()['next_attempt_at'],1010)
        self.s=Store(self.s.path)
        sent=[];flush_outbox(self.s,'fake',True,lambda *x:sent.append(x),now=1009)
        self.assertEqual(sent,[])
        self.assertEqual(flush_outbox(self.s,'fake',True,lambda *x:{'message_id':'mid-1'},now=1010)['sent'],1)
        self.assertEqual(self.row()['attempts'],2);self.assertEqual(self.row()['message_id'],'mid-1')

    def test_retry_after_http_date_and_fallback(self):
        retry=format_datetime(datetime.fromtimestamp(1040,timezone.utc),usegmt=True)
        flush_outbox(self.s,'fake',True,self.fail(self.http(429,retry)),now=1000)
        self.assertEqual(self.row()['next_attempt_at'],1040)
        flush_outbox(self.s,'fake',True,self.fail(self.http(429,'nonsense')),now=1040)
        self.assertGreater(self.row()['next_attempt_at'],1040)

    def test_retries_have_a_limit(self):
        with self.s.connection(True) as db:db.execute('UPDATE outbox SET attempts=? WHERE id=?',(MAX_ATTEMPTS-1,self.key))
        self.assertEqual(flush_outbox(self.s,'fake',True,self.fail(self.http(429)),now=1000)['failed'],1)
        self.assertEqual(self.row()['last_error'],'retry_exhausted')

    def test_preconnect_failure_retries_but_timeout_does_not(self):
        flush_outbox(self.s,'fake',True,self.fail(URLError(socket.gaierror('synthetic'))),now=1000)
        self.assertEqual(self.row()['state'],'pending')
        flush_outbox(self.s,'fake',True,self.fail(TimeoutError()),now=2000)
        self.assertEqual(self.row()['state'],'uncertain')
        calls=[];flush_outbox(self.s,'fake',True,lambda *x:calls.append(x),now=3000)
        self.assertEqual(calls,[])

    def test_5xx_uncertain_and_4xx_rejected(self):
        self.assertEqual(flush_outbox(self.s,'fake',True,self.fail(self.http(503)),now=1000)['uncertain'],1)
        self.assertEqual(self.row()['last_http_status'],503)
        with self.s.connection(True) as db:db.execute("UPDATE outbox SET state='pending' WHERE id=?",(self.key,))
        self.assertEqual(flush_outbox(self.s,'fake',True,self.fail(self.http(401)),now=1001)['failed'],1)

    def test_certificate_failure_never_bypasses_tls(self):
        err=URLError(ssl.SSLCertVerificationError('synthetic'))
        self.assertEqual(flush_outbox(self.s,'fake',True,self.fail(err),now=1000)['failed'],1)
        self.assertEqual(self.row()['last_error'],'tls_verification_failed')

    def test_interrupted_send_needs_reconciliation(self):
        with self.s.connection(True) as db:db.execute("UPDATE outbox SET state='sending',claimed_at=100,attempts=1 WHERE id=?",(self.key,))
        self.assertEqual(recover_stale_sending(self.s,now=200)['outbox'],0)
        self.assertEqual(recover_stale_sending(self.s,now=221)['outbox'],1)
        self.assertEqual(self.row()['state'],'uncertain')
        calls=[];flush_outbox(self.s,'fake',True,lambda *x:calls.append(x),now=500)
        self.assertEqual(calls,[])

    def test_concurrent_workers_claim_once(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def send(*x):calls.append(x);entered.set();release.wait(5);return {'message_id':'once'}
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first=pool.submit(flush_outbox,self.s,'fake',True,send,1000)
            self.assertTrue(entered.wait(5))
            second=pool.submit(flush_outbox,self.s,'fake',True,send,1000)
            self.assertEqual(second.result(timeout=5)['sent'],0)
            release.set();self.assertEqual(first.result(timeout=5)['sent'],1)
        self.assertEqual(len(calls),1)

    def test_missing_mapping_is_visible_and_unblocks_after_link(self):
        with self.s.connection(True) as db:db.execute("UPDATE users SET max_id=NULL WHERE id='student-author'")
        self.assertEqual(flush_outbox(self.s,'fake',True,lambda *x:None,now=1000)['blocked'],1)
        self.assertEqual(self.row()['state'],'blocked')
        with self.s.connection(True) as db:db.execute("UPDATE users SET max_id='1001' WHERE id='student-author'")
        self.assertEqual(flush_outbox(self.s,'fake',True,lambda *x:None,now=1001)['sent'],1)

    def test_operator_retry_requires_actual_check_and_audits(self):
        flush_outbox(self.s,'fake',True,self.fail(TimeoutError()),now=1000)
        data={'queue':'outbox','key':str(self.key),'decision':'retry_not_delivered','note':'Проверен тестовый чат, сообщения нет.'}
        self.error('confirmation_required',lambda:resolve_delivery(self.s,self.admin(),data))
        resolve_delivery(self.s,self.admin(),{**data,'confirmed_not_delivered':True})
        self.assertEqual(self.row()['state'],'pending')
        self.assertEqual(flush_outbox(self.s,'fake',True,lambda *x:{'message_id':'checked'},now=2000)['sent'],1)
        with self.s.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM delivery_decisions').fetchone()[0],1)

    def test_operator_mark_delivered_requires_message_id(self):
        flush_outbox(self.s,'fake',True,self.fail(TimeoutError()),now=1000)
        data={'queue':'outbox','key':str(self.key),'decision':'mark_delivered','note':'Сообщение найдено в тестовом чате.'}
        self.error('message_id_required',lambda:resolve_delivery(self.s,self.admin(),data))
        resolve_delivery(self.s,self.admin(),{**data,'message_id':'verified-mid'})
        self.assertEqual(self.row()['state'],'sent')
        self.assertEqual(flush_outbox(self.s,'fake',True,lambda *x:None,now=1001)['sent'],0)

    def test_students_and_curators_cannot_manage_delivery(self):
        for uid in ['student-author','curator']:
            self.error('forbidden',lambda:delivery_summary(self.s,uid))
        summary=delivery_summary(self.s,self.admin())
        self.assertNotIn('payload',summary['items'][0]);self.assertNotIn('Тестовое уведомление',str(summary))

    def test_other_school_admin_cannot_read_or_resolve_queue(self):
        with self.s.connection(True) as db:
            db.execute("INSERT INTO schools VALUES('other','Другая школа',365)")
            db.execute("INSERT INTO users(id,name,role,class_id,school_id,is_admin) VALUES('other-admin','Other','curator','staff','other',1)")
            db.execute("UPDATE outbox SET state='uncertain' WHERE id=?",(self.key,))
        self.assertEqual(delivery_summary(self.s,'other-admin')['items'],[])
        self.error('not_found',lambda:resolve_delivery(self.s,'other-admin',{'queue':'outbox','key':str(self.key),'decision':'cancel','note':'Проверка чужой очереди запрещена.'}))

    def test_queued_bot_reply_invalidated_by_role_change(self):
        actor=self.s.user('student-author')
        with self.s.connection(True) as db:
            db.execute("INSERT INTO bot_outbox(event_key,max_user_id,text,created_at,source_user_id,source_school_id,source_access_version) VALUES('private',1001,'Private synthetic card',?,'student-author',?,?)",(now_iso(),actor['school_id'],actor['access_version']))
            db.execute("UPDATE users SET access_version=access_version+1 WHERE id='student-author'")
        calls=[];r=flush_bot_outbox(self.s,'fake',lambda *x:calls.append(x),now=1000)
        self.assertEqual(calls,[]);self.assertEqual(r['cancelled'],1)

    def test_legacy_known_account_reply_is_not_implicitly_reauthorized(self):
        with self.s.connection(True) as db:db.execute("INSERT INTO bot_outbox(event_key,max_user_id,text,created_at) VALUES('old',1001,'Old private card',?)",(now_iso(),))
        self.assertEqual(flush_bot_outbox(self.s,'fake',lambda *x:None,now=1000)['cancelled'],1)

    def test_prepared_card_does_not_send_after_member_revocation(self):
        from vneurok.bot import BotRouter
        with self.s.connection(True) as db:
            db.execute("UPDATE users SET max_id='1002' WHERE id='student-helper'")
        iid='demo-game-workshop';row=self.s.detail(iid,'student-author')
        self.s.action('student-author',iid,{'action':'invite','user_id':'student-helper','version':row['version']})
        router=BotRouter(Path(self.tmp.name)/'bot',self.s)
        router.handle(1002,'/school','choose-school')
        event={'update_type':'message_created','message':{'sender':{'user_id':1002},'recipient':{'chat_type':'dialog'},'body':{'mid':'private-view','text':'/view '+iid}}}
        process_bot_update(self.s,router,event,'s','s')
        row=self.s.detail(iid,'student-author')
        self.s.action('student-author',iid,{'action':'remove_member','user_id':'student-helper','version':row['version']})
        calls=[];r=flush_bot_outbox(self.s,'fake',lambda *x:calls.append(x),now=1000)
        self.assertEqual(calls,[]);self.assertEqual(r['cancelled'],1)

    def test_render_snapshot_not_reauthorized_after_role_race(self):
        from vneurok.bot import BotRouter
        router=BotRouter(Path(self.tmp.name)/'bot',self.s)
        router.handle(1001,'/school','choose-school')
        original=router.handle
        def change_after_render(uid,text,key):
            answer=original(uid,text,key)
            with self.s.connection(True) as db:db.execute("UPDATE users SET access_version=access_version+1 WHERE id='student-author'")
            return answer
        router.handle=change_after_render
        event={'update_type':'message_created','message':{'sender':{'user_id':1001},'recipient':{'chat_type':'dialog'},'body':{'mid':'view-race','text':'/view demo-game-workshop'}}}
        process_bot_update(self.s,router,event,'s','s')
        calls=[];r=flush_bot_outbox(self.s,'fake',lambda *x:calls.append(x),now=1000)
        self.assertEqual(calls,[]);self.assertEqual(r['cancelled'],1)

    def test_generic_obsolete_notice_cannot_send_or_retry(self):
        with self.s.connection(True) as db:
            db.execute("UPDATE outbox SET payload=? WHERE id=?",(json.dumps({'text':'Old','obsolete':True}),self.key))
        self.assertEqual(flush_outbox(self.s,'fake',True,lambda *x: self.fail('must not send'),now=1000)['cancelled'],1)
        with self.s.connection(True) as db:db.execute("UPDATE outbox SET state='uncertain' WHERE id=?",(self.key,))
        self.error('forbidden',lambda:resolve_delivery(self.s,'admin',dict(queue='outbox',key=str(self.key),decision='retry_not_delivered',confirmed_not_delivered=True,note='Synthetic checked destination')))
        self.assertEqual(self.row()['state'],'uncertain')

    def test_generic_notice_binds_original_access_version(self):
        with self.s.connection(True) as db:
            db.execute("UPDATE outbox SET payload=? WHERE id=?",(json.dumps({'text':'Private','source_access_version':1}),self.key))
            db.execute("UPDATE users SET access_version=2 WHERE id='student-author'")
        calls=[]
        self.assertEqual(flush_outbox(self.s,'fake',True,lambda *x:calls.append(x),now=1000)['cancelled'],1)
        self.assertEqual(calls,[])

    def test_transport_uses_authorization_header_and_official_keyboard(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,*a):return json.dumps({'message':{'body':{'mid':'m-test'}}}).encode()
        class Opener:
            def open(inner,request,timeout):
                self.assertEqual(request.full_url,'https://platform-api2.max.ru/messages?user_id=54321')
                self.assertEqual(request.get_header('Authorization'),'test-not-a-secret')
                self.assertNotIn('test-not-a-secret',request.full_url)
                body=json.loads(request.data);self.assertEqual(body['attachments'][0]['type'],'inline_keyboard')
                return Response()
        with patch('vneurok.max_api.build_opener',return_value=Opener()):
            r=send_max_message('test-not-a-secret',54321,{'text':'Demo','attachments':navigation_keyboard('ideya_fiks_111_bot')})
        self.assertEqual(r['message_id'],'m-test')

if __name__=='__main__':unittest.main()
