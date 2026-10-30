# Deploy `majetisushas-dev/test` on Render

The repository contains a ready-to-use Blueprint: [`render.yaml`](../render.yaml).
It creates the **vaani-testing** web service, sets `VAANI_ENVIRONMENT=testing`,
uses `Dockerfile.voice`, checks `/health`, and mounts persistent storage at `/data`.
Both Dockerfiles respect Render's injected `PORT`.

## Deploy the dashboard first

1. In Render, choose **New → Blueprint** and connect
   `https://github.com/majetisushas-dev/test` on branch `main`.
2. Review the Blueprint and plan before applying it. It specifies a **paid
   Standard instance and a 10 GB persistent disk**. No Render service or paid
   resource was created by this coding session. Confirm current prices in Render.
   The chosen plan is a starting point for testing, not a measured concurrency
   or latency guarantee.
3. Apply the Blueprint. You do not need carrier credentials or model weights to
   deploy the dashboard, sign in, or configure agents. Phone calling starts disabled.
4. After Render reports a healthy deployment, open its service URL. Sign in as
   **owner@voice.local** using the generated `VAANI_BOOTSTRAP_PASSWORD` shown in
   that service's Environment settings. Keep this value private.

The generated bootstrap password is used only when the initial account is first
created. Changing the environment value later does not reset an existing account.
Preserve `/data` across redeploys. Do not scale this SQLite/in-process-worker setup
to multiple instances; use PostgreSQL and separate workers for that architecture.

For a manual Docker Web Service, select the same repository and branch, Dockerfile
Path `Dockerfile.voice`, Docker Build Context `.`, Health Check Path `/health`,
and the variables/disk in `render.yaml`. The image starts the app itself; no build
or start command override is needed. Keep a single Uvicorn worker.

## Enable voice calling after deployment

1. Open the Render service's Shell and run:

   ```sh
   python /app/scripts/download-models.py
   ```

   This downloads the faster-whisper small model and English Piper voice to the
   mounted disk. Review their licenses before downloading. The initial model
   downloads need network access and disk capacity; a server restart does not
   download them again. These models are not bundled in the image.

2. Run Ollama on a suitable separate always-on server, install `qwen3:4b` (or
   another model that fits), and set `OLLAMA_BASE_URL` in Render to its reachable
   private endpoint. The Blueprint's localhost value is only a disabled-service
   default: the Render image does not start Ollama. Keep `OLLAMA_MODEL` matched
   to the installed model. A model server on your laptop is not reachable from
   Render using `localhost`.
3. Configure a LiveKit deployment with SIP support and an outbound trunk for your
   SIP carrier. Add these private Render environment variables:
   `LIVEKIT_URL` (wss), `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and
   `LIVEKIT_SIP_TRUNK_ID`. Keep `VAANI_TELEPHONY_PROVIDER=livekit_sip`.
4. Verify model readiness in Integrations and verify STT/TTS using `/api/voice/test`
   through the API docs. Confirm that Render can connect to LiveKit's media paths;
   an HTTP health check alone does not test WebRTC audio. If its network cannot
   support this path, move the voice worker to an appropriate host.
5. Set `VAANI_SIP_ENABLED=true` and `VAANI_ENABLE_OUTBOUND_CALLS=true`, redeploy,
   publish an agent, and add a phone contact with permission in Calls. Start a
   controlled test during the organization's calling hours. Verify two-way audio
   and a ten-minute conversation before using it for customer traffic.

`VAANI_MAX_CALL_SECONDS=900` sets a 15-minute maximum. It does not guarantee that
the callee, provider or connection stays on the line for that duration. Local AI
avoids model API charges; regular phone minutes, LiveKit hosting and compute may
still incur charges. See [the detailed SIP guide](sip-deployment.md).

## Build verification

The GitHub Actions workflow installs the app and SIP SDK dependencies, runs API
and offline voice tests, builds the exact Render voice image, then starts it with
`PORT=10000` and checks `/health` and the dashboard. Check the repository's
**Actions** tab for its actual result before deployment. A passing workflow proves
container startup, not a live SIP call or model inference with downloaded weights.
