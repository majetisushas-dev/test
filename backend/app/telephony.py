"""Twilio Programmable Voice adapter. Real calls are disabled by default."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin


class TelephonyNotReady(RuntimeError):
    pass


class TwilioVoiceProvider:
    def __init__(self) -> None:
        self.enabled = os.getenv("VAANI_TWILIO_ENABLED", "false").lower() == "true"
        self.outbound_enabled = os.getenv("VAANI_ENABLE_OUTBOUND_CALLS", "false").lower() == "true"
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.from_number = os.getenv("TWILIO_FROM_NUMBER", "")
        self.public_base_url = os.getenv("VAANI_PUBLIC_BASE_URL", "").rstrip("/")
        self.media_url = os.getenv("VAANI_MEDIA_WS_URL", "").rstrip("/")

    def status(self) -> dict[str, Any]:
        credentials = bool(self.account_sid and self.auth_token and self.from_number)
        return {
            "provider": "twilio",
            "enabled": self.enabled,
            "credentials_configured": credentials,
            "public_https_configured": self.public_base_url.startswith("https://"),
            "secure_media_url_configured": self.media_url.startswith("wss://"),
            "outbound_enabled": self.enabled and self.outbound_enabled and credentials,
            "ready_for_inbound": self.enabled and credentials and self.public_base_url.startswith("https://") and self.media_url.startswith("wss://"),
        }

    def _require_credentials(self) -> None:
        if not self.enabled:
            raise TelephonyNotReady("Twilio is disabled. Set VAANI_TWILIO_ENABLED=true only after configuring and reviewing the provider integration.")
        if not (self.account_sid and self.auth_token):
            raise TelephonyNotReady("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.")

    def validate_signature(self, url: str, params: dict[str, str], signature: str | None) -> bool:
        self._require_credentials()
        if not signature:
            return False
        try:
            from twilio.request_validator import RequestValidator
        except ImportError as exc:
            raise TelephonyNotReady("Install the Twilio SDK from backend/requirements.txt.") from exc
        return RequestValidator(self.auth_token).validate(url, params, signature)

    def answer_url(self) -> str:
        self._require_credentials()
        if not self.public_base_url.startswith("https://") or not self.media_url.startswith("wss://"):
            raise TelephonyNotReady("Set VAANI_PUBLIC_BASE_URL (https) and VAANI_MEDIA_WS_URL (wss) to public TLS endpoints.")
        return urljoin(self.public_base_url + "/", "api/telephony/twilio/answer")

    def stream_twiml(self, agent_id: int, call_id: int) -> str:
        try:
            from twilio.twiml.voice_response import VoiceResponse
        except ImportError as exc:
            raise TelephonyNotReady("Install the Twilio SDK from backend/requirements.txt.") from exc
        response = VoiceResponse()
        connect = response.connect()
        stream = connect.stream(url=self.media_url)
        stream.parameter(name="agent_id", value=str(agent_id))
        stream.parameter(name="call_id", value=str(call_id))
        return str(response)

    def create_call(self, to_number: str, answer_url: str, status_url: str):
        self._require_credentials()
        if not self.outbound_enabled:
            raise TelephonyNotReady("Outbound calling is disabled. Set VAANI_ENABLE_OUTBOUND_CALLS=true only after consent, DNC, and calling-window controls are configured.")
        if not self.from_number:
            raise TelephonyNotReady("Set TWILIO_FROM_NUMBER before enabling outbound calls.")
        try:
            from twilio.rest import Client
        except ImportError as exc:
            raise TelephonyNotReady("Install the Twilio SDK from backend/requirements.txt.") from exc
        client = Client(self.account_sid, self.auth_token)
        try:
            limit = max(600, min(3600, int(os.getenv("VAANI_MAX_CALL_SECONDS", "900"))))
        except ValueError:
            limit = 900
        return client.calls.create(to=to_number, from_=self.from_number, url=answer_url, method="POST", status_callback=status_url, status_callback_method="POST", time_limit=limit)


twilio_provider = TwilioVoiceProvider()

