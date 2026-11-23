from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from unittest.mock import patch

from pplan.access import create_invite,redeem_invite,revoke_access,parent_for_actor
from pplan.core import Plan,PlanError,Store
from pplan.feedback import enqueue_improvement
from pplan.miniapp import make_handler
from pplan.telegram import Bot
from tests.test_plan import Fixture
from tests.test_miniapp import signed,TOKEN


class AccessTests(Fixture):
    def join(self,actor=99):
        invitation=create_invite(self.plan,42)
        return redeem_invite(self.plan,actor,invitation['code'])

    def update(self,actor,text,update_id=1,chat_type='private'):
        return {'update_id':update_id,'message':{'from':{'id':actor},'chat':{'id':actor,'type':chat_type},'text':text}}

    def test_invitation_is_hashed_once_and_normalizes_code(self):
        invitation=create_invite(self.plan,42)
        row=self.store.db.execute('SELECT * FROM parent_invites').fetchone()
        raw=invitation['code'].replace('-','')
        self.assertEqual(row['code_hash'],hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn(raw,str(dict(row)))
        self.assertEqual(redeem_invite(self.plan,99,invitation['code'].lower())['parent_id'],'p2')
        self.assertFalse(redeem_invite(self.plan,99,invitation['code'])['new'])
        with self.assertRaises(PlanError):redeem_invite(self.plan,100,invitation['code'])
        with self.assertRaises(PlanError):create_invite(self.plan,42)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM family_members WHERE revoked IS NULL').fetchone()[0],1)

    def test_expired_replaced_and_owner_codes_do_not_grant_access(self):
        old=create_invite(self.plan,42);new=create_invite(self.plan,42)
        with self.assertRaises(PlanError):redeem_invite(self.plan,99,old['code'])
        with self.assertRaises(PlanError):redeem_invite(self.plan,42,new['code'])
        self.now+=timedelta(hours=24)
        with self.assertRaises(PlanError):redeem_invite(self.plan,99,new['code'])
        self.assertFalse(self.plan.has_access(99))

    def test_failed_attempts_persist_and_throttle(self):
        invitation=create_invite(self.plan,42)
        for _ in range(5):
            with self.assertRaises(PlanError):redeem_invite(self.plan,99,'WRONG')
        with self.assertRaisesRegex(PlanError,'Too many'):redeem_invite(self.plan,99,invitation['code'])
        self.now+=timedelta(minutes=15)
        self.assertTrue(redeem_invite(self.plan,99,invitation['code'])['new'])

    def test_invited_parent_can_change_schedule_and_manage_shared_events(self):
        existing=self.plan.add_special(42,self.start,self.start,'Owner event')
        self.join()
        pending=self.plan.draft(99,self.start,self.start,'p2',['c1'])
        with self.assertRaises(PlanError):self.plan.confirm(42,pending['id'])
        self.plan.confirm(99,pending['id'])
        self.assertEqual(self.plan.assignment('c1',self.start)['parent'],'p2')
        self.plan.update_special(99,existing['id'],self.start,self.start,'Edited by second','Notes','#2563eb',expected_revision=1)
        created=self.plan.add_special(99,self.start,self.start,'Second event')
        self.assertEqual(len(self.plan.special_events()),2)
        self.plan.remove_special(42,created['id'])
        self.plan.remove_special(99,existing['id'])
        self.assertEqual(self.plan.special_events(),[])
        self.assertEqual(parent_for_actor(self.plan,99),'p2')
        with self.assertRaises(PlanError):enqueue_improvement(self.plan,99,'Unapproved coding task')
        with self.assertRaises(PlanError):create_invite(self.plan,99)
        with self.assertRaises(PlanError):revoke_access(self.plan,99)

    def test_revocation_blocks_old_buttons_and_rejoining_old_code(self):
        invitation=create_invite(self.plan,42);redeem_invite(self.plan,99,invitation['code'])
        owner=self.draft();guest=self.plan.draft(99,self.start,self.start,'p2',['c2'])
        revoke_access(self.plan,42)
        self.assertFalse(self.plan.has_access(99))
        with self.assertRaises(PlanError):self.plan.confirm(99,guest['id'])
        with self.assertRaises(PlanError):redeem_invite(self.plan,99,invitation['code'])
        self.plan.confirm(42,owner['id'])
        new=create_invite(self.plan,42);redeem_invite(self.plan,100,new['code'])
        self.assertTrue(self.plan.has_access(100))
        self.assertFalse(self.plan.has_access(99))

    def test_bot_join_is_private_deduplicated_and_does_not_expose_plan_to_strangers(self):
        bot=Bot(self.plan,SimpleNamespace())
        invitation=create_invite(self.plan,42)
        bot.handle_update(self.update(99,'/join '+invitation['code'],chat_type='group'))
        self.assertFalse(self.plan.has_access(99))
        with patch.dict(os.environ,{'PPLAN_MINIAPP_URL':'https://calendar.example:8443'}):
            update=self.update(99,'/join '+invitation['code'],2)
            bot.handle_update(update);bot.handle_update(update)
        jobs=[json.loads(row[0]) for row in self.store.db.execute('SELECT payload FROM outbox')]
        self.assertEqual(sum(job['method']=='setChatMenuButton' for job in jobs),1)
        self.assertEqual(sum(job['method']=='sendMessage' and job['payload']['chat_id']==42 for job in jobs),1)
        bot.handle_update(self.update(100,'/start',3))
        message=json.loads(self.store.db.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertIn('/join CODE',message['payload']['text'])
        self.assertNotIn(self.config.children['c1'],message['payload']['text'])

    def test_ai_context_and_conversations_are_separate_for_each_parent(self):
        self.join();contexts=[]
        def interpret(text,context):
            contexts.append(context)
            return {'action':'clarify','start':None,'end':None,'parent':None,'children':[],'question':'Which date?'}
        bot=Bot(self.plan,SimpleNamespace(),SimpleNamespace(interpret=interpret))
        bot.handle_update(self.update(42,'Please change my time',1))
        bot.handle_update(self.update(99,'Please change my time',2))
        self.assertEqual([context['operator_parent_id'] for context in contexts],['p1','p2'])
        self.assertEqual(contexts[1]['conversation'],[])
        self.assertIn('conversation:99',[row[0] for row in self.store.db.execute('SELECT key FROM settings')])

    def test_queued_family_replies_are_dropped_after_revocation(self):
        self.join();sent=[]
        bot=Bot(self.plan,SimpleNamespace(call=lambda method,payload:sent.append((method,payload))))
        bot.say(99,'Private family details');bot.say(42,'Owner details')
        revoke_access(self.plan,42);bot.flush()
        self.assertEqual([payload['chat_id'] for _,payload in sent],[42])

    def test_concurrent_redemption_allows_only_one_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'plan.sqlite3';store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            invitation=create_invite(plan,42);store.close()
            def attempt(actor):
                current=Store(path,initialize=False)
                try:
                    return redeem_invite(Plan(self.config,current,lambda:self.now,initialize=False),actor,invitation['code'])['telegram_id']
                except PlanError:return None
                finally:current.close()
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,(99,100)))
            self.assertEqual(sum(result is not None for result in results),1)

    def test_invited_parent_api_can_read_and_edit_then_loses_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'plan.sqlite3';store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            event=plan.add_special(42,self.start,self.start,'Shared')
            invitation=create_invite(plan,42);redeem_invite(plan,99,invitation['code'])
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.config,path,TOKEN))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def call(route,body):
                try:
                    with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+route,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'tma '+signed(99)})) as response:return response.status,json.load(response)
                except HTTPError as exc:return exc.code,json.load(exc)
            try:
                self.assertEqual(call('/api/calendar',{'month':'2026-11'})[0],200)
                body={key:event[key] for key in ('id','start','end','title','description','color','revision')};body.update(title='Guest edit')
                self.assertEqual(call('/api/specials/update',body)[0],200)
                self.assertEqual(plan.special_events()[0]['title'],'Guest edit')
                revoke_access(plan,42)
                for route in ('/api/calendar','/api/specials/list','/api/specials/update'):
                    self.assertEqual(call(route,{})[0],401)
            finally:server.shutdown();server.server_close();thread.join();store.close()
