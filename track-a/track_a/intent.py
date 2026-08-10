"""LLM intent parsing: normalized message_text -> contract-valid intent.

Takes the channel-agnostic `message_text` produced by the inbound
pipeline (A2) plus the owner id, and produces either

- an intent object that passes `validate_intent()` against
  `shared-contract/intent.schema.json`, or
- a signal that parsing cannot proceed.

Three result statuses:

- `intent`          — a validated intent object; `confidence` comes from
                      the LLM's own output (it is prompted to return it),
                      never bolted on afterwards.
- `low_confidence`  — the LLM output failed contract validation, was
                      malformed, or could not be produced. Treated as
                      equivalent to low confidence: no intent is emitted,
                      so the next step (A4) can ask a clarifying question
                      instead of acting on a guess.
- `unsupported`     — the request is out of scope for this product
                      ("redesign my homepage", "add a new page", ...).
                      The semantic output is the UNSUPPORTED_SENTINEL
                      (`content_type: null`, `confidence: 0`), which A4
                      routes to the escalation message.

The LLM backend is pluggable: `IntentParser` talks to any `LLMClient`
(protocol). `OpenAILLMClient` is the real implementation (lazy import,
optional `llm` extra); tests inject a scripted fake.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared_contract import CONTRACT_VERSION, ContractValidationError, validate_intent

logger = logging.getLogger("track_a.intent")

# Sentinel for out-of-scope requests. Deliberately NOT a valid intent
# (content_type is null): A4 routes this to the escalation message.
UNSUPPORTED_SENTINEL: dict[str, Any] = {"content_type": None, "confidence": 0.0}

SYSTEM_PROMPT = """\
You are the intent parser for WP-Bot, a WhatsApp bot that lets a small \
business owner maintain their WordPress site by texting. From the owner's \
message, produce a strict-JSON intent describing the change they want.

Reply with EXACTLY ONE JSON object and nothing else. Two shapes:

1. OUT-OF-SCOPE request — site redesign, adding whole new pages, or \
anything that is not adding/updating/removing a job posting, announcement, \
business info, or site image:
{"unsupported": true}

2. SUPPORTED request:
{
  "action": "create" | "update" | "delete",
  "content_type": "job" | "announcement" | "business_info" | "image",
  "fields": { ... },
  "confidence": <number 0.0-1.0>
}

Fields per content_type:
- job: title, description, location, remote (boolean), category. \
create requires title AND description.
- announcement: title, body, expires_at (optional RFC 3339 timestamp). \
create requires title AND body.
- business_info: phone, hours, address, prices. ALL optional — partial \
updates are expected ("change my hours to 9-6" -> {"hours": "9-6"}).
- image: slot ("homepage_banner" | "logo" | "gallery") plus exactly one \
of media_url or media_base64 for create/update; slot only for delete.

Confidence rules — be CONSERVATIVE. The "confidence" field is the number \
0.0-1.0 YOU return; nothing else computes it. Prefer a LOW confidence \
score (and let the bot ask a clarifying question) over a confident wrong \
guess. Lower it when the message is ambiguous, required fields are \
missing, the owner's wording is vague, or you are guessing. Out-of-scope \
requests must use shape 1, never shape 2."""


@dataclass
class IntentParseResult:
    status: str  # "intent" | "low_confidence" | "unsupported"
    intent: dict[str, Any] | None = None
    confidence: float = 0.0
    raw: dict[str, Any] | None = None  # raw LLM JSON, for logging/diagnostics


class LLMClient(Protocol):
    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]: ...


class OpenAILLMClient:
    """Real LLM backend: OpenAI chat completions with JSON output mode.

    Lazily imports `openai` (optional `llm` extra) and reads
    OPENAI_API_KEY / OPENAI_MODEL from the environment when not passed.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        client: Any = None,
    ) -> None:
        import os

        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = client

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        if not self._api_key:
            raise ValueError("OPENAI_API_KEY is not configured; cannot call the LLM")
        from openai import AsyncOpenAI

        client = self._client or AsyncOpenAI(api_key=self._api_key)
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)


class IntentParser:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or OpenAILLMClient()

    async def parse(self, message_text: str, owner_id: str) -> IntentParseResult:
        """message_text + owner_id in; validated intent (or sentinel) out."""
        message_text = (message_text or "").strip()
        if not message_text:
            return IntentParseResult(status="low_confidence")

        try:
            raw = await self.llm.complete_json(
                system=SYSTEM_PROMPT, user=message_text
            )
        except Exception as exc:
            logger.warning("LLM call failed; treating as low confidence: %s", exc)
            return IntentParseResult(status="low_confidence")

        if not isinstance(raw, dict):
            return IntentParseResult(status="low_confidence", raw={"raw": raw})

        if raw.get("unsupported") is True:
            return IntentParseResult(
                status="unsupported", confidence=0.0, raw=raw
            )

        # The LLM must return its own confidence score; we never invent one.
        # Missing confidence = malformed output -> low confidence.
        if raw.get("confidence") is None:
            logger.warning(
                "LLM output missing confidence; treating as low confidence: %s", raw
            )
            return IntentParseResult(status="low_confidence", raw=raw)

        intent = self._build_intent(raw, owner_id)
        try:
            validate_intent(intent)
        except ContractValidationError as exc:
            # The LLM produced something the contract rejects — equivalent to
            # low confidence: emit nothing, let A4 ask for clarification.
            logger.warning(
                "LLM intent failed contract validation; treating as low "
                "confidence: %s (raw=%s)",
                exc,
                raw,
            )
            return IntentParseResult(
                status="low_confidence", confidence=intent["confidence"], raw=raw
            )
        return IntentParseResult(
            status="intent",
            intent=intent,
            confidence=intent["confidence"],
            raw=raw,
        )

    @staticmethod
    def _build_intent(raw: dict[str, Any], owner_id: str) -> dict[str, Any]:
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        fields = raw.get("fields")
        if not isinstance(fields, dict):
            fields = {}

        return {
            "contract_version": CONTRACT_VERSION,
            "owner_id": owner_id,
            "action": raw.get("action"),
            "content_type": raw.get("content_type"),
            "fields": fields,
            "confidence": confidence,
        }
