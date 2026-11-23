from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock,patch

from pplan.calendar import BASE,EVENT_SCOPES,CalendarSync,GoogleServiceAccountClient,configured_google_client,google_credentials_available
from pplan.core import PlanError
from tests.test_plan import Fixture,FakeGoogle

TARGET='fixture-calendar@group.calendar.google.com'


class ServiceAccountTests(Fixture):
    def client(self,status=200,role='writer'):
        client=SimpleNamespace(expected_calendar_id=TARGET,request=Mock(return_value=(status,{'accessRole':role})))
        return client

    def test_connect_uses_only_supplied_calendar_and_checks_writer_role(self):
        client=self.client();sync=CalendarSync(self.plan,client)
        self.assertEqual(sync.connect(),TARGET)
        client.request.assert_called_once_with('GET','/calendars/fixture-calendar%40group.calendar.google.com/events?maxResults=1&fields=accessRole')
        self.assertEqual(self.store.get('google_calendar_id'),TARGET)
        self.assertEqual(self.store.get('calendar_dirty'),'1')
        self.assertEqual(sync.connect(),TARGET)
        self.assertTrue(all(call.args[0]=='GET' for call in client.request.call_args_list))

    def test_readonly_missing_or_inaccessible_calendar_does_not_connect(self):
        for status,role in ((200,'reader'),(200,'freeBusyReader'),(200,'none'),(403,'writer'),(404,'writer')):
            with self.subTest(status=status,role=role),self.assertRaises(PlanError):CalendarSync(self.plan,self.client(status,role)).connect()
            self.assertIsNone(self.store.get('google_calendar_id'))

    def test_different_target_is_rejected_before_any_google_request(self):
        self.store.set('google_calendar_id','other-calendar@example.test')
        client=self.client()
        with self.assertRaises(PlanError):CalendarSync(self.plan,client)
        client.request.assert_not_called()

    def test_target_change_during_runtime_cannot_redirect_sync(self):
        client=self.client();sync=CalendarSync(self.plan,client);sync.connect();client.request.reset_mock()
        self.store.set('google_calendar_id','other-calendar@example.test')
        with self.assertRaises(PlanError):sync.sync(horizon=1)
        client.request.assert_not_called()

    def test_service_account_uses_events_scope_without_impersonation_or_token_files(self):
        with patch('google.oauth2.service_account.Credentials.from_service_account_file') as credentials,patch('google.auth.transport.requests.AuthorizedSession') as session:
            client=GoogleServiceAccountClient('/fixture/key.json',TARGET)
            credentials.assert_called_once_with('/fixture/key.json',scopes=EVENT_SCOPES)
            session.assert_called_once_with(credentials.return_value)
            self.assertEqual(client.expected_calendar_id,TARGET)
            credentials.return_value.with_subject.assert_not_called()

    def test_client_blocks_calendar_creation_other_targets_and_path_escape(self):
        with patch('google.oauth2.service_account.Credentials.from_service_account_file'),patch('google.auth.transport.requests.AuthorizedSession') as session:
            client=GoogleServiceAccountClient('/fixture/key.json',TARGET)
            for method,path in (('POST','/calendars'),('GET','/users/me/calendarList'),('PUT','/calendars/other/events/id'),
                                ('GET',client.event_path+'-other'),('DELETE',client.event_path+'/not-an-app-id'),('PUT',client.event_path+'/../other'),('PUT',client.event_path+'/%2e%2e/other'),('GET',client.event_path+'/../other')):
                with self.subTest(method=method,path=path),self.assertRaises(PlanError):client.request(method,path)
            session.return_value.request.assert_not_called()
            session.return_value.request.return_value=SimpleNamespace(status_code=200,json=lambda:{'accessRole':'writer'})
            self.assertEqual(client.request('GET',client.event_path+'?fields=accessRole'),(200,{'accessRole':'writer'}))
            session.return_value.request.assert_called_once_with('GET',BASE+client.event_path+'?fields=accessRole',json=None,timeout=20)

    def test_client_returns_sanitized_provider_error_without_rewriting_key(self):
        with patch('google.oauth2.service_account.Credentials.from_service_account_file'),patch('google.auth.transport.requests.AuthorizedSession') as session:
            client=GoogleServiceAccountClient('/fixture/key.json',TARGET)
            session.return_value.request.return_value=SimpleNamespace(status_code=403,json=lambda:{'error':{'errors':[{'reason':'forbidden'}]}})
            self.assertEqual(client.request('PUT',client.event_path+'/'+'0'*32,{}),(403,None))

    def test_calendar_id_is_required_and_primary_alias_is_rejected(self):
        for target in ('','primary','bad\nvalue','x'*1025,None):
            with self.subTest(target=target),self.assertRaises(PlanError):GoogleServiceAccountClient('/fixture/key.json',target)

    def test_factory_prefers_service_account_only_when_configured(self):
        with patch.dict('os.environ',{'GOOGLE_SERVICE_ACCOUNT_FILE':'/fixture/key.json','GOOGLE_CALENDAR_ID':TARGET},clear=True),patch('pplan.calendar.GoogleServiceAccountClient') as client,patch('pplan.calendar.GoogleClient') as oauth:
            self.assertIs(configured_google_client(),client.return_value)
            client.assert_called_once_with('/fixture/key.json',TARGET);oauth.assert_not_called()
        with patch.dict('os.environ',{'GOOGLE_TOKEN_PATH':'/fixture/token.json'},clear=True),patch('pplan.calendar.GoogleClient') as oauth:
            self.assertIs(configured_google_client(),oauth.return_value)
            oauth.assert_called_once_with('/fixture/token.json')

    def test_credentials_readiness_supports_both_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            key=Path(tmp)/'key.json';key.write_text('{}')
            with patch.dict('os.environ',{'GOOGLE_SERVICE_ACCOUNT_FILE':str(key)},clear=True):self.assertTrue(google_credentials_available())
            with patch.dict('os.environ',{'GOOGLE_TOKEN_PATH':str(key)},clear=True):self.assertTrue(google_credentials_available())
            with patch.dict('os.environ',{'GOOGLE_SERVICE_ACCOUNT_FILE':str(key)+'-missing','GOOGLE_TOKEN_PATH':str(key)},clear=True):self.assertFalse(google_credentials_available())

    def test_service_account_sync_preserves_stable_ids_and_other_calendar_events(self):
        client=FakeGoogle();client.expected_calendar_id=TARGET
        client.events['manual-event']={'summary':'Existing unrelated event'}
        sync=CalendarSync(self.plan,client);sync.connect()
        self.assertEqual(sync.sync(horizon=2),1)
        ids=set(client.events)
        self.assertEqual(sync.sync(horizon=2),0)
        self.assertEqual(client.events['manual-event'],{'summary':'Existing unrelated event'})
        self.assertEqual(set(client.events),ids)
        self.assertEqual(len(client.events),2)
        self.assertTrue(all(path.startswith('/calendars/fixture-calendar%40group.calendar.google.com/events') for _,path,_ in client.calls))

    def test_delete_empty_response_is_supported_for_managed_event_cleanup(self):
        with patch('google.oauth2.service_account.Credentials.from_service_account_file'),patch('google.auth.transport.requests.AuthorizedSession') as session:
            client=GoogleServiceAccountClient('/fixture/key.json',TARGET)
            session.return_value.request.return_value=SimpleNamespace(status_code=204,json=Mock(side_effect=AssertionError('Empty body')))
            self.assertEqual(client.request('DELETE',client.event_path+'/'+'a'*32),(204,None))

    def test_rate_limit_is_retried_with_backoff_and_same_event_id(self):
        with patch('google.oauth2.service_account.Credentials.from_service_account_file'),patch('google.auth.transport.requests.AuthorizedSession') as session,patch('pplan.calendar.time.sleep') as sleep,patch('pplan.calendar.random.random',return_value=0):
            client=GoogleServiceAccountClient('/fixture/key.json',TARGET)
            limited=SimpleNamespace(status_code=403,json=lambda:{'error':{'errors':[{'reason':'rateLimitExceeded'}]}})
            success=SimpleNamespace(status_code=204,json=lambda:None)
            session.return_value.request.side_effect=[limited,success]
            self.assertEqual(client.request('DELETE',client.event_path+'/'+'a'*32),(204,None))
            self.assertEqual(session.return_value.request.call_count,2)
            self.assertEqual(session.return_value.request.call_args_list[0],session.return_value.request.call_args_list[1])
            sleep.assert_called_once_with(1)

    def test_permanent_permission_error_is_not_retried(self):
        with patch('google.oauth2.service_account.Credentials.from_service_account_file'),patch('google.auth.transport.requests.AuthorizedSession') as session,patch('pplan.calendar.time.sleep') as sleep:
            client=GoogleServiceAccountClient('/fixture/key.json',TARGET)
            session.return_value.request.return_value=SimpleNamespace(status_code=403,json=lambda:{'error':{'errors':[{'reason':'forbidden'}]}})
            self.assertEqual(client.request('GET',client.event_path),(403,None))
            self.assertEqual(session.return_value.request.call_count,1);sleep.assert_not_called()

    def test_transient_retries_are_bounded(self):
        with patch('google.oauth2.service_account.Credentials.from_service_account_file'),patch('google.auth.transport.requests.AuthorizedSession') as session,patch('pplan.calendar.time.sleep') as sleep,patch('pplan.calendar.random.random',return_value=0):
            client=GoogleServiceAccountClient('/fixture/key.json',TARGET)
            session.return_value.request.return_value=SimpleNamespace(status_code=429,json=lambda:{})
            self.assertEqual(client.request('GET',client.event_path),(429,None))
            self.assertEqual(session.return_value.request.call_count,5)
            self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,2,4,8])
