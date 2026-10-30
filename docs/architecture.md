# Architecture

```text
Dashboard (web/) -> FastAPI API -> PostgreSQL (SQLite locally)
                                -> Twilio Voice adapter + signed callbacks
                                -> bidirectional Twilio Media Stream (WSS)
                                -> local voice loop (VAD -> faster-whisper -> Ollama -> Piper)
                                -> Redis queue (planned for campaigns)
                                -> MinIO recordings (planned)
```

The API is the authority for organization scoping and role checks. Every tenant-owned query includes the authenticated user's `organization_id`. Published agents and flows are immutable snapshots so later edits do not silently change active call behavior. Twilio requests are checked with its official SDK validator. Outbound dialing requires an active consent record, a DNC check, a published agent version, a configured local model pipeline, a calling window, and both provider gates.

## Planned runtime boundaries

- `telephony.py`: current Twilio calls, signed callbacks, TwiML stream creation, DNC/consent checks, and safe defaults; other providers can implement the same boundary.
- `voice_models.py`: local Ollama, faster-whisper, and Piper adapters with health and stage latency reporting.
- `main.py`: per-call μ-law audio framing, VAD, STT -> LLM -> TTS loop, response audio and barge-in clearing.
- `models/`: voice cloning, embeddings, and persistent model health metrics remain planned.
- `campaigns/`: durable queues, calling windows, concurrency, retry, and DNC enforcement.
- `storage/`: recording and uploaded knowledge-file storage.

No hosted proprietary AI service should be called by the final system. Keep provider-specific code behind interfaces and record the model/license/hardware used for each deployment.

