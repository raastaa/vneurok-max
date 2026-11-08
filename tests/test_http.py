import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from vneurok.server import Application,handler_for

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=Application(Path(self.tmp.name)/'test.db')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.app))
        self.port=self.server.server_address[1]; self.app.origin=f'http://127.0.0.1:{self.port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.cookie=''; self.csrf=''
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def call(self,path,method='GET',data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port)
        h={'Cookie':self.cookie}
        if data is not None: h['Content-Type']='application/json'
        if headers: h.update(headers)
        conn.request(method,path,json.dumps(data) if data is not None else None,h)
        response=conn.getresponse(); body=response.read(); hs=dict(response.getheaders()); status=response.status; conn.close()
        return status,json.loads(body) if hs.get('Content-Type','').startswith('application/json') else body,hs
    def login(self):
        status,data,headers=self.call('/api/demo/login','POST',{'user_id':'student-author'})
        self.assertEqual(status,200); self.csrf=data['csrf']; self.cookie=headers['Set-Cookie'].split(';')[0]
    def test_auth_and_csrf(self):
        self.assertEqual(self.call('/api/bootstrap')[0],401)
        self.login(); self.assertEqual(self.call('/api/bootstrap')[0],200)
        self.assertEqual(self.call('/api/initiatives/demo-game-workshop/actions','POST',{'action':'submit','version':1})[0],403)
        self.assertEqual(self.call('/api/initiatives/demo-game-workshop/actions','POST',{'action':'submit','version':1},{'X-CSRF-Token':self.csrf})[0],200)
    def test_host_and_cross_origin(self):
        self.assertEqual(self.call('/health',headers={'Host':'attacker.test'})[0],400)
        self.assertEqual(self.call('/api/demo/login','POST',{'user_id':'curator'},{'Origin':'https://attacker.test'})[0],403)
    def test_role_forgery_rejected(self):
        self.assertEqual(self.call('/api/demo/login','POST',{'user_id':'admin','role':'curator'})[0],400)
        self.login()
        self.assertEqual(self.call('/api/initiatives/demo-game-workshop/actions','POST',{'action':'approve','version':1},{'X-CSRF-Token':self.csrf,'X-Role':'curator'})[0],403)
    def test_demo_gates(self):
        self.assertEqual(self.call('/api/config')[1],{'demo':True})
        self.assertEqual(self.call('/api/max/login','POST',{'init_data':'x'})[0],404)
        self.assertEqual(self.call('/api/max/webhook','POST',{})[0],404)
    def test_static_allowlist_and_security_headers(self):
        self.assertEqual(self.call('/../vneurok/core.py')[0],404)
        _,_,headers=self.call('/health')
        self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
    def test_json_error(self):
        self.login(); conn=http.client.HTTPConnection('127.0.0.1',self.port)
        conn.request('POST','/api/initiatives',b'broken',{'Content-Type':'application/json','Cookie':self.cookie,'X-CSRF-Token':self.csrf})
        resp=conn.getresponse(); self.assertEqual(resp.status,400); self.assertEqual(json.loads(resp.read())['error']['code'],'invalid_json'); conn.close()
    def test_max_mode_disables_demo(self):
        self.app.demo=False
        self.assertEqual(self.call('/api/demo/login','POST',{'user_id':'curator'})[0],404)
    def test_health_and_static(self):
        self.assertTrue(self.call('/health')[1]['ok'])
        for path in ['/','/app.js','/style.css']: self.assertEqual(self.call(path)[0],200)

    def test_private_team_hidden_in_http_detail_and_bootstrap(self):
        with self.app.store.connection(True) as db:
            db.execute("UPDATE initiatives SET kind='team',status='submitted' WHERE id='demo-game-workshop'")
        status,data,headers=self.call('/api/demo/login','POST',{'user_id':'student-visitor'})
        self.assertEqual(status,200);self.cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.call('/api/bootstrap')[1]['initiatives'],[])
        self.assertEqual(self.call('/api/initiatives/demo-game-workshop')[0],403)

    def login_as(self,user_id):
        status,data,headers=self.call('/api/demo/login','POST',{'user_id':user_id})
        self.assertEqual(status,200);self.csrf=data['csrf'];self.cookie=headers['Set-Cookie'].split(';')[0]

    def test_admin_and_topic_routes_enforce_school_roles(self):
        self.login_as('curator');self.assertEqual(self.call('/api/admin')[0],403)
        self.login_as('student-author')
        headers={'X-CSRF-Token':self.csrf}
        self.assertEqual(self.call('/api/interests','POST',{'interest_id':'science','enabled':True},headers)[0],200)
        self.assertIn('science',self.call('/api/bootstrap')[1]['interest_subscriptions'])
        self.assertEqual(self.call('/api/interests','POST',{'interest_id':'science','enabled':False,'school_id':'other'},headers)[0],400)
        self.assertEqual(self.call('/api/admin','POST',{'action':'set_retention','retention_days':100},headers)[0],403)
        self.login_as('admin');headers={'X-CSRF-Token':self.csrf}
        self.assertEqual(self.call('/api/admin')[0],200)
        self.assertEqual(self.call('/api/delivery')[0],200)
        self.assertEqual(self.call('/api/admin','POST',{'action':'upsert_class','id':'9А','name':'9А','parallel':'9'},headers)[0],200)

    def test_access_change_invalidates_existing_http_session(self):
        self.login_as('student-author')
        with self.app.store.connection(True) as db:db.execute("UPDATE users SET access_version=access_version+1 WHERE id='student-author'")
        self.assertEqual(self.call('/api/bootstrap')[0],401)

    def test_max_enrollment_uses_signed_identity_not_role_fields(self):
        from test_max import TOKEN,signed
        self.login_as('admin')
        headers={'X-CSRF-Token':self.csrf}
        status,inv,_=self.call('/api/admin/invitations','POST',{'max_user_id':998877,'name':'Тестовый взрослый аккаунт','role':'student','class_id':'7А','confirm':True},headers)
        self.assertEqual(status,201)
        self.app.demo=False;self.app.token=TOKEN
        init=signed(user=json.dumps({'id':998877}))
        forged={'init_data':init,'invitation_code':inv['code'],'is_admin':True}
        self.assertEqual(self.call('/api/max/enroll','POST',forged)[0],400)
        status,data,_=self.call('/api/max/enroll','POST',{'init_data':init,'invitation_code':inv['code']})
        self.assertEqual(status,200);self.assertEqual(data['user']['is_admin'],0)
        self.assertEqual(data['user']['role'],'student')

    def test_official_sdk_only_in_max_html_before_application(self):
        body=self.call('/')[1].decode();self.assertNotIn('https://st.max.ru/js/max-web-app.js',body)
        self.app.demo=False
        status,body,headers=self.call('/');body=body.decode()
        self.assertEqual(status,200)
        self.assertLess(body.index('https://st.max.ru/js/max-web-app.js'),body.index('/app.js'))
        self.assertIn('https://st.max.ru/js/max-web-app.js',headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline',headers['Content-Security-Policy'])

if __name__=='__main__': unittest.main()
