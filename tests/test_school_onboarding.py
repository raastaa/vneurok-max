"""Synthetic, no-network enrolment and permissions tests. No real access granted."""
import concurrent.futures
from datetime import datetime
import hashlib
from pathlib import Path
import tempfile
import unittest
from vneurok.core import Store,DomainError
from vneurok.onboarding import create_invitation,redeem_invitation,revoke_invitation,list_invitations

class SchoolOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Store(Path(self.tmp.name)/'enroll.db');self.s.seed_demo()
    def invite(self,**kw):
        return create_invitation(self.s,'admin',dict(max_user_id=98765,name='Тестовый участник',role='student',class_id='7А',confirm=True,**kw),now=1000)
    def error(self,code,fn):
        with self.assertRaises(DomainError) as got:fn()
        self.assertEqual(got.exception.code,code)

    def test_code_hash_only_exact_account_and_no_admin(self):
        inv=self.invite()
        self.assertTrue(datetime.fromisoformat(inv['expires_at']))
        with self.s.connection() as db:
            row=dict(db.execute('SELECT * FROM school_invitations').fetchone())
            self.assertEqual(row['token_hash'],hashlib.sha256(inv['code'].encode()).hexdigest())
            self.assertNotIn(inv['code'],str(row))
        self.error('invalid_invitation',lambda:redeem_invitation(self.s,98766,inv['code'],now=1001))
        user=redeem_invitation(self.s,98765,inv['code'],now=1002)
        self.assertEqual(user['role'],'student');self.assertEqual(user['class_id'],'7А')
        self.assertEqual(user['school_id'],'default');self.assertEqual(user['is_admin'],0)
        self.assertEqual(self.s.max_user(98765)['id'],user['id'])

    def test_repeat_same_redeem_is_idempotent(self):
        inv=self.invite();one=redeem_invitation(self.s,98765,inv['code'],now=1001)
        two=redeem_invitation(self.s,98765,inv['code'],now=1002)
        self.assertEqual(one['id'],two['id'])
        with self.s.connection() as db:self.assertEqual(db.execute("SELECT count(*) FROM users WHERE max_id='98765'").fetchone()[0],1)

    def test_concurrent_redeem_creates_one_account(self):
        inv=self.invite()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            rows=list(pool.map(lambda _:redeem_invitation(self.s,98765,inv['code'],now=1001),range(2)))
        self.assertEqual(rows[0]['id'],rows[1]['id'])

    def test_expired_and_revoked_codes_do_not_grant_access(self):
        inv=self.invite(expires_hours=1)
        self.error('invalid_invitation',lambda:redeem_invitation(self.s,98765,inv['code'],now=4600))
        revoke_invitation(self.s,'admin',inv['id'])
        self.error('invalid_invitation',lambda:redeem_invitation(self.s,98765,inv['code'],now=1001))
        self.error('school_invitation_required',lambda:self.s.max_user(98765))

    def test_new_invitation_revokes_older_pending_code(self):
        first=self.invite();second=self.invite()
        self.error('invalid_invitation',lambda:redeem_invitation(self.s,98765,first['code'],now=1001))
        self.assertEqual(redeem_invitation(self.s,98765,second['code'],now=1001)['role'],'student')

    def test_admin_and_confirmation_required(self):
        raw=dict(max_user_id=98765,name='Synthetic',role='student',class_id='7А',confirm=True)
        for uid in ('student-author','curator'):
            self.error('forbidden',lambda:create_invitation(self.s,uid,raw,now=1000))
        self.error('confirmation_required',lambda:create_invitation(self.s,'admin',{**raw,'confirm':False},now=1000))
        self.error('invalid_role',lambda:create_invitation(self.s,'admin',{**raw,'role':'admin'},now=1000))

    def test_invitation_does_not_override_an_existing_account(self):
        with self.s.connection(True) as db:db.execute("UPDATE users SET max_id='98765' WHERE id='student-author'")
        self.error('already_connected',self.invite)

    def test_creator_revocation_invalidates_pending_code(self):
        inv=self.invite()
        with self.s.connection(True) as db:db.execute("UPDATE users SET is_admin=0 WHERE id='admin'")
        self.error('invalid_invitation',lambda:redeem_invitation(self.s,98765,inv['code'],now=1001))

    def test_old_code_cannot_log_into_rebound_max_account(self):
        inv=self.invite();user=redeem_invitation(self.s,98765,inv['code'],now=1001)
        with self.s.connection(True) as db:db.execute("UPDATE users SET max_id='98766',access_version=access_version+1 WHERE id=?",(user['id'],))
        self.error('invalid_invitation',lambda:redeem_invitation(self.s,98765,inv['code'],now=1002))

    def test_attempt_rate_limit_persists_and_expires(self):
        for _ in range(5):self.error('invalid_invitation',lambda:redeem_invitation(self.s,98765,'not-a-code',now=1000))
        self.s=Store(self.s.path)
        self.error('rate_limit',lambda:redeem_invitation(self.s,98765,'not-a-code',now=1001))
        self.error('invalid_invitation',lambda:redeem_invitation(self.s,98765,'not-a-code',now=1061))

    def test_school_bound_listing_and_revocation(self):
        inv=self.invite()
        with self.s.connection(True) as db:
            db.execute("INSERT INTO schools VALUES('b','Other',365)")
            db.execute("INSERT INTO users(id,name,role,class_id,school_id,is_admin) VALUES('b-admin','Other','curator','staff','b',1)")
        self.assertEqual(list_invitations(self.s,'b-admin')['invitations'],[])
        self.error('not_found',lambda:revoke_invitation(self.s,'b-admin',inv['id']))
        self.assertNotIn('token_hash',str(list_invitations(self.s,'admin')))

    def test_invalid_class_and_max_identity_rejected(self):
        self.error('invalid_class',lambda:create_invitation(self.s,'admin',dict(max_user_id=98765,name='X',role='student',class_id='other',confirm=True),now=1000))
        for value in (True,0,'98765'):
            self.error('invalid_max_id',lambda:create_invitation(self.s,'admin',dict(max_user_id=value,name='X',role='student',class_id='7А',confirm=True),now=1000))
            self.error('invalid_auth',lambda:redeem_invitation(self.s,value,'x'*43,now=1000))

if __name__=='__main__':unittest.main()
