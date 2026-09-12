"""Catalog of Kokoro voices the web app offers.

Kokoro ships 54 voice packs. The English pipeline (`lang_code="a"`) is what the
speech service loads, so only the American and British voices are listed here;
the other languages need a different pipeline and would be read with the wrong
phonemizer.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_KOKORO_VOICE = "af_heart"
VOICE_ID_RE = re.compile(r"^[a-z]{2}_[a-z]+$")

# (id, display name, accent, gender). Order within each group is the order shown.
_ENGLISH_VOICES: tuple[tuple[str, str, str, str], ...] = (
    ("af_heart", "Heart", "US", "female"),
    ("af_bella", "Bella", "US", "female"),
    ("af_nicole", "Nicole", "US", "female"),
    ("af_sarah", "Sarah", "US", "female"),
    ("af_sky", "Sky", "US", "female"),
    ("af_nova", "Nova", "US", "female"),
    ("af_aoede", "Aoede", "US", "female"),
    ("af_kore", "Kore", "US", "female"),
    ("af_alloy", "Alloy", "US", "female"),
    ("af_jessica", "Jessica", "US", "female"),
    ("af_river", "River", "US", "female"),
    ("am_michael", "Michael", "US", "male"),
    ("am_adam", "Adam", "US", "male"),
    ("am_fenrir", "Fenrir", "US", "male"),
    ("am_puck", "Puck", "US", "male"),
    ("am_echo", "Echo", "US", "male"),
    ("am_eric", "Eric", "US", "male"),
    ("am_liam", "Liam", "US", "male"),
    ("am_onyx", "Onyx", "US", "male"),
    ("am_santa", "Santa", "US", "male"),
    ("bf_emma", "Emma", "UK", "female"),
    ("bf_isabella", "Isabella", "UK", "female"),
    ("bf_alice", "Alice", "UK", "female"),
    ("bf_lily", "Lily", "UK", "female"),
    ("bm_george", "George", "UK", "male"),
    ("bm_daniel", "Daniel", "UK", "male"),
    ("bm_fable", "Fable", "UK", "male"),
    ("bm_lewis", "Lewis", "UK", "male"),
)

KOKORO_BACKENDS = {"local-kokoro", "tailscale-kokoro", "tailscale-4090", "auto", "http-tts"}


def english_voices() -> list[dict[str, Any]]:
    return [
        {
            "id": voice_id,
            "name": name,
            "accent": accent,
            "gender": gender,
            "label": f"{name} ({accent}, {gender})",
            "default": voice_id == DEFAULT_KOKORO_VOICE,
        }
        for voice_id, name, accent, gender in _ENGLISH_VOICES
    ]


def is_known_voice(voice_id: object) -> bool:
    """True when the id is one of the English voices this app lists."""
    candidate = normalize_voice(voice_id)
    return any(candidate == entry[0] for entry in _ENGLISH_VOICES)


def voice_label(voice_id: str) -> str:
    for voice_id_, name, accent, gender in _ENGLISH_VOICES:
        if voice_id_ == voice_id:
            return f"{name} ({accent}, {gender})"
    return voice_id


def normalize_voice(value: object) -> str:
    """Return a safe Kokoro voice id, or an empty string when the value is not one."""
    candidate = str(value or "").strip().lower()
    if not candidate or not VOICE_ID_RE.match(candidate):
        return ""
    return candidate


def sample_sentence(voice_id: str) -> str:
    for voice_id_, name, _accent, _gender in _ENGLISH_VOICES:
        if voice_id_ == voice_id:
            return f"Hi, I'm {name}. This is how I'll read your documents."
    return "This is how I'll read your documents."
