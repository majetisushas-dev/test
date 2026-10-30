"""Authenticated remote voice pipeline integration; model inference is mocked."""
import base64
import io
import os
import sys
import unittest
import wave
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from app.runtime import app, models
from app.voice_models import LocalVoiceModels, ModelUnavailable


class RuntimeAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_denies_missing_and_incorrect_tokens(self):
        with patch.dict(os.environ, {'VAANI_VOICE_RUNTIME_TOKEN': 'test-only-runtime-token'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://runtime.example.test') as client:
                self.assertEqual((await client.get('/health')).status_code, 200)
                self.assertEqual((await client.get('/status')).status_code, 401)
                self.assertEqual((await client.post('/reply', headers={'X-Voice-Runtime-Token': 'wrong'}, json={'text': 'Hello'})).status_code, 401)
                with patch.object(models, 'status', new=AsyncMock(return_value={'ready': True})):
                    self.assertEqual((await client.get('/status', headers={'X-Voice-Runtime-Token': 'test-only-runtime-token'})).json(), {'ready': True})


class RemotePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_crosses_authenticated_http_pipeline(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0' * 640)
        output_audio = buffer.getvalue()
        config = {'VAANI_VOICE_RUNTIME_URL': 'https://voice.example.test', 'VAANI_VOICE_RUNTIME_TOKEN': 'test-only-runtime-token'}
        actual_client = httpx.AsyncClient

        def client_factory(**kwargs):
            return actual_client(transport=httpx.ASGITransport(app=app), **kwargs)

        with patch.dict(os.environ, config), patch('app.voice_models.httpx.AsyncClient', side_effect=client_factory), patch.object(models, 'transcribe', new=AsyncMock(return_value={'text': 'Hello', 'language': 'en'})) as stt, patch.object(models, 'reply', new=AsyncMock(return_value='How can I help?')) as llm, patch.object(models, 'synthesize', new=AsyncMock(return_value=output_audio)) as tts:
            remote = LocalVoiceModels()
            result = await remote.run_voice_test(output_audio, 'test.wav', 'Be helpful')
        self.assertEqual(result['transcript']['text'], 'Hello')
        self.assertEqual(result['answer'], 'How can I help?')
        self.assertEqual(base64.b64decode(result['audio_wav_base64']), output_audio)
        stt.assert_awaited_once()
        llm.assert_awaited_once_with('Hello', 'Be helpful', history=[])
        tts.assert_awaited_once_with('How can I help?')

    async def test_remote_http_never_sends_token_without_tls(self):
        with patch.dict(os.environ, {'VAANI_VOICE_RUNTIME_URL': 'http://voice.example.test', 'VAANI_VOICE_RUNTIME_TOKEN': 'test-only', 'VAANI_ALLOW_INSECURE_RUNTIME': 'false'}):
            remote = LocalVoiceModels()
            with self.assertRaises(ModelUnavailable):
                await remote.transcribe(b'sample', 'test.wav')

    async def test_remote_unavailable_is_reported_without_credentials(self):
        with patch.dict(os.environ, {'VAANI_VOICE_RUNTIME_URL': 'https://voice.example.test', 'VAANI_VOICE_RUNTIME_TOKEN': ''}):
            remote = LocalVoiceModels()
            status = await remote.status()
        self.assertFalse(status['connection']['available'])
        self.assertFalse(status['llm']['model_available'])
        self.assertNotIn('token', str(status))


if __name__ == '__main__':
    unittest.main()
