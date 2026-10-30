# Redesign and calls without Twilio

This update preserves the FastAPI app, organization data, roles, and API routes.
It replaces the dashboard design and adds an outbound LiveKit SIP bridge. The
existing Twilio adapter remains available explicitly. No phone call has been
placed and no production service has been changed as part of this update.

## What is free, and what is paid?

- Ollama with a locally hosted Qwen model, faster-whisper, and Piper can perform
  inference without a hosted model API bill. Check the licenses of the specific
  weights and Piper voice you choose. Suitable hardware and hosting still cost money.
- LiveKit's server and SIP bridge can be self-hosted. Managed LiveKit has its own
  pricing. Neither option provides free telephone network access.
- A SIP carrier supplies the phone number/caller ID and mobile/landline minutes.
  Examples to evaluate are Telnyx and Plivo. Confirm LiveKit interoperability,
  outbound destination coverage, verified caller ID requirements, and current
  rates with the provider. No claim is made that either is cheapest or free.
- Trial credits are not a permanent way to fund ten-minute PSTN calls.

The default maximum is **900 seconds (15 minutes)**, configurable from 600 to
3600 seconds using `VAANI_MAX_CALL_SECONDS`. A matching maximum is passed to
LiveKit's SIP service so a lost application worker does not leave an unlimited
call. The callee, carrier, network, model latency, or server restart can still end
or impair a call earlier. A ten-minute conversation has not yet been live-tested.

## Architecture

```text
Published agent + authorized phone contact
                 |
          FastAPI call worker
                 |
       LiveKit room <--> LiveKit SIP <--> Your SIP carrier <--> Phone
                 |
     Whisper --> Ollama --> Piper
```

The backend joins a unique room, publishes synthesized PCM audio, subscribes to
the SIP caller, and uses VAD to separate turns. Caller interruptions cancel the
agent response and clear queued audio. Conversation context is bounded to the
last 20 messages; each turn is also recorded in the existing call transcript.
Status moves through queued, ringing, in_progress, and completed/failed. Calls
can be ended from the dashboard. An operator hangup verifies the room is removed.

## 1. Prepare the voice server

Use Python 3.12 and a persistent, always-on server with sufficient memory for the
Whisper model. The web dashboard can be hosted on Render, but a sleeping/free web
service and a tiny CPU instance are not a verified live voice deployment. For
larger deployments move the per-call worker out of the web process; this first
integration is intended for one always-on app instance. Avoid redeploying during
calls. Process shutdown ends its active SIP sessions.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements-ai.txt -r backend/requirements-sip.txt
```

Run Ollama on suitable hardware, pull `qwen3:4b` (or another fitting model), and
set `OLLAMA_BASE_URL` and `OLLAMA_MODEL=qwen3:4b`. Download a faster-whisper
CTranslate2 model directory separately and set `STT_MODEL_PATH`. For Piper,
provide both the ONNX voice and its accompanying `.onnx.json` configuration, then
set `PIPER_MODEL_PATH`; the installed Piper CLI is used if its HTTP service is not
available. Select a voice and model that actually support your desired languages.
Agent language labels alone do not install multilingual voices.

If Ollama or Piper is on another server, use a private network or an authenticated
service proxy. Do not expose an unauthenticated model server to the Internet.
The current adapters do not add custom authentication headers.

## 2. Connect LiveKit SIP and your carrier

Use a LiveKit deployment with working SIP support. With self-hosting you need
both the LiveKit server and the separate LiveKit SIP service, their required
Redis/network configuration, and the appropriate SIP/RTP/WebRTC ports. A web-only
LiveKit server without SIP is insufficient. See the official
[LiveKit outbound call guide](https://docs.livekit.io/sip/outbound-calls/) and
[SIP self-hosting guide](https://docs.livekit.io/home/self-hosting/sip/).

Create an **outbound trunk in LiveKit** using your carrier's SIP endpoint,
credentials, and authorized caller ID. Keep carrier secrets in LiveKit's trunk
configuration. This app needs only the resulting trunk ID and LiveKit credentials.
The dashboard does not ask visitors to paste API secrets into the browser.

Set these server-side variables (example values are placeholders):

```dotenv
VAANI_TELEPHONY_PROVIDER=livekit_sip
VAANI_SIP_ENABLED=true
VAANI_ENABLE_OUTBOUND_CALLS=true
VAANI_MAX_CALL_SECONDS=900
LIVEKIT_URL=wss://your-livekit-host.example
LIVEKIT_API_KEY=your-server-api-key
LIVEKIT_API_SECRET=your-server-api-secret
LIVEKIT_SIP_TRUNK_ID=your-outbound-trunk-id
OLLAMA_BASE_URL=http://your-private-ollama-host:11434
OLLAMA_MODEL=qwen3:4b
STT_MODEL_PATH=/data/models/faster-whisper-small
PIPER_MODEL_PATH=/data/models/piper/voice.onnx
```

Keep the existing `VAANI_SECRET_KEY`, database settings, bootstrap password, and
calling-window settings. `.env.example` documents the variables but the app does
**not** automatically load a `.env` file; export them in your shell or set them
in Render's environment settings. Never paste real secrets into GitHub or chat.

The SIP path does not require Twilio credentials, Twilio webhooks, or
`VAANI_MEDIA_WS_URL`. Only the legacy Twilio path uses those settings. Set
`VAANI_TELEPHONY_PROVIDER=twilio` to explicitly choose the old adapter.
This new SIP bridge handles outbound calls; inbound SIP routing is not implemented.

## 3. Apply this update to Render

1. Connect `https://github.com/majetisushas-dev/test` on branch `main` to Render.
   The redesigned source has been pushed to this repository. Use the included
   `render.yaml` Blueprint for the `vaani-testing` service, or follow the
   [step-by-step Render guide](render.md). Review the paid plan before applying.
2. For the dashboard alone, keep the default `Dockerfile`. For live local Whisper
   inference and Piper CLI support, select **`Dockerfile.voice`** in the Render
   service's Dockerfile Path setting. This installs the optional model runtimes.
3. Keep the persistent disk mounted at `/data` and SQLite at
   `sqlite:////data/vaani.db` (or preserve your PostgreSQL configuration). Put the
   model weights on persistent storage; they are not bundled in the image.
4. Configure the environment variables in Render and redeploy. Confirm that your
   host allows the WebRTC media paths needed to reach LiveKit; HTTPS alone does
   not prove audio connectivity. Use another always-on worker host if required.
5. Open Integrations and confirm the local services are configured. A green
   provider configuration check does not prove carrier reachability or balance.

## 4. Place a controlled test call

1. Sign in with your existing account. Create or edit an agent and publish it.
2. Use the agent's **Test** button to verify Ollama responds. Verify STT/TTS using
   `POST /api/voice/test` with a WAV sample through the API docs; the text-only
   test does not verify audio. Models must be installed first.
3. Go to Calls, add a contact with an international E.164 phone number and a
   reference to their permission to receive calls, then choose that contact.
4. During the organization's configured calling hours, select the published
   agent, check the call confirmation, and press **Place phone call**. The backend
   retains the existing role, consent, DNC, published-version, and model checks.
5. Verify two-way audio, several turns, and an interruption. Continue for at least
   ten minutes; check that conversation context and audio still work. End the
   call in the dashboard and verify it ended in the carrier's records too.
6. Check carrier billing for the actual rate and duration. Repeat with network
   failure/restart scenarios before using this for real customer traffic.

## Verification performed in this session

- Python source compilation, dashboard JavaScript syntax checks, and unique static HTML ID checks.
- A Node-based mocked DOM/API smoke check for call readiness, the queued call response, and disabling calls when a model is unavailable; this does not verify browser rendering.
- Offline unit tests for configuration gating, minimum call budget, PCM audio
  conversion, bounded conversation history, and mocked SIP lifecycle/failure
  cleanup, including a simulated 15-minute clock and carrier-side limit.
- No real carrier, LiveKit server, model weights, browser rendering, full FastAPI
  runtime, Render deployment, or ten-minute phone conversation was verified.
  Dependency installation was unavailable in the execution environment.

Run offline checks with `python -m unittest discover -s tests -v` after installing
at least the base dependencies. Run the dashboard control-flow check with `node tests/dashboard-smoke.cjs`. The SDK and phone network are mocked in these tests.
