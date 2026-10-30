from __future__ import annotations

import asyncio
import audioop
import base64
import json
import os
import wave
from importlib.util import find_spec
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .telephony import TelephonyNotReady, twilio_provider
from .sip_voice import call_limit, sip_provider
from .voice_models import ModelUnavailable, local_voice_models


ROOT = Path(__file__).resolve().parents[2]
# In the source checkout this resolves to the repository root. In the Docker
# image the app is copied to /app/app, so the project root is one level closer.
if not (ROOT / "web").is_dir():
    ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
DATABASE_URL = os.getenv("VAANI_DATABASE_URL", "sqlite:///./vaani.db")
database_url = make_url(DATABASE_URL)
if database_url.get_backend_name() == "sqlite" and database_url.database not in (None, "", ":memory:"):
    Path(database_url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
SECRET_KEY = os.getenv("VAANI_SECRET_KEY", "local-development-key-change-before-deploy")
ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 12
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if database_url.get_backend_name() == "sqlite" else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="owner")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    greeting: Mapped[str] = mapped_column(Text, default="Namaste! How can I help you today?")
    voice_id: Mapped[str] = mapped_column(String(100), default="stock-neutral")
    temperature: Mapped[float] = mapped_column(default=0.3)
    capture_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    published_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    versions: Mapped[list["AgentVersion"]] = relationship(cascade="all, delete-orphan", order_by="AgentVersion.version")


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class IVRFlow(Base):
    __tablename__ = "ivr_flows"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    graph: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    published_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    versions: Mapped[list["IVRVersion"]] = relationship(cascade="all, delete-orphan", order_by="IVRVersion.version")


class IVRVersion(Base):
    __tablename__ = "ivr_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("ivr_flows.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    graph: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CallRecord(Base):
    __tablename__ = "calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    direction: Mapped[str] = mapped_column(String(12), default="outbound")
    phone: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(24), default="queued")
    outcome: Mapped[str] = mapped_column(String(120), default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PhoneConsent(Base):
    __tablename__ = "phone_consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    contact_name: Mapped[str] = mapped_column(String(120), default="")
    evidence_reference: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class VoiceCloneConsent(Base):
    __tablename__ = "voice_clone_consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    speaker_name: Mapped[str] = mapped_column(String(120))
    evidence_reference: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class DNCEntry(Base):
    __tablename__ = "dnc_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    phone: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CallProviderRef(Base):
    __tablename__ = "call_provider_refs"
    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="twilio")
    provider_call_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    agent_version_id: Mapped[int] = mapped_column(ForeignKey("agent_versions.id"))


class AgentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    languages: list[str] = Field(default_factory=lambda: ["Hindi", "English", "Hinglish"])
    system_prompt: str = ""
    greeting: str = "Namaste! How can I help you today?"
    voice_id: str = "stock-neutral"
    temperature: float = Field(default=0.3, ge=0, le=1)
    capture_fields: list[dict[str, Any]] = Field(default_factory=list)


class FlowInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    graph: dict[str, Any] = Field(default_factory=dict)


class SignupInput(BaseModel):
    organization_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=12, max_length=128)


class ConsentInput(BaseModel):
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    contact_name: str = Field(default="", max_length=120)
    evidence_reference: str = Field(min_length=4, max_length=255)
    consent_attested: bool


class VoiceCloneConsentInput(BaseModel):
    speaker_name: str = Field(min_length=1, max_length=120)
    evidence_reference: str = Field(min_length=4, max_length=255)
    consent_attested: bool


class DNCInput(BaseModel):
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    reason: str = Field(default="", max_length=255)


class OutboundCallInput(BaseModel):
    agent_id: int
    consent_id: int


class VoiceTextInput(BaseModel):
    agent_id: int
    text: str = Field(min_length=1, max_length=2000)


def db_session():
    with SessionLocal() as session:
        yield session


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def token_for(user: User) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MINUTES)
    return jwt.encode({"sub": str(user.id), "org": user.organization_id, "role": user.role, "exp": expiry}, SECRET_KEY, algorithm=ALGORITHM)


def current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(db_session)) -> User:
    error = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise error
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise error
    return user


def require_editor(user: User = Depends(current_user)) -> User:
    if user.role not in {"owner", "editor"}:
        raise HTTPException(403, "Editor access required")
    return user


def org_query(model: Any, user: User, db: Session, item_id: int):
    item = db.scalar(select(model).where(model.id == item_id, model.organization_id == user.organization_id))
    if item is None:
        raise HTTPException(404, "Not found")
    return item


def validate_ivr(graph: dict[str, Any]) -> list[str]:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    errors: list[str] = []
    by_id = {str(n.get("id")): n for n in nodes if n.get("id") is not None}
    if not by_id:
        return ["Add at least one node."]
    if len(by_id) != len(nodes):
        errors.append("Every node must have a unique id.")
    starts = [node_id for node_id, node in by_id.items() if node.get("type", "").lower() == "start"]
    if len(starts) != 1:
        errors.append("A flow must contain exactly one Start node.")
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for edge in edges:
        source, target = str(edge.get("source", "")), str(edge.get("target", ""))
        if source not in by_id or target not in by_id:
            errors.append(f"Edge {edge.get('id', '(unnamed)')} points to a missing node.")
            continue
        adjacency[source].add(target)
    if starts:
        seen, todo = set(), [starts[0]]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            todo.extend(adjacency[current] - seen)
        unreachable = sorted(set(by_id) - seen)
        if unreachable:
            errors.append("Unreachable nodes: " + ", ".join(unreachable))
    for node_id, node in by_id.items():
        if node.get("type", "").lower() == "menu":
            defaults = node.get("data", {}).get("defaultPath") or node.get("data", {}).get("default")
            if not defaults:
                errors.append(f"Menu node {node_id} needs a default path.")
    return errors


app = FastAPI(title="AI Voice Calling & IVR Platform with Voice Cloning", version="0.1.0", description="Tenant-scoped agent and IVR project foundation")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == "owner@voice.local")) is None:
            org = Organization(name="AI Voice Calling & IVR Demo", timezone="Asia/Kolkata")
            db.add(org)
            db.flush()
            db.add(User(organization_id=org.id, email="owner@voice.local", password_hash=hash_password(os.getenv("VAANI_BOOTSTRAP_PASSWORD", "change-me-now")), role="owner"))
            db.commit()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "ai-voice-platform-api"}


@app.post("/api/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email == form.username.lower()))
    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(401, "Email or password is incorrect")
    return {"access_token": token_for(user), "token_type": "bearer", "user": {"email": user.email, "role": user.role, "organization_id": user.organization_id}}


@app.post("/api/auth/signup", status_code=201)
def signup(payload: SignupInput, db: Session = Depends(db_session)):
    email = payload.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    org = Organization(name=payload.organization_name.strip(), timezone="Asia/Kolkata")
    db.add(org)
    db.flush()
    user = User(organization_id=org.id, email=email, password_hash=hash_password(payload.password), role="owner")
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"access_token": token_for(user), "token_type": "bearer", "user": {"email": user.email, "role": user.role, "organization_id": user.organization_id}}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    org = db.get(Organization, user.organization_id)
    return {"email": user.email, "role": user.role, "organization": org.name if org else "", "timezone": org.timezone if org else "Asia/Kolkata"}


@app.get("/api/agents")
def list_agents(user: User = Depends(current_user), db: Session = Depends(db_session)):
    agents = db.scalars(select(Agent).where(Agent.organization_id == user.organization_id).order_by(Agent.id.desc())).all()
    return [{"id": x.id, "name": x.name, "languages": x.languages, "voice_id": x.voice_id, "published_version": x.published_version} for x in agents]


@app.post("/api/agents", status_code=201)
def create_agent(payload: AgentInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    agent = Agent(organization_id=user.organization_id, **payload.model_dump())
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return {"id": agent.id, "name": agent.name, "published_version": agent.published_version}


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    agent = org_query(Agent, user, db, agent_id)
    return {"id": agent.id, "name": agent.name, "languages": agent.languages, "system_prompt": agent.system_prompt, "greeting": agent.greeting, "voice_id": agent.voice_id, "temperature": agent.temperature, "capture_fields": agent.capture_fields, "published_version": agent.published_version}


@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: int, payload: AgentInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    agent = org_query(Agent, user, db, agent_id)
    for key, value in payload.model_dump().items():
        setattr(agent, key, value)
    db.commit()
    return {"id": agent.id, "name": agent.name, "published_version": agent.published_version}


@app.post("/api/agents/{agent_id}/publish")
def publish_agent(agent_id: int, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    agent = org_query(Agent, user, db, agent_id)
    version = agent.published_version + 1
    snapshot = {"name": agent.name, "languages": agent.languages, "system_prompt": agent.system_prompt, "greeting": agent.greeting, "voice_id": agent.voice_id, "temperature": agent.temperature, "capture_fields": agent.capture_fields}
    db.add(AgentVersion(agent_id=agent.id, version=version, snapshot=snapshot))
    agent.published_version = version
    db.commit()
    return {"id": agent.id, "published_version": version}


@app.get("/api/ivr-flows")
def list_flows(user: User = Depends(current_user), db: Session = Depends(db_session)):
    flows = db.scalars(select(IVRFlow).where(IVRFlow.organization_id == user.organization_id).order_by(IVRFlow.id.desc())).all()
    return [{"id": x.id, "name": x.name, "published_version": x.published_version, "node_count": len(x.graph.get("nodes", []))} for x in flows]


@app.post("/api/ivr-flows", status_code=201)
def create_flow(payload: FlowInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    flow = IVRFlow(organization_id=user.organization_id, name=payload.name, graph=payload.graph)
    db.add(flow)
    db.commit()
    db.refresh(flow)
    return {"id": flow.id, "name": flow.name, "published_version": flow.published_version}


@app.get("/api/ivr-flows/{flow_id}")
def get_flow(flow_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    flow = org_query(IVRFlow, user, db, flow_id)
    return {"id": flow.id, "name": flow.name, "graph": flow.graph, "published_version": flow.published_version}


@app.patch("/api/ivr-flows/{flow_id}")
def update_flow(flow_id: int, payload: FlowInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    flow = org_query(IVRFlow, user, db, flow_id)
    flow.name, flow.graph = payload.name, payload.graph
    db.commit()
    return {"id": flow.id, "name": flow.name, "published_version": flow.published_version}


@app.post("/api/ivr-flows/{flow_id}/validate")
def check_flow(flow_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    flow = org_query(IVRFlow, user, db, flow_id)
    errors = validate_ivr(flow.graph)
    return {"valid": not errors, "errors": errors}


@app.post("/api/ivr-flows/{flow_id}/publish")
def publish_flow(flow_id: int, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    flow = org_query(IVRFlow, user, db, flow_id)
    errors = validate_ivr(flow.graph)
    if errors:
        raise HTTPException(422, {"message": "Flow validation failed", "errors": errors})
    version = flow.published_version + 1
    db.add(IVRVersion(flow_id=flow.id, version=version, graph=flow.graph))
    flow.published_version = version
    db.commit()
    return {"id": flow.id, "published_version": version}


@app.get("/api/calls")
def list_calls(user: User = Depends(current_user), db: Session = Depends(db_session)):
    calls = db.scalars(select(CallRecord).where(CallRecord.organization_id == user.organization_id).order_by(CallRecord.created_at.desc()).limit(50)).all()
    refs = {ref.call_id: ref.provider for ref in db.scalars(select(CallProviderRef).where(CallProviderRef.call_id.in_([c.id for c in calls]))).all()} if calls else {}
    return [{"id": x.id, "direction": x.direction, "phone": x.phone, "status": x.status, "outcome": x.outcome, "duration_seconds": x.duration_seconds, "created_at": x.created_at.isoformat(), "provider": refs.get(x.id)} for x in calls]


@app.post("/api/calls/{call_id}/hangup")
async def hangup_call(call_id: int, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    call = org_query(CallRecord, user, db, call_id)
    ref = db.scalar(select(CallProviderRef).where(CallProviderRef.call_id == call.id))
    if not ref or ref.provider != "livekit_sip" or call.status not in {"queued", "ringing", "in_progress"}:
        raise HTTPException(409, "Only active LiveKit SIP calls can be ended here")
    # A queued worker may not have created its room yet; cancellation prevents dialing.
    task = sip_provider.tasks.get(ref.provider_call_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        # Confirm deletion after cancellation, including any cleanup that failed.
        await sip_provider.hangup(ref.provider_call_id)
    except Exception as exc:
        raise HTTPException(502, "Could not confirm that the phone call ended") from exc
    db.refresh(call)
    call.status = "completed"
    call.outcome = "ended_by_operator"
    db.commit()
    return {"id": call.id, "status": call.status}


@app.get("/api/analytics/summary")
def analytics(user: User = Depends(current_user), db: Session = Depends(db_session)):
    calls = db.scalars(select(CallRecord).where(CallRecord.organization_id == user.organization_id)).all()
    completed = [c for c in calls if c.status == "completed"]
    agents = db.scalar(select(func.count()).select_from(Agent).where(Agent.organization_id == user.organization_id)) or 0
    flows = db.scalar(select(func.count()).select_from(IVRFlow).where(IVRFlow.organization_id == user.organization_id)) or 0
    return {"total_calls": len(calls), "completed": len(completed), "agents": agents, "ivr_flows": flows}


@app.get("/api/organization")
def organization(user: User = Depends(current_user), db: Session = Depends(db_session)):
    org = db.get(Organization, user.organization_id)
    return {"id": org.id, "name": org.name, "timezone": org.timezone, "role": user.role}


def _public_request_url(request: Request) -> str:
    base = os.getenv("VAANI_PUBLIC_BASE_URL", "").rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(503, "Set VAANI_PUBLIC_BASE_URL to the public HTTPS origin configured in Twilio")
    return base + request.url.path + (f"?{request.url.query}" if request.url.query else "")


async def _validate_twilio(request: Request, params: dict[str, str]) -> None:
    try:
        valid = twilio_provider.validate_signature(_public_request_url(request), params, request.headers.get("X-Twilio-Signature"))
    except TelephonyNotReady as exc:
        raise HTTPException(503, str(exc)) from exc
    if not valid:
        raise HTTPException(403, "Invalid Twilio request signature")


def _wav_from_pcm(pcm: bytes, sample_rate: int = 8000) -> bytes:
    import io

    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm)
    return output.getvalue()


def _twilio_audio_payload(wav_bytes: bytes) -> bytes:
    import io

    with wave.open(io.BytesIO(wav_bytes), "rb") as stream:
        channels, width, rate = stream.getnchannels(), stream.getsampwidth(), stream.getframerate()
        pcm = stream.readframes(stream.getnframes())
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    elif channels > 2:
        raise ValueError("Piper output must be mono or stereo audio")
    if rate != 8000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
    return audioop.lin2ulaw(pcm, 2)


@app.get("/api/integrations")
async def integrations(user: User = Depends(current_user)):
    selected = os.getenv("VAANI_TELEPHONY_PROVIDER", "livekit_sip")
    providers = {"livekit_sip": sip_provider.status(), "twilio": twilio_provider.status()}
    return {"telephony": providers.get(selected, {"provider": selected, "outbound_enabled": False, "ready_for_inbound": False}), "providers": providers, "models": await local_voice_models.status(), "max_call_seconds": call_limit()}


@app.on_event("shutdown")
async def stop_sip_sessions():
    await sip_provider.shutdown()


@app.post("/api/voice/vibevoice/consents", status_code=201)
def record_voice_clone_consent(payload: VoiceCloneConsentInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    if not payload.consent_attested:
        raise HTTPException(400, "Confirm that recorded, explicit speaker consent exists before registering this voice")
    row = VoiceCloneConsent(organization_id=user.organization_id, speaker_name=payload.speaker_name.strip(), evidence_reference=payload.evidence_reference.strip(), created_by=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "speaker_name": row.speaker_name, "evidence_reference": row.evidence_reference, "active": row.active, "created_at": row.created_at.isoformat()}


@app.get("/api/voice/vibevoice/consents")
def list_voice_clone_consents(user: User = Depends(current_user), db: Session = Depends(db_session)):
    rows = db.scalars(select(VoiceCloneConsent).where(VoiceCloneConsent.organization_id == user.organization_id).order_by(VoiceCloneConsent.id.desc())).all()
    return [{"id": row.id, "speaker_name": row.speaker_name, "evidence_reference": row.evidence_reference, "active": row.active} for row in rows]


@app.post("/api/voice/vibevoice/consents/{consent_id}/revoke")
def revoke_voice_clone_consent(consent_id: int, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    row = db.scalar(select(VoiceCloneConsent).where(VoiceCloneConsent.id == consent_id, VoiceCloneConsent.organization_id == user.organization_id))
    if row is None:
        raise HTTPException(404, "Voice consent record not found")
    row.active = False
    db.commit()
    return {"id": row.id, "active": row.active}


@app.post("/api/voice/vibevoice/preview")
async def vibevoice_preview(consent_id: int = Form(...), text: str = Form(...), reference_audio: UploadFile = File(...), user: User = Depends(require_editor), db: Session = Depends(db_session)):
    consent = db.scalar(select(VoiceCloneConsent).where(VoiceCloneConsent.id == consent_id, VoiceCloneConsent.organization_id == user.organization_id, VoiceCloneConsent.active.is_(True)))
    if consent is None:
        raise HTTPException(403, "An active organization voice-consent record is required")
    audio = await reference_audio.read(20 * 1024 * 1024 + 1)
    if len(audio) > 20 * 1024 * 1024:
        raise HTTPException(413, "Voice reference audio must be 20 MB or smaller")
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise HTTPException(400, "Upload a valid WAV voice sample")
    try:
        audio_out = await local_voice_models.vibevoice_preview(text, audio)
    except ModelUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"model": "Microsoft VibeVoice", "purpose": "offline preview only", "speaker_name": consent.speaker_name, "audio_wav_base64": base64.b64encode(audio_out).decode("ascii"), "note": "Research preview. VibeVoice is not connected to live calls and supports English and Chinese only. Use this voice only with the speaker's recorded explicit consent."}


@app.post("/api/phone-consents", status_code=201)
def record_phone_consent(payload: ConsentInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    if not payload.consent_attested:
        raise HTTPException(400, "Confirm that this person agreed to receive calls before recording consent")
    row = PhoneConsent(organization_id=user.organization_id, phone=payload.phone, contact_name=payload.contact_name.strip(), evidence_reference=payload.evidence_reference.strip(), created_by=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "phone": row.phone, "active": row.active, "created_at": row.created_at.isoformat()}


@app.get("/api/phone-consents")
def list_phone_consents(user: User = Depends(current_user), db: Session = Depends(db_session)):
    rows = db.scalars(select(PhoneConsent).where(PhoneConsent.organization_id == user.organization_id).order_by(PhoneConsent.id.desc())).all()
    return [{"id": row.id, "phone": row.phone, "contact_name": row.contact_name, "evidence_reference": row.evidence_reference, "active": row.active} for row in rows]


@app.post("/api/dnc", status_code=201)
def add_dnc(payload: DNCInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    row = db.scalar(select(DNCEntry).where(DNCEntry.organization_id == user.organization_id, DNCEntry.phone == payload.phone))
    if row is None:
        row = DNCEntry(organization_id=user.organization_id, phone=payload.phone, reason=payload.reason.strip())
        db.add(row)
        db.commit()
        db.refresh(row)
    return {"id": row.id, "phone": row.phone}


@app.post("/api/calls/outbound", status_code=202)
async def place_outbound_call(payload: OutboundCallInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    provider_name = os.getenv("VAANI_TELEPHONY_PROVIDER", "livekit_sip")
    if provider_name not in {"livekit_sip", "twilio"}:
        raise HTTPException(503, "VAANI_TELEPHONY_PROVIDER must be livekit_sip or twilio")
    settings = sip_provider.status() if provider_name == "livekit_sip" else twilio_provider.status()
    if not settings["outbound_enabled"]:
        raise HTTPException(503, "Configure the selected phone provider and enable outbound calling before dialing")
    agent = org_query(Agent, user, db, payload.agent_id)
    if agent.published_version < 1:
        raise HTTPException(409, "Publish the agent before placing a call")
    version = db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.version == agent.published_version))
    if version is None:
        raise HTTPException(409, "Published agent version is missing")
    consent = db.scalar(select(PhoneConsent).where(PhoneConsent.id == payload.consent_id, PhoneConsent.organization_id == user.organization_id, PhoneConsent.active.is_(True)))
    if consent is None:
        raise HTTPException(403, "An active consent record is required before calling")
    if db.scalar(select(DNCEntry.id).where(DNCEntry.organization_id == user.organization_id, DNCEntry.phone == consent.phone)):
        raise HTTPException(409, "This number is on the organization's do-not-call list")
    org = db.get(Organization, user.organization_id)
    try:
        local_now = datetime.now(ZoneInfo(org.timezone if org else "Asia/Kolkata"))
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(409, "Organization timezone is invalid") from exc
    start_hour = int(os.getenv("VAANI_CALL_START_HOUR", "9"))
    end_hour = int(os.getenv("VAANI_CALL_END_HOUR", "20"))
    if not start_hour <= local_now.hour < end_hour:
        raise HTTPException(409, f"Calls are permitted from {start_hour:02d}:00 to {end_hour:02d}:00 in the organization's timezone")
    if provider_name == "twilio" and (not twilio_provider.public_base_url.startswith("https://") or not twilio_provider.media_url.startswith("wss://")):
        raise HTTPException(503, "Configure public HTTPS and WSS URLs before enabling outbound calls")
    model_state = await local_voice_models.status()
    if not (model_state["llm"].get("model_available") and model_state["stt"]["available"] and model_state["tts"]["available"] and find_spec("webrtcvad")):
        raise HTTPException(503, "Outbound calls require configured local STT, LLM, and TTS models")
    call = CallRecord(organization_id=user.organization_id, direction="outbound", phone=consent.phone, status="queued", outcome=f"consent:{consent.id}")
    db.add(call)
    db.flush()
    if provider_name == "livekit_sip":
        call_id = call.id

        def update_status(call_status: str, duration: int):
            with SessionLocal() as session:
                record = session.get(CallRecord, call_id)
                if record:
                    record.status = call_status
                    record.duration_seconds = duration
                    if call_status == "failed":
                        record.outcome = "sip_or_model_error"
                    session.commit()

        def append_turn(caller_text: str, agent_text: str):
            with SessionLocal() as session:
                record = session.get(CallRecord, call_id)
                if record:
                    at = datetime.now(timezone.utc).isoformat()
                    record.transcript = [*record.transcript, {"speaker": "caller", "text": caller_text, "at": at}, {"speaker": "agent", "text": agent_text, "at": at}]
                    session.commit()

        # Store the route before scheduling the worker; its callbacks use new sessions.
        room_name = ""
        try:
            room_name = sip_provider.start(consent.phone, dict(version.snapshot), update_status, append_turn)
            db.add(CallProviderRef(call_id=call_id, provider="livekit_sip", provider_call_id=room_name, agent_id=agent.id, agent_version_id=version.id))
            db.commit()
        except Exception as exc:
            if room_name and room_name in sip_provider.tasks:
                sip_provider.tasks[room_name].cancel()
            db.rollback()
            raise HTTPException(502, "Could not queue the SIP voice session") from exc
        return {"id": call_id, "status": "queued", "provider": "livekit_sip", "max_call_seconds": call_limit()}
    twiml_url = twilio_provider.answer_url() + "?" + urlencode({"call_id": call.id, "agent_id": agent.id})
    status_url = twilio_provider.public_base_url + "/api/telephony/twilio/status?" + urlencode({"call_id": call.id})
    try:
        provider_call = await asyncio.to_thread(twilio_provider.create_call, consent.phone, twiml_url, status_url)
        db.add(CallProviderRef(call_id=call.id, provider="twilio", provider_call_id=provider_call.sid, agent_id=agent.id, agent_version_id=version.id))
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = CallRecord(organization_id=user.organization_id, direction="outbound", phone=consent.phone, status="failed", outcome="provider_error")
        db.add(failed)
        db.commit()
        raise HTTPException(502, "Telephony provider could not start the call") from exc
    return {"id": call.id, "status": call.status, "provider": "twilio"}


@app.post("/api/telephony/twilio/inbound", response_class=Response)
async def twilio_inbound(request: Request, db: Session = Depends(db_session)):
    params = {key: str(value) for key, value in (await request.form()).items()}
    await _validate_twilio(request, params)
    try:
        if not twilio_provider.status()["ready_for_inbound"]:
            raise TelephonyNotReady("Inbound calling requires enabled Twilio credentials, HTTPS, and WSS URLs")
        org_id = int(os.getenv("TWILIO_ORGANIZATION_ID", "0"))
        agent_id = int(os.getenv("TWILIO_INBOUND_AGENT_ID", "0"))
    except (TelephonyNotReady, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc
    org = db.get(Organization, org_id)
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.organization_id == org_id))
    if org is None or agent is None or agent.published_version < 1:
        raise HTTPException(503, "Configure TWILIO_ORGANIZATION_ID and a published TWILIO_INBOUND_AGENT_ID")
    model_state = await local_voice_models.status()
    if not (model_state["llm"].get("model_available") and model_state["stt"]["available"] and model_state["tts"]["available"] and find_spec("webrtcvad")):
        raise HTTPException(503, "Inbound calls require configured local STT, LLM, TTS, and VAD models")
    call_sid = params.get("CallSid")
    if not call_sid:
        raise HTTPException(400, "Missing Twilio CallSid")
    call = CallRecord(organization_id=org_id, direction="inbound", phone=params.get("From", ""), status="in_progress")
    db.add(call)
    db.flush()
    version = db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.version == agent.published_version))
    if version is None:
        raise HTTPException(503, "Published agent version is missing")
    db.add(CallProviderRef(call_id=call.id, provider="twilio", provider_call_id=call_sid, agent_id=agent.id, agent_version_id=version.id))
    db.commit()
    try:
        xml = twilio_provider.stream_twiml(agent.id, call.id)
    except TelephonyNotReady as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(xml, media_type="application/xml")


@app.post("/api/telephony/twilio/answer", response_class=Response)
async def twilio_outbound_answer(request: Request, db: Session = Depends(db_session)):
    params = {key: str(value) for key, value in (await request.form()).items()}
    await _validate_twilio(request, params)
    try:
        call_id, agent_id = int(request.query_params["call_id"]), int(request.query_params["agent_id"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Invalid call routing parameters") from exc
    ref = db.scalar(select(CallProviderRef).where(CallProviderRef.call_id == call_id, CallProviderRef.agent_id == agent_id))
    call = db.get(CallRecord, call_id)
    if ref is None or call is None or call.status not in {"queued", "ringing", "in_progress"}:
        raise HTTPException(404, "Call is not active")
    if params.get("CallSid") and ref.provider_call_id != params["CallSid"]:
        raise HTTPException(403, "Call SID does not match the queued call")
    try:
        xml = twilio_provider.stream_twiml(agent_id, call_id)
    except TelephonyNotReady as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(xml, media_type="application/xml")


@app.post("/api/telephony/twilio/status")
async def twilio_status(request: Request, db: Session = Depends(db_session)):
    params = {key: str(value) for key, value in (await request.form()).items()}
    await _validate_twilio(request, params)
    try:
        call_id = int(request.query_params["call_id"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Invalid call routing parameter") from exc
    call = db.get(CallRecord, call_id)
    ref = db.scalar(select(CallProviderRef).where(CallProviderRef.call_id == call_id))
    if call is None or ref is None or (params.get("CallSid") and ref.provider_call_id != params["CallSid"]):
        raise HTTPException(404, "Call not found")
    state = params.get("CallStatus", "").lower().replace("-", "_")
    if state in {"queued", "ringing", "in_progress", "completed", "busy", "failed", "no_answer", "canceled"}:
        call.status = "failed" if state in {"busy", "failed", "no_answer", "canceled"} else state
    if params.get("CallDuration", "").isdigit():
        call.duration_seconds = int(params["CallDuration"])
    db.commit()
    return {"ok": True}


@app.post("/api/voice/respond")
async def local_voice_respond(payload: VoiceTextInput, user: User = Depends(require_editor), db: Session = Depends(db_session)):
    agent = org_query(Agent, user, db, payload.agent_id)
    try:
        response_text = await local_voice_models.reply(payload.text, agent.system_prompt)
    except ModelUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"answer": response_text, "model": os.getenv("OLLAMA_MODEL", "")}


@app.post("/api/voice/test")
async def local_voice_test(agent_id: int = Form(...), audio: UploadFile = File(...), user: User = Depends(require_editor), db: Session = Depends(db_session)):
    agent = org_query(Agent, user, db, agent_id)
    audio_bytes = await audio.read(20 * 1024 * 1024 + 1)
    if len(audio_bytes) > 20 * 1024 * 1024:
        raise HTTPException(413, "Voice test audio must be 20 MB or smaller")
    try:
        result = await local_voice_models.run_voice_test(audio_bytes, audio.filename or "audio.wav", agent.system_prompt)
    except ModelUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.websocket("/api/telephony/twilio/media")
async def twilio_media_stream(websocket: WebSocket):
    signature = websocket.headers.get("X-Twilio-Signature")
    try:
        if not twilio_provider.status()["ready_for_inbound"]:
            await websocket.close(code=1013, reason="Twilio media service is not configured")
            return
        canonical = twilio_provider.media_url.replace("wss://", "https://", 1).rstrip("/") + "/"
        if not twilio_provider.validate_signature(canonical, dict(websocket.query_params), signature):
            await websocket.close(code=1008, reason="Invalid Twilio signature")
            return
    except TelephonyNotReady:
        await websocket.close(code=1013, reason="Twilio media service is not configured")
        return
    try:
        import webrtcvad
    except ImportError:
        await websocket.close(code=1013, reason="Install backend/requirements-ai.txt for live voice activity detection")
        return
    await websocket.accept()
    stream_sid, call_id = "", 0
    agent_prompt, greeting = "", "Namaste! How can I help you today?"
    vad = webrtcvad.Vad(2)
    frame_bytes, silence_frames, started_speaking = 320, 0, False
    utterance: list[bytes] = []
    response_task: asyncio.Task | None = None
    speaking = False

    async def send_audio(pcm_wav: bytes):
        nonlocal speaking
        mulaw = _twilio_audio_payload(pcm_wav)
        speaking = True
        try:
            for offset in range(0, len(mulaw), 160):
                chunk = mulaw[offset : offset + 160]
                await websocket.send_json({"event": "media", "streamSid": stream_sid, "media": {"payload": base64.b64encode(chunk).decode("ascii")}})
                await asyncio.sleep(0.02)
        finally:
            speaking = False

    async def answer_turn(pcm: bytes):
        nonlocal speaking
        try:
            transcript = await local_voice_models.transcribe(_wav_from_pcm(pcm), "turn.wav")
            if not transcript["text"]:
                return
            response_text = await local_voice_models.reply(transcript["text"], agent_prompt)
            with SessionLocal() as session:
                record = session.get(CallRecord, call_id)
                if record:
                    record.transcript = [*record.transcript, {"speaker": "caller", "text": transcript["text"], "at": datetime.now(timezone.utc).isoformat()}, {"speaker": "agent", "text": response_text, "at": datetime.now(timezone.utc).isoformat()}]
                    session.commit()
            wav = await local_voice_models.synthesize(response_text)
            await send_audio(wav)
        except asyncio.CancelledError:
            raise
        except (ModelUnavailable, ValueError, OSError):
            return

    try:
        while True:
            message = json.loads(await websocket.receive_text())
            event = message.get("event")
            if event == "start":
                stream_sid = message.get("streamSid", "")
                custom = message.get("start", {}).get("customParameters", {})
                call_id = int(custom.get("call_id", "0"))
                with SessionLocal() as session:
                    ref = session.scalar(select(CallProviderRef).where(CallProviderRef.call_id == call_id))
                    version = session.get(AgentVersion, ref.agent_version_id) if ref else None
                    if ref is None or version is None:
                        await websocket.close(code=1008, reason="No active call route")
                        return
                    agent_prompt = version.snapshot.get("system_prompt", "")
                    greeting = version.snapshot.get("greeting", greeting)
                try:
                    greeting_wav = await local_voice_models.synthesize(greeting)
                    asyncio.create_task(send_audio(greeting_wav))
                except ModelUnavailable:
                    pass
            elif event == "media":
                encoded = message.get("media", {}).get("payload", "")
                if not encoded:
                    continue
                pcm = audioop.ulaw2lin(base64.b64decode(encoded), 2)
                for pos in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
                    frame = pcm[pos : pos + frame_bytes]
                    is_speech = vad.is_speech(frame, 8000)
                    if is_speech and speaking:
                        await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                        speaking = False
                    if is_speech:
                        started_speaking = True
                        silence_frames = 0
                        utterance.append(frame)
                    elif started_speaking:
                        utterance.append(frame)
                        silence_frames += 1
                        if silence_frames >= 30:
                            if response_task and not response_task.done():
                                response_task.cancel()
                            response_task = asyncio.create_task(answer_turn(b"".join(utterance)))
                            utterance, silence_frames, started_speaking = [], 0, False
            elif event == "stop":
                if response_task and not response_task.done():
                    await response_task
                with SessionLocal() as session:
                    record = session.get(CallRecord, call_id)
                    if record and record.status == "in_progress":
                        record.status = "completed"
                        session.commit()
                break
    except WebSocketDisconnect:
        if response_task and not response_task.done():
            response_task.cancel()

