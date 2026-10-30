"""Outbound PSTN calls through LiveKit SIP and the existing self-hosted models.

LiveKit/SIP transport and PSTN minutes are distinct from model inference costs.
Imports stay optional so the dashboard can run without a voice worker runtime.
"""
from __future__ import annotations

import asyncio
import audioop
import contextlib
import importlib.util
import io
import logging
import os
import time
import uuid
import wave
from collections.abc import Callable

from .telephony import TelephonyNotReady
from .voice_models import local_voice_models

log = logging.getLogger(__name__)


def call_limit() -> int:
    try:
        return max(600, min(3600, int(os.getenv("VAANI_MAX_CALL_SECONDS", "900"))))
    except ValueError:
        return 900


def runtime_available() -> bool:
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("livekit.rtc", "livekit.api", "webrtcvad"))
    except (ModuleNotFoundError, ValueError):
        return False


def pcm_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(pcm)
    return output.getvalue()


def speech_pcm(wav: bytes) -> bytes:
    with wave.open(io.BytesIO(wav), "rb") as stream:
        channels, width, rate = stream.getnchannels(), stream.getsampwidth(), stream.getframerate()
        pcm = stream.readframes(stream.getnframes())
    if channels not in (1, 2):
        raise ValueError("Speech audio must have one or two channels")
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, .5, .5)
    if rate != 16000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
    return pcm


class SIPVoiceProvider:
    name = "livekit_sip"

    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}

    def status(self) -> dict:
        configured = bool(os.getenv("LIVEKIT_URL", "").startswith("wss://") and os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET") and os.getenv("LIVEKIT_SIP_TRUNK_ID"))
        enabled = os.getenv("VAANI_SIP_ENABLED", "false").lower() == "true"
        runtime = runtime_available()
        return {
            "provider": self.name, "enabled": enabled,
            "credentials_configured": configured, "runtime_installed": runtime,
            "outbound_enabled": enabled and configured and runtime and os.getenv("VAANI_ENABLE_OUTBOUND_CALLS", "false").lower() == "true",
            "ready_for_inbound": False,
            "max_call_seconds": call_limit(),
            "configuration_only": True,
            "note": "Outbound SIP configuration check; carrier connectivity is verified when dialing. PSTN and hosting charges apply.",
        }

    def start(self, number: str, snapshot: dict, on_status: Callable, on_turn: Callable) -> str:
        if not self.status()["outbound_enabled"]:
            raise TelephonyNotReady("Configure LiveKit SIP, install requirements-sip.txt and enable outbound calling.")
        room_name = "vaani-" + uuid.uuid4().hex
        async def supervise():
            try:
                await self._run(room_name, number, snapshot, on_status, on_turn)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("SIP runtime could not start for room %s", room_name)
                on_status("failed", 0)

        task = asyncio.create_task(supervise())
        self.tasks[room_name] = task
        task.add_done_callback(lambda _: self.tasks.pop(room_name, None))
        return room_name

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def hangup(self, room_name: str):
        from livekit import api
        async with api.LiveKitAPI() as client:
            try:
                await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except Exception as exc:
                if getattr(exc, "code", None) != "not_found":
                    raise

    async def _run(self, room_name, number, snapshot, on_status, on_turn):
        from livekit import api, rtc
        from google.protobuf.duration_pb2 import Duration
        from aiohttp import ClientTimeout
        import webrtcvad

        room = rtc.Room()
        client = api.LiveKitAPI(timeout=ClientTimeout(total=75))
        ended = asyncio.Event()
        answered = asyncio.Event()
        reader_tasks: set[asyncio.Task] = set()
        turn_task = None
        started = None
        failed = False
        history: list[dict] = []
        sip_identity = "caller"
        source = rtc.AudioSource(16000, 1)

        async def say(text):
            pcm = speech_pcm(await local_voice_models.synthesize(text))
            # 20 ms PCM frames. AudioSource applies backpressure to its queue.
            for offset in range(0, len(pcm), 640):
                chunk = pcm[offset:offset + 640]
                if len(chunk) < 640:
                    chunk += b"\0" * (640 - len(chunk))
                await source.capture_frame(rtc.AudioFrame(data=chunk, sample_rate=16000, num_channels=1, samples_per_channel=320))
            await source.wait_for_playout()

        async def turn(pcm):
            try:
                result = await local_voice_models.transcribe(pcm_wav(pcm), "phone-turn.wav")
                text = result["text"].strip()
                if not text:
                    return
                reply = await local_voice_models.reply(text, snapshot.get("system_prompt", ""), history=history)
                history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
                del history[:-20]
                on_turn(text, reply)
                await say(reply)
            except asyncio.CancelledError:
                raise
            except Exception:
                # End the call rather than leaving a connected, silent AI worker.
                nonlocal failed
                failed = True
                ended.set()
                log.warning("SIP voice turn failed for room %s", room_name)

        async def listen(track):
            nonlocal turn_task
            vad = webrtcvad.Vad(2)
            pending = b""
            utterance = bytearray()
            silence = 0
            stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            try:
                await answered.wait()
                async for event in stream:
                    pending += bytes(event.frame.data)
                    while len(pending) >= 640:
                        frame, pending = pending[:640], pending[640:]
                        speech = vad.is_speech(frame, 16000)
                        if speech:
                            if not utterance:
                                if turn_task and not turn_task.done():
                                    turn_task.cancel()
                                    await asyncio.gather(turn_task, return_exceptions=True)
                                source.clear_queue()
                            utterance.extend(frame)
                            silence = 0
                        elif utterance:
                            utterance.extend(frame)
                            silence += 1
                        # 600 ms trailing silence or 20 s speech cap bounds memory.
                        if utterance and (silence >= 30 or len(utterance) >= 640000):
                            if turn_task and not turn_task.done():
                                turn_task.cancel()
                                await asyncio.gather(turn_task, return_exceptions=True)
                            turn_task = asyncio.create_task(turn(bytes(utterance)))
                            utterance.clear()
                            silence = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                nonlocal failed
                failed = True
                ended.set()
            finally:
                await stream.aclose()

        @room.on("track_subscribed")
        def subscribed(track, publication, participant):
            if participant.identity == sip_identity and track.kind == rtc.TrackKind.KIND_AUDIO:
                task = asyncio.create_task(listen(track))
                reader_tasks.add(task)
                task.add_done_callback(reader_tasks.discard)

        @room.on("participant_disconnected")
        def disconnected(participant):
            if participant.identity == sip_identity:
                ended.set()

        @room.on("disconnected")
        def room_disconnected(*args):
            ended.set()

        try:
            token = api.AccessToken().with_identity("vaani-agent").with_grants(api.VideoGrants(room_join=True, room=room_name)).to_jwt()
            await asyncio.wait_for(room.connect(os.environ["LIVEKIT_URL"], token), timeout=20)
            track = rtc.LocalAudioTrack.create_audio_track("agent-voice", source)
            await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            on_status("ringing", 0)
            # Carrier-side duration limit also applies if the app crashes.
            await asyncio.wait_for(client.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                sip_trunk_id=os.environ["LIVEKIT_SIP_TRUNK_ID"], sip_call_to=number,
                room_name=room_name, participant_identity=sip_identity,
                wait_until_answered=True, ringing_timeout=Duration(seconds=45),
                max_call_duration=Duration(seconds=call_limit()),
            )), timeout=60)
            started = time.monotonic()
            on_status("in_progress", 0)
            answered.set()
            async def greet():
                try:
                    await say(snapshot.get("greeting") or "Hello, I am an AI voice assistant. How can I help?")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    nonlocal failed
                    failed = True
                    ended.set()
            turn_task = asyncio.create_task(greet())
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ended.wait(), timeout=call_limit())
        except asyncio.CancelledError:
            failed = True
            raise
        except Exception:
            failed = True
            log.warning("SIP session failed for room %s", room_name)
        finally:
            for task in [*reader_tasks, turn_task]:
                if task:
                    task.cancel()
            await asyncio.gather(*reader_tasks, *([turn_task] if turn_task else []), return_exceptions=True)
            # Deleting this per-call room hangs up its SIP participant.
            with contextlib.suppress(Exception):
                await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            with contextlib.suppress(Exception):
                await room.disconnect()
            with contextlib.suppress(Exception):
                await source.aclose()
            with contextlib.suppress(Exception):
                await client.aclose()
            on_status("failed" if failed else "completed", int(time.monotonic() - started) if started else 0)


sip_provider = SIPVoiceProvider()
