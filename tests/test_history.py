from datetime import date,timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from pplan.access import create_invite,redeem_invite,revoke_access
from pplan.core import Plan,PlanError,Store
from pplan.history import capture_state,confirm_restore,list_versions,prepare_restore,cancel_restore
from pplan.miniapp import update_event
from pplan.telegram import Bot
from tests.test_plan import Fixture


class HistoryTests(Fixture):
    def latest(self):
        return self.store.db.execute('SELECT max(id) FROM plan_versions').fetchone()[0]

    def join(self):
        invitation=create_invite(self.plan,42);redeem_invite(self.plan,99,invitation['code'])

    def test_baseline_and_confirmed_changes_only_are_versioned(self):
        self.assertEqual(len(list_versions(self.plan,42)),1)
        pending=self.draft();self.assertEqual(self.latest(),1)
        self.plan.confirm(42,pending['id'])
        event=self.plan.add_special(42,self.start,self.start,'Event')
        self.plan.add_special(42,self.start,self.start,'Event')
        self.plan.update_special(42,event['id'],self.start,self.start,'Edited','Details','#2563eb',expected_revision=1)
        self.plan.update_special(42,event['id'],self.start,self.start,'Edited','Details','#2563eb',expected_revision=1)
        self.plan.remove_special(42,event['id'])
        self.assertEqual([row['action'] for row in reversed(list_versions(self.plan,42))],['baseline','schedule_confirmed','special_created','special_updated','special_removed'])

    def test_restoration_recovers_assignments_rules_events_without_regranting_access(self):
        event=self.plan.add_special(42,self.start,date(2026,11,25),'Original')
        self.plan.confirm(42,self.draft()['id']);target=self.latest()
        expected=self.plan.schedule(self.start,date(2026,12,6))
        self.join()
        self.plan.update_special(99,event['id'],date(2026,11,26),date(2026,11,28),'Changed','Unwanted','#2563eb',expected_revision=1)
        cycle=self.plan.draft_actions(99,[{'type':'set_cycle','start':'2026-11-24','end':None,'parent':None,'children':['c2'],'cycle':['p2','p1']}])
        self.plan.confirm(99,cycle['id']);self.plan.remove_special(99,event['id'])
        extra=self.plan.add_special(99,date(2026,12,1),date(2026,12,2),'Unwanted extra')
        pending=self.plan.draft(42,date(2026,11,25),date(2026,11,25),'p2',['c1'])
        former_version=self.latest();revoke_access(self.plan,42)
        preview=prepare_restore(self.plan,42,target)
        self.assertNotEqual(self.plan.schedule(self.start,date(2026,12,6)),expected)
        restored=confirm_restore(self.plan,42,preview['id'])
        self.assertGreater(restored,former_version)
        self.assertEqual(self.plan.schedule(self.start,date(2026,12,6)),expected)
        events=self.plan.special_events()
        self.assertEqual(len(events),1)
        self.assertEqual((events[0]['id'],events[0]['title'],events[0]['color'],events[0]['description']),(event['id'],'Original','#7845a6',''))
        self.assertGreater(events[0]['revision'],event['revision'])
        self.assertFalse(self.plan.has_access(99))
        self.assertEqual(self.plan.request(pending['id'])['status'],'cancelled')
        self.assertEqual(self.store.get('calendar_dirty'),'1')
        new=self.plan.add_special(42,self.start,self.start,'New event')
        self.assertGreater(new['id'],extra['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM plan_versions WHERE id=?',(former_version,)).fetchone()[0],1)

    def test_guest_cannot_list_preview_confirm_or_cancel_a_restore(self):
        self.join();self.plan.add_special(42,self.start,self.start,'Event')
        preview=prepare_restore(self.plan,42,1)
        for action in (lambda:list_versions(self.plan,99),lambda:prepare_restore(self.plan,99,1),
                       lambda:confirm_restore(self.plan,99,preview['id']),lambda:cancel_restore(self.plan,99,preview['id'])):
            with self.assertRaises(PlanError):action()
        self.assertEqual(len(self.plan.special_events()),1)

    def test_restore_preview_expires_or_becomes_stale_and_can_be_cancelled(self):
        self.plan.add_special(42,self.start,self.start,'First')
        preview=prepare_restore(self.plan,42,1)
        self.plan.add_special(42,self.start,self.start,'Second')
        with self.assertRaisesRegex(PlanError,'changed since'):confirm_restore(self.plan,42,preview['id'])
        expired=prepare_restore(self.plan,42,1);self.now+=timedelta(minutes=15)
        with self.assertRaisesRegex(PlanError,'expired'):confirm_restore(self.plan,42,expired['id'])
        cancelled=prepare_restore(self.plan,42,1);cancel_restore(self.plan,42,cancelled['id'])
        with self.assertRaises(PlanError):confirm_restore(self.plan,42,cancelled['id'])
        self.assertEqual(len(self.plan.special_events()),2)

    def test_history_failure_rolls_back_the_change_and_confirmation(self):
        request=self.draft()
        with patch('pplan.history.record_change',side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):self.plan.confirm(42,request['id'])
        self.assertEqual(self.plan.request(request['id'])['status'],'draft')
        self.assertEqual(self.plan.assignment('c1',self.start)['parent'],'p1')
        self.assertEqual(self.latest(),1)

    def test_restore_failure_rolls_back_data_requests_and_restore_status(self):
        self.plan.confirm(42,self.draft()['id'])
        pending=self.plan.draft(42,date(2026,11,24),date(2026,11,24),'p2',['c1'])
        preview=prepare_restore(self.plan,42,1);before=capture_state(self.plan);version=self.latest()
        with patch.object(self.plan,'_audit',side_effect=RuntimeError('storage failure')):
            with self.assertRaises(RuntimeError):confirm_restore(self.plan,42,preview['id'])
        self.assertEqual(capture_state(self.plan),before)
        self.assertEqual(self.plan.request(pending['id'])['status'],'draft')
        self.assertEqual(self.store.db.execute('SELECT status FROM restore_previews WHERE id=?',(preview['id'],)).fetchone()[0],'draft')
        self.assertEqual(self.latest(),version)

    def test_corrupt_version_is_rejected(self):
        self.plan.add_special(42,self.start,self.start,'Current')
        self.store.db.execute("UPDATE plan_versions SET state='{}' WHERE id=1")
        with self.assertRaises(PlanError):prepare_restore(self.plan,42,1)
        self.assertEqual(len(self.plan.special_events()),1)

    def test_migration_baseline_preserves_existing_events_and_pending_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'plan.sqlite3';store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            event=plan.add_special(42,self.start,self.start,'Existing')
            pending=plan.draft(42,self.start,self.start,'p2',['c1'])
            store.db.executescript('DROP TABLE plan_versions; DROP TABLE restore_previews;');store.close()
            for attempt in range(2):
                current=Store(path)
                try:
                    plan=Plan(self.config,current,lambda:self.now)
                    self.assertEqual(len(list_versions(plan,42)),1)
                    self.assertEqual(plan.special_events()[0]['id'],event['id'])
                    self.assertEqual(plan.request(pending['id'])['status'],'draft')
                    if attempt:plan.confirm(42,pending['id'])
                finally:current.close()

    def test_special_api_records_history_with_actual_editor_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'plan.sqlite3';store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            event=plan.add_special(42,self.start,self.start,'Existing')
            invitation=create_invite(plan,42);redeem_invite(plan,99,invitation['code'])
            body={key:event[key] for key in ('id','start','end','title','description','color','revision')};body['title']='By second parent'
            update_event(self.config,path,body,99)
            version=list_versions(plan,42)[0]
            self.assertEqual((version['actor'],version['action']),(99,'special_updated'))
            store.close()

    def test_bot_restores_only_after_owner_button_confirmation(self):
        self.plan.add_special(42,self.start,self.start,'Current')
        bot=Bot(self.plan,SimpleNamespace());bot.command(42,'/restore 1')
        job=json.loads(self.store.db.execute('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])
        callback=job['payload']['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.assertEqual(len(self.plan.special_events()),1)
        self.join()
        def update(actor,uid):
            return {'update_id':uid,'callback_query':{'id':str(uid),'from':{'id':actor},'data':callback,'message':{'chat':{'id':actor,'type':'private'}}}}
        bot.handle_update(update(99,1));self.assertEqual(len(self.plan.special_events()),1)
        bot.handle_update(update(42,2));self.assertEqual(self.plan.special_events(),[])
        bot.handle_update(update(42,2));self.assertEqual(self.latest(),3)

    def test_restoration_is_reversible_and_stale_event_editors_cannot_overwrite_it(self):
        from pplan.core import SpecialEventConflict
        event=self.plan.add_special(42,self.start,self.start,'Original');original=self.latest()
        changed=self.plan.update_special(42,event['id'],self.start,self.start,'Unwanted','Changed','#2563eb',expected_revision=1)
        later=self.latest();preview=prepare_restore(self.plan,42,original)
        confirm_restore(self.plan,42,preview['id'])
        with self.assertRaises(SpecialEventConflict):
            self.plan.update_special(42,event['id'],self.start,self.start,'Stale edit','Changed','#2563eb',expected_revision=changed['revision'])
        undo=prepare_restore(self.plan,42,later);confirm_restore(self.plan,42,undo['id'])
        self.assertEqual(self.plan.special_events()[0]['title'],'Unwanted')
        self.assertEqual(len(list_versions(self.plan,42)),5)

    def test_special_editor_cannot_delete_or_overwrite_history(self):
        import sqlite3
        from pplan.miniapp import event_write_authorizer
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'plan.sqlite3';store=Store(path);Plan(self.config,store,lambda:self.now)
            restricted=Store(path,initialize=False);restricted.db.set_authorizer(event_write_authorizer)
            try:
                for sql in ("DELETE FROM plan_versions", "UPDATE plan_versions SET state='{}'", "DROP TABLE plan_versions"):
                    with self.subTest(sql=sql),self.assertRaises(sqlite3.DatabaseError):restricted.db.execute(sql)
                self.assertEqual(store.db.execute('SELECT count(*) FROM plan_versions').fetchone()[0],1)
            finally:restricted.close();store.close()
