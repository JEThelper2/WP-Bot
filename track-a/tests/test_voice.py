"""Voice-note handling: routing of clear / noisy / silent clips.

The three sample clips are real audio generated with the stdlib `wave`
module (a 440 Hz tone, white noise, and silence). The fake media client
serves them by media id; the stub transcriber returns what Whisper would
for that content (clear -> high-confidence transcript, noise -> garbled
low-confidence transcript, silence -> no speech). The routing logic under
test is the real pipeline: clear voice notes become normalized
`message_text`; noisy/silent ones take the low-confidence fallback and
never get a `message_text` (so intent parsing will skip them).
"""

import io
import math
import random
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from track_a.config import Settings
from track_a.main import create_app
from track_a.media import MediaPayload
from track_a.pipeline import MessageProcessor
from track_a.reply import FALLBACK_REPLY_TEXT
from track_a.store import get_message, list_messages
from track_a.transcribe import StubTranscriber, Transcription

PHONE = "15551234567"
RATE = 16000


# ------------------------------------------------------------ sample clips


def _make_wav(frames: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def sine_clip(freq: float = 440.0, seconds: float = 1.5) -> bytes:
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / RATE)))
        for i in range(int(RATE * seconds))
    )
    return _make_wav(frames)


def noise_clip(seconds: float = 1.5) -> bytes:
    n = int(RATE * seconds)
    frames = bytes(random.randrange(0, 256) for _ in range(n * 2))
    return _make_wav(frames)


def silent_clip(seconds: float = 1.5) -> bytes:
    return _make_wav(b"\x00\x00" * int(RATE * seconds))


CLIPS = {
    "clip-clear": sine_clip(),
    "clip-noise": noise_clip(),
    "clip-silent": silent_clip(),
}


class FakeMediaClient:
    def __init__(self, clips: dict[str, bytes]) -> None:
        self.clips = clips

    async def download_media(self, media_id: str) -> MediaPayload:
        if media_id not in self.clips:
            raise ValueError(f"no clip for media id {media_id!r}")
        return MediaPayload(
            content=self.clips[media_id], mime_type="audio/wav", media_id=media_id
        )


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> None:
        self.sent.append((to, text))


TRANSCRIPTIONS = {
    "clip-clear": Transcription(text="change my hours to 9-6", confidence=0.92),
    "clip-noise": Transcription(text="hxsh hrh snrf", confidence=0.15),
    "clip-silent": Transcription(text="", confidence=0.0, is_voice=False),
}


@pytest.fixture()
def app(tmp_path):
    settings = Settings(
        verify_token="test-verify-token",
        track_b_url="http://track-b:8200",
        db_path=tmp_path / "inbound.db",
        api_token="test-token",
    )
    sender = RecordingSender()
    processor = MessageProcessor(
        db_path=settings.db_path,
        media_client=FakeMediaClient(CLIPS),
        transcriber=StubTranscriber(script=TRANSCRIPTIONS),
        sender=sender,
    )
    app = create_app(settings, processor=processor)
    app.state.sender = sender
    return app


@pytest.fixture()
def client(app):
    return TestClient(app)


def audio_payload(media_id: str, wam_id: str, timestamp: str = "1700000100") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [{"profile": {"name": "Owner"}, "wa_id": PHONE}],
                            "messages": [
                                {
                                    "from": PHONE,
                                    "id": wam_id,
                                    "timestamp": timestamp,
                                    "type": "audio",
                                    "audio": {
                                        "mime_type": "audio/wav",
                                        "id": media_id,
                                        "voice": True,
                                    },
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def text_payload(body: str, wam_id: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [{"profile": {"name": "Owner"}, "wa_id": PHONE}],
                            "messages": [
                                {
                                    "from": PHONE,
                                    "id": wam_id,
                                    "timestamp": "1700000200",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def post(client: TestClient, payload: dict) -> dict:
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    return resp.json()


def only_row(app):
    rows = list_messages(app.state.settings.db_path)
    assert len(rows) == 1
    return rows[0]


# ------------------------------------------------------------------- clear


def test_clear_voice_note_becomes_normalized_text(client, app) -> None:
    post(client, audio_payload("clip-clear", "wamid.CLEAR"))
    row = only_row(app)
    assert row["processing_status"] == "transcribed"
    assert row["message_text"] == "change my hours to 9-6"
    assert app.state.sender.sent == []  # no fallback reply needed


# --------------------------------------------------------- noisy / silent


def test_noisy_voice_note_routes_to_low_confidence_fallback(client, app) -> None:
    post(client, audio_payload("clip-noise", "wamid.NOISE"))
    row = only_row(app)
    assert row["processing_status"] == "low_confidence"
    assert row["message_text"] is None  # intent parsing will skip this row
    assert app.state.sender.sent == [(PHONE, FALLBACK_REPLY_TEXT)]


def test_silent_voice_note_routes_to_low_confidence_fallback(client, app) -> None:
    post(client, audio_payload("clip-silent", "wamid.SILENT"))
    row = only_row(app)
    assert row["processing_status"] == "low_confidence"
    assert row["message_text"] is None
    assert app.state.sender.sent == [(PHONE, FALLBACK_REPLY_TEXT)]


def test_download_failure_routes_to_fallback(client, app) -> None:
    post(client, audio_payload("clip-missing", "wamid.MISSING"))
    row = only_row(app)
    assert row["processing_status"] == "failed"
    assert row["message_text"] is None
    assert app.state.sender.sent == [(PHONE, FALLBACK_REPLY_TEXT)]


# ------------------------------------------------------------------- text


def test_text_message_is_normalized_without_reply(client, app) -> None:
    post(client, text_payload("change my hours to 9-6", "wamid.TXT"))
    row = only_row(app)
    assert row["processing_status"] == "text"
    assert row["message_text"] == "change my hours to 9-6"
    assert app.state.sender.sent == []


def test_text_and_voice_normalize_to_same_field(client, app) -> None:
    """Downstream (intent parsing) only ever sees `message_text`."""
    payload = text_payload("update the menu prices", "wamid.TXT2")
    payload["entry"][0]["changes"][0]["value"]["messages"].append(
        audio_payload("clip-clear", "wamid.CLEAR2")["entry"][0]["changes"][0][
            "value"
        ]["messages"][0]
    )
    post(client, payload)

    rows = list_messages(app.state.settings.db_path)
    assert len(rows) == 2
    by_type = {r["message_type"]: r for r in rows}
    assert by_type["text"]["message_text"] == "update the menu prices"
    assert by_type["audio"]["message_text"] == "change my hours to 9-6"
    assert all(r["message_text"] for r in rows)


# ------------------------------------------------------------ non-audio


def test_image_message_is_logged_but_unsupported(client, app) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [{"profile": {"name": "Owner"}, "wa_id": PHONE}],
                            "messages": [
                                {
                                    "from": PHONE,
                                    "id": "wamid.IMG",
                                    "timestamp": "1700000300",
                                    "type": "image",
                                    "image": {"id": "IMG_1", "mime_type": "image/jpeg"},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    post(client, payload)
    row = only_row(app)
    assert row["message_type"] == "image"
    assert row["processing_status"] == "unsupported"
    assert row["message_text"] is None
    assert app.state.sender.sent == []
