"""Local guided bot acceptance tests. Synthetic actors only, no MAX requests."""
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from vneurok.bot import BotRouter
from vneurok.core import Store


class GuidedBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'bot';self.school=Store(Path(self.tmp.name)/'school.db');self.school.seed_demo()
        self.bot=BotRouter(self.root,self.school);self.n=0
        self.say('/demo')

    def say(self,text,user=1):
        self.n+=1
        reply=self.bot.handle(user,text,'guided-'+str(self.n))
        self.assertNotIn('Не удалось закончить обработку',reply)
        self.assertNotIn('Новые действия пока остановлены',reply)
        return reply

    def store(self):
        return Store(self.root/(hashlib.sha256(b'1').hexdigest()+'.sqlite3'))

    def state(self):
        identity=self.bot._identity('1')
        return self.bot._state('1',identity[3])

    def create(self,kind='1'):
        for text in ('/new','Идея клуба','Создадим клуб настольных игр после уроков',kind):self.say(text)
        reply=self.say('да');self.assertIn('Сохранено',reply)
        return self.state()['current']

    def plan(self):
        tomorrow=(datetime.now(timezone.utc)+timedelta(days=2)).astimezone(timezone(timedelta(hours=9))).replace(hour=15,minute=0,second=0,microsecond=0)
        for text in ('/plan','1','0','2',tomorrow.strftime('%Y-%m-%d %H:%M'),(tomorrow+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'),'5'):
            self.say(text)
        return self.say('да')

    def test_short_draft_no_dates_and_durable_resume(self):
        self.say('/new');self.say('Новый школьный клуб')
        self.bot=BotRouter(self.root,self.school)
        self.assertIn('Коротко опишите',self.say('/resume'))
        self.say('Будем обсуждать и создавать настольные игры');self.say('2')
        key='exact-create-confirm'
        reply=self.bot.handle(1,'да',key);self.assertIn('Черновик',reply)
        self.assertEqual(reply,self.bot.handle(1,'да',key))
        row=self.store().detail(self.state()['current'],'student-author')
        self.assertEqual(row['kind'],'team');self.assertIsNone(row['space_id']);self.assertFalse(row['starts_at']);self.assertIsNone(row['capacity'])
        self.assertNotIn(row['id'],reply)
        self.assertEqual(len(self.store().bootstrap('student-author')['initiatives']),2)

    def test_back_cancel_and_validation_preserve_existing_card(self):
        self.say('/new');self.say('Первая идея');self.say('Описание первой школьной идеи')
        self.assertIn('Коротко опишите',self.say('/back'))
        self.assertIn('10–2000',self.say('мало'))
        self.assertIn('Коротко опишите',self.say('/resume'))
        self.assertIn('Ввод остановлен',self.say('/cancel'))
        self.assertEqual(len(self.store().bootstrap('student-author')['initiatives']),1)

    def test_alias_navigation_and_number_choices(self):
        self.assertIn('1.',self.say('Все идеи'))
        row=self.say('1');self.assertIn('Создаём',row);self.assertNotIn('demo-game-workshop',row)
        menu=self.say('/menu');self.assertIn('1. Заполнить условия',menu)
        self.assertIn('Кому предназначена',self.say('1'))
        self.assertIn('незавершённый ввод',self.say('Новая идея'))
        self.assertIn('/resume',self.say('Помощь'))
        self.assertEqual(self.state()['wizard']['step'],'audience')

    def test_plan_no_room_submit_and_helper_before_review(self):
        iid=self.create('2');self.say('/invite');self.say('student-helper')
        self.assertIn('увидит черновик',self.say('/resume'));self.say('да')
        self.assertIn('student-helper',self.store().detail(iid,'student-author')['members'][0]['user_id'])
        self.assertIn('Сохранено',self.plan())
        row=self.store().detail(iid,'student-author')
        self.assertEqual(row['needs_space'],0);self.assertIsNone(row['space_id']);self.assertEqual(row['booking_status'],'not_required')
        self.assertIn('На согласовании',self.say('/submit'))
        self.say('/role participant')
        self.assertNotIn('Идея клуба',self.say('/list'))

    def test_versions_reject_stale_current_and_wizard(self):
        iid=self.create();store=self.store();row=store.detail(iid,'student-author')
        store.edit('student-author',iid,dict(version=row['version'],description='Другой клиент сохранил более свежий текст'))
        self.assertIn('Карточка уже изменилась',self.say('/submit'))
        self.say('/view');self.say('/task');self.say('Подготовить листовку')
        row=store.detail(iid,'student-author');store.edit('student-author',iid,dict(version=row['version'],description='Вторая правка в другом открытом клиенте'))
        self.assertIn('Карточка уже изменилась',self.say('да'))
        self.assertEqual(store.detail(iid,'student-author')['tasks'],[])
        self.say('/cancel');self.say('/view');self.assertIn('Вторая правка',self.say('/view'))

    def test_interest_opt_in_opt_out_confirmation(self):
        self.assertIn('выключены',self.say('Мои интересы'));self.say('1')
        self.assertEqual(self.store().bootstrap('student-author')['interest_subscriptions'],[])
        self.say('да');self.assertEqual(len(self.store().bootstrap('student-author')['interest_subscriptions']),1)
        self.say('/interests');self.say('1');self.say('нет')
        self.assertEqual(len(self.store().bootstrap('student-author')['interest_subscriptions']),1)
        self.say('/interests');self.say('1');self.say('да')
        self.assertEqual(self.store().bootstrap('student-author')['interest_subscriptions'],[])

    def test_review_decisions_require_reason_and_confirmation(self):
        iid=self.create();self.plan();self.say('/submit');self.say('/role curator');self.say('/view '+iid)
        menu=self.say('/menu');self.assertIn('Отложить',menu);self.assertIn('Отклонить',menu)
        self.say('/defer');self.assertIn('3–1000',self.say('x'));self.say('Дождёмся окончания контрольной недели')
        self.assertEqual(self.store().detail(iid,'curator')['status'],'submitted')
        self.assertIn('Отложено',self.say('да'))
        self.say('/role author');self.say('/view '+iid);self.say('/submit')
        self.say('/role curator');self.say('/view '+iid);self.say('/decline');self.say('Идея сейчас не соответствует правилам школы')
        self.assertIn('закрыть инициативу',self.say('/resume'));self.assertIn('Отклонено',self.say('да'))
        self.assertNotIn('Отменить инициативу',self.say('/menu'))

    def test_attendance_explicit_absence_then_completion(self):
        iid=self.create();self.plan();self.say('/submit');self.say('/role curator');self.say('/approve '+iid)
        self.say('/role author');self.say('/open '+iid);self.say('/role participant');self.say('/join '+iid)
        self.say('/demo_end '+iid);self.say('/role author');self.say('/view '+iid)
        self.say('/result');self.say('Провели встречу и записали полезные выводы');self.say('да')
        self.say('/role curator');self.say('/view '+iid)
        self.assertIn('каждого',self.say('/confirm'))
        self.say('/attend');self.say('1');self.say('нет')
        self.assertIn('Не пришёл',self.say('/resume'));self.say('да')
        self.assertIn('Завершено',self.say('/confirm'))

    def test_closed_team_attendance_is_guided_and_required(self):
        iid=self.create('2');self.plan()
        self.say('/invite '+iid+' student-helper');self.say('/submit')
        self.say('/role curator');self.say('/approve '+iid);self.say('/demo_end '+iid)
        self.say('/role author');self.say('/view '+iid)
        self.say('/result');self.say('Команда провела встречу и зафиксировала итоги');self.say('да')
        self.say('/role curator');self.say('/view '+iid)
        self.assertIn('Отметить пришёл',self.say('/menu'))
        self.assertIn('каждого',self.say('/confirm'))
        for choice,answer in [('1','да'),('2','нет')]:
            self.say('/attend');self.say(choice);self.say(answer);self.say('да')
        self.assertIn('Завершено',self.say('/confirm'))
        row=self.store().detail(iid,'curator')
        self.assertEqual(row['registrations'],[])
        self.assertEqual(len(row['team_attendance']),2)
        self.assertEqual(row['attendance_pending'],0)

    def test_cancel_has_confirmation_and_no_side_effect_on_no(self):
        iid=self.create();self.say('/cancel .');self.say('нет')
        self.assertEqual(self.store().detail(iid,'student-author')['status'],'draft')
        self.say('/cancel .');self.say('да')
        self.assertEqual(self.store().detail(iid,'student-author')['status'],'cancelled')

    def test_school_unknown_identity_no_role_grant(self):
        with self.school.connection() as db:before=db.execute('SELECT count(*) FROM users').fetchone()[0]
        self.assertIn('MAX ID: 92',self.say('/identity',92))
        self.assertIn('приглашения',self.say('/school',92))
        self.assertIn('MAX ID: 92',self.say('/me',92))
        self.assertIn('прервано',self.say('/cancel',92))
        with self.school.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],before)

    def test_private_cached_school_reply_revoked(self):
        with self.school.connection(True) as db:db.execute("UPDATE users SET max_id='77' WHERE id='student-author'")
        self.say('/school',77)
        reply=self.bot.handle(77,'/view demo-game-workshop','private-card');self.assertIn('Создаём',reply)
        with self.school.connection(True) as db:db.execute("UPDATE users SET active=0,access_version=access_version+1 WHERE id='student-author'")
        hidden=self.bot.handle(77,'/view demo-game-workshop','private-card');self.assertNotIn('Создаём',hidden);self.assertIn('Старый ответ скрыт',hidden)

    def test_school_onboarding_bound_code_and_no_self_elevation(self):
        from vneurok.onboarding import create_invitation
        invitation=create_invitation(self.school,'admin',dict(max_user_id=109,name='Тестовый ученик',role='student',class_id='7А',confirm=True))
        self.say('/school',110)
        self.assertIn('другому аккаунту',self.say(invitation['code'],110))
        self.say('/school',109)
        reply=self.say(invitation['code'],109)
        self.assertIn('Приглашение принято',reply);self.assertNotIn(invitation['code'],reply)
        user=self.school.max_user(109);self.assertEqual(user['role'],'student');self.assertFalse(user['is_admin'])
        self.assertIn('администратор',self.say('/role curator',109));self.assertEqual(self.school.max_user(109)['role'],'student')
        self.assertIn('Коротко опишите',self.say('/new Идея школы',109));self.say('/cancel',109)
        again=self.say('/school '+invitation['code'],109);self.assertIn('Приглашение принято',again)
        self.assertEqual(self.school.max_user(109)['id'],user['id'])

    def test_school_invite_picker_does_not_require_uuid(self):
        with self.school.connection(True) as db:db.execute("UPDATE users SET max_id='88' WHERE id='student-author'")
        self.say('/school',88);self.say('/view demo-game-workshop',88)
        prompt=self.say('/invite',88);self.assertIn('1.',prompt)
        candidates=self.school.detail('demo-game-workshop','student-author')['helper_candidates'];chosen=candidates[0]
        self.say('1',88);self.assertIn(chosen['name'],self.say('/resume',88));self.say('да',88)
        self.assertIn(chosen['id'],[p['user_id'] for p in self.school.detail('demo-game-workshop','student-author')['members']])

    def test_cached_school_card_hidden_after_helper_removed(self):
        iid='demo-game-workshop'
        with self.school.connection(True) as db:db.execute("UPDATE users SET max_id='91' WHERE id='student-helper'")
        row=self.school.detail(iid,'student-author')
        self.school.action('student-author',iid,dict(action='invite',user_id='student-helper',version=row['version']))
        self.say('/school',91)
        original=self.bot.handle(91,'/view '+iid,'helper-private-view');self.assertIn('Создаём',original)
        guard=self.bot.reply_guard(91,'helper-private-view');self.assertEqual(guard['cards'][0]['id'],iid)
        self.assertEqual(guard['access']['id'],'student-helper')
        row=self.school.detail(iid,'student-author')
        self.school.action('student-author',iid,dict(action='remove_member',user_id='student-helper',version=row['version']))
        hidden=self.bot.handle(91,'/view '+iid,'helper-private-view')
        self.assertNotIn('Создаём',hidden);self.assertIn('Старый ответ скрыт',hidden)

    def test_cached_public_card_hidden_when_revision_becomes_private(self):
        iid='demo-game-workshop'
        with self.school.connection(True) as db:db.execute("UPDATE users SET max_id='93' WHERE id='student-visitor'")
        row=self.school.detail(iid,'student-author')
        row=self.school.action('student-author',iid,dict(action='submit',version=row['version']))
        row=self.school.action('curator',iid,dict(action='approve',version=row['version']))
        self.say('/school',93)
        self.bot.handle(93,'/view '+iid,'public-before-revision')
        original_guard=self.bot.reply_guard(93,'public-before-revision')
        self.school.edit('student-author',iid,dict(version=row['version'],title='Новая закрытая редакция'))
        hidden=self.bot.handle(93,'/view '+iid,'public-before-revision')
        self.assertIn('Старый ответ скрыт',hidden);self.assertNotIn('Новая закрытая',hidden)
        self.assertEqual(self.bot.reply_guard(93,'public-before-revision'),original_guard)
        self.assertNotIn('Новая закрытая',self.say('/list',93))

    def test_legacy_edit_of_approved_event_confirms_registration_loss(self):
        iid=self.create();self.plan();self.say('/submit');self.say('/role curator');self.say('/approve '+iid)
        self.say('/role author');self.say('/view '+iid)
        prompt=self.say('/edit . title | Исправленное название')
        self.assertIn('записи будут отменены',prompt)
        self.assertEqual(self.store().detail(iid,'student-author')['status'],'approved')
        self.say('нет');self.assertEqual(self.store().detail(iid,'student-author')['status'],'approved')
        self.say('/edit . title | Исправленное название');self.say('да')
        self.assertEqual(self.store().detail(iid,'student-author')['status'],'needs_changes')

    def test_missing_school_store_is_recoverable(self):
        with self.school.connection(True) as db:db.execute("UPDATE users SET max_id='94' WHERE id='student-author'")
        self.say('/school',94);self.bot=BotRouter(self.root,None)
        self.assertIn('не подключён',self.say('/list',94))
        self.assertIn('Учебный пример',self.say('/demo',94))

    def test_class_audience_picker_uses_school_name_and_own_class(self):
        self.create();self.say('/plan');prompt=self.say('3')
        self.assertIn('1. 7А',prompt);self.assertNotIn('8Б',prompt)
        self.say('1');self.assertEqual(self.state()['wizard']['data']['audience_value'],'7А')
        self.say('/cancel')

    def test_legacy_cached_reply_without_access_metadata_is_hidden(self):
        with self.bot._db() as db:db.execute('INSERT INTO inbox VALUES(?,?,?)',('1','old-cache','Закрытый текст из старого кеша'))
        reply=self.bot.handle(1,'/view hidden','old-cache')
        self.assertNotIn('Закрытый текст',reply);self.assertIn('Старый ответ скрыт',reply)

    def test_spaces_include_rules_and_no_private_titles(self):
        reply=self.say('/spaces')
        self.assertIn('Оборудование',reply);self.assertIn('Правила',reply);self.assertIn('Ответственный',reply);self.assertIn('без помещения',reply)

if __name__=='__main__':unittest.main()
