# End-to-end setup: your own models + Render + phone calls

The app has the complete software flow: create/publish an agent, record/upload an
audio test, register a permitted contact, start a SIP call, end it, and inspect
its transcript. Completing live phone setup requires your own server and provider
accounts. No carrier account, paid resource, or real phone call is created by
these instructions automatically.

## Run models on your computer/server

Use an always-on Linux/macOS/Windows computer with Docker Compose, adequate free
disk space (allow at least 10 GB for the images and models), and sufficient RAM
for Whisper, Piper and a 4B Ollama model. 16 GB RAM is a practical starting point,
not a benchmark or guarantee. A GPU can improve latency; this default stack uses
CPU and has not been benchmarked on your machine. A sleeping laptop stops calls.

1. Clone the repository and open it in a terminal:

   ```sh
   git clone https://github.com/majetisushas-dev/test.git
   cd test
   ```

2. Create a local `.env` file, which Git ignores. Generate a strong random token
   with your password manager and enter it locally. Use the same token in Render
   later. Do not commit it or paste it into chat.

   ```dotenv
   VAANI_VOICE_RUNTIME_TOKEN=your-private-random-token
   OLLAMA_MODEL=qwen3:4b
   VOICE_DOMAIN=voice.your-domain.example
   ```

3. Start the inference stack:

   ```sh
   docker compose -f docker-compose.voice.yml up -d --build
   docker compose -f docker-compose.voice.yml logs -f model-init ollama-init
   ```

   The init services download Whisper, a Piper English voice, and the Ollama
   model. The inference server starts after those downloads succeed. This can
   take several minutes and consumes bandwidth. Review model/voice licenses.
   The weights remain in named volumes across restarts. Do not delete the volumes
   unless you intend to remove them. Ollama is not exposed on a public port.

4. Check local process health:

   ```sh
   curl --fail http://127.0.0.1:8090/health
   ```

   This confirms process startup only. Authenticated `/status` and the app's audio
   test verify model readiness and inference. All inference endpoints require the
   shared token; only `/health` is public.

## Make your runtime reachable from Render

Render cannot reach `localhost` on your computer. Provide a stable HTTPS endpoint
to the runtime using your own server/domain or a suitable private connection.
For a publicly reachable server with a domain you control:

1. Point the `VOICE_DOMAIN` DNS record to your server's public IP.
2. Allow inbound TCP 80/443 to that server; ensure another web server is not
   already using those ports. Behind a home router, port forwarding and a public
   IP may be needed. CGNAT can prevent this setup; use an appropriate hosted
   server or secure tunnel instead.
3. Start the included HTTPS proxy:

   ```sh
   docker compose -f docker-compose.voice.yml --profile public up -d
   ```

   Caddy obtains TLS for your real domain. The example domain is a placeholder,
   not a working address. Never disable TLS validation. The shared token protects
   inference endpoints; add network restrictions/rate limits for production.

4. In the Render **test** service, set these values privately:

   | Variable | Value |
   |---|---|
   | `VAANI_VOICE_RUNTIME_URL` | `https://voice.YOUR-REAL-DOMAIN` |
   | `VAANI_VOICE_RUNTIME_TOKEN` | The same private token from your local `.env` |

5. Save and deploy the Render service. The existing lightweight `Dockerfile`
   supports this remote inference mode; no local Whisper installation is required
   on Render. Keep your own model server running whenever calls are enabled.

For a wholly local development setup, the runtime may use HTTP only if you
explicitly set `VAANI_ALLOW_INSECURE_RUNTIME=true` in the app. Do not enable this
for an Internet-facing endpoint. This option is off by default.

## Test an actual voice response

1. Sign in to the deployed app and open **Setup & voice test**.
2. Click **Create test agent**. It creates and publishes the English Test Assistant
   once, or reuses the existing agent of that name.
3. Refresh status. Ollama, Whisper, and Piper must all be available.
4. Select the agent, record a short sample (allow microphone access) or upload an
   audio file, and click **Run voice test**. Your browser sends audio to Render;
   Render uses the authenticated model server for STT, LLM and TTS.
5. Read the recognized text and agent response, listen to the generated speech,
   and review the latency. This is real inference, not a canned demonstration.
   It does not dial a phone. If latency is too high, use suitable hardware or a
   smaller model before attempting live calls.

## Connect phone calling

You do not yet have a LiveKit or carrier account. Complete these account-specific
steps yourself; credentials and outbound calling permissions cannot be invented.

1. Create a LiveKit Cloud project with SIP/telephony support, or self-host both
   the LiveKit server and its separate SIP service. Cloud is usually simpler for
   initial setup, but review its current charges and quotas.
2. Create an account with a SIP carrier such as Telnyx or Plivo. Confirm support
   for outbound SIP to your destination country, an authorized caller ID, account
   verification requirements, and actual per-minute rates. Fund the account if
   required. This app does not provide free phone-network minutes.
3. In LiveKit, create an **outbound SIP trunk** using the carrier's SIP domain,
   authentication username/password, and authorized originating phone number.
   Keep the carrier credentials in LiveKit. Use the resulting **LiveKit trunk ID**
   in the app, not the carrier's trunk identifier.
4. Create LiveKit API credentials for that project. Add these values privately
   to the Render service:

   | Variable | Value from your accounts |
   |---|---|
   | `LIVEKIT_URL` | Your LiveKit `wss://` project URL |
   | `LIVEKIT_API_KEY` | Your LiveKit API key |
   | `LIVEKIT_API_SECRET` | Your LiveKit API secret |
   | `LIVEKIT_SIP_TRUNK_ID` | Your LiveKit outbound trunk ID |
   | `VAANI_TELEPHONY_PROVIDER` | `livekit_sip` |
   | `VAANI_SIP_ENABLED` | `true` |
   | `VAANI_ENABLE_OUTBOUND_CALLS` | `true` |
   | `VAANI_MAX_CALL_SECONDS` | `900` |

   See the current [LiveKit outbound calling documentation](https://docs.livekit.io/sip/outbound-calls/)
   and the selected carrier's LiveKit/SIP guide for their exact account fields.

5. Save and deploy. Open Calls, refresh readiness, select your published agent,
   add your own real international phone number with permission reference
   `self-test-001`, and confirm the call. Make calls during the organization's
   configured calling hours (default 09:00–20:00 Asia/Kolkata).
6. Check ringing and connection status. Verify two-way speech, interruptions, and
   conversation memory. Continue for at least ten minutes, then click **End call**
   and verify the leg ended in the carrier dashboard. Open **Details** for the
   transcript and recorded duration. Compare with actual carrier billing.

The 900-second maximum supports a ten-minute test budget; it does not guarantee
that hardware, network, carrier, or callee stays connected. No live call has yet
been tested with your accounts.

## Persistence and the free Render service

Your current Render service is Free with an ephemeral SQLite database. Redeploys
can lose agents, contacts and call history; the free instance can also sleep.
Moving inference off Render does not change those limits. For durable operation,
configure a persistent database or an always-on deployment with a disk. No paid
upgrade has been applied. The included paid Blueprint is available when you
choose to make that change.

## Verification boundaries

GitHub checks exercise login, agent publication, contacts, outbound API gating,
call details, authenticated remote audio HTTP flow, SIP cleanup, Compose syntax,
and Docker startup. Model inference and the phone network are mocked in the
automated integration tests. Your own machine, downloaded models, provider
account and ten-minute phone call must still be tested with the steps above.
