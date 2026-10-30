"""Private authenticated inference server for a separate machine with model weights."""
import os
import secrets
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .voice_models import LocalVoiceModels, ModelUnavailable

app = FastAPI(title="Vaani Private Voice Runtime", docs_url=None, redoc_url=None, openapi_url=None)
models = LocalVoiceModels(remote=False)


async def authenticate(x_voice_runtime_token: Annotated[str | None, Header()] = None):
    expected = os.getenv("VAANI_VOICE_RUNTIME_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Voice runtime token is not configured")
    if not x_voice_runtime_token or not secrets.compare_digest(x_voice_runtime_token, expected):
        raise HTTPException(401, "Invalid voice runtime token")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ReplyInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    system_prompt: str = Field(default="", max_length=16000)
    history: list[Message] = Field(default_factory=list, max_length=20)


class SpeechInput(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@app.exception_handler(ModelUnavailable)
async def unavailable(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={"detail": "Model inference is unavailable. Check the runtime model configuration."})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status", dependencies=[Depends(authenticate)])
async def status():
    return await models.status()


@app.post("/reply", dependencies=[Depends(authenticate)])
async def reply(payload: ReplyInput):
    return {"answer": await models.reply(payload.text, payload.system_prompt, history=[x.model_dump() for x in payload.history])}


@app.post("/transcribe", dependencies=[Depends(authenticate)])
async def transcribe(audio: UploadFile = File(...)):
    data = await audio.read(20 * 1024 * 1024 + 1)
    if not data or len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Provide an audio file no larger than 20 MB")
    return await models.transcribe(data, audio.filename or "sample.wav")


@app.post("/synthesize", dependencies=[Depends(authenticate)])
async def synthesize(payload: SpeechInput):
    return Response(await models.synthesize(payload.text), media_type="audio/wav")
