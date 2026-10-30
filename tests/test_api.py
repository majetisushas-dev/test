"""API and persistence smoke tests. No external services or phone calls."""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
_database = tempfile.TemporaryDirectory(prefix='vaani-api-check-')
os.environ['VAANI_DATABASE_URL'] = 'sqlite:///' + _database.name + '/test.db'
os.environ['VAANI_BOOTSTRAP_PASSWORD'] = 'test-only-bootstrap-password'
os.environ['VAANI_SECRET_KEY'] = 'test-only-session-signing-key'
os.environ['VAANI_ENABLE_OUTBOUND_CALLS'] = 'false'
os.environ['VAANI_SIP_ENABLED'] = 'false'

from fastapi.testclient import TestClient
from app.main import app
from app import main


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()
        result = cls.client.post('/api/auth/login', data={'username': 'owner@voice.local', 'password': 'test-only-bootstrap-password'})
        assert result.status_code == 200, result.text
        cls.headers = {'Authorization': 'Bearer ' + result.json()['access_token']}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        _database.cleanup()

    def test_health_dashboard_and_auth(self):
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertIn('Vaani', self.client.get('/').text)
        self.assertEqual(self.client.get('/api/agents').status_code, 401)

    def test_agent_publish_and_outbound_default_gate(self):
        result = self.client.post('/api/agents', headers=self.headers, json={'name': 'Smoke agent', 'languages': ['English']})
        self.assertEqual(result.status_code, 201)
        agent_id = result.json()['id']
        result = self.client.post(f'/api/agents/{agent_id}/publish', headers=self.headers)
        self.assertEqual(result.json()['published_version'], 1)
        result = self.client.post('/api/calls/outbound', headers=self.headers, json={'agent_id': agent_id, 'consent_id': 999})
        self.assertEqual(result.status_code, 503)

    def test_phone_validation_and_tenant_boundary(self):
        invalid = self.client.post('/api/phone-consents', headers=self.headers, json={'phone': '123', 'evidence_reference': 'test-only', 'consent_attested': True})
        self.assertEqual(invalid.status_code, 422)
        agent = self.client.post('/api/agents', headers=self.headers, json={'name': 'Private agent'}).json()
        other = self.client.post('/api/auth/signup', json={'organization_name':'Another org', 'email':'other@example.test', 'password':'test-only-other-password'})
        self.assertEqual(other.status_code, 201)
        headers = {'Authorization': 'Bearer ' + other.json()['access_token']}
        self.assertEqual(self.client.get(f'/api/agents/{agent["id"]}', headers=headers).status_code, 404)
        self.assertEqual(self.client.post('/api/calls/999/hangup', headers=headers).status_code, 404)

    def test_published_agent_contact_dial_and_call_details(self):
        agent = self.client.post('/api/agents', headers=self.headers, json={'name': 'Outbound test agent'}).json()
        self.client.post(f'/api/agents/{agent["id"]}/publish', headers=self.headers)
        consent = self.client.post('/api/phone-consents', headers=self.headers, json={'phone': '+12025550123', 'contact_name': 'Test only', 'evidence_reference': 'mocked-call-test', 'consent_attested': True})
        self.assertEqual(consent.status_code, 201)
        # Exercise all API checks, but never contact LiveKit or a real phone.
        with patch.dict(os.environ, {'VAANI_CALL_START_HOUR': '0', 'VAANI_CALL_END_HOUR': '24', 'VAANI_TELEPHONY_PROVIDER': 'livekit_sip'}), patch.object(main.sip_provider, 'status', return_value={'outbound_enabled': True}), patch.object(main.sip_provider, 'start', return_value='mock-room') as start, patch.object(main.local_voice_models, 'status', new=AsyncMock(return_value={'llm': {'model_available': True}, 'stt': {'available': True}, 'tts': {'available': True}})), patch.object(main, 'find_spec', return_value=object()):
            call = self.client.post('/api/calls/outbound', headers=self.headers, json={'agent_id': agent['id'], 'consent_id': consent.json()['id']})
        self.assertEqual(call.status_code, 202, call.text)
        start.assert_called_once()
        detail = self.client.get(f'/api/calls/{call.json()["id"]}', headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['phone'], '+12025550123')
        self.assertEqual(detail.json()['transcript'], [])


if __name__ == '__main__':
    unittest.main()
