"""MVP domain acceptance tests. All people, schools and MAX IDs are synthetic."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from vneurok.core import DomainError, INITIATIVE_FIELDS, SCHEMA, Store, now_iso


class DomainFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'domain.db')
        self.store.seed_demo()
        self.store.admin('admin',dict(action='upsert_class',id='7Б',name='7Б',parallel='7'))
        self.n = 0
        self.start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=7, minute=0, second=0, microsecond=0)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, owner='student-author', **extra):
        self.n += 1
        data = dict(title='Школьный проект', description='Вместе подготовим проект и покажем результат.', kind='event',
                    space_id='computer', starts_at=self.start.isoformat(), ends_at=(self.start + timedelta(hours=1)).isoformat(),
                    capacity=2, audience_type='school', audience_value='', interest_id='technology')
        data.update(extra)
        return self.store.create(owner, data, f'domain-create-{self.n}')

    def act(self, row, action, who='student-author', **extra):
        return self.store.action(who, row['id'], dict(action=action, version=row['version'], **extra))

    def opened(self, **extra):
        row = self.make(**extra)
        row = self.act(row, 'submit', who=row['owner_id'])
        row = self.act(row, 'approve', who='curator')
        return self.act(row, 'open_registration', who=row['owner_id'])

    def error(self, code, callback):
        with self.assertRaises(DomainError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def admin(self, action, **data):
        return self.store.admin('admin', dict(action=action, **data))


class DomainRequirementsTests(DomainFixture):
    def test_partial_draft_survives_restart_and_can_invite_before_dates(self):
        row = self.store.create('student-author', {'title': 'Идея'}, 'partial-idea-01')
        self.assertIsNone(row['space_id'])
        self.assertEqual(row['starts_at'], '')
        self.assertIsNone(row['capacity'])
        row = self.act(row, 'invite', user_id='student-helper')
        row = self.store.task('student-author', row['id'], dict(version=row['version'], title='Обсудить идею', assignee_id='student-helper'))
        restored = Store(self.store.path).detail(row['id'], 'student-helper')
        self.assertEqual(restored['title'], 'Идея')
        self.assertEqual(len(restored['tasks']), 1)
        self.error('invalid_field', lambda: self.act(row, 'submit'))

    def test_full_validation_happens_at_submit_and_approve(self):
        row = self.store.create('student-author', {'title': 'Идея'}, 'partial-idea-02')
        full = self.make()
        fields = {k: full[k] for k in INITIATIVE_FIELDS}
        row = self.store.edit('student-author', row['id'], dict(version=row['version'], **fields))
        row = self.act(row, 'submit')
        with self.store.connection(True) as db:
            db.execute('DELETE FROM availability WHERE space_id=?', ('computer',))
        self.error('unavailable', lambda: self.act(row, 'approve', who='curator'))
        self.assertEqual(self.store.detail(row['id'], 'curator')['status'], 'submitted')

    def test_no_room_flow_and_parallel_events_have_no_room_conflict(self):
        first = self.make(needs_space=False, space_id=None)
        first = self.act(first, 'submit')
        first = self.act(first, 'approve', who='curator')
        self.assertEqual(first['booking_status'], 'not_required')
        second = self.make(owner='student-helper', needs_space=False, space_id=None)
        second = self.act(second, 'submit', who='student-helper')
        second = self.act(second, 'approve', who='curator')
        self.assertEqual(second['status'], 'approved')
        self.assertEqual(second['space_name'], 'Без помещения')
        self.error('invalid_space', lambda: self.make(needs_space=False))

    def test_submit_needs_choice_of_room_or_explicit_no_room(self):
        row = self.make(space_id=None)
        self.error('invalid_space', lambda: self.act(row, 'submit'))
        row = self.store.edit('student-author', row['id'], dict(version=row['version'], needs_space=False))
        self.assertEqual(self.act(row, 'submit')['booking_status'], 'not_required')

    def test_defer_requires_reason_and_can_resubmit(self):
        row = self.act(self.make(), 'submit')
        self.error('invalid_field', lambda: self.act(row, 'defer', who='curator', note=''))
        row = self.act(row, 'defer', who='curator', note='Уточните доступность оборудования.')
        self.assertEqual(row['status'], 'deferred')
        self.assertEqual(row['booking_status'], 'released')
        row = self.act(row, 'submit')
        self.assertEqual(row['booking_status'], 'pending')
        self.assertEqual(self.act(row, 'approve', who='curator')['booking_status'], 'approved')

    def test_decline_requires_reason_and_is_terminal(self):
        row = self.act(self.make(), 'submit')
        self.error('invalid_field', lambda: self.act(row, 'decline', who='curator'))
        row = self.act(row, 'decline', who='curator', note='План небезопасен для участников.')
        self.assertEqual(row['status'], 'declined')
        for action in ('submit', 'cancel', 'invite'):
            self.error('invalid_state', lambda action=action: self.act(row, action, user_id='student-helper'))
        self.error('invalid_state', lambda: self.store.edit('student-author', row['id'], dict(version=row['version'], title='Новая версия')))
        self.error('invalid_state', lambda: self.store.task('student-author', row['id'], dict(version=row['version'], title='Новая задача')))

    def test_only_assigned_responsible_can_approve_room(self):
        row = self.act(self.make(), 'submit')
        self.error('not_responsible', lambda: self.act(row, 'approve', who='admin'))
        row = self.act(row, 'approve', who='curator')
        self.assertEqual(row['responsible_id'], 'curator')

    def test_responsible_must_be_active_same_school_teacher(self):
        for target in ('student-author', 'missing'):
            self.error('invalid_responsible', lambda target=target: self.admin('upsert_space', id='new-space', name='Зал', capacity=10, responsible_id=target))
        self.error('responsible_in_use', lambda: self.admin('upsert_user', id='curator', name='Педагог', role='student', class_id='7А'))

    def test_space_capacity_accounts_for_public_seats_team_and_adult(self):
        row = self.make(capacity=13)
        self.error('space_capacity', lambda: self.act(row, 'submit'))
        row = self.make(capacity=12)
        row = self.act(row, 'submit')
        row = self.act(row, 'approve', who='curator')
        self.error('space_capacity', lambda: self.act(row, 'invite', user_id='student-helper'))
        self.error('space_capacity', lambda: self.store.task('student-author', row['id'], dict(version=row['version'], title='Новый помощник', assignee_id='student-helper')))

    def test_admin_space_change_requires_reapproval_and_reregistration(self):
        row = self.opened()
        row = self.act(row, 'register', who='student-helper')
        result = self.admin('upsert_space', id='computer', name='Компьютерный класс', capacity=14, equipment=['Компьютеры'], access_rules='Новые условия доступа.', responsible_id='curator')
        self.assertTrue(result['spaces'])
        revised = self.store.detail(row['id'], 'student-author')
        self.assertEqual(revised['status'], 'needs_changes')
        self.assertEqual(revised['registration_count'], 0)
        self.assertIsNone(revised['approved_by'])
        self.assertEqual(revised['booking_status'], 'draft')
        self.assertEqual(revised['history'][0]['action'], 'space_changed')
        self.assertTrue(self.store.bootstrap('student-helper')['initiatives'] == [])

    def test_admin_cannot_remove_interval_of_approved_booking(self):
        row = self.act(self.act(self.make(), 'submit'), 'approve', who='curator')
        self.error('bookings_outside_intervals', lambda: self.admin('set_availability', space_id='computer', intervals=[]))
        self.assertEqual(self.store.detail(row['id'], 'curator')['booking_status'], 'approved')

    def test_busy_slots_do_not_reveal_private_team_identity(self):
        row = self.make(kind='team', title='Секретная проектная встреча')
        row = self.act(self.act(row, 'submit'), 'approve', who='curator')
        data = self.store.bootstrap('student-visitor')
        self.assertNotIn(row['id'], [i['id'] for i in data['initiatives']])
        self.assertEqual(data['busy_slots'], [dict(space_id='computer', starts_at=row['starts_at'], ends_at=row['ends_at'])])
        self.assertNotIn('Секретная', json.dumps(data, ensure_ascii=False))

    def test_interest_optin_targets_only_matching_public_topic(self):
        self.store.set_interest_subscription('student-helper', 'technology', True)
        self.store.set_interest_subscription('student-visitor', 'art', True)
        row = self.act(self.act(self.make(), 'submit'), 'approve', who='curator')
        with self.store.connection() as db:
            notices = [dict(r) for r in db.execute('SELECT user_id,payload FROM outbox WHERE initiative_id=?', (row['id'],))]
        interested = [n for n in notices if json.loads(n['payload']).get('interest_id')]
        self.assertEqual([n['user_id'] for n in interested], ['student-helper'])
        self.assertFalse(self.store.detail(row['id'], 'student-helper')['my_membership'])
        self.assertFalse(self.store.detail(row['id'], 'student-helper')['my_registration'])

    def test_interest_optout_cancels_queued_topic_notice_and_survives_restart(self):
        self.store.set_interest_subscription('student-helper', 'technology', True)
        row = self.act(self.act(self.make(), 'submit'), 'approve', who='curator')
        self.store.set_interest_subscription('student-helper', 'technology', False)
        self.assertEqual(Store(self.store.path).bootstrap('student-helper')['interest_subscriptions'], [])
        with self.store.connection() as db:
            queued = db.execute('SELECT * FROM outbox WHERE initiative_id=? AND user_id=?', (row['id'], 'student-helper')).fetchone()
            self.assertEqual(queued['state'], 'cancelled')
            self.assertFalse(self.store.notification_allowed(db, row, self.store.user('student-helper', db), json.loads(queued['payload'])))

    def test_private_team_never_broadcasts_to_interest_subscribers(self):
        self.store.set_interest_subscription('student-helper', 'technology', True)
        row = self.act(self.act(self.make(kind='team'), 'submit'), 'approve', who='curator')
        with self.store.connection() as db:
            self.assertFalse(db.execute('SELECT 1 FROM outbox WHERE initiative_id=? AND user_id=?', (row['id'], 'student-helper')).fetchone())
        self.error('forbidden', lambda: self.store.detail(row['id'], 'student-helper'))

    def test_teacher_is_not_admin_and_student_cannot_self_promote(self):
        for who in ('student-author', 'student-helper', 'curator'):
            self.error('forbidden', lambda who=who: self.store.school_admin(who))
            self.error('forbidden', lambda who=who: self.store.admin(who, dict(action='upsert_user', id=who, name='Новое имя', role='curator', class_id='staff', is_admin=True)))
        self.assertFalse(self.store.bootstrap('curator')['capabilities']['manage_school'])
        self.assertTrue(self.store.bootstrap('admin')['capabilities']['manage_school'])
        self.error('invalid_role', lambda: self.admin('upsert_user', id='new-student', name='Ученик', role='student', class_id='7А', is_admin=True))

    def test_admin_class_user_room_and_manual_intervals(self):
        self.admin('upsert_class', id='9В', name='9В', parallel='9')
        self.admin('upsert_user', id='pupil-9', name='Вымышленный ученик', role='student', class_id='9В', max_id='900009')
        self.admin('upsert_space', id='hall', name='Учебный зал', capacity=20, equipment=['Доска'], access_rules='Только с педагогом.', responsible_id='curator')
        result = self.admin('set_availability', space_id='hall', intervals=[dict(starts_at=self.start.isoformat(), ends_at=(self.start + timedelta(hours=2)).isoformat())])
        self.assertEqual(self.store.max_user('900009')['school_id'], 'default')
        space = next(x for x in result['spaces'] if x['id'] == 'hall')
        self.assertEqual(space['equipment'], ['Доска'])
        self.assertEqual(space['access_rules'], 'Только с педагогом.')
        row = self.make(owner='pupil-9', space_id='hall', audience_type='class', audience_value='9В')
        self.assertEqual(self.act(row, 'submit', who='pupil-9')['status'], 'submitted')

    def test_access_version_increases_only_on_access_changes(self):
        before = self.store.user('student-helper')['access_version']
        self.admin('upsert_user', id='student-helper', name='Другое имя', role='student', class_id='7А')
        self.assertEqual(self.store.user('student-helper')['access_version'], before)
        self.admin('upsert_user', id='student-helper', name='Другое имя', role='student', class_id='7Б')
        self.assertEqual(self.store.user('student-helper')['access_version'], before + 1)
        self.admin('upsert_user', id='student-helper', name='Другое имя', role='student', class_id='7Б', active=False)
        self.error('unauthorized', lambda: self.store.user('student-helper'))

    def test_storage_policy_is_bounded_audited_and_non_destructive(self):
        self.error('invalid_retention', lambda: self.admin('set_retention', retention_days=0))
        result = self.admin('set_retention', retention_days=180)
        self.assertEqual(result['school']['retention_days'], 180)
        self.assertIn('автоматического удаления истории нет', result['retention_policy'])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admin_history WHERE action='set_retention'").fetchone()[0], 1)
            self.assertTrue(db.execute("SELECT 1 FROM initiatives WHERE id='demo-game-workshop'").fetchone())

    def test_inactive_helper_can_be_removed_without_losing_history(self):
        row=self.act(self.make(),'invite',user_id='student-helper')
        self.admin('upsert_user',id='student-helper',name='Помощник',role='student',class_id='7А',active=False)
        row=self.act(row,'remove_member',user_id='student-helper')
        self.assertEqual(row['members'],[])
        self.assertEqual(self.act(row,'submit')['status'],'submitted')

    def test_revision_cancels_old_conditions_before_queue_can_send(self):
        self.store.set_interest_subscription('student-helper','technology',True)
        row=self.act(self.act(self.make(),'submit'),'approve',who='curator')
        with self.store.connection() as db:
            old_ids=[x[0] for x in db.execute("SELECT id FROM outbox WHERE initiative_id=? AND state='pending'",(row['id'],))]
        row=self.store.edit('student-author',row['id'],dict(version=row['version'],title='Новое название и условия'))
        row=self.act(self.act(row,'submit'),'approve',who='curator')
        with self.store.connection() as db:
            self.assertTrue(old_ids)
            for oid in old_ids:
                self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?',(oid,)).fetchone()[0],'cancelled')

    def test_deactivated_author_history_remains_readable_to_teacher(self):
        row = self.make()
        self.admin('upsert_user', id='student-author', name='Автор идеи', role='student', class_id='7А', active=False)
        self.assertEqual(self.store.detail(row['id'], 'curator')['owner_name'], 'Автор идеи')

    def test_old_database_migration_preserves_history_all_links_and_decisions(self):
        old = Path(self.tmp.name) / 'legacy.db'
        with closing(sqlite3.connect(old)) as db:
            db.executescript(SCHEMA)
            db.executemany('INSERT INTO users(id,name,role,class_id) VALUES(?,?,?,?)', [('a','Автор','student','7А'), ('b','Помощник','student','7А'), ('c','Педагог','curator','staff')])
            db.execute("INSERT INTO spaces VALUES('room','Комната',10)")
            db.execute("INSERT INTO initiatives(id,owner_id,title,description,kind,status,space_id,starts_at,ends_at,capacity,published,approved_by,confirmed_by,confirmed_at,result,created_at,updated_at) VALUES('old','a','Старое событие','Сохранённое описание','event','completed','room','2026-01-01T07:00:00+00:00','2026-01-01T08:00:00+00:00',2,1,'c','c','2026-01-01T09:00:00+00:00','Сохранённый итог','created','updated')")
            db.execute("INSERT INTO tasks VALUES('t','old','Задача','b',1)")
            db.execute("INSERT INTO registrations VALUES('old','b',1,1,'created')")
            db.execute("INSERT INTO initiative_members VALUES('old','b')")
            db.execute("INSERT INTO initiative_followers VALUES('old','b')")
            db.execute("INSERT INTO history VALUES(12,'old','c','complete','Подтверждено','history-date')")
            db.execute("INSERT INTO idempotency VALUES('a','old-create','fingerprint','old')")
            db.execute("INSERT INTO outbox(id,user_id,initiative_id,event,payload,state,attempts,created_at) VALUES(15,'b','old','notice','{}','sent',1,'notice-date')")
            db.commit()
        first = Store(old)
        row = first.detail('old', 'c')
        self.assertEqual(row['school_id'], 'default')
        self.assertEqual(row['booking_status'], 'approved')
        self.assertEqual(row['confirmed_by'], 'c')
        self.assertEqual(row['registrations'][0]['attendance_recorded'], 1)
        self.assertEqual(row['history'][0]['note'], 'Подтверждено')
        with first.connection() as db:
            snapshots = {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in ('initiatives','tasks','registrations','initiative_members','history','outbox','idempotency')}
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        second = Store(old)
        with second.connection() as db:
            self.assertEqual(snapshots, {table: [tuple(r) for r in db.execute('SELECT * FROM ' + table)] for table in snapshots})
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        draft = second.create('a', {'title':'Короткая новая идея'}, 'new-after-migration')
        self.assertIsNone(draft['space_id'])
        self.assertEqual(len(second.detail('old','c')['history']), 1)


class NotificationRevisionTests(DomainFixture):
    def queue_rows(self, iid, user='student-helper'):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM outbox WHERE initiative_id=? AND user_id=? ORDER BY id',(iid,user))]

    def clone_states(self, item):
        result={}
        with self.store.connection(True) as db:
            for state in ('pending','blocked','failed','uncertain','sending'):
                db.execute('INSERT INTO outbox(user_id,initiative_id,event,payload,state,attempts,created_at,last_error) VALUES(?,?,?,?,?,2,?,?)',(item['user_id'],item['initiative_id'],item['event'],item['payload'],state,now_iso(),'original-audit'))
                result[db.execute('SELECT last_insert_rowid()').fetchone()[0]]=state
        return result

    def assert_invalidated(self, before):
        with self.store.connection() as db:
            for key, previous in before.items():
                row=db.execute('SELECT * FROM outbox WHERE id=?',(key,)).fetchone()
                self.assertEqual(row['state'],previous if previous in ('uncertain','sending') else 'cancelled')
                self.assertTrue(json.loads(row['payload'])['obsolete'])
                self.assertEqual(row['attempts'],2)
                if previous in ('uncertain','sending'):
                    self.assertEqual(row['last_error'],'original-audit')

    def test_actual_blocked_register_edit_link_cannot_revive_confirmation(self):
        from vneurok.delivery import flush_queue
        row=self.opened()
        row=self.act(row,'register',who='student-helper')
        flush_queue(self.store,'outbox',lambda *args:None,now=1000,limit=100)
        old=[r for r in self.queue_rows(row['id']) if 'запись подтверждена' in r['event']][0]
        self.assertEqual(old['state'],'blocked')
        row=self.store.edit('student-author',row['id'],dict(version=row['version'],starts_at=(self.start+timedelta(hours=1)).isoformat(),ends_at=(self.start+timedelta(hours=2)).isoformat()))
        row=self.act(self.act(row,'submit'),'approve',who='curator')
        row=self.act(row,'open_registration')
        self.admin('upsert_user',id='student-helper',name='Помощник',role='student',class_id='7А',max_id='99001')
        sent=[];flush_queue(self.store,'outbox',lambda uid,message:sent.append(json.loads(message['payload'])['text']),now=2000,limit=100)
        self.assertFalse(any('запись подтверждена' in text for text in sent))
        self.assertEqual(row['registration_count'],0)
        with self.store.connection() as db:
            stale=db.execute('SELECT * FROM outbox WHERE id=?',(old['id'],)).fetchone()
            self.assertEqual(stale['state'],'cancelled')
            # Even forced restoration by an operator cannot reauthorize old terms.
            self.assertFalse(self.store.notification_allowed(db,row,self.store.user('student-helper',db),json.loads(stale['payload'])))

    def test_edit_cancel_and_space_invalidate_all_delivery_states(self):
        for mutation in ('edit','cancel','space'):
            with self.subTest(mutation=mutation):
                self.start+=timedelta(days=1)
                row=self.act(self.opened(),'register',who='student-helper')
                source=[r for r in self.queue_rows(row['id']) if 'запись подтверждена' in r['event']][0]
                before=self.clone_states(source)
                if mutation=='edit':
                    row=self.store.edit('student-author',row['id'],dict(version=row['version'],title='Изменённое мероприятие'))
                elif mutation=='cancel':
                    row=self.act(row,'cancel')
                else:
                    self.admin('upsert_space',id='computer',name='Обновлённый класс',capacity=14,equipment=['Компьютеры'],access_rules='Новые условия.',responsible_id='curator')
                    row=self.store.detail(row['id'],'student-author')
                self.assertEqual(row['conditions_revision'],2)
                self.assert_invalidated(before)

    def test_uncertain_stale_notice_keeps_audit_but_retry_is_forbidden(self):
        from vneurok.delivery import resolve_delivery
        row=self.act(self.opened(),'register',who='student-helper')
        old=[r for r in self.queue_rows(row['id']) if 'запись подтверждена' in r['event']][0]
        with self.store.connection(True) as db:
            db.execute("UPDATE outbox SET state='uncertain',attempts=1,last_error='delivery_not_confirmed' WHERE id=?",(old['id'],))
        row=self.store.edit('student-author',row['id'],dict(version=row['version'],title='Новые условия участия'))
        row=self.act(self.act(row,'submit'),'approve',who='curator')
        self.error('forbidden',lambda:resolve_delivery(self.store,'admin',dict(queue='outbox',key=str(old['id']),decision='retry_not_delivered',note='Тестовый оператор проверил отсутствие сообщения.',confirmed_not_delivered=True)))
        with self.store.connection() as db:
            audited=db.execute('SELECT * FROM outbox WHERE id=?',(old['id'],)).fetchone()
            self.assertEqual(audited['state'],'uncertain')
            self.assertEqual(audited['last_error'],'delivery_not_confirmed')

    def test_unregister_invalidates_confirmation_for_participant_and_owner(self):
        row=self.act(self.opened(),'register',who='student-helper')
        originals=[]
        for who in ('student-helper','student-author'):
            item=[r for r in self.queue_rows(row['id'],who) if 'запись подтверждена' in r['event']][0]
            originals.append(self.clone_states(item))
        row=self.act(row,'unregister',who='student-helper')
        for before in originals:self.assert_invalidated(before)
        row=self.act(row,'register',who='student-helper')
        # A later new signup must not revive any earlier confirmation or cancellation.
        for before in originals:self.assert_invalidated(before)
        self.assertEqual(row['conditions_revision'],1)

    def test_unfollow_and_topic_optout_do_not_revive_on_resubscribe(self):
        row=self.opened()
        row=self.act(row,'follow',who='student-helper')
        row=self.store.edit('student-author',row['id'],dict(version=row['version'],title='Изменённые условия'))
        source=self.queue_rows(row['id'])[-1];before=self.clone_states(source)
        self.store.unsubscribe('student-helper',row['id']);self.assert_invalidated(before)
        row=self.store.detail(row['id'],'student-author')
        self.store.set_interest_subscription('student-helper','technology',True)
        row=self.act(self.act(row,'submit'),'approve',who='curator')
        topic=[r for r in self.queue_rows(row['id']) if json.loads(r['payload']).get('interest_id')][-1]
        before=self.clone_states(topic)
        self.store.set_interest_subscription('student-helper','technology',False)
        self.store.set_interest_subscription('student-helper','technology',True)
        self.assert_invalidated(before)

    def test_admin_access_changes_revoke_all_unsent_states(self):
        row=self.act(self.opened(),'register',who='student-helper')
        source=[r for r in self.queue_rows(row['id']) if 'запись подтверждена' in r['event']][0]
        before=self.clone_states(source)
        self.admin('upsert_user',id='student-helper',name='Помощник',role='student',class_id='7Б')
        self.assert_invalidated(before)
        self.admin('upsert_user',id='student-helper',name='Помощник',role='student',class_id='7А')
        self.assert_invalidated(before)

    def test_other_registration_and_task_do_not_obsolete_current_confirmation(self):
        row=self.act(self.opened(),'register',who='student-helper')
        original=self.queue_rows(row['id'])[-1]
        row=self.act(row,'register',who='student-visitor')
        row=self.store.task('student-author',row['id'],dict(version=row['version'],title='Подготовить материалы'))
        self.assertEqual(row['conditions_revision'],1)
        with self.store.connection() as db:
            self.assertTrue(self.store.notification_allowed(db,row,self.store.user('student-helper',db),json.loads(original['payload'])))


class ClosedTeamAttendanceTests(DomainFixture):
    def approved_team(self):
        row=self.make(kind='team',capacity=4)
        row=self.act(row,'invite',user_id='student-helper')
        row=self.store.task('student-author',row['id'],dict(version=row['version'],title='Подготовить комнату',assignee_id='student-visitor'))
        return self.act(self.act(row,'submit'),'approve',who='curator')

    def past(self,row):
        with self.store.connection(True) as db:
            db.execute('UPDATE initiatives SET starts_at=?,ends_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),row['id']))
        return self.store.detail(row['id'],'student-author')

    def test_team_attendance_is_separate_private_and_required(self):
        row=self.approved_team()
        self.assertEqual(row['registrations'],[])
        self.assertEqual(row['registration_count'],0)
        self.assertEqual({p['user_id'] for p in row['team_attendance']},{'student-author','student-helper','student-visitor'})
        self.assertEqual(row['attendance_pending'],3)
        helper=self.store.detail(row['id'],'student-helper')
        self.assertEqual([p['user_id'] for p in helper['team_attendance']],['student-helper'])
        self.assertIsNone(helper['attendance_pending'])
        self.error('invalid_state',lambda:self.act(row,'attendance',who='curator',user_id='student-helper',attended=True))
        row=self.past(row)
        row=self.act(row,'submit_result',result='Команда подготовила и обсудила школьный проект.')
        self.error('attendance_required',lambda:self.act(row,'complete',who='curator'))
        self.error('forbidden',lambda:self.act(row,'attendance',who='student-helper',user_id='student-helper',attended=True))
        self.error('not_team_member',lambda:self.act(row,'attendance',who='curator',user_id='admin',attended=True))
        for uid,attended in (('student-author',True),('student-helper',True),('student-visitor',False)):
            row=self.act(row,'attendance',who='curator',user_id=uid,attended=attended)
        self.assertEqual(row['attendance_pending'],0)
        self.assertTrue(all(p['recorded_by']=='curator' and p['recorded_at'] for p in row['team_attendance']))
        row=self.act(row,'complete',who='curator')
        self.assertEqual(row['status'],'completed')
        self.assertFalse(row['attendance_legacy_unknown'])
        restored=Store(self.store.path).detail(row['id'],'curator')
        self.assertEqual(restored['team_attendance'],row['team_attendance'])
        helper=self.store.detail(row['id'],'student-helper')
        attendance_notes=[h['note'] for h in helper['history'] if h['action']=='attendance']
        self.assertNotIn('student-visitor',str(attendance_notes))
        self.assertNotIn('отсутствовал',str(attendance_notes))

    def test_roster_sync_before_start_and_no_removal_after_start(self):
        row=self.approved_team()
        row=self.act(row,'remove_member',user_id='student-helper')
        self.assertEqual({p['user_id'] for p in row['team_attendance']},{'student-author','student-visitor'})
        row=self.act(row,'invite',user_id='student-helper')
        row=self.past(row)
        row=self.act(row,'attendance',who='curator',user_id='student-helper',attended=True)
        self.error('already_started',lambda:self.act(row,'remove_member',user_id='student-helper'))
        self.error('already_started',lambda:self.store.task('student-author',row['id'],dict(action='release',version=row['version']),row['tasks'][0]['id']))
        self.error('already_started',lambda:self.store.task('student-author',row['id'],dict(title='Поздняя роль',assignee_id='student-helper',version=row['version'])))
        stable=self.store.detail(row['id'],'curator')
        self.assertEqual(len(stable['team_attendance']),3)
        self.assertEqual(next(p for p in stable['team_attendance'] if p['user_id']=='student-helper')['attendance_recorded'],1)

    def test_rereview_rebuilds_only_unstarted_roster_without_event_registration(self):
        row=self.approved_team()
        row=self.store.edit('student-author',row['id'],dict(version=row['version'],title='Уточнённый план команды'))
        self.assertEqual(row['team_attendance'],[])
        row=self.act(self.act(row,'submit'),'approve',who='curator')
        self.assertEqual(len(row['team_attendance']),3)
        self.assertEqual(row['registrations'],[])
        self.assertTrue(all(not p['attendance_recorded'] for p in row['team_attendance']))

    def test_legacy_completed_team_does_not_invent_attendance(self):
        row=self.approved_team()
        with self.store.connection(True) as db:
            db.execute('DELETE FROM team_attendance WHERE initiative_id=?',(row['id'],))
            db.execute("UPDATE initiatives SET status='completed',confirmed_by='curator',confirmed_at='legacy-time',result='Исторический подтверждённый итог.' WHERE id=?",(row['id'],))
        restored=Store(self.store.path).detail(row['id'],'curator')
        self.assertEqual(restored['status'],'completed')
        self.assertEqual(restored['confirmed_at'],'legacy-time')
        self.assertTrue(restored['attendance_legacy_unknown'])
        self.assertEqual(len(restored['team_attendance']),3)
        self.assertTrue(all(p['attendance_recorded']==0 and p['recorded_by'] is None for p in restored['team_attendance']))
        self.error('invalid_state',lambda:self.act(restored,'attendance',who='curator',user_id='student-author',attended=True))


if __name__ == '__main__':
    unittest.main()
