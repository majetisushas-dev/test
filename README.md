# Vaani · AI Voice Calling & IVR Platform

**Own-server setup:** [Complete model, audio-test, and phone-calling walkthrough](docs/end-to-end.md). Open **Setup & voice test** in the app to follow the same flow.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/majetisushas-dev/test)

**Repository:** `majetisushas-dev/test` · **Testing service:** `vaani-testing`.
Use the included `render.yaml` Blueprint; see the [Render deployment guide](docs/render.md).
It uses a paid instance and disk. No external model or carrier credentials are
needed to start the dashboard; configuring those services is required for calls.

A redesigned API-backed voice workspace with agent editing/publishing, model tests, a phone-call composer, call activity and operator hangup. Outbound calls default to LiveKit SIP with a carrier of your choice. Ollama, faster-whisper and Piper supply self-hosted AI inference. Twilio remains an optional legacy adapter.

**Start here:** [SIP and Render deployment guide](docs/sip-deployment.md). Local models avoid hosted AI API charges; regular mobile/landline calls still incur carrier and infrastructure charges. The default call budget is 15 minutes, but a live ten-minute conversation has not yet been verified.

## Current scope

- Organization-scoped agent and IVR flow CRUD
- Owner/editor/viewer roles enforced by the API
- Versioned agent and IVR publishing
- IVR graph validation for reachability, dangling edges, and menu defaults
- Dashboard for agents, IVR flows, and call activity
- SQLite for local development; PostgreSQL is the deployment target
- Twilio signed webhook validation, bidirectional media-stream endpoint, call status callbacks, and outbound call gate
- Consent attestations, organization DNC list, organization-timezone calling window
- Local model health reporting, text turn endpoint, and uploaded-audio STT -> LLM -> TTS test endpoint

Voice/telephony routes are present, but live use still needs a configured LiveKit SIP outbound trunk and carrier account (or an optional Twilio account), hosting, local model weights, and hardware sizing. The dashboard can initiate and end LiveKit SIP calls after the server is configured. Outbound calling remains disabled by default until credentials, models, and existing consent checks are configured. Voice cloning, recording storage, campaigns, and production compliance controls remain future work; see `docs/roadmap.md`.

## Run locally

Requirements: Python 3.11+ and Docker is optional. The local dashboard does not require Node.js.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
uvicorn app.main:app --reload --app-dir backend
```

Open <http://127.0.0.1:8000>. API docs are at <http://127.0.0.1:8000/docs>.

The local starter creates a demo organization and user (`owner@voice.local` / `change-me-now`) on first run. New organizations can also be created through `POST /api/auth/signup`; email verification is a remaining requirement. Change the bootstrap password before exposing the app beyond localhost. Set `VAANI_SECRET_KEY` to a long random value before deployment.

## Self-hosted voice models

Install the optional voice runtime dependencies in the active Python environment:

```powershell
pip install -r backend\requirements-ai.txt
```

Run Ollama locally, pull an open-weight model that fits your hardware, then set `OLLAMA_MODEL` to its local tag. Example: `ollama pull qwen3:4b`. Run Piper's local HTTP service with a voice model and set `PIPER_HTTP_URL` (default `http://127.0.0.1:5000`). Put a faster-whisper CTranslate2 model directory in `models/faster-whisper-small` and set `STT_MODEL_PATH` to that directory. Do not set the LLM URL to a hosted API; the platform calls the configured local Ollama `/api/chat` endpoint.

### Optional Microsoft VibeVoice offline preview

VibeVoice is available here only for offline, consent-gated voice previews. Microsoft marks its speech model as research-only, supports English and Chinese, and excludes real-time or low-latency telephony; therefore it is deliberately not selected by the live call pipeline. Its model card also requires explicit recorded consent for cloning and adds an audible AI disclaimer and provenance watermark.

On a machine with suitable PyTorch hardware and storage, install `backend/requirements-vibevoice.txt`, download `microsoft/VibeVoice-1.5B` with `hf download microsoft/VibeVoice-1.5B --local-dir models/vibevoice-1.5b`, and set `VIBEVOICE_MODEL_PATH` if you choose another local path. The app uses local files only and will not fetch weights during startup or inference. The Integrations page exposes model readiness and the preview form. Register the speaker's consent record there, then provide the speaker's WAV reference and preview text. Revoking that record blocks further previews. Keep Piper as the telephony TTS until a production-suitable model meeting the assignment's language and latency requirements is selected.

Use `GET /api/integrations` or the Integrations dashboard page to see which local services and weights are ready. `POST /api/voice/respond` checks text-to-LLM. `POST /api/voice/test` accepts `agent_id` and an audio file and runs the full local STT -> LLM -> TTS path. A successful model test requires the model weights and services to be installed first.

## Optional legacy Twilio setup

Set `VAANI_TELEPHONY_PROVIDER=twilio` to select this adapter. The default is now `livekit_sip`; follow [the SIP guide](docs/sip-deployment.md) to use a non-Twilio carrier.

Configure Twilio credentials plus `VAANI_PUBLIC_BASE_URL` (public HTTPS), `VAANI_MEDIA_WS_URL` (public WSS ending in `/api/telephony/twilio/media`), and the organization/agent IDs for inbound routing. Point your Twilio number's incoming voice webhook to `/api/telephony/twilio/inbound` and use POST. Twilio signs these requests; the platform validates the signature with the official Twilio SDK. Media Streams use bidirectional `<Connect><Stream>` and signed WebSocket handshakes.

`VAANI_TWILIO_ENABLED` and `VAANI_ENABLE_OUTBOUND_CALLS` default to `false`. Before enabling outbound calls, configure the local STT/LLM/TTS services, submit a consent record through `POST /api/phone-consents`, and maintain the org DNC list through `POST /api/dnc`. The outbound route also checks the published agent and `VAANI_CALL_START_HOUR` / `VAANI_CALL_END_HOUR` in the organization's timezone. No real call was made while building this integration.

To run the containerized local stack, set a secret and then start Compose:

```powershell
$env:VAANI_SECRET_KEY = "use-a-long-random-value-here"
docker compose up --build
```

### Render deployment

For a Docker web service, use the repository root as the Docker build context and `Dockerfile` as the Dockerfile path. The image creates `/data` before the app starts, which prevents SQLite's `unable to open database file` startup error. Keep `VAANI_DATABASE_URL` set to `sqlite:////data/vaani.db` and attach a persistent disk mounted at `/data`; Render's filesystem is otherwise ephemeral and the database will be lost on restart or redeploy. Set `VAANI_SECRET_KEY` and `VAANI_BOOTSTRAP_PASSWORD` as private environment variables in the Render dashboard. For multi-instance or production use, use managed PostgreSQL and configure `VAANI_DATABASE_URL` for that database instead.

## Project structure

```text
backend/app/       FastAPI API, persistence, auth, and IVR validation
web/               Dashboard assets served by the API
docs/              Architecture, roadmap, and security notes
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `VAANI_TELEPHONY_PROVIDER` | `livekit_sip` | Outbound adapter: LiveKit SIP or legacy Twilio |
| `VAANI_SIP_ENABLED` | `false` | Enable the SIP bridge after configuration |
| `VAANI_MAX_CALL_SECONDS` | `900` | Call budget, clamped to 600–3600 seconds |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_SIP_TRUNK_ID` | unset | LiveKit server access and carrier outbound trunk |
| `VAANI_DATABASE_URL` | `sqlite:///./vaani.db` | SQLAlchemy database URL |
| `VAANI_SECRET_KEY` | development-only value | Signs session tokens; set a secret in deployment |
| `VAANI_BOOTSTRAP_PASSWORD` | `change-me-now` | Password for the initial local owner account |
| `VAANI_PUBLIC_BASE_URL` | unset | Public HTTPS origin Twilio uses for callbacks |
| `VAANI_MEDIA_WS_URL` | unset | Public WSS endpoint for Twilio bidirectional media |
| `VAANI_TWILIO_ENABLED` | `false` | Master Twilio adapter gate |
| `VAANI_ENABLE_OUTBOUND_CALLS` | `false` | Additional outbound dialing gate |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | unset | Twilio API credentials and caller ID |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | local Ollama, unset model | Local LLM endpoint and model tag |
| `STT_MODEL_PATH` | `models/faster-whisper-small` | Local faster-whisper model directory |
| `PIPER_HTTP_URL` | `http://127.0.0.1:5000` | Local Piper HTTP service |

Never commit secrets or real call recordings. See `.env.example` and `.gitignore`.

