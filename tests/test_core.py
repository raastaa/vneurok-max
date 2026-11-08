import concurrent.futures
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from vneurok.core import Store,DomainError,now_iso


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'test.db'); self.store.seed_demo()
        self.author='student-author'; self.curator='curator'; self.helper='student-helper'; self.visitor='student-visitor'
        self.n=0

    def tearDown(self): self.tmp.cleanup()

    def create(self,owner=None,**overrides):
        self.n+=1
        start=(datetime.now(timezone.utc)+timedelta(days=2)).replace(hour=7,minute=0,second=0,microsecond=0)
        raw={'title':'Мастерская игры','description':'Создадим игру вместе и покажем школьной команде.','kind':'event','space_id':'computer','starts_at':start.isoformat(),'ends_at':(start+timedelta(hours=1)).isoformat(),'capacity':2}
        raw.update(overrides)
        return self.store.create(owner or self.author,raw,f'create-test-{self.n}')

    def action(self,who,row,action,**extra):
        return self.store.action(who,row['id'],{'action':action,'version':row['version'],**extra})

    def opened(self,**overrides):
        row=self.create(**overrides)
        row=self.action(row['owner_id'],row,'submit'); row=self.action(self.curator,row,'approve')
        return self.action(row['owner_id'],row,'open_registration')

    def assertError(self,code,fn):
        with self.assertRaises(DomainError) as caught: fn()
        self.assertEqual(caught.exception.code,code)

    def test_whole_flow_and_persisted_result(self):
        row=self.create(); row=self.store.task(self.author,row['id'],{'title':'Подготовить афишу','version':row['version'],'assignee_id':self.helper})
        task=row['tasks'][0]
        row=self.store.task(self.helper,row['id'],{'action':'complete','version':row['version']},task['id'])
        self.assertEqual(row['tasks'][0]['done'],1)
        row=self.action(self.author,row,'submit'); row=self.action(self.curator,row,'approve'); row=self.action(self.author,row,'open_registration')
        row=self.action(self.visitor,row,'register')
        self.assertEqual(row['registration_count'],1)
        with self.store.connection(True) as db:
            db.execute('UPDATE initiatives SET starts_at=?,ends_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),row['id']))
        row=self.action(self.curator,row,'attendance',user_id=self.visitor,attended=True)
        row=self.action(self.author,row,'submit_result',result='Собрали игровой уровень и показали всем участникам.')
        self.assertEqual(row['status'],'awaiting_confirmation')
        row=self.action(self.curator,row,'complete')
        self.assertEqual(row['status'],'completed')
        self.assertEqual(row['registrations'][0]['attended'],1)
        self.assertEqual(Store(self.store.path).detail(row['id'],self.author)['result'],row['result'])

    def test_create_idempotency_and_payload_mismatch(self):
        row=self.create(); raw={k:row[k] for k in ('title','description','kind','space_id','starts_at','ends_at','capacity')}
        a=self.store.create(self.author,raw,'repeat-create'); b=self.store.create(self.author,raw,'repeat-create')
        self.assertEqual(a['id'],b['id'])
        self.assertError('idempotency_conflict',lambda:self.store.create(self.author,{**raw,'title':'Другая заявка'},'repeat-create'))

    def test_student_cannot_approve(self):
        row=self.action(self.author,self.create(),'submit')
        self.assertError('forbidden',lambda:self.action(self.helper,row,'approve'))

    def test_curator_cannot_self_approve(self):
        row=self.create(owner=self.curator); row=self.action(self.curator,row,'submit')
        self.assertError('forbidden',lambda:self.action(self.curator,row,'approve'))

    def test_stale_version(self):
        row=self.create(); self.action(self.author,row,'submit')
        self.assertError('stale_version',lambda:self.action(self.author,row,'submit'))

    def test_concurrent_booking_only_one_approved(self):
        a=self.action(self.author,self.create(),'submit'); b=self.action(self.helper,self.create(owner=self.helper),'submit')
        def approve(row):
            try: return self.action(self.curator,row,'approve')['status']
            except DomainError as e: return e.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool: results=list(pool.map(approve,[a,b]))
        self.assertCountEqual(results,['approved','booking_conflict'])

    def test_boundary_adjacent_reservations_allowed(self):
        a=self.action(self.author,self.create(),'submit'); self.action(self.curator,a,'approve')
        b=self.create(starts_at=a['ends_at'],ends_at=(datetime.fromisoformat(a['ends_at'])+timedelta(hours=1)).isoformat())
        b=self.action(self.author,b,'submit'); self.assertEqual(self.action(self.curator,b,'approve')['status'],'approved')

    def test_approval_revision_releases_space_and_cancels_signups(self):
        row=self.opened(); row=self.action(self.helper,row,'register')
        row=self.store.edit(self.author,row['id'],{'version':row['version'],'title':'Изменённая мастерская'})
        self.assertEqual(row['status'],'needs_changes'); self.assertIsNone(row['approved_by']); self.assertEqual(row['registration_count'],0)
        second=self.action(self.author,self.create(),'submit')
        self.assertEqual(self.action(self.curator,second,'approve')['status'],'approved')

    def test_concurrent_last_seat(self):
        row=self.opened(capacity=1)
        def join(uid):
            try: return self.action(uid,row,'register')['registration_count']
            except DomainError as e: return e.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool: results=list(pool.map(join,[self.helper,self.visitor]))
        self.assertIn(1,results); self.assertEqual(self.store.detail(row['id'],self.author)['registration_count'],1)
        fresh=self.store.detail(row['id'],self.author)
        loser=self.visitor if self.store.detail(row['id'],self.helper)['my_registration'] else self.helper
        self.assertError('full',lambda:self.action(loser,fresh,'register'))

    def test_repeated_join_is_idempotent(self):
        row=self.opened(); old=dict(row)
        row=self.action(self.helper,row,'register'); again=self.action(self.helper,old,'register')
        self.assertEqual(row['version'],again['version']); self.assertEqual(again['registration_count'],1)

    def test_unregister_and_cancel_free_booking(self):
        row=self.opened(); row=self.action(self.helper,row,'register'); old=dict(row)
        row=self.action(self.helper,row,'unregister'); again=self.action(self.helper,old,'unregister')
        self.assertEqual(again['registration_count'],0)
        row=self.action(self.author,row,'cancel')
        other=self.action(self.author,self.create(),'submit')
        self.assertEqual(self.action(self.curator,other,'approve')['status'],'approved')

    def test_no_enrollment_before_approval(self):
        row=self.create(); row=self.action(self.author,row,'invite',user_id=self.helper)
        row=self.action(self.author,row,'submit')
        self.assertError('invalid_state',lambda:self.action(self.helper,row,'register'))

    def test_scheduling_conflict_for_pupil(self):
        first=self.opened(); self.action(self.helper,first,'register')
        second=self.opened(space_id='media')
        self.assertError('schedule_conflict',lambda:self.action(self.helper,second,'register'))

    def test_draft_private_and_roster_minimized(self):
        row=self.create()
        self.assertError('forbidden',lambda:self.store.detail(row['id'],self.helper))
        row=self.opened(); row=self.action(self.helper,row,'register')
        self.assertEqual(self.store.detail(row['id'],self.visitor)['registrations'],[])
        self.assertEqual(self.store.detail(row['id'],self.visitor)['registration_count'],1)

    def test_helper_task_claim_before_approval(self):
        row=self.create(); row=self.store.task(self.author,row['id'],{'title':'Подготовить материалы','version':row['version']})
        row=self.action(self.author,row,'invite',user_id=self.helper)
        row=self.action(self.author,row,'invite',user_id=self.visitor)
        tid=row['tasks'][0]['id']
        row=self.store.task(self.helper,row['id'],{'action':'claim','version':row['version']},tid)
        self.assertEqual(row['tasks'][0]['assignee_id'],self.helper)
        self.assertError('task_taken',lambda:self.store.task(self.visitor,row['id'],{'action':'claim','version':row['version']},tid))

    def test_request_changes_and_resubmit(self):
        row=self.action(self.author,self.create(),'submit')
        row=self.action(self.curator,row,'request_changes',note='Добавьте план проведения.')
        self.assertEqual(row['status'],'needs_changes')
        row=self.store.edit(self.author,row['id'],{'version':row['version'],'description':'План: знакомимся, собираем уровень и показываем результат.'})
        self.assertEqual(self.action(self.author,row,'submit')['status'],'submitted')

    def test_invalid_capacity_time_and_fields(self):
        self.assertError('invalid_capacity',lambda:self.create(capacity=100))
        self.assertError('invalid_capacity',lambda:self.create(capacity=True))
        self.assertError('invalid_field',lambda:self.create(role='curator'))
        self.assertError('past_time',lambda:self.create(starts_at='2020-01-01T07:00:00Z'))
        self.assertError('invalid_time',lambda:self.create(starts_at='2027-01-01T07:00:00'))
        self.assertError('unavailable',lambda:self.create(starts_at='2030-01-01T07:00:00Z',ends_at='2030-01-01T08:00:00Z'))

    def test_team_has_no_public_registration(self):
        row=self.create(kind='team'); row=self.action(self.author,row,'submit'); row=self.action(self.curator,row,'approve')
        self.assertError('invalid_state',lambda:self.action(self.author,row,'open_registration'))

    def test_no_premature_attendance_or_completion(self):
        row=self.opened(); row=self.action(self.helper,row,'register')
        self.assertError('forbidden',lambda:self.action(self.author,row,'attendance',user_id=self.helper,attended=True))
        self.assertError('invalid_state',lambda:self.action(self.curator,row,'attendance',user_id=self.helper,attended=True))
        self.assertError('invalid_state',lambda:self.action(self.author,row,'submit_result',result='Попытка выдать будущее событие за завершённое.'))

    def test_cannot_remove_confirmed_attendance(self):
        row=self.opened(); row=self.action(self.helper,row,'register')
        with self.store.connection(True) as db:
            db.execute('UPDATE initiatives SET starts_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),row['id']))
        row=self.action(self.curator,row,'attendance',user_id=self.helper,attended=True)
        self.assertError('already_started',lambda:self.action(self.helper,row,'unregister'))
        self.assertEqual(self.store.detail(row['id'],self.curator)['registrations'][0]['attended'],1)

    def test_release_last_task_loses_draft_access_but_commits(self):
        row=self.create(); row=self.store.task(self.author,row['id'],{'title':'Подготовить афишу','version':row['version'],'assignee_id':self.helper})
        response=self.store.task(self.helper,row['id'],{'action':'release','version':row['version']},row['tasks'][0]['id'])
        self.assertTrue(response['removed_from_card'])
        self.assertIsNone(self.store.detail(row['id'],self.author)['tasks'][0]['assignee_id'])
        self.assertError('forbidden',lambda:self.store.detail(row['id'],self.helper))

    def test_notifications_do_not_affect_commit(self):
        row=self.action(self.author,self.create(),'submit'); row=self.action(self.curator,row,'approve')
        with self.store.connection() as db:
            self.assertGreater(db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM outbox WHERE state='sent'").fetchone()[0],0)
        self.assertEqual(self.store.detail(row['id'],self.author)['status'],'approved')

if __name__=='__main__': unittest.main()
