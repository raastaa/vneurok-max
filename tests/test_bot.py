import tempfile
import unittest
from pathlib import Path
from vneurok.bot import BotRouter
from vneurok.core import Store

class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.school = Store(Path(self.tmp.name)/'school.db')
        self.school.seed_demo()
        self.bot = BotRouter(Path(self.tmp.name)/'demos',self.school)
        self.n = 0
    def say(self,text,user=1):
        self.n += 1
        return self.bot.handle(user,text,str(self.n))
    def test_full_route(self):
        iid='demo-game-workshop'
        self.say('/demo')
        self.assertIn('student-helper',self.say('/invite '+iid+' student-helper'))
        self.assertIn('На согласовании',self.say('/submit '+iid))
        self.say('/role helper')
        self.assertIn('в работе',self.say('/claim '+iid+' demo-poster'))
        self.assertIn('готово',self.say('/done '+iid+' demo-poster'))
        self.say('/role curator')
        self.assertIn('Согласовано',self.say('/approve '+iid))
        self.say('/role author')
        self.assertIn('Запись открыта',self.say('/open '+iid))
        self.say('/role participant')
        self.assertIn('Записано 1/10',self.say('/join '+iid))
        self.say('/demo_end '+iid)
        self.say('/role curator')
        self.assertIn('Запись открыта',self.say('/attend '+iid+' student-visitor да'))
        self.say('/role author')
        self.assertIn('Итог ждёт подтверждения',self.say('/result '+iid+' Все вместе создали учебную игру.'))
        self.say('/role curator')
        self.assertIn('Завершено',self.say('/confirm '+iid))
    def test_replay_isolation(self):
        self.say('/demo')
        first=self.bot.handle(1,'/task demo-game-workshop Подготовить комнату','same')
        self.assertEqual(first,self.bot.handle(1,'/task demo-game-workshop Другая задача','same'))
        self.say('/demo',2)
        self.assertNotIn('Подготовить комнату',self.say('/view demo-game-workshop',2))
        self.assertNotIn('Подготовить комнату',self.school.detail('demo-game-workshop','student-author')['description'])
    def test_school_roles(self):
        self.assertIn('список',self.say('/school'))
        with self.school.connection(True) as db:
            db.execute("UPDATE users SET max_id='1' WHERE id='student-author'")
        self.say('/school')
        self.assertIn('администратор',self.say('/role curator'))
        self.assertIn('только',self.say('/demo_end demo-game-workshop'))
        self.say('/submit demo-game-workshop')
        self.assertIn('другой пользователь',self.say('/approve demo-game-workshop'))
    def test_bad_create_preserves_mode(self):
        self.say('/demo')
        self.assertIn('Неверный формат',self.say('/new Игра | Описание события | event | computer | нет | нет | 4'))
        self.assertIn('Создаём',self.say('/list'))
    def test_uncertain_not_replayed(self):
        with self.bot._db() as db:
            db.execute('INSERT INTO inbox VALUES(?,?,NULL)',('1','crash'))
        self.assertIn('уточняется',self.bot.handle(1,'/demo','crash'))
        self.assertIn('остановлены',self.say('/demo'))

    def test_short_welcome(self):
        reply=self.say('/start')
        self.assertIn('/demo',reply)
        self.assertNotIn('/new',reply)
        self.assertLess(len(reply),500)

    def test_long_card_and_team_next(self):
        row=self.school.detail('demo-game-workshop','student-author')
        row.update(description='Я'*2000,result='И'*2000,review_note='К'*1000,
                   status='approved',kind='team')
        row['tasks']=[dict(id=str(i).zfill(32),title='З'*160,done=0,assignee_id=None) for i in range(20)]
        reply=self.bot._card(row)
        self.assertLessEqual(len(reply),3900)
        self.assertNotIn('/open',reply)
        self.assertIn('/result',reply)
        self.assertIn('Ещё задач: 10',reply)
        self.assertNotIn(str(10).zfill(32)+':',reply)
        self.assertEqual(len(self.bot._limit('x'*10000)),3900)

    def test_list_limit(self):
        with self.school.connection(True) as db:
            db.execute("UPDATE users SET max_id='1' WHERE id='student-author'")
        self.say('/school')
        row=self.school.detail('demo-game-workshop','student-author')
        fields={k:row[k] for k in ('title','description','kind','space_id','starts_at','ends_at','capacity')}
        for i in range(12):
            self.school.create('student-author',fields,'more-rows-'+str(i))
        reply=self.say('/list')
        self.assertEqual(reply.count('/view '),11) # 10 карточек и подсказка.
        self.assertIn('Ещё 3 инициатив',reply)
        self.assertLessEqual(len(reply),3900)

    def test_school_private_card_hidden_from_uninvited_account(self):
        with self.school.connection(True) as db:
            db.execute("UPDATE users SET max_id='1' WHERE id='student-author'")
            db.execute("UPDATE users SET max_id='2' WHERE id='student-visitor'")
            db.execute("UPDATE initiatives SET kind='team' WHERE id='demo-game-workshop'")
        self.say('/school');self.say('/submit demo-game-workshop')
        self.say('/school',2)
        self.assertIn('Пока ничего нет',self.say('/list',2))
        self.assertNotIn('Создаём',self.say('/view demo-game-workshop',2))

    def test_school_unfollow_does_not_need_private_detail(self):
        iid='demo-game-workshop'
        with self.school.connection(True) as db:db.execute("UPDATE users SET max_id='2' WHERE id='student-visitor'")
        row=self.school.detail(iid,'student-author')
        for who,action in [('student-author','submit'),('curator','approve')]:
            row=self.school.action(who,iid,dict(action=action,version=row['version']))
        self.say('/school',2);self.say('/follow '+iid,2)
        row=self.school.detail(iid,'student-author')
        self.school.edit('student-author',iid,dict(version=row['version'],title='Закрытая редакция'))
        self.assertNotIn('Закрытая редакция',self.say('/view '+iid,2))
        self.assertIn('Подписка отключена',self.say('/unfollow '+iid,2))

    def test_curator_next_step_does_not_offer_student_commands(self):
        row=self.school.detail('demo-game-workshop','student-author')
        row['status']='approved'
        self.assertNotIn('/follow',self.bot._card(row,self.school.user('curator')))
        row['status']='registration'
        self.assertNotIn('/join',self.bot._card(row,self.school.user('curator')))

if __name__ == '__main__': unittest.main()
