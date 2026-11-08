"""Cross-school and audience attacks through the public Store API."""
import json
import unittest
from vneurok.core import Store
from test_domain_requirements import DomainFixture


class SchoolIsolationTests(DomainFixture):
    def setUp(self):
        super().setUp()
        with self.store.connection(True) as db:
            db.execute("INSERT INTO schools(id,name) VALUES('other','Другая вымышленная школа')")
            db.execute("INSERT INTO classes VALUES('other','7А','7А','7')")
            db.executemany('INSERT INTO users(id,name,role,class_id,school_id,is_admin) VALUES(?,?,?,?,?,?)', [
                ('other-pupil','Другой ученик','student','7А','other',0),
                ('other-teacher','Другой педагог','curator','staff','other',0),
                ('other-admin','Другой администратор','curator','staff','other',1)])
            db.execute("INSERT INTO spaces(id,name,capacity,school_id,responsible_id) VALUES('other-room','Чужой зал',20,'other','other-teacher')")
            db.execute("INSERT INTO availability(space_id,starts_at,ends_at) SELECT 'other-room',starts_at,ends_at FROM availability WHERE space_id='computer'")
        self.store = Store(self.store.path)
        self.admin('upsert_user', id='parallel-pupil', name='Ученик параллели', role='student', class_id='7Б')

    def test_bootstrap_and_admin_data_never_list_foreign_resources(self):
        row = self.make(owner='other-pupil', space_id='other-room')
        for who in ('student-author','student-helper','student-visitor','curator','admin'):
            data = self.store.bootstrap(who, demo=True)
            self.assertEqual({u['school_id'] for u in data['users']}, {'default'})
            self.assertNotIn('other-room', [s['id'] for s in data['spaces']])
            self.assertNotIn('other-room', [s['space_id'] for s in data['availability']])
            self.assertNotIn(row['id'], [r['id'] for r in data['initiatives']])
        data = self.store.school_admin('other-admin')
        self.assertEqual({u['school_id'] for u in data['users']}, {'other'})
        self.assertEqual([s['id'] for s in data['spaces']], ['other-room'])

    def test_all_roles_cannot_read_or_mutate_foreign_card_in_every_status(self):
        row = self.make()
        actions = ('submit','approve','request_changes','defer','decline','register','unregister','follow','unfollow','invite','remove_member','cancel','attendance','submit_result','complete','open_registration')
        for status in ('draft','needs_changes','submitted','approved','registration','deferred','declined','cancelled','awaiting_confirmation','completed'):
            with self.store.connection(True) as db:
                db.execute('UPDATE initiatives SET status=?,published=1,approved_by=? WHERE id=?',(status,'curator',row['id']))
            for who in ('other-pupil','other-teacher','other-admin'):
                with self.subTest(status=status,who=who):
                    self.error('forbidden', lambda: self.store.detail(row['id'],who))
                    for action in actions:
                        self.error('forbidden', lambda action=action: self.act(row,action,who=who,user_id='student-helper',attended=True,result='Выдуманный итог',note='Чужое решение'))
                    self.error('forbidden',lambda:self.store.edit(who,row['id'],dict(version=row['version'],title='Чужая правка')))
                    self.error('forbidden',lambda:self.store.task(who,row['id'],dict(version=row['version'],title='Чужая задача')))

    def test_helper_picker_only_owner_same_school_and_audience(self):
        row=self.make(audience_type='class',audience_value='7А')
        self.assertEqual(row['helper_candidates'],[{'id':'student-helper','name':'Помощник'}])
        self.assertEqual(self.store.detail(row['id'],'curator')['helper_candidates'],[])
        row=self.act(row,'invite',user_id='student-helper')
        self.assertEqual(row['helper_candidates'],[])
        self.assertEqual(self.store.detail(row['id'],'student-helper')['helper_candidates'],[])

    def test_foreign_space_invitation_assignment_and_responsible_rejected(self):
        self.error('invalid_space',lambda:self.make(space_id='other-room'))
        row=self.make()
        self.error('invalid_member',lambda:self.act(row,'invite',user_id='other-pupil'))
        self.error('invalid_assignee',lambda:self.store.task('student-author',row['id'],dict(version=row['version'],title='Чужая задача',assignee_id='other-pupil')))
        self.error('invalid_responsible',lambda:self.admin('upsert_space',id='school-room',name='Зал',capacity=20,responsible_id='other-teacher'))
        self.error('invalid_field',lambda:self.store.create('student-author',dict(title='Попытка подмены',school_id='other'),'forge-school-01'))

    def test_admin_cannot_overwrite_foreign_user_or_space_ids(self):
        self.error('forbidden',lambda:self.admin('upsert_user',id='other-pupil',name='Подмена',role='curator',class_id='staff'))
        self.error('forbidden',lambda:self.admin('upsert_space',id='other-room',name='Подмена',capacity=10,responsible_id='curator'))
        self.error('forbidden',lambda:self.admin('set_availability',space_id='other-room',intervals=[]))
        self.error('invalid_field',lambda:self.store.admin('admin',dict(action='set_retention',retention_days=50,school_id='other')))
        self.assertEqual(self.store.user('other-pupil')['role'],'student')

    def test_class_audience_limits_view_invite_task_follow_and_registration(self):
        row=self.make(audience_type='class',audience_value='7А')
        self.error('invalid_member',lambda:self.act(row,'invite',user_id='parallel-pupil'))
        self.error('invalid_assignee',lambda:self.store.task('student-author',row['id'],dict(version=row['version'],title='Помочь классу',assignee_id='student-visitor')))
        row=self.act(self.act(row,'submit'),'approve',who='curator')
        row=self.act(row,'open_registration')
        for who in ('parallel-pupil','student-visitor','other-pupil'):
            self.error('forbidden',lambda who=who:self.store.detail(row['id'],who))
            for action in ('follow','register'):
                self.error('forbidden',lambda who=who,action=action:self.act(row,action,who=who))
        self.assertEqual(self.act(row,'register',who='student-helper')['registration_count'],1)

    def test_parallel_audience_uses_class_mapping_not_string_prefix(self):
        row=self.opened(audience_type='parallel',audience_value='7')
        self.assertEqual(self.store.detail(row['id'],'parallel-pupil')['id'],row['id'])
        self.error('forbidden',lambda:self.store.detail(row['id'],'student-visitor'))
        self.error('forbidden',lambda:self.store.detail(row['id'],'other-pupil'))
        self.assertEqual(self.act(row,'register',who='parallel-pupil')['registration_count'],1)

    def test_student_cannot_target_foreign_class_or_parallel(self):
        self.error('invalid_audience',lambda:self.make(audience_type='class',audience_value='8Б'))
        self.error('invalid_audience',lambda:self.make(audience_type='parallel',audience_value='8'))
        self.error('invalid_audience',lambda:self.make(audience_type='class',audience_value='99Я'))
        self.error('invalid_audience',lambda:self.make(audience_type='school',audience_value='7А'))

    def test_topic_broadcast_respects_school_and_current_audience(self):
        for who in ('student-helper','parallel-pupil','student-visitor','other-pupil'):
            self.store.set_interest_subscription(who,'technology',True)
        row=self.act(self.act(self.make(audience_type='class',audience_value='7А'),'submit'),'approve',who='curator')
        with self.store.connection() as db:
            notices=[dict(n) for n in db.execute('SELECT user_id,payload FROM outbox WHERE initiative_id=?',(row['id'],))]
        topic_users=[n['user_id'] for n in notices if json.loads(n['payload']).get('interest_id')]
        self.assertEqual(topic_users,['student-helper'])
        self.assertNotIn('other-teacher',[n['user_id'] for n in notices])
        self.assertNotIn('other-admin',[n['user_id'] for n in notices])

    def test_foreign_safe_notice_cannot_bypass_permission_even_with_corrupt_follower(self):
        row=self.opened()
        with self.store.connection(True) as db:
            db.execute('INSERT INTO initiative_followers VALUES(?,?)',(row['id'],'other-pupil'))
            db.execute("UPDATE initiatives SET status='needs_changes',approved_by=NULL WHERE id=?",(row['id'],))
            private=self.store._row(db,row['id'])
            self.assertFalse(self.store.notification_allowed(db,private,self.store.user('other-pupil',db),{'restricted_notice':True}))
            self.store._notify(db,private,'Закрытая новая редакция',['other-pupil'])
            self.assertFalse(db.execute('SELECT 1 FROM outbox WHERE initiative_id=? AND user_id=?',(row['id'],'other-pupil')).fetchone())
        self.error('forbidden',lambda:self.store.unsubscribe('other-pupil',row['id']))

    def test_audience_revision_cancels_enrolment_and_blocks_old_follower(self):
        row=self.opened()
        row=self.act(row,'register',who='student-visitor')
        row=self.store.edit('student-author',row['id'],dict(version=row['version'],audience_type='class',audience_value='7А'))
        self.assertEqual(row['registration_count'],0)
        self.error('forbidden',lambda:self.store.detail(row['id'],'student-visitor'))
        with self.store.connection() as db:
            notice=db.execute("SELECT payload FROM outbox WHERE initiative_id=? AND user_id='student-visitor' AND event='participation_cancelled' AND state='pending'",(row['id'],)).fetchone()
            self.assertIsNotNone(notice)
            payload=json.loads(notice['payload'])
            self.assertTrue(self.store.notification_allowed(db,row,self.store.user('student-visitor',db),payload))
            for secret in (row['title'],row['starts_at'],row['ends_at'],'7А','Компьютерный'):
                self.assertNotIn(secret,payload['text'])
            self.assertNotIn('interest_id',payload)
            self.assertFalse(self.store.notification_allowed(db,row,self.store.user('other-pupil',db),payload))
            self.assertFalse(self.store.notification_allowed(db,row,self.store.user('student-helper',db),payload))
        # Opt-out is still available within the school, even without current audience access.
        self.assertTrue(self.store.unsubscribe('student-visitor',row['id'])['removed_from_card'])

    def test_narrowed_audience_receipt_delivers_without_granting_access(self):
        from vneurok.delivery import flush_queue
        with self.store.connection(True) as db:
            db.execute("UPDATE users SET max_id='990008' WHERE id='student-visitor'")
        row=self.act(self.opened(),'register',who='student-visitor')
        row=self.store.edit('student-author',row['id'],dict(version=row['version'],audience_type='class',audience_value='7А'))
        sent=[]
        flush_queue(self.store,'outbox',lambda uid,message:sent.append((uid,json.loads(message['payload']))),now=1000,limit=100)
        self.assertEqual(len(sent),1)
        self.assertEqual(sent[0][0],'990008')
        self.assertTrue(sent[0][1]['participation_cancelled'])
        self.assertNotIn(row['title'],sent[0][1]['text'])
        self.error('forbidden',lambda:self.store.detail(row['id'],'student-visitor'))
        self.store.unsubscribe('student-visitor',row['id'])
        with self.store.connection() as db:
            self.assertFalse(self.store.notification_allowed(db,row,self.store.user('student-visitor',db),sent[0][1]))

    def test_member_prevents_silent_audience_change_excluding_team(self):
        row=self.make()
        row=self.act(row,'invite',user_id='student-visitor')
        self.error('invalid_member',lambda:self.store.edit('student-author',row['id'],dict(version=row['version'],audience_type='class',audience_value='7А')))
        self.assertEqual(self.store.detail(row['id'],'student-author')['audience_type'],'school')

    def test_scoped_topics_cannot_subscribe_to_other_school_only_interest(self):
        with self.store.connection(True) as db:
            db.execute("INSERT INTO interests VALUES('other','private-topic','Чужая тема')")
        self.error('invalid_interest',lambda:self.store.set_interest_subscription('student-helper','private-topic',True))
        self.error('invalid_interest',lambda:self.make(interest_id='private-topic'))

    def test_admin_class_change_cancels_only_ineligible_future_signup(self):
        row=self.act(self.opened(audience_type='class',audience_value='7А'),'register',who='student-helper')
        self.admin('upsert_user',id='student-helper',name='Помощник',role='student',class_id='8Б')
        fresh=self.store.detail(row['id'],'curator')
        self.assertEqual(fresh['registration_count'],0)
        self.error('forbidden',lambda:self.store.detail(row['id'],'student-helper'))
        with self.store.connection() as db:
            payload=json.loads(db.execute("SELECT payload FROM outbox WHERE initiative_id=? AND user_id='student-helper' AND event='participation_cancelled' ORDER BY id DESC",(row['id'],)).fetchone()[0])
            self.assertTrue(self.store.notification_allowed(db,fresh,self.store.user('student-helper',db),payload))
        self.assertTrue(self.store.unsubscribe('student-helper',row['id'])['removed_from_card'])

    def test_admin_class_change_does_not_erase_past_attendance(self):
        from datetime import datetime,timedelta,timezone
        row=self.act(self.opened(audience_type='class',audience_value='7А'),'register',who='student-helper')
        with self.store.connection(True) as db:
            db.execute('UPDATE initiatives SET starts_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),row['id']))
        row=self.act(row,'attendance',who='curator',user_id='student-helper',attended=True)
        self.admin('upsert_user',id='student-helper',name='Помощник',role='student',class_id='8Б')
        restored=self.store.detail(row['id'],'curator')
        self.assertEqual(restored['registration_count'],1)
        self.assertEqual(restored['registrations'][0]['attended'],1)
        self.assertEqual(restored['registrations'][0]['attendance_recorded'],1)

    def test_changed_class_revokes_old_queued_permission(self):
        row=self.opened(audience_type='class',audience_value='7А')
        row=self.act(row,'follow',who='student-helper')
        self.admin('upsert_user',id='student-helper',name='Помощник',role='student',class_id='8Б')
        self.error('forbidden',lambda:self.store.detail(row['id'],'student-helper'))
        with self.store.connection() as db:
            self.assertFalse(self.store.notification_allowed(db,row,self.store.user('student-helper',db),{'restricted_notice':False}))


if __name__=='__main__':
    unittest.main()
