from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
import hashlib
import json
from unittest.mock import patch

from pplan.calendar import CalendarSync
from pplan.core import Plan,PlanError
from pplan.history import prepare_restore,confirm_restore
from tests.test_plan import Fixture,FakeGoogle


class GooglePeriodsTests(Fixture):
    def setUp(self):
        super().setUp();self.client=FakeGoogle();self.sync=CalendarSync(self.plan,self.client);self.sync.connect()

    def legacy(self,child='c1',day=None):
        day=day or self.start
        event_id=hashlib.sha256(f"{self.store.get('plan_id')}:{child}:{day}".encode()).hexdigest()[:32]
        event={'id':event_id,'start':{'dateTime':self.config.boundary(day).isoformat()},
               'end':{'dateTime':self.config.boundary(day+timedelta(days=1)).isoformat()},
               'extendedProperties':{'private':{'pplan':self.store.get('plan_id'),'child':child}}}
        self.client.events[event_id]=event
        self.store.db.execute('INSERT OR REPLACE INTO calendar_cache VALUES(?,?)',(f'{child}:{day}','legacy'))
        return event_id

    def test_matching_week_is_one_shared_event_with_exact_handover_boundaries(self):
        self.sync.sync(horizon=2)
        event=next(iter(self.client.events.values()))
        self.assertEqual(event['summary'],'Older and Younger with First')
        self.assertEqual(event['start']['dateTime'],'2026-11-23T15:00:00-08:00')
        self.assertEqual(event['end']['dateTime'],'2026-11-30T15:00:00-08:00')
        self.assertEqual(event['extendedProperties']['private']['children'],'c1,c2')
        self.assertEqual(len(self.client.events),1)

    def test_current_period_is_not_cut_at_today_or_horizon(self):
        self.now=datetime(2026,12,3,12,tzinfo=timezone.utc)
        self.sync.sync(horizon=2)
        event=next(iter(self.client.events.values()))
        self.assertEqual(event['start']['dateTime'][:10],'2026-11-30')
        self.assertEqual(event['end']['dateTime'][:10],'2026-12-07')

    def test_matching_override_does_not_break_a_continuous_period(self):
        request=self.plan.draft_actions(42,[{'type':'overwrite','start':'2026-11-24','end':'2026-11-24','parent':'p1','children':['c1'],'cycle':[]}])
        self.plan.confirm(42,request['id']);self.sync.sync(horizon=2)
        self.assertEqual(len(self.client.events),1)

    def test_differing_runs_remain_separate_and_cover_each_child_once(self):
        request=self.plan.draft(42,date(2026,11,25),date(2026,11,26),'p2',['c2'])
        self.plan.confirm(42,request['id']);self.sync.sync(horizon=7)
        self.assertEqual(len(self.client.events),4)
        older=[e for e in self.client.events.values() if e['extendedProperties']['private']['children']=='c1']
        self.assertEqual(len(older),1)
        self.assertEqual(older[0]['end']['dateTime'][:10],'2026-11-30')
        for child in self.config.children:
            for n in range(7):
                day=self.start+timedelta(days=n)
                matches=[e for e in self.client.events.values() if child in e['extendedProperties']['private']['children'].split(',')
                         and e['start']['dateTime'][:10]<=day.isoformat()<e['end']['dateTime'][:10]]
                self.assertEqual(len(matches),1)
                self.assertIn(self.config.parents[self.plan.assignment(child,day)['parent']],matches[0]['summary'])

    def test_migration_deletes_only_legacy_events_owned_by_this_plan(self):
        old={self.legacy('c1'),self.legacy('c2')}
        foreign='f'*32;special='e'*32;manual='d'*32
        self.client.events[foreign]={'id':foreign,'extendedProperties':{'private':{'pplan':'another-plan','child':'c1'}}}
        self.client.events[special]={'id':special,'extendedProperties':{'private':{'pplan':self.store.get('plan_id'),'format':'specials-v1','children':'c1,c2'}}}
        self.client.events[manual]={'id':manual,'summary':'Manual event'}
        self.sync.sync(horizon=2)
        self.assertFalse(old&set(self.client.events))
        self.assertTrue({foreign,special,manual}<=set(self.client.events))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM calendar_cache').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM calendar_period_cache').fetchone()[0],1)

    def test_upload_failure_leaves_legacy_events_and_daily_cache_for_retry(self):
        old=self.legacy();original=self.client.request
        def request(method,path,body=None):
            return (503,None) if method=='POST' else original(method,path,body)
        self.client.request=request
        with self.assertRaises(PlanError):self.sync.sync(horizon=2)
        self.assertIn(old,self.client.events)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM calendar_cache').fetchone()[0],1)
        self.client.request=original;self.sync.sync(horizon=2)
        self.assertNotIn(old,self.client.events)
        self.assertEqual(len(self.client.events),1)

    def test_interrupted_post_is_retried_without_duplicate_periods(self):
        original=self.client.request;failed=False
        def request(method,path,body=None):
            nonlocal failed
            result=original(method,path,body)
            if method=='POST' and not failed:
                failed=True;raise OSError('lost response after Google accepted event')
            return result
        self.client.request=request
        with self.assertRaises(OSError):self.sync.sync(horizon=2)
        self.assertEqual(len(self.client.events),1)
        self.client.request=original;self.sync.sync(horizon=2)
        self.assertEqual(len(self.client.events),1)
        self.assertEqual(self.sync.sync(horizon=2),0)

    def test_retired_period_gets_new_id_if_restored_after_split(self):
        self.sync.sync(horizon=2);old=next(iter(self.client.events))
        request=self.plan.draft(42,self.start,self.start,'p2',['c1']);self.plan.confirm(42,request['id'])
        self.sync.sync(horizon=2);self.assertIn(old,self.client.deleted)
        preview=prepare_restore(self.plan,42,1);confirm_restore(self.plan,42,preview['id'])
        self.sync.sync(horizon=2)
        self.assertEqual(len(self.client.events),1)
        self.assertNotIn(old,self.client.events)

    def test_manually_deleted_period_is_recreated_without_reusing_tombstone(self):
        self.sync.sync(horizon=2);old=next(iter(self.client.events))
        del self.client.events[old];self.client.deleted.add(old)
        self.sync.sync(horizon=2)
        self.assertEqual(len(self.client.events),1)
        self.assertNotIn(old,self.client.events)

    def test_different_rotation_boundaries_do_not_extend_horizon_on_each_sync(self):
        request=self.plan.draft_actions(42,[{'type':'set_cycle','start':'2026-11-26','end':None,'parent':None,'children':['c2'],'cycle':['p2']*7+['p1']*7}])
        self.plan.confirm(42,request['id']);self.sync.sync(horizon=30)
        ids=set(self.client.events)
        self.assertEqual(self.sync.sync(horizon=30),0)
        self.assertEqual(set(self.client.events),ids)

    def test_constant_parent_has_one_bounded_event_with_stable_id(self):
        self.config=replace(self.config,cycle=('p1',));self.plan=Plan(self.config,self.store,lambda:self.now);self.sync=CalendarSync(self.plan,self.client)
        self.sync.sync(horizon=10);old=next(iter(self.client.events))
        self.assertEqual(len(self.client.events),1)
        self.assertEqual(self.sync.sync(horizon=10),0)
        self.now+=timedelta(days=2);self.sync.sync(horizon=10)
        self.assertEqual(set(self.client.events),{old})

    def test_full_period_respects_daylight_saving_offsets(self):
        self.config=replace(self.config,anchor_date=date(2027,3,8));self.now=datetime(2027,3,7,12,tzinfo=timezone.utc)
        self.plan=Plan(self.config,self.store,lambda:self.now);self.sync=CalendarSync(self.plan,self.client)
        self.sync.sync(horizon=2);event=next(iter(self.client.events.values()))
        self.assertTrue(event['start']['dateTime'].endswith('-08:00'))
        self.assertTrue(event['end']['dateTime'].endswith('-07:00'))
        start=datetime.fromisoformat(event['start']['dateTime']);end=datetime.fromisoformat(event['end']['dateTime'])
        self.assertEqual((end-start).total_seconds(),167*3600)

    def test_concurrent_plan_change_keeps_sync_dirty_for_followup(self):
        original=self.client.request;changed=False
        def request(method,path,body=None):
            nonlocal changed
            result=original(method,path,body)
            if method=='POST' and not changed:
                changed=True;self.plan.add_special(42,self.start,self.start,'Concurrent change')
            return result
        self.client.request=request;self.sync.sync(horizon=2)
        self.assertEqual(self.store.get('calendar_dirty'),'1')
        self.client.request=original;self.sync.sync(horizon=2)
        self.assertEqual(self.store.get('calendar_dirty'),'0')
