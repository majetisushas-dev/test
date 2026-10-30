"""Offline checks: mock transport and models; never dial a real number."""
import asyncio
import io
import os
import sys
import types
import unittest
import wave
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from app import sip_voice
from app.voice_models import LocalVoiceModels
from app.telephony import TelephonyNotReady


class ConfigurationTests(unittest.TestCase):
    def test_duration_never_below_ten_minutes(self):
        for raw, expected in [('30', 600), ('600', 600), ('900', 900), ('10000', 3600), ('invalid', 900)]:
            with self.subTest(raw=raw), patch.dict(os.environ, {'VAANI_MAX_CALL_SECONDS': raw}):
                self.assertEqual(sip_voice.call_limit(), expected)

    def test_cannot_dial_when_disabled_or_runtime_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = sip_voice.SIPVoiceProvider()
            self.assertFalse(provider.status()['outbound_enabled'])
            with self.assertRaises(TelephonyNotReady):
                provider.start('+12025550123', {}, lambda *a: None, lambda *a: None)
        config = dict(LIVEKIT_URL='wss://voice.example', LIVEKIT_API_KEY='key', LIVEKIT_API_SECRET='secret', LIVEKIT_SIP_TRUNK_ID='trunk', VAANI_SIP_ENABLED='true', VAANI_ENABLE_OUTBOUND_CALLS='true')
        with patch.dict(os.environ, config, clear=True), patch.object(sip_voice, 'runtime_available', return_value=False):
            self.assertFalse(sip_voice.SIPVoiceProvider().status()['outbound_enabled'])

    def test_audio_converts_stereo_to_16khz_mono(self):
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\0\0\0\0' * 320)
        self.assertEqual(len(sip_voice.speech_pcm(data.getvalue())), 640)
        with wave.open(io.BytesIO(sip_voice.pcm_wav(b'\0' * 640)), 'rb') as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels()), (16000, 1))


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_context_bounded_and_retained(self):
        model = LocalVoiceModels()
        model.ollama_model = 'qwen3:4b'
        history = [{'role': 'user', 'content': f'turn {i}'} for i in range(28)]
        response = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'message': {'content': 'Hello'}})
        client = AsyncMock()
        client.post.return_value = response
        with patch('app.voice_models.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(await model.reply('continue', 'Be helpful', history=history), 'Hello')
        messages = client.post.call_args.kwargs['json']['messages']
        self.assertEqual(len(messages), 22)
        self.assertEqual(messages[1]['content'], 'turn 8')
        self.assertEqual(messages[-1]['content'], 'continue')


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def run_session(self, fail=False):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Room:
            def __init__(self):
                self.handlers = {}
                self.local_participant = Obj(publish_track=AsyncMock())
            def on(self, event):
                def register(fn):
                    self.handlers[event] = fn
                    return fn
                return register
            async def connect(self, *a):
                pass
            async def disconnect(self):
                pass

        room = Room()
        source = Obj(capture_frame=AsyncMock(), wait_for_playout=AsyncMock(), clear_queue=lambda: None, aclose=AsyncMock())
        async def dial(request):
            if fail:
                raise RuntimeError('carrier unavailable')
            asyncio.get_running_loop().call_soon(room.handlers['participant_disconnected'], Obj(identity='caller'))
            return Obj()

        client = Obj(sip=Obj(create_sip_participant=AsyncMock(side_effect=dial)), room=Obj(delete_room=AsyncMock()), aclose=AsyncMock())
        class Token:
            def with_identity(self, *a):
                return self
            def with_grants(self, *a):
                return self
            def to_jwt(self):
                return 'test-token'

        api = Obj(LiveKitAPI=lambda **kw: client, AccessToken=Token, VideoGrants=Obj, CreateSIPParticipantRequest=Obj, DeleteRoomRequest=Obj)
        rtc = Obj(Room=lambda: room, AudioSource=lambda *a: source, LocalAudioTrack=Obj(create_audio_track=lambda *a: Obj()), TrackPublishOptions=Obj, TrackSource=Obj(SOURCE_MICROPHONE=1), AudioFrame=Obj)
        fake_modules = {'livekit': Obj(api=api, rtc=rtc), 'google.protobuf.duration_pb2': Obj(Duration=Obj), 'webrtcvad': Obj()}
        # Exercise a full session lifecycle with a simulated 15-minute clock.
        statuses = []
        with patch.dict(sys.modules, fake_modules), patch.dict(os.environ, {'LIVEKIT_URL': 'wss://test.example', 'LIVEKIT_SIP_TRUNK_ID': 'trunk', 'VAANI_MAX_CALL_SECONDS': '900'}), patch.object(sip_voice, 'time', types.SimpleNamespace(monotonic=Mock(side_effect=[100, 1000] if not fail else [1000]))), patch.object(sip_voice.local_voice_models, 'synthesize', new=AsyncMock(return_value=sip_voice.pcm_wav(b'\0' * 640))):
            await sip_voice.SIPVoiceProvider()._run('test-room', '+12025550123', {}, lambda *args: statuses.append(args), lambda *args: None)
        return client, statuses

    async def test_fifteen_minute_call_sets_carrier_limit_and_cleans_up(self):
        client, statuses = await self.run_session()
        request = client.sip.create_sip_participant.call_args.args[0]
        self.assertEqual(request.max_call_duration.seconds, 900)
        self.assertTrue(request.wait_until_answered)
        self.assertEqual(statuses, [('ringing', 0), ('in_progress', 0), ('completed', 900)])
        client.room.delete_room.assert_awaited_once()
        client.aclose.assert_awaited_once()

    async def test_carrier_failure_is_recorded_and_room_removed(self):
        client, statuses = await self.run_session(fail=True)
        self.assertEqual(statuses[-1], ('failed', 0))
        client.room.delete_room.assert_awaited_once()
        client.aclose.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
