"""Self-hosted voice model adapters. No hosted AI API is called by this module."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any

import httpx


class ModelUnavailable(RuntimeError):
    pass


class LocalVoiceModels:
    """Thin adapters for a local Ollama server, faster-whisper, and Piper CLI."""

    def __init__(self, remote: bool = True) -> None:
        self.runtime_url = os.getenv("VAANI_VOICE_RUNTIME_URL", "").rstrip("/") if remote else ""
        self.runtime_token = os.getenv("VAANI_VOICE_RUNTIME_TOKEN", "") if remote else ""
        self.ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        self.ollama_model = os.getenv("OLLAMA_MODEL", "")
        self.stt_path = Path(os.getenv("STT_MODEL_PATH", "models/faster-whisper-small")).resolve()
        self.stt_device = os.getenv("STT_DEVICE", "cpu")
        self.stt_compute_type = os.getenv("STT_COMPUTE_TYPE", "int8" if self.stt_device == "cpu" else "float16")
        self.piper_bin = os.getenv("PIPER_BIN", "piper")
        self.piper_model = Path(os.getenv("PIPER_MODEL_PATH", "models/piper/voice.onnx")).resolve()
        self.piper_url = os.getenv("PIPER_HTTP_URL", "http://127.0.0.1:5000").rstrip("/")
        self.vibevoice_path = Path(os.getenv("VIBEVOICE_MODEL_PATH", "models/vibevoice-1.5b")).resolve()
        self.vibevoice_device_map = os.getenv("VIBEVOICE_DEVICE_MAP", "auto")
        self._whisper: Any = None
        self._vibevoice: tuple[Any, Any] | None = None
        self._vibevoice_lock = threading.Lock()

    async def status(self) -> dict[str, Any]:
        if self.runtime_url:
            try:
                result = await self._runtime_request("GET", "/status", timeout=6)
                return {**result.json(), "connection": {"mode": "remote", "available": True}}
            except ModelUnavailable:
                return {"connection": {"mode": "remote", "available": False},
                        "llm": {"available": False, "model_available": False, "model": None},
                        "stt": {"available": False}, "tts": {"available": False},
                        "microsoft_vibevoice": {"available": False}}
        ollama = {"configured": bool(self.ollama_model), "available": False, "model": self.ollama_model or None, "base_url": self.ollama_url}
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                response = await client.get(f"{self.ollama_url}/api/tags")
                ollama["available"] = response.is_success
                if response.is_success:
                    ollama["loaded_models"] = [m.get("name") for m in response.json().get("models", [])]
                    ollama["model_available"] = self.ollama_model in ollama["loaded_models"] or f"{self.ollama_model}:latest" in ollama["loaded_models"]
        except httpx.HTTPError:
            pass
        piper = shutil.which(self.piper_bin) or (self.piper_bin if Path(self.piper_bin).is_file() else None)
        piper_http = False
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                piper_http = (await client.get(f"{self.piper_url}/info")).is_success
        except httpx.HTTPError:
            pass
        return {
            "connection": {"mode": "local", "available": True},
            "llm": ollama,
            "stt": {"available": importlib.util.find_spec("faster_whisper") is not None and self.stt_path.is_dir(), "runtime": "faster-whisper", "model_path": str(self.stt_path), "device": self.stt_device},
            "tts": {"available": piper_http or (bool(piper) and self.piper_model.is_file()), "runtime": "Piper", "http_available": piper_http, "http_url": self.piper_url, "binary": piper, "model_path": str(self.piper_model)},
            "microsoft_vibevoice": {"available": self._vibevoice_weights_exist() and importlib.util.find_spec("transformers") is not None and importlib.util.find_spec("torch") is not None, "runtime": "Transformers local inference", "model_path": str(self.vibevoice_path), "configured": self._vibevoice_weights_exist(), "purpose": "offline voice preview only", "supports_live_calls": False, "language_limit": "English and Chinese only"},
        }

    def _vibevoice_weights_exist(self) -> bool:
        return self.vibevoice_path.is_dir() and (any(self.vibevoice_path.glob("*.safetensors")) or (self.vibevoice_path / "model.safetensors.index.json").is_file()) and (self.vibevoice_path / "config.json").is_file()

    def _load_vibevoice(self):
        if self._vibevoice is None:
            with self._vibevoice_lock:
                if self._vibevoice is None:
                    if not self._vibevoice_weights_exist():
                        raise ModelUnavailable(f"Local Microsoft VibeVoice model is missing: {self.vibevoice_path}. Download it separately into this directory.")
                    if importlib.util.find_spec("transformers") is None or importlib.util.find_spec("torch") is None:
                        raise ModelUnavailable("Install backend/requirements-vibevoice.txt to enable local Microsoft VibeVoice previews.")
                    try:
                        from transformers import AutoModelForTextToWaveform, AutoProcessor

                        processor = AutoProcessor.from_pretrained(str(self.vibevoice_path), local_files_only=True)
                        model = AutoModelForTextToWaveform.from_pretrained(str(self.vibevoice_path), device_map=self.vibevoice_device_map, local_files_only=True).eval()
                    except Exception as exc:
                        raise ModelUnavailable(f"Could not load local Microsoft VibeVoice weights: {exc}") from exc
                    self._vibevoice = (processor, model)
        return self._vibevoice

    async def vibevoice_preview(self, text: str, reference_audio: bytes | None = None) -> bytes:
        if not text.strip():
            raise ValueError("Text to synthesize is empty")
        if len(text) > 500:
            raise ValueError("Microsoft VibeVoice preview text must be 500 characters or fewer")

        def work():
            import torch

            processor, model = self._load_vibevoice()
            temporary_audio = None
            try:
                content = [{"type": "text", "text": text}]
                if reference_audio:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as source:
                        source.write(reference_audio)
                        temporary_audio = Path(source.name)
                    content.append({"type": "audio", "url": str(temporary_audio)})
                conversation = [{"role": "0", "content": content}]
                inputs = processor.apply_chat_template(conversation, return_dict=True, tokenize=True, add_generation_prompt=True).to(model.device, model.dtype)
                with torch.inference_mode():
                    generated = model.generate(**inputs)
                with tempfile.TemporaryDirectory(prefix="voice-preview-") as temp_dir:
                    output_path = Path(temp_dir) / "vibevoice.wav"
                    processor.save_audio(generated, str(output_path))
                    if not output_path.is_file():
                        raise ModelUnavailable("Microsoft VibeVoice did not produce a WAV file")
                    return output_path.read_bytes()
            except ModelUnavailable:
                raise
            except Exception as exc:
                raise ModelUnavailable(f"Microsoft VibeVoice preview failed: {exc}") from exc
            finally:
                if temporary_audio:
                    temporary_audio.unlink(missing_ok=True)

        return await asyncio.to_thread(work)

    def _load_whisper(self):
        if self._whisper is None:
            if importlib.util.find_spec("faster_whisper") is None:
                raise ModelUnavailable("Install backend/requirements-ai.txt to enable the self-hosted STT adapter.")
            if not self.stt_path.is_dir():
                raise ModelUnavailable(f"Local STT model directory is missing: {self.stt_path}")
            from faster_whisper import WhisperModel

            self._whisper = WhisperModel(str(self.stt_path), device=self.stt_device, compute_type=self.stt_compute_type)
        return self._whisper

    async def transcribe(self, audio: bytes, filename: str = "audio.wav") -> dict[str, Any]:
        if not audio:
            raise ValueError("Audio file is empty")
        if self.runtime_url:
            return (await self._runtime_request("POST", "/transcribe", files={"audio": (filename, audio)})).json()

        def work():
            model = self._load_whisper()
            suffix = Path(filename).suffix if Path(filename).suffix else ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as stream:
                stream.write(audio)
                temp_path = stream.name
            try:
                segments, info = model.transcribe(temp_path, beam_size=1, vad_filter=True)
                text = " ".join(segment.text.strip() for segment in segments).strip()
                return {"text": text, "language": info.language, "language_probability": info.language_probability}
            finally:
                Path(temp_path).unlink(missing_ok=True)

        return await asyncio.to_thread(work)

    async def reply(self, user_text: str, system_prompt: str, history: list[dict[str, str]] | None = None) -> str:
        if self.runtime_url:
            result = await self._runtime_request("POST", "/reply", json={"text": user_text, "system_prompt": system_prompt, "history": (history or [])[-20:]})
            return result.json()["answer"]
        if not self.ollama_model:
            raise ModelUnavailable("Set OLLAMA_MODEL to a locally available Ollama model.")
        if not user_text.strip():
            raise ValueError("Transcript is empty")
        payload = {
            "model": self.ollama_model,
            "think": False,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt or "You are a concise, helpful voice assistant. Reply in the caller's language."},
                *(history or [])[-20:],
                {"role": "user", "content": user_text},
            ],
            "options": {"temperature": 0.3},
        }
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(f"{self.ollama_url}/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"Local Ollama request failed: {exc}") from exc
        return response.json().get("message", {}).get("content", "").strip()

    async def synthesize(self, text: str) -> bytes:
        if not text.strip():
            raise ValueError("Text to synthesize is empty")
        if self.runtime_url:
            return (await self._runtime_request("POST", "/synthesize", json={"text": text})).content
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(f"{self.piper_url}/synthesize", json={"text": text})
                if response.is_success and response.headers.get("content-type", "").startswith("audio/"):
                    return response.content
        except httpx.HTTPError:
            pass
        binary = shutil.which(self.piper_bin) or (self.piper_bin if Path(self.piper_bin).is_file() else None)
        if not binary or not self.piper_model.is_file():
            raise ModelUnavailable("Run the local Piper HTTP server and set PIPER_HTTP_URL, or configure PIPER_BIN and PIPER_MODEL_PATH.")

        def work():
            with tempfile.TemporaryDirectory(prefix="vaani-tts-") as temp_dir:
                output_path = Path(temp_dir) / "speech.wav"
                completed = subprocess.run(
                    [str(binary), "--model", str(self.piper_model), "--output_file", str(output_path)],
                    input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
                )
                if completed.returncode != 0 or not output_path.is_file():
                    detail = completed.stderr.decode("utf-8", errors="replace")[-1000:]
                    raise ModelUnavailable(f"Piper synthesis failed: {detail}")
                return output_path.read_bytes()

        return await asyncio.to_thread(work)

    async def _runtime_request(self, method: str, path: str, timeout: float = 120, **kwargs):
        if not self.runtime_token:
            raise ModelUnavailable("Set VAANI_VOICE_RUNTIME_TOKEN for the remote voice server")
        if not self.runtime_url.startswith("https://") and not os.getenv("VAANI_ALLOW_INSECURE_RUNTIME", "false").lower() == "true":
            raise ModelUnavailable("Remote voice server requires HTTPS; HTTP is allowed only with the explicit local-development setting")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                result = await client.request(method, self.runtime_url + path, headers={"X-Voice-Runtime-Token": self.runtime_token}, **kwargs)
                result.raise_for_status()
            return result
        except httpx.HTTPError as exc:
            raise ModelUnavailable("Voice server is unavailable or rejected the request. Check its URL, token and model setup.") from exc

    async def run_voice_test(self, audio: bytes, filename: str, system_prompt: str) -> dict[str, Any]:
        started = time.perf_counter()
        transcript = await self.transcribe(audio, filename)
        stt_done = time.perf_counter()
        answer = await self.reply(transcript["text"], system_prompt)
        llm_done = time.perf_counter()
        audio_out = await self.synthesize(answer)
        tts_done = time.perf_counter()
        return {
            "transcript": transcript,
            "answer": answer,
            "audio_wav_base64": __import__("base64").b64encode(audio_out).decode("ascii"),
            "latency_ms": {"stt": round((stt_done - started) * 1000), "llm": round((llm_done - stt_done) * 1000), "tts": round((tts_done - llm_done) * 1000), "total": round((tts_done - started) * 1000)},
        }


local_voice_models = LocalVoiceModels()

