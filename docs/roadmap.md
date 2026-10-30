# Roadmap from the assignment brief

## Foundation delivered in this repository

- Tenant-scoped account and role model
- Agent CRUD and publish snapshots
- IVR graph persistence, validation, and publish snapshots
- Calls and analytics data surfaces for the dashboard
- Twilio inbound/outbound adapter with SDK signature validation, bidirectional media stream, consent record, DNC list, and default-off outbound gate
- Local Ollama, faster-whisper, and Piper service adapters, health status, text response endpoint, and audio-to-audio test endpoint

## Remaining milestone sequence

1. **Research and design:** document model licenses, benchmark Hindi/English/Hinglish speech, select the self-hosted model runtime and hardware.
2. **Browser voice loop:** add streaming STT, LLM, TTS, turn detection, and barge-in over WebRTC/WebSocket.
3. **Real phone calls:** configure the Twilio account and public HTTPS/WSS deployment, then exercise inbound/outbound call control; add recording storage, caller lookup, business hours/holidays, voicemail, and transfer.
4. **IVR execution:** add the visual graph editor and simulator, then execute published graphs in the media engine.
5. **Consent-based voice cloning:** consent challenge, speaker verification, blocklist, preview, deletion, and audit trail.
6. **Campaigns, knowledge base, languages:** queue/retry/DNC rules, grounded retrieval, language routing, and concurrent-call measurements.
7. **Hardening and handover:** tenant-isolation review, signed webhooks, deployment, operational runbook, benchmark report, and demo.

The brief explicitly requires consent before any real call or voice clone. Do not enable provider calling until test recipients have agreed and provider credentials/webhook verification are configured.

