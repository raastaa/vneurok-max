"""Regression checks for the 8 November privacy/process audit, no live MAX calls."""
from datetime import datetime,timedelta,timezone
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from vneurok.core import Store,DomainError,SCHEMA
from vneurok.max_api import flush_outbox

class CriticalAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Store(Path(self.tmp.name)/'test.db');self.s.seed_demo()
        self.author='student-author';self.helper='student-helper';self.visitor='student-visitor';self.curator='curator'
        self.i='demo-game-workshop'
    def row(self,who=None):return self.s.detail(self.i,who or self.author)
    def action(self,who,action,**kw):
        with self.s.connection() as db:version=self.s._row(db,self.i)['version']
        return self.s.action(who,self.i,dict(action=action,version=version,**kw))
    def error(self,code,fn):
        with self.assertRaises(DomainError) as got:fn()
        self.assertEqual(got.exception.code,code)
    def opened(self):
        self.action(self.author,'submit');self.action(self.curator,'approve')
        self.action(self.author,'open_registration')
    def past(self):
        now=datetime.now(timezone.utc)
        with self.s.connection(True) as db:
            db.execute('UPDATE initiatives SET starts_at=?,ends_at=? WHERE id=?',((now-timedelta(hours=2)).isoformat(),(now-timedelta(hours=1)).isoformat(),self.i))
    def clear_outbox(self):
        with self.s.connection(True) as db:db.execute('DELETE FROM outbox')
    def messages(self,who):
        with self.s.connection() as db:return [dict(r) for r in db.execute('SELECT * FROM outbox WHERE user_id=? ORDER BY id',(who,))]
    def edit(self,**kw):return self.s.edit(self.author,self.i,dict(version=self.row()['version'],**kw))

    def test_team_private_in_every_state(self):
        with self.s.connection(True) as db:db.execute("UPDATE initiatives SET kind='team',published=1 WHERE id=?",(self.i,))
        for state in ('draft','submitted','needs_changes','approved','registration','awaiting_confirmation','completed','cancelled'):
            with self.subTest(state=state):
                with self.s.connection(True) as db:db.execute('UPDATE initiatives SET status=? WHERE id=?',(state,self.i))
                self.error('forbidden',lambda:self.row(self.visitor))
                self.assertNotIn(self.i,[r['id'] for r in self.s.bootstrap(self.visitor)['initiatives']])
                self.error('forbidden',lambda:self.action(self.visitor,'follow'))
                self.assertEqual(self.row(self.curator)['id'],self.i)

    def test_unmoderated_event_not_public_after_submit_or_cancel(self):
        self.action(self.author,'submit')
        self.error('forbidden',lambda:self.row(self.visitor))
        self.action(self.author,'cancel')
        self.error('forbidden',lambda:self.row(self.visitor))
        self.assertEqual(self.s.bootstrap(self.visitor)['initiatives'],[])

    def test_invited_helper_works_before_submit_and_membership_is_independent(self):
        self.error('forbidden',lambda:self.s.task(self.helper,self.i,{'version':1,'action':'claim'},'demo-poster'))
        self.action(self.author,'invite',user_id=self.helper)
        row=self.s.task(self.helper,self.i,{'version':self.row()['version'],'action':'claim'},'demo-poster')
        self.error('member_has_tasks',lambda:self.action(self.author,'remove_member',user_id=self.helper))
        row=self.s.task(self.helper,self.i,{'version':row['version'],'action':'release'},'demo-poster')
        self.assertTrue(row['my_membership'])
        self.action(self.author,'remove_member',user_id=self.helper)
        self.error('forbidden',lambda:self.row(self.helper))
        self.assertTrue(all(m['state']=='cancelled' for m in self.messages(self.helper)))

    def test_invite_cannot_grant_adult_or_invite_by_nonowner(self):
        self.action(self.author,'invite',user_id=self.helper)
        self.error('forbidden',lambda:self.action(self.helper,'invite',user_id=self.visitor))
        self.error('invalid_member',lambda:self.action(self.author,'invite',user_id=self.curator))
        self.error('invalid_member',lambda:self.action(self.author,'invite',user_id=self.author))

    def test_kind_cannot_turn_public_followers_into_private_team(self):
        self.opened();self.action(self.visitor,'follow')
        self.error('immutable_kind',lambda:self.edit(kind='team'))
        self.assertEqual(self.row()['kind'],'event')

    def test_edit_reapproval_reopen_retains_notifications_not_registration(self):
        self.opened();self.action(self.visitor,'register');self.clear_outbox()
        self.edit(title='Новое название после проверки')
        notice=json.loads(self.messages(self.visitor)[0]['payload'])
        self.assertTrue(notice['restricted_notice']);self.assertNotIn('Новое название',notice['text'])
        self.assertNotIn('/view',notice['text']);self.assertIn('/unfollow '+self.i,notice['text'])
        self.error('forbidden',lambda:self.row(self.visitor))
        self.assertEqual(self.row()['registration_count'],0)
        self.assertEqual(len(self.messages(self.curator)),1)
        self.action(self.author,'submit');self.action(self.curator,'approve');self.action(self.author,'open_registration')
        messages=[json.loads(m['payload']) for m in self.messages(self.visitor)]
        self.assertEqual(len(messages),3)
        self.assertIn('согласовано',messages[1]['text']);self.assertFalse(messages[1]['restricted_notice'])
        self.assertIn('запись открыта',messages[2]['text']);self.assertIn('/join '+self.i,messages[2]['text'])
        self.assertEqual(self.row(self.visitor)['registration_count'],0)
        self.assertTrue(self.row(self.visitor)['my_following'])

    def test_safe_reapproval_notice_is_deliverable_without_card_access(self):
        self.opened();self.action(self.visitor,'register');self.clear_outbox()
        with self.s.connection(True) as db:db.execute("UPDATE users SET max_id='123' WHERE id=?",(self.visitor,))
        self.edit(title='Неопубликованные изменения')
        sent=[];flush_outbox(self.s,'fake-token',True,lambda uid,payload:sent.append((uid,payload)))
        self.assertEqual(len(sent),1);self.assertNotIn('Неопубликованные',sent[0][1]['text'])
        self.assertIn('повторной проверке',sent[0][1]['text'])

    def test_optout_works_when_card_is_private_and_stays_after_restart(self):
        self.opened();self.action(self.visitor,'register');self.edit(title='После переноса')
        self.error('forbidden',lambda:self.row(self.visitor))
        result=self.s.unsubscribe(self.visitor,self.i);self.assertTrue(result['removed_from_card'])
        self.s=Store(self.s.path)
        self.action(self.author,'submit');self.action(self.curator,'approve');self.action(self.author,'open_registration')
        self.assertFalse(self.row(self.visitor)['my_following'])
        self.assertTrue(all(m['state']=='cancelled' for m in self.messages(self.visitor)))

    def test_registered_user_must_cancel_before_optout(self):
        self.opened();self.action(self.visitor,'register')
        self.error('still_registered',lambda:self.s.unsubscribe(self.visitor,self.i))
        self.action(self.visitor,'unregister')
        self.assertFalse(self.row(self.visitor)['my_following'])

    def test_cancel_during_reapproval_notifies_followers_of_cancellation(self):
        self.opened();self.action(self.visitor,'follow');self.edit(title='Исправленная идея');self.clear_outbox()
        self.action(self.author,'cancel')
        payload=json.loads(self.messages(self.visitor)[0]['payload'])
        self.assertIn('отменена',payload['text']);self.assertTrue(payload['restricted_notice'])
        self.assertNotIn('Исправленная идея',payload['text'])
        self.error('forbidden',lambda:self.row(self.visitor))

    def test_task_changes_notify_author_and_assignee(self):
        self.action(self.author,'invite',user_id=self.helper);self.clear_outbox()
        r=self.s.task(self.helper,self.i,{'version':self.row()['version'],'action':'claim'},'demo-poster')
        self.s.task(self.helper,self.i,{'version':r['version'],'action':'complete'},'demo-poster')
        self.assertEqual(len(self.messages(self.author)),2)
        self.assertIn('выполнена',self.messages(self.author)[-1]['event'])
        self.assertEqual(len(self.messages(self.helper)),1)

    def test_pending_private_delivery_rechecks_access(self):
        self.action(self.author,'invite',user_id=self.helper)
        with self.s.connection(True) as db:
            db.execute("UPDATE users SET max_id='123' WHERE id=?",(self.helper,))
            db.execute('DELETE FROM initiative_members WHERE initiative_id=?',(self.i,))
        sent=[];flush_outbox(self.s,'fake-token',True,lambda *args:sent.append(args))
        self.assertEqual(sent,[])
        self.assertEqual(self.messages(self.helper)[0]['state'],'cancelled')

    def test_author_result_waits_for_explicit_attendance_and_curator(self):
        self.opened();self.action(self.visitor,'register');self.past()
        r=self.action(self.author,'submit_result',result='Провели занятие и сохранили результаты.')
        self.assertEqual(r['status'],'awaiting_confirmation')
        self.assertEqual(r['attendance_pending'],1)
        self.error('forbidden',lambda:self.action(self.author,'complete'))
        self.error('attendance_required',lambda:self.action(self.curator,'complete'))
        self.action(self.curator,'attendance',user_id=self.visitor,attended=False)
        r=self.action(self.curator,'complete')
        self.assertEqual(r['status'],'completed');self.assertEqual(r['confirmed_by'],self.curator)
        self.assertTrue(r['confirmed_at']);self.assertEqual(r['registrations'][0]['attendance_recorded'],1)
        self.assertEqual(r['registrations'][0]['attended'],0)

    def test_conditions_frozen_after_result_submission(self):
        self.opened();self.past();self.action(self.author,'submit_result',result='Подвели итог совместной работы.')
        self.error('invalid_state',lambda:self.edit(title='Попытка заменить событие'))

    def test_past_edit_cannot_destroy_recorded_attendance(self):
        self.opened();self.action(self.visitor,'register');self.past()
        self.action(self.curator,'attendance',user_id=self.visitor,attended=True)
        future=datetime.now(timezone.utc)+timedelta(days=1)
        self.error('already_started',lambda:self.edit(starts_at=future.isoformat(),ends_at=(future+timedelta(hours=1)).isoformat()))
        self.assertEqual(self.row()['registration_count'],1)
        self.assertEqual(self.row()['registrations'][0]['attended'],1)

    def test_curator_cannot_replace_author_result(self):
        self.opened();self.past()
        self.error('forbidden',lambda:self.action(self.curator,'submit_result',result='Педагог написал вместо автора.'))

    def test_team_without_registrations_still_requires_curator_confirmation(self):
        with self.s.connection(True) as db:db.execute("UPDATE initiatives SET kind='team' WHERE id=?",(self.i,))
        self.action(self.author,'submit');self.action(self.curator,'approve');self.past()
        r=self.action(self.author,'submit_result',result='Команда подготовила учебный проект.')
        self.assertEqual(r['status'],'awaiting_confirmation')
        self.error('forbidden',lambda:self.action(self.author,'complete'))
        self.assertEqual(self.action(self.curator,'complete')['status'],'completed')
        self.error('forbidden',lambda:self.row(self.visitor))

    def test_curator_author_cannot_confirm_own_result(self):
        self.opened();self.past()
        with self.s.connection(True) as db:db.execute("UPDATE users SET role='curator' WHERE id=?",(self.author,))
        self.action(self.author,'submit_result',result='Автор подготовил итог встречи.')
        self.error('forbidden',lambda:self.action(self.author,'complete'))

    def test_pending_result_keeps_booking_reserved(self):
        self.opened()
        with self.s.connection(True) as db:db.execute("UPDATE initiatives SET status='awaiting_confirmation' WHERE id=?",(self.i,))
        row=self.row();raw={k:row[k] for k in ('title','description','kind','space_id','starts_at','ends_at','capacity')}
        second=self.s.create(self.helper,raw,'second-booking')
        second=self.s.action(self.helper,second['id'],{'action':'submit','version':second['version']})
        self.error('booking_conflict',lambda:self.s.action(self.curator,second['id'],{'action':'approve','version':second['version']}))

    def test_team_capacity_counts_author_members_and_assignees(self):
        with self.s.connection(True) as db:db.execute("UPDATE initiatives SET kind='team',capacity=2 WHERE id=?",(self.i,))
        self.action(self.author,'invite',user_id=self.helper)
        self.error('team_full',lambda:self.action(self.author,'invite',user_id=self.visitor))
        self.error('team_full',lambda:self.s.task(self.author,self.i,{'version':self.row()['version'],'title':'Помочь команде','assignee_id':self.visitor}))
        self.assertEqual(self.row()['team_count'],2)
        self.action(self.author,'submit');self.action(self.curator,'approve')

    def test_task_history_cannot_masquerade_as_curator_confirmation(self):
        self.action(self.author,'invite',user_id=self.helper)
        r=self.s.task(self.helper,self.i,{'version':self.row()['version'],'action':'claim'},'demo-poster')
        r=self.s.task(self.helper,self.i,{'version':r['version'],'action':'complete'},'demo-poster')
        self.assertEqual(r['history'][0]['action'],'task_complete')
        self.assertNotIn('complete',[h['action'] for h in r['history']])

    def test_legacy_migration_does_not_invent_absence_or_confirmation(self):
        old=Path(self.tmp.name)/'old.db'
        schema=SCHEMA.replace('published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),','').replace('confirmed_by TEXT REFERENCES users(id), confirmed_at TEXT,','').replace('attendance_recorded INTEGER NOT NULL DEFAULT 0 CHECK(attendance_recorded IN (0,1)),','')
        with closing(sqlite3.connect(old)) as db:
            db.executescript(schema)
            db.executemany('INSERT INTO users(id,name,role) VALUES(?,?,?)',[('a','Author','student'),('v','Visitor','student'),('h','Helper','student'),('c','Curator','curator')])
            db.execute("INSERT INTO spaces VALUES('room','Room',3)")
            db.execute("INSERT INTO initiatives(id,owner_id,title,description,kind,status,space_id,starts_at,ends_at,capacity,approved_by,created_at,updated_at) VALUES('old','a','Title','Description','event','completed','room','2026-01-01T10:00:00+00:00','2026-01-01T11:00:00+00:00',3,'c','now','now')")
            db.executemany("INSERT INTO registrations(initiative_id,user_id,attended,created_at) VALUES('old',?,?,'now')",[('v',0),('h',1)])
            db.commit()
        migrated=Store(old);r=migrated.detail('old','c')
        people={x['user_id']:x for x in r['registrations']}
        self.assertEqual(people['v']['attendance_recorded'],0);self.assertEqual(people['h']['attendance_recorded'],1)
        self.assertEqual(r['published'],1);self.assertIsNone(r['confirmed_by']);self.assertIsNone(r['confirmed_at'])

if __name__=='__main__':unittest.main()
