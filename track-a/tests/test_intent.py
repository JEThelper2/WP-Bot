"""LLM intent parsing: message_text -> validated intent or sentinel.

The LLM is a scripted fake: it returns what a conservative, correctly
prompted model should return for each sample message. The parser logic
under test is real — prompt assembly, intent construction, contract
validation, and the unsupported/low-confidence routing.
"""

import pytest

from shared_contract import CONTRACT_VERSION, validate_intent
from track_a.intent import (
    UNSUPPORTED_SENTINEL,
    IntentParser,
    IntentParseResult,
)

OWNER = "15551234567"


class FakeLLM:
    def __init__(self, script: dict[str, dict]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, *, system: str, user: str) -> dict:
        self.calls.append((system, user))
        if user not in self.script:
            raise AssertionError(f"no scripted LLM response for message: {user!r}")
        return self.script[user]


def make_parser(script: dict[str, dict]) -> IntentParser:
    return IntentParser(llm=FakeLLM(script))


def parse(parser: IntentParser, text: str) -> IntentParseResult:
    return __import__("asyncio").run(parser.parse(text, OWNER))


# ------------------------------------------------------------ the 15+ cases


def sample_script() -> dict[str, dict]:
    """One scripted LLM response per sample message."""
    return {
        # --- clear job postings (3) ---
        "post a job for a part-time barista, $18/hr, downtown": {
            "action": "create",
            "content_type": "job",
            "fields": {
                "title": "Part-time Barista",
                "description": "$18/hr, downtown location",
                "location": "downtown",
                "category": "Hospitality",
            },
            "confidence": 0.9,
        },
        "remove the barista job posting": {
            "action": "delete",
            "content_type": "job",
            "fields": {"title": "Barista"},
            "confidence": 0.85,
        },
        "update the job posting: the position is now full-time and remote": {
            "action": "update",
            "content_type": "job",
            "fields": {"title": "Barista", "remote": True, "category": "Hospitality"},
            "confidence": 0.8,
        },
        # --- clear announcements (3) ---
        "post an announcement: we are closed on July 4th": {
            "action": "create",
            "content_type": "announcement",
            "fields": {"title": "Closed July 4th", "body": "We are closed on July 4th."},
            "confidence": 0.9,
        },
        "take down the announcement about the summer sale": {
            "action": "delete",
            "content_type": "announcement",
            "fields": {"title": "Summer Sale"},
            "confidence": 0.8,
        },
        "update the announcement about parking to mention the new lot": {
            "action": "update",
            "content_type": "announcement",
            "fields": {"title": "Parking", "body": "Use the new lot behind the building."},
            "confidence": 0.75,
        },
        # --- clear business info updates (4) ---
        "change my hours to 9-6": {
            "action": "update",
            "content_type": "business_info",
            "fields": {"hours": "9-6"},
            "confidence": 0.95,
        },
        "update my phone number to (555) 123-4567": {
            "action": "update",
            "content_type": "business_info",
            "fields": {"phone": "(555) 123-4567"},
            "confidence": 0.95,
        },
        "update my address to 123 Main St": {
            "action": "update",
            "content_type": "business_info",
            "fields": {"address": "123 Main St"},
            "confidence": 0.95,
        },
        "remove the prices from my website": {
            "action": "delete",
            "content_type": "business_info",
            "fields": {"prices": ""},
            "confidence": 0.85,
        },
        # --- ambiguous / missing-field (2) ---
        "post a job": {
            "action": "create",
            "content_type": "job",
            "fields": {},
            "confidence": 0.3,
        },
        "make some changes to the site": {
            "action": "update",
            "content_type": "business_info",
            "fields": {},
            "confidence": 0.15,
        },
        # --- out-of-scope (2) -> unsupported sentinel ---
        "redesign my homepage": {"unsupported": True},
        "add a new page about our catering": {"unsupported": True},
        # --- image (v1.5 path) (1) ---
        "make this the new homepage banner": {
            "action": "update",
            "content_type": "image",
            "fields": {
                "slot": "homepage_banner",
                "media_url": "https://cdn.example.com/new-banner.jpg",
            },
            "confidence": 0.8,
        },
    }


CLEAR_EXPECTATIONS = [
    # (message, content_type, action, min_confidence)
    ("post a job for a part-time barista, $18/hr, downtown", "job", "create", 0.7),
    ("remove the barista job posting", "job", "delete", 0.7),
    ("update the job posting: the position is now full-time and remote", "job", "update", 0.7),
    ("post an announcement: we are closed on July 4th", "announcement", "create", 0.7),
    ("take down the announcement about the summer sale", "announcement", "delete", 0.7),
    ("update the announcement about parking to mention the new lot", "announcement", "update", 0.7),
    ("change my hours to 9-6", "business_info", "update", 0.7),
    ("update my phone number to (555) 123-4567", "business_info", "update", 0.7),
    ("update my address to 123 Main St", "business_info", "update", 0.7),
    ("remove the prices from my website", "business_info", "delete", 0.7),
]


@pytest.fixture()
def parser() -> IntentParser:
    return make_parser(sample_script())


@pytest.mark.parametrize("message,content_type,action,min_conf", CLEAR_EXPECTATIONS)
def test_clear_requests_produce_valid_intents(
    parser: IntentParser, message: str, content_type: str, action: str, min_conf: float
) -> None:
    result = parse(parser, message)
    assert result.status == "intent"
    assert result.intent is not None
    # The delivered intent must satisfy the shared contract exactly.
    validate_intent(result.intent)
    assert result.intent["content_type"] == content_type
    assert result.intent["action"] == action
    assert result.intent["owner_id"] == OWNER
    assert result.intent["contract_version"] == CONTRACT_VERSION
    # Confidence is the LLM's own output, carried through.
    assert result.confidence >= min_conf
    assert result.intent["confidence"] == result.confidence


def test_ambiguous_missing_fields_route_to_low_confidence(parser: IntentParser) -> None:
    # "post a job" with no details: the LLM recognizes the category but the
    # intent fails contract validation (job create needs title+description).
    result = parse(parser, "post a job")
    assert result.status == "low_confidence"
    assert result.intent is None
    assert result.confidence <= 0.5
    assert result.raw["content_type"] == "job"  # category still detectable


def test_vague_message_routes_to_low_confidence(parser: IntentParser) -> None:
    result = parse(parser, "make some changes to the site")
    assert result.status == "low_confidence"
    assert result.intent is None
    assert result.confidence <= 0.5


@pytest.mark.parametrize(
    "message",
    [
        "redesign my homepage",
        "add a new page about our catering",
    ],
)
def test_out_of_scope_requests_produce_unsupported_sentinel(
    parser: IntentParser, message: str
) -> None:
    result = parse(parser, message)
    assert result.status == "unsupported"
    assert result.intent is None
    assert result.confidence == 0.0
    # The sentinel A4 routes to the escalation message.
    assert result.raw == {"unsupported": True}
    assert UNSUPPORTED_SENTINEL == {"content_type": None, "confidence": 0.0}


def test_image_request_uses_v15_path(parser: IntentParser) -> None:
    # image content_type is v1.5 scope — implemented, not required for the
    # v1 MVP (flagged in the A3 summary).
    result = parse(parser, "make this the new homepage banner")
    assert result.status == "intent"
    validate_intent(result.intent)
    assert result.intent["content_type"] == "image"
    assert result.intent["fields"]["slot"] == "homepage_banner"
    assert result.confidence >= 0.7


# ------------------------------------------------------- malformed LLM edge


def test_non_json_llm_output_routes_to_low_confidence() -> None:
    class GarbageLLM:
        async def complete_json(self, *, system: str, user: str) -> dict:
            return ["not", "a", "dict"]  # type: ignore[return-value]

    parser = IntentParser(llm=GarbageLLM())
    result = parse(parser, "change my hours to 9-6")
    assert result.status == "low_confidence"
    assert result.intent is None


def test_missing_confidence_routes_to_low_confidence() -> None:
    parser = make_parser(
        {
            "change my hours to 9-6": {
                "action": "update",
                "content_type": "business_info",
                "fields": {"hours": "9-6"},
                # no confidence key
            }
        }
    )
    result = parse(parser, "change my hours to 9-6")
    assert result.status == "low_confidence"
    assert result.intent is None


def test_confidence_is_clamped_and_never_invented() -> None:
    parser = make_parser(
        {
            "change my hours to 9-6": {
                "action": "update",
                "content_type": "business_info",
                "fields": {"hours": "9-6"},
                "confidence": 1.7,  # out of range
            }
        }
    )
    result = parse(parser, "change my hours to 9-6")
    assert result.status == "intent"
    assert result.intent["confidence"] == 1.0  # clamped, not invented


def test_llm_exception_routes_to_low_confidence() -> None:
    class ExplodingLLM:
        async def complete_json(self, *, system: str, user: str) -> dict:
            raise RuntimeError("api down")

    parser = IntentParser(llm=ExplodingLLM())
    result = parse(parser, "change my hours to 9-6")
    assert result.status == "low_confidence"
    assert result.intent is None


def test_empty_message_is_low_confidence_without_calling_llm() -> None:
    llm = FakeLLM({})
    parser = IntentParser(llm=llm)
    result = parse(parser, "   ")
    assert result.status == "low_confidence"
    assert llm.calls == []  # never called the LLM


def test_prompt_asks_for_llm_owned_confidence() -> None:
    """The system prompt must make the LLM return its own confidence."""
    llm = FakeLLM({"change my hours to 9-6": {"unsupported": True}})
    parser = IntentParser(llm=llm)
    parse(parser, "change my hours to 9-6")
    system_prompt = llm.calls[0][0]
    assert '"confidence"' in system_prompt
    assert "0.0-1.0" in system_prompt
    assert "CONSERVATIVE" in system_prompt
    assert "clarifying" in system_prompt
