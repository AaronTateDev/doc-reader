from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import tempfile
import uuid
import zlib
from collections import Counter
from dataclasses import dataclass, asdict, field, fields
from email.parser import BytesParser
from email.policy import default
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, unquote, urlparse

from .extract import iter_document_blocks
from .platform_tools import (
    IS_WINDOWS,
    LOCAL_KOKORO_LABEL,
    LOCAL_STT_LABEL,
    dictation_hotkey_label,
    find_tool,
    kill_process_tree,
    popen_process_group_kwargs,
)
from .speech import (
    DEFAULT_TTS_MAC_URL,
    DEFAULT_TTS_UMBRA_URL,
    OPENAI_TTS_INSTRUCTIONS,
    OPENAI_TTS_MODEL,
    OPENAI_TTS_RESPONSE_FORMAT,
    OPENAI_TTS_VOICE,
    resolve_openai_api_key,
)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".markdown"}
SUPPORTED_AUDIO_SUFFIXES = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
    ".webm",
}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_RATE = 180
MIN_READ_RATE = 90
MAX_READ_RATE = 300
READ_RATE_STEP = 5
DEFAULT_LIBRARY_AUDIO_TIMEOUT_SECONDS = 300
DEFAULT_ANALYSIS_INTERVAL_SECONDS = 1800
DEFAULT_ANALYSIS_INITIAL_DELAY_SECONDS = 60
DEFAULT_ANALYSIS_BATCH_SIZE = 12
DEFAULT_ANALYSIS_ITEM_MAX_CHARS = 4000
DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 120
DEFAULT_ANALYSIS_MODEL = "llama3.1:8b"
DEFAULT_SERVICE_HEALTH_TIMEOUT_SECONDS = 0.8
DEFAULT_WEB_SPEECH_BACKEND = "local-kokoro"
CHUNK_START_RE = re.compile(r"^\[doc-reader\]\s+chunk-start\s+index=(\d+)\s*$")
CHUNK_DONE_RE = re.compile(r"^\[doc-reader\]\s+chunk-done\s+index=(\d+)\s*$")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")
TIMESTAMP_LINE_PREFIX_RE = re.compile(r"^\s*\[[0-9:.]+\s*-\s*[0-9:.]+\]\s*")
NATIVE_HELPER_LABEL = "com.docreader.tray"
NATIVE_HELPER_STALE_SECONDS = 8.0
DEFAULT_MICROPHONE_MATCH = "logi,logitech"
SPEECH_BACKENDS = {
    "tailscale-4090": "Remote Kokoro (strict)",
    "auto": "Local fallback",
    "tailscale-chatterbox": "Remote Chatterbox (experimental)",
    "tailscale-kokoro": "Remote Kokoro",
    "local-kokoro": LOCAL_KOKORO_LABEL,
    "macsay": "macOS Voice",
    "openai": "OpenAI API",
}
if sys.platform != "darwin":
    SPEECH_BACKENDS.pop("macsay", None)
    if "pyttsx3" in SPEECH_BACKENDS:
        SPEECH_BACKENDS["pyttsx3"] = "Windows Voice" if IS_WINDOWS else "System Voice"
DEFAULT_STT_ENABLED = True
STYLE_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "even",
        "for",
        "from",
        "get",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "me",
        "more",
        "my",
        "next",
        "no",
        "not",
        "now",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "so",
        "some",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "use",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "you",
        "your",
    }
)


@dataclass
class HistoryItem:
    id: str
    kind: str
    title: str
    source_path: str
    snippet: str
    created_at: float
    updated_at: float
    last_seconds: float = 0.0
    last_chunk_start_seconds: float = 0.0
    resume_chunk_index: int = 0
    completed: bool = False
    source: str = ""
    source_item_id: str = ""
    source_meta: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    text_hash: str = ""
    audio_state: str = ""
    audio_path: str = ""
    audio_content_type: str = ""
    audio_bytes: int = 0
    audio_error: str = ""
    audio_created_at: float = 0.0
    audio_updated_at: float = 0.0
    word_count: int = 0
    metrics_channel: str = ""


class ReaderService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.upload_dir = root / "web-documents"
        self.text_dir = root / "web-text"
        self.audio_dir = root / "prepared-audio"
        self.service_inbox_dir = root / "service-inbox"
        self.recordings_dir = root / "dictation-recordings"
        self.history_path = root / "web-history.json"
        self.analysis_path = root / "library-analysis.json"
        self.analysis_batch_dir = root / "library-analysis-batches"
        self.settings_path = root / "web-settings.json"
        self.rate_control_path = root / "read-rate-control.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.text_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.service_inbox_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_batch_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._active_id: str | None = None
        self._paused_id: str | None = None
        self._active_chunk_index: int | None = None
        self._last_completed_chunk_index = -1
        self._resume_chunk_index = 0
        self._start_time: float | None = None
        self._start_offset_seconds = 0.0
        self._active_chunk_start_seconds = 0.0
        self._resume_start_chunk_offset_seconds = 0.0
        self._last_position_seconds = 0.0
        self._status = "Ready."
        self._last_reader_error: str | None = None
        self._suppress_exit_status_for_pid: int | None = None
        self._audio_jobs: set[str] = set()
        self._analysis_lock = threading.RLock()
        self._analysis_stop = threading.Event()
        self._analysis_running = False
        self._analysis_last_error = ""
        self._analysis_thread: threading.Thread | None = None
        if _env_flag("DOC_READER_ANALYSIS_ENABLED", True):
            self._start_analysis_worker()

    def state(self) -> dict[str, Any]:
        with self._lock:
            items = self._items()
            metrics = self._metrics_snapshot(items)
            library = [self._item_payload(item) for item in items]
            settings = self._settings()
            readings = [
                self._item_payload(item)
                for item in items
                if not _is_dictation_item(item) and not _is_clawdad_item(item)
            ]
            dictations = [self._item_payload(item) for item in items if _is_dictation_item(item)]
            clawdad_items = [self._item_payload(item) for item in items if _is_clawdad_item(item)]
            return {
                "ok": True,
                "app": "doc-reader",
                "status": self._status,
                "tts": self.tts_status(),
                "stt": self.stt_status(),
                "running": self._process is not None and self._process.poll() is None,
                "paused": self._process is None and self._paused_id is not None,
                "active_id": self._active_id or self._paused_id,
                "items": library,
                "library": library,
                "readings": readings,
                "dictations": dictations,
                "clawdad": clawdad_items,
                "metrics": metrics,
                "analysis": self.analysis_status(items=items, metrics=metrics),
                "settings": self._settings_payload(settings),
            }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "app": "doc-reader",
            "running": self._process is not None and self._process.poll() is None,
            "paused": self._process is None and self._paused_id is not None,
            "stt_enabled": self._stt_enabled(),
        }

    def native_status(self) -> dict[str, Any]:
        settings = self._settings()
        return {
            "ok": True,
            "app": "doc-reader",
            "status": self._status,
            "running": self._process is not None and self._process.poll() is None,
            "paused": self._process is None and self._paused_id is not None,
            "active_id": self._active_id or self._paused_id,
            "stt": {
                "enabled": self._stt_enabled(),
                "hotkey": dictation_hotkey_label(),
                "microphone": _microphone_payload(settings),
            },
        }

    def add_text(self, text: str, *, label: str = "Text", kind: str = "text") -> HistoryItem:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("No text to read.")

        item_id = str(uuid.uuid4())
        source_path = self.text_dir / f"{item_id}.txt"
        source_path.write_text(f"{cleaned}\n", encoding="utf-8")
        now = time.time()
        snippet = _snippet(cleaned)
        item = HistoryItem(
            id=item_id,
            kind=kind,
            title=_title(label, snippet),
            source_path=str(source_path),
            snippet=snippet,
            created_at=now,
            updated_at=now,
            text_hash=_text_hash(cleaned),
            word_count=_word_count(cleaned),
            metrics_channel=_metrics_channel_for_kind(kind),
        )
        with self._lock:
            items = self._items()
            items.append(item)
            self._save_items(items)
        return item

    def add_document(self, filename: str, data: bytes) -> HistoryItem:
        safe_name = _safe_filename(filename)
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError("Unsupported file type.")
        if not data:
            raise ValueError("Uploaded file was empty.")

        item_id = str(uuid.uuid4())
        source_path = self.upload_dir / f"{item_id}-{safe_name}"
        source_path.write_bytes(data)
        now = time.time()
        item = HistoryItem(
            id=item_id,
            kind="document",
            title=safe_name,
            source_path=str(source_path),
            snippet=safe_name,
            created_at=now,
            updated_at=now,
            word_count=_document_word_count(source_path),
            metrics_channel="tts",
        )
        with self._lock:
            items = self._items()
            items.append(item)
            self._save_items(items)
        return item

    def upsert_library_item(self, payload: dict[str, Any]) -> tuple[HistoryItem, bool]:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("No text to save.")

        source = _safe_source(payload.get("source"))
        source_item_id = _safe_source_item_id(
            payload.get("source_item_id")
            or payload.get("sourceItemId")
            or payload.get("external_id")
            or payload.get("externalId")
        )
        kind = _safe_library_kind(payload.get("kind"), source=source)
        source_meta = _source_meta_from_payload(payload)
        tags = _safe_tags(payload.get("tags"))
        text_hash = _text_hash(text)
        now = time.time()
        item: HistoryItem | None = None

        with self._lock:
            items = self._items()
            if source and source_item_id:
                for existing in items:
                    if existing.source == source and existing.source_item_id == source_item_id:
                        item = existing
                        break

            text_changed = item is not None and item.text_hash and item.text_hash != text_hash
            if item is None:
                item_id = str(uuid.uuid4())
                source_path = self.text_dir / f"{item_id}.txt"
                source_path.write_text(f"{text}\n", encoding="utf-8")
                snippet = _snippet(text)
                item = HistoryItem(
                    id=item_id,
                    kind=kind,
                    title=_library_title(payload, kind=kind, source=source, snippet=snippet),
                    source_path=str(source_path),
                    snippet=snippet,
                    created_at=now,
                    updated_at=now,
                    source=source,
                    source_item_id=source_item_id,
                    source_meta=source_meta,
                    tags=tags,
                    text_hash=text_hash,
                    word_count=_word_count(text),
                    metrics_channel=_metrics_channel_for_kind(kind),
                )
                items.append(item)
            else:
                if text_changed or not Path(item.source_path).is_file():
                    source_path = Path(item.source_path) if item.source_path else self.text_dir / f"{item.id}.txt"
                    source_path.write_text(f"{text}\n", encoding="utf-8")
                    self._clear_item_audio_locked(item, remove_file=True)
                item.kind = kind
                item.title = _library_title(payload, kind=kind, source=source, snippet=_snippet(text))
                item.source_path = item.source_path or str(self.text_dir / f"{item.id}.txt")
                item.snippet = _snippet(text)
                item.updated_at = now
                item.source = source
                item.source_item_id = source_item_id
                item.source_meta = source_meta
                item.tags = tags
                item.text_hash = text_hash
                item.word_count = _word_count(text)
                item.metrics_channel = _metrics_channel_for_kind(kind)

            self._save_items(items)

        prepare_audio = bool(payload.get("prepare_audio") or payload.get("prepareAudio"))
        if prepare_audio:
            self.prepare_library_audio(item.id)
        return item, prepare_audio

    def library_items(self, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        source = str(filters.get("source") or "").strip()
        source_item_id = str(filters.get("source_item_id") or filters.get("sourceItemId") or "").strip()
        kind = str(filters.get("kind") or "").strip()
        status = str(filters.get("status") or "").strip()
        query = str(filters.get("q") or filters.get("query") or "").strip().lower()
        with self._lock:
            items = self._items()
            if source:
                items = [item for item in items if item.source == source]
            if source_item_id:
                items = [item for item in items if item.source_item_id == source_item_id]
            if kind:
                items = [item for item in items if item.kind == kind]
            if status:
                items = [item for item in items if _item_audio_state(item) == status]
            if query:
                items = [
                    item
                    for item in items
                    if query in item.title.lower() or query in item.snippet.lower()
                ]
            return [self._item_payload(item) for item in items]

    def library_item_payload(
        self,
        *,
        item_id: str | None = None,
        source: str | None = None,
        source_item_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            item = self._find_library_item_locked(
                item_id=item_id,
                source=source,
                source_item_id=source_item_id,
            )
            if item is None:
                raise KeyError("Library item not found.")
            return self._item_payload(item)

    def prepare_library_audio(self, item_id: str, *, retry: bool = False) -> dict[str, Any]:
        start_job = False
        with self._lock:
            item = self._find_item(item_id)
            if item is None:
                raise KeyError("Library item not found.")
            if _item_audio_state(item) == "ready" and Path(item.audio_path).is_file() and not retry:
                return self._item_payload(item)
            if _item_audio_state(item) == "failed" and not retry:
                return self._item_payload(item)
            if item.id in self._audio_jobs and not retry:
                return self._item_payload(item)
            if retry:
                self._clear_item_audio_locked(item, remove_file=True)
            now = time.time()
            item.audio_state = "queued"
            item.audio_error = ""
            item.audio_updated_at = now
            item.updated_at = now
            self._upsert_item(item)
            if item.id not in self._audio_jobs:
                self._audio_jobs.add(item.id)
                start_job = True
            payload = self._item_payload(item)

        if start_job:
            thread = threading.Thread(
                target=self._prepare_audio_job,
                args=(item_id,),
                name="doc-reader-library-audio",
                daemon=True,
            )
            thread.start()
        return payload

    def library_audio(self, item_id: str) -> tuple[bytes, str]:
        with self._lock:
            item = self._find_item(item_id)
            if item is None:
                raise KeyError("Library item not found.")
            if _item_audio_state(item) != "ready":
                raise FileNotFoundError("Library audio is not ready.")
            path = Path(item.audio_path).expanduser()
            audio_dir = self.audio_dir.resolve()
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise FileNotFoundError("Library audio is not readable.") from exc
            if audio_dir not in resolved.parents:
                raise PermissionError("Library audio is outside the prepared-audio folder.")
            if not resolved.is_file():
                raise FileNotFoundError("Library audio file not found.")
            content_type = item.audio_content_type or "audio/wav"
        return resolved.read_bytes(), content_type

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._metrics_snapshot(self._items())

    def analysis_status(
        self,
        *,
        items: list[HistoryItem] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if items is None:
            with self._lock:
                items = self._items()
        if metrics is None:
            metrics = self._metrics_snapshot(items)
        analysis = self._load_analysis()
        analyzed = analysis.get("items", {}) if isinstance(analysis.get("items"), dict) else {}
        pending = 0
        for item in items:
            text = _analysis_text_for_item(item)
            text_hash = item.text_hash or _text_hash(text)
            entry = analyzed.get(item.id)
            if not isinstance(entry, dict) or entry.get("text_hash") != text_hash:
                pending += 1
        latest_batch = _latest_batch(analysis)
        backend, model, url = _analysis_backend_config()
        return {
            "ok": True,
            "enabled": _env_flag("DOC_READER_ANALYSIS_ENABLED", True),
            "running": self._analysis_running,
            "path": str(self.analysis_path),
            "batch_dir": str(self.analysis_batch_dir),
            "backend": analysis.get("backend") or backend,
            "model": analysis.get("model") or model,
            "url": analysis.get("url") or url,
            "updated_at": analysis.get("updated_at") or None,
            "items_analyzed": len(analyzed),
            "pending_items": pending,
            "latest_batch": latest_batch,
            "latest_summary": str(latest_batch.get("summary") or analysis.get("summary") or ""),
            "last_error": self._analysis_last_error,
            "metrics": metrics,
            "style_map": analysis.get("style_map") if isinstance(analysis.get("style_map"), dict) else {},
        }

    def queue_library_analysis(self, *, reason: str = "manual") -> dict[str, Any]:
        with self._analysis_lock:
            if self._analysis_running:
                return self.analysis_status()
            self._analysis_running = True

        thread = threading.Thread(
            target=self._analysis_job,
            kwargs={"reason": reason},
            name="doc-reader-library-analysis-manual",
            daemon=True,
        )
        thread.start()
        return self.analysis_status()

    def run_library_analysis_once(self, *, reason: str = "manual") -> dict[str, Any]:
        with self._analysis_lock:
            if self._analysis_running:
                return self.analysis_status()
            self._analysis_running = True
        try:
            self._run_library_analysis_batch(reason=reason)
        finally:
            with self._analysis_lock:
                self._analysis_running = False
        return self.analysis_status()

    def play(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._find_item(item_id)
            if item is None:
                raise KeyError("History item not found.")
            if not Path(item.source_path).is_file():
                raise FileNotFoundError("Source file not found.")

            if self._process is not None and self._process.poll() is None:
                self._pause_locked()

            if item.completed:
                item.last_seconds = 0.0
                item.last_chunk_start_seconds = 0.0
                item.resume_chunk_index = 0
                item.completed = False
                self._upsert_item(item)

            resume_chunk = max(0, int(item.resume_chunk_index))
            saved_seconds = max(0.0, float(item.last_seconds))
            chunk_start_seconds = min(
                saved_seconds,
                max(0.0, float(item.last_chunk_start_seconds)),
            )
            start_seconds = max(0.0, saved_seconds - chunk_start_seconds)
            display_seconds = saved_seconds

            backend = self._speech_backend()
            read_rate = self._read_rate()
            self._write_rate_control(read_rate)
            args = [
                sys.executable,
                "-m",
                "doc_reader",
                item.source_path,
                "--mode",
                os.getenv("DOC_READER_WEB_MODE", "full"),
                "--style",
                os.getenv("DOC_READER_WEB_STYLE", "balanced"),
                "--rate",
                str(read_rate),
                "--rate-control-file",
                str(self.rate_control_path),
                "--speech-backend",
                backend,
                "--start-chunk-index",
                str(resume_chunk),
                "--start-seconds",
                f"{start_seconds:.2f}",
                "--verbose",
            ]

            env = os.environ.copy()
            package_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = package_root + (
                f"{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
            )
            if backend == "openai":
                self._extend_openai_args(args)
                api_key = resolve_openai_api_key()
                if api_key:
                    env["OPENAI_API_KEY"] = api_key
            else:
                env["DOC_READER_AUTO_ALLOW_OPENAI"] = "0"
                env.pop("OPENAI_API_KEY", None)
                env.pop("DOC_READER_OPENAI_API_KEY", None)

            process = subprocess.Popen(
                args,
                cwd=str(self.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **popen_process_group_kwargs(),
            )

            self._process = process
            self._active_id = item.id
            self._paused_id = None
            self._active_chunk_index = None
            self._last_completed_chunk_index = resume_chunk - 1
            self._resume_chunk_index = resume_chunk
            self._last_reader_error = None
            self._start_time = time.monotonic()
            self._start_offset_seconds = display_seconds
            self._active_chunk_start_seconds = chunk_start_seconds
            self._resume_start_chunk_offset_seconds = start_seconds
            self._last_position_seconds = display_seconds
            self._status = f"Reading {item.title}"
            item.last_seconds = display_seconds
            item.last_chunk_start_seconds = chunk_start_seconds
            item.resume_chunk_index = resume_chunk
            item.completed = False
            self._upsert_item(item)

            thread = threading.Thread(
                target=self._watch_process,
                args=(process, item.id),
                name="doc-reader-web-playback",
                daemon=True,
            )
            thread.start()
            return self.state()

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        backend = payload.get("speech_backend")
        if backend is not None:
            backend = str(backend).strip()
            if backend not in SPEECH_BACKENDS:
                raise ValueError("Unknown speech backend.")
            settings["speech_backend"] = backend
            self._status = f"Voice: {SPEECH_BACKENDS[backend]}"
        if "stt_enabled" in payload:
            settings["stt_enabled"] = bool(payload.get("stt_enabled"))
            self._status = (
                "Dictation hotkey enabled."
                if settings["stt_enabled"]
                else "Dictation hotkey disabled."
            )
        rate_value = payload.get("read_rate", payload.get("readRate"))
        if rate_value is not None:
            read_rate = _normalize_read_rate(rate_value)
            settings["read_rate"] = read_rate
            self._write_rate_control(read_rate)
            self._status = f"Read speed: {read_rate} WPM."
        if "microphone_id" in payload:
            microphone_id = str(payload.get("microphone_id") or "").strip()
            devices = _sanitized_microphone_devices(settings.get("microphones"))
            if not microphone_id:
                preferred_device = _preferred_microphone_device(devices)
                if preferred_device:
                    microphone_id = preferred_device["id"]
                    self._status = f"Microphone pinned to {preferred_device['name']}."
                else:
                    self._status = "Microphone setting updated."
            else:
                self._status = "Microphone setting updated."
            settings["microphone_id"] = microphone_id
        self._save_settings(settings)
        return self.state()

    def start_native_helper(self) -> dict[str, Any]:
        if IS_WINDOWS:
            from .windows_app import start_helper

            self._clear_native_helper_runtime_status("native helper starting from web app")
            message = start_helper()
            with self._lock:
                self._status = message
                status = self._status
            return {"ok": True, "status": status}
        if sys.platform != "darwin":
            raise RuntimeError("The native helper is only available on macOS.")
        uid = os.getuid()
        domain = f"gui/{uid}"
        target = f"{domain}/{NATIVE_HELPER_LABEL}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{NATIVE_HELPER_LABEL}.plist"
        if not plist.exists():
            raise FileNotFoundError("Doc Reader LaunchAgent is not installed.")

        self._clear_native_helper_runtime_status("native helper starting from web app")
        if not _launch_agent_loaded(target):
            subprocess.run(
                ["/bin/launchctl", "bootstrap", domain, str(plist)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        subprocess.run(
            ["/bin/launchctl", "enable", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        kickstart_code, kickstart_error = _kickstart_launch_agent(target)
        if kickstart_code != 0:
            message = kickstart_error or "Could not start Doc Reader app helper."
            raise RuntimeError(message)
        with self._lock:
            self._status = "Doc Reader app helper start requested."
            status = self._status
        return {"ok": True, "status": status}

    def reset_native_helper(self) -> dict[str, Any]:
        if IS_WINDOWS:
            from .windows_app import restart_helper

            self._clear_native_helper_runtime_status("native helper reset requested")
            message = restart_helper()
            with self._lock:
                self._status = message
                status = self._status
            return {"ok": True, "status": status}
        if sys.platform != "darwin":
            raise RuntimeError("The native helper is only available on macOS.")
        uid = os.getuid()
        domain = f"gui/{uid}"
        target = f"{domain}/{NATIVE_HELPER_LABEL}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{NATIVE_HELPER_LABEL}.plist"
        if not plist.exists():
            raise FileNotFoundError("Doc Reader LaunchAgent is not installed.")

        self._clear_native_helper_runtime_status("native helper reset requested")
        if _launch_agent_loaded(target):
            result = subprocess.run(
                ["/bin/launchctl", "bootout", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0 and _launch_agent_loaded(target):
                message = (result.stderr or "Could not reset Doc Reader app helper.").strip()
                raise RuntimeError(message)
        _terminate_native_helper_processes()
        subprocess.run(
            ["/bin/launchctl", "bootstrap", domain, str(plist)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["/bin/launchctl", "enable", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        kickstart_code, kickstart_error = _kickstart_launch_agent(target)
        if kickstart_code != 0:
            message = kickstart_error or "Could not restart Doc Reader app helper."
            raise RuntimeError(message)
        with self._lock:
            self._status = "Doc Reader app helper reset requested."
            status = self._status
        return {"ok": True, "status": status}

    def stop_native_helper(self) -> dict[str, Any]:
        if IS_WINDOWS:
            from .windows_app import stop_helper

            message = stop_helper()
            self._clear_native_helper_runtime_status("native helper stopped from web app")
            with self._lock:
                self._status = message
                status = self._status
            return {"ok": True, "status": status}
        if sys.platform != "darwin":
            raise RuntimeError("The native helper is only available on macOS.")
        uid = os.getuid()
        target = f"gui/{uid}/{NATIVE_HELPER_LABEL}"
        if _launch_agent_loaded(target):
            result = subprocess.run(
                ["/bin/launchctl", "bootout", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0 and _launch_agent_loaded(target):
                message = (result.stderr or "Could not stop Doc Reader app helper.").strip()
                raise RuntimeError(message)
        _terminate_native_helper_processes()
        self._clear_native_helper_runtime_status("native helper stopped from web app")
        with self._lock:
            self._status = "Doc Reader app helper stopped."
            status = self._status
        return {"ok": True, "status": status}

    def _clear_native_helper_runtime_status(self, event: str) -> None:
        settings = self._settings()
        settings["native_dictation_status_at"] = 0
        settings["active_microphone_id"] = ""
        settings["recording"] = False
        settings["recording_start_pending"] = False
        settings["audio_level"] = 0
        settings["audio_peak_level"] = 0
        settings["last_dictation_event"] = event
        self._save_settings(settings)

    def update_native_dictation_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings()
        devices = payload.get("devices")
        if isinstance(devices, list):
            sanitized = []
            for device in devices:
                if not isinstance(device, dict):
                    continue
                device_id = str(device.get("id") or "").strip()
                name = str(device.get("name") or "").strip()
                if device_id and name:
                    sanitized.append({"id": device_id, "name": name})
            settings["microphones"] = sanitized
            _pin_preferred_microphone(settings, sanitized)
        for key in [
            "microphone_authorization",
            "input_monitoring_trusted",
            "accessibility_trusted",
            "active_microphone_id",
            "recording",
            "recording_start_pending",
            "last_dictation_event",
            "audio_level",
            "audio_peak_level",
            "last_recording_path",
            "last_recording_bytes",
            "last_recording_seconds",
            "last_recording_content_type",
            "last_recording_peak_level",
            "last_recording_created_at",
        ]:
            if key in payload:
                settings[key] = payload.get(key)
        settings["native_dictation_status_at"] = time.time()
        self._save_settings(settings)
        if str(payload.get("last_dictation_event") or "") == "native helper started":
            with self._lock:
                if self._status == "Doc Reader app helper start requested.":
                    self._status = "Doc Reader app helper started."
        return {"ok": True, "stt": self.stt_status()}

    def tts_status(self) -> dict[str, Any]:
        backend = self._speech_backend()
        return {
            "backend": backend,
            "label": SPEECH_BACKENDS.get(backend, backend),
            "options": [
                {"value": value, "label": label}
                for value, label in SPEECH_BACKENDS.items()
            ],
            "services": {
                "umbra": _service_health(_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL)),
                "mac": _service_health(_env("DOC_READER_TTS_MAC_URL", DEFAULT_TTS_MAC_URL)),
            },
        }

    def stt_status(self) -> dict[str, Any]:
        stt_backend, service = _stt_service_health()
        whisper = _service_whisper_status(service)
        settings = self._settings()
        return {
            "enabled": self._stt_enabled(),
            "hotkey": dictation_hotkey_label(),
            "backend": stt_backend,
            "label": _stt_service_label(stt_backend),
            "service": service,
            "microphone": _microphone_payload(settings),
            "ready": bool(service.get("ok")) and bool(whisper.get("enabled")),
            "loaded": bool(whisper.get("loaded")),
            "error": whisper.get("error", "") if isinstance(whisper, dict) else "",
        }

    def transcribe_audio_file(
        self,
        filename: str,
        audio: bytes,
        *,
        content_type: str = "",
        language: str | None = None,
        timestamped: bool = False,
    ) -> dict[str, Any]:
        safe_name = _safe_filename(filename or "audio.wav")
        suffix = Path(safe_name).suffix.lower()
        normalized_content_type = _audio_upload_content_type(safe_name, content_type)
        if suffix not in SUPPORTED_AUDIO_SUFFIXES and not _is_audio_content_type(normalized_content_type):
            raise ValueError("Unsupported audio file type.")
        if not audio:
            raise ValueError("Uploaded audio file was empty.")

        audio_hash = hashlib.sha256(audio).hexdigest()
        timestamp_mode = "phrase" if timestamped else "plain"
        return self.transcribe_audio(
            audio,
            content_type=normalized_content_type,
            language=language,
            source="audio-upload",
            source_item_id=f"audio-upload:{audio_hash}:{timestamp_mode}",
            source_meta={
                "filename": safe_name,
                "contentType": normalized_content_type,
                "bytes": len(audio),
                "timestamped": bool(timestamped),
                "timestampMode": timestamp_mode,
            },
            title=f"Audio: {safe_name}",
            label="Audio",
            status_label="Audio",
            timestamped=timestamped,
        )

    def transcribe_audio(
        self,
        audio: bytes,
        *,
        content_type: str = "audio/wav",
        elapsed_seconds: float | None = None,
        language: str | None = None,
        source: str | None = None,
        source_item_id: str | None = None,
        source_meta: dict[str, Any] | None = None,
        title: str | None = None,
        label: str = "Dictation",
        status_label: str = "Dictation",
        timestamped: bool = False,
    ) -> dict[str, Any]:
        if not self._stt_enabled():
            raise PermissionError("Speech-to-text is disabled.")
        if not audio:
            raise ValueError("No audio to transcribe.")

        started = time.perf_counter()
        source_bytes = len(audio)
        normalize_started = time.perf_counter()
        normalized_audio, normalized_content_type, normalization = _normalize_stt_audio(
            audio,
            content_type=content_type,
            elapsed_seconds=elapsed_seconds,
        )
        normalize_seconds = time.perf_counter() - normalize_started
        if isinstance(normalization, dict):
            normalization["seconds"] = round(normalize_seconds, 3)
            normalization["source_bytes"] = source_bytes
            normalization["normalized_bytes"] = len(normalized_audio)
        transcribe_started = time.perf_counter()
        stt_backend, stt_service = _require_stt_service()
        result = _transcribe_on_stt_service(
            normalized_audio,
            content_type=normalized_content_type,
            base_url=str(stt_service.get("url") or _stt_default_url()),
            service_label=_stt_service_label(stt_backend),
            language=language,
            word_timestamps=timestamped,
        )
        transcribe_seconds = time.perf_counter() - transcribe_started
        result["normalization"] = normalization
        plain_text = str(result.get("text") or "").strip()
        text = (
            _format_timestamped_transcript(result.get("segments"), fallback_text=plain_text)
            if timestamped
            else plain_text
        )
        item_payload = None
        if text:
            if source:
                payload = {
                    "text": text,
                    "label": label or "Dictation",
                    "kind": "dictation",
                    "source": source,
                    "source_item_id": source_item_id or f"dictation:{uuid.uuid4()}",
                    "source_meta": source_meta or {},
                }
                if title:
                    payload["title"] = title
                item, _prepare_audio = self.upsert_library_item(payload)
            else:
                item = self.add_text(text, label=label or "Dictation", kind="dictation")
            if timestamped and plain_text:
                item.word_count = _word_count(plain_text)
                item.updated_at = time.time()
                with self._lock:
                    self._upsert_item(item)
            item_payload = self._item_payload(item)
        total_seconds = time.perf_counter() - started
        print(
            "[doc-reader-stt] "
            f"source_bytes={source_bytes} normalized_bytes={len(normalized_audio)} "
            f"elapsed={elapsed_seconds or 0:.2f}s normalize={normalize_seconds:.2f}s "
            f"backend={stt_backend} transcribe={transcribe_seconds:.2f}s "
            f"total={total_seconds:.2f}s chars={len(text)}",
            file=sys.stderr,
            flush=True,
        )
        with self._lock:
            self._status = (
                f"{status_label or 'Dictation'} transcribed in {total_seconds:.1f}s."
                if text
                else f"{status_label or 'Dictation'} produced no text."
            )
        return {
            "ok": True,
            "text": text,
            "plain_text": plain_text,
            "item": item_payload,
            "transcription": result,
            "state": self.state(),
        }

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._status = "Nothing is currently playing."
                return self.state()
            self._pause_locked()
            return self.state()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_locked(save_progress=True)
            self._active_id = None
            self._paused_id = None
            self._status = "Stopped."
            return self.state()

    def shutdown(self) -> None:
        self._analysis_stop.set()
        with self._lock:
            self._stop_locked(save_progress=True)
            self._active_id = None
            self._paused_id = None

    def drain_service_inbox(self) -> int:
        files = sorted(
            self.service_inbox_dir.glob("*.txt"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
        )
        latest_item: HistoryItem | None = None
        drained = 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
                item = self.add_text(text, label="Highlighted Text")
                path.unlink(missing_ok=True)
            except OSError:
                continue
            except ValueError:
                path.unlink(missing_ok=True)
                continue
            latest_item = item
            drained += 1
        if latest_item is not None:
            self.play(latest_item.id)
        return drained

    def _pause_locked(self) -> None:
        if self._process is None:
            return
        item_id = self._active_id
        self._save_active_progress_locked()
        self._stop_locked(save_progress=False)
        self._active_id = None
        self._paused_id = item_id
        title = self._find_item(item_id).title if item_id and self._find_item(item_id) else "reading"
        self._status = f"Paused {title}"

    def _stop_locked(self, *, save_progress: bool) -> None:
        process = self._process
        if process is None:
            return
        if save_progress:
            self._save_active_progress_locked()
        self._suppress_exit_status_for_pid = process.pid
        _terminate_process_group(process)
        self._process = None
        self._start_time = None

    def _watch_process(self, process: subprocess.Popen[str], item_id: str) -> None:
        code: int | None = None
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if line:
                        self._handle_reader_line(line)
            code = process.wait()
        finally:
            with self._lock:
                if self._process is process:
                    completed = process.returncode == 0 or code == 0
                    item = self._find_item(item_id)
                    if item is not None:
                        self._save_active_progress_locked(item=item, completed=completed)
                    self._process = None
                    self._active_id = None
                    self._paused_id = None
                    self._start_time = None
                    if self._suppress_exit_status_for_pid == process.pid:
                        self._suppress_exit_status_for_pid = None
                    else:
                        self._status = "Ready." if completed else (self._last_reader_error or "Reader stopped.")

    def _handle_reader_line(self, line: str) -> None:
        with self._lock:
            match = CHUNK_START_RE.match(line)
            if match:
                index = int(match.group(1))
                self._active_chunk_index = index
                self._resume_chunk_index = max(0, index)
                offset = (
                    self._resume_start_chunk_offset_seconds
                    if index == self._resume_chunk_index
                    else 0.0
                )
                self._active_chunk_start_seconds = max(
                    0.0,
                    self._current_position_seconds_locked() - offset,
                )
                self._resume_start_chunk_offset_seconds = 0.0
                return

            match = CHUNK_DONE_RE.match(line)
            if match:
                index = int(match.group(1))
                self._last_completed_chunk_index = max(self._last_completed_chunk_index, index)
                if self._active_chunk_index == index:
                    self._active_chunk_index = None
                self._resume_chunk_index = max(self._resume_chunk_index, index + 1)
                self._active_chunk_start_seconds = self._current_position_seconds_locked()
                self._save_active_progress_locked()
                return

            if _is_reader_error_line(line):
                self._last_reader_error = line
                self._status = line
                return

            if not line.startswith("[doc-reader] page number="):
                self._status = line

    def _save_active_progress_locked(
        self,
        *,
        item: HistoryItem | None = None,
        completed: bool | None = None,
    ) -> None:
        target = item
        if target is None:
            target = self._find_item(self._active_id or self._paused_id)
        if target is None:
            return
        current_position = self._current_position_seconds_locked()
        target.last_seconds = current_position
        target.last_chunk_start_seconds = min(
            current_position,
            max(0.0, self._active_chunk_start_seconds),
        )
        target.resume_chunk_index = max(
            0,
            self._active_chunk_index
            if self._active_chunk_index is not None
            else max(self._resume_chunk_index, self._last_completed_chunk_index + 1),
        )
        if completed is not None:
            target.completed = completed
        target.updated_at = time.time()
        self._upsert_item(target)

    def _current_position_seconds_locked(self) -> float:
        if self._process is not None and self._process.poll() is None and self._start_time is not None:
            self._last_position_seconds = self._start_offset_seconds + max(
                0.0,
                time.monotonic() - self._start_time,
            )
        return max(0.0, self._last_position_seconds)

    def _extend_openai_args(self, args: list[str]) -> None:
        model = os.getenv("DOC_READER_OPENAI_MODEL", OPENAI_TTS_MODEL)
        voice = os.getenv("DOC_READER_OPENAI_VOICE", OPENAI_TTS_VOICE)
        response_format = os.getenv("DOC_READER_OPENAI_RESPONSE_FORMAT", OPENAI_TTS_RESPONSE_FORMAT)
        instructions = os.getenv("DOC_READER_OPENAI_INSTRUCTIONS", OPENAI_TTS_INSTRUCTIONS)
        args.extend(["--openai-model", model])
        args.extend(["--openai-voice", voice])
        args.extend(["--openai-response-format", response_format])
        if instructions:
            args.extend(["--openai-instructions", instructions])

    def _items(self) -> list[HistoryItem]:
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        items: list[HistoryItem] = []
        item_fields = {entry.name for entry in fields(HistoryItem)}
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            entry = _normalize_history_entry_payload(entry, item_fields)
            try:
                items.append(HistoryItem(**entry))
            except TypeError:
                continue
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    def _save_items(self, items: list[HistoryItem]) -> None:
        deduped = sorted(items, key=lambda item: item.updated_at, reverse=True)
        for item in deduped:
            _refresh_item_metrics(item)
        temp_path = self.history_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps([asdict(item) for item in deduped], indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.history_path)

    def _find_item(self, item_id: str | None) -> HistoryItem | None:
        if not item_id:
            return None
        for item in self._items():
            if item.id == item_id:
                return item
        return None

    def _find_library_item_locked(
        self,
        *,
        item_id: str | None = None,
        source: str | None = None,
        source_item_id: str | None = None,
    ) -> HistoryItem | None:
        normalized_item_id = str(item_id or "").strip()
        normalized_source = _safe_source(source)
        normalized_source_item_id = _safe_source_item_id(source_item_id)
        for item in self._items():
            if normalized_item_id and item.id == normalized_item_id:
                return item
            if (
                normalized_source
                and normalized_source_item_id
                and item.source == normalized_source
                and item.source_item_id == normalized_source_item_id
            ):
                return item
        return None

    def _upsert_item(self, item: HistoryItem) -> None:
        items = [existing for existing in self._items() if existing.id != item.id]
        items.append(item)
        self._save_items(items)

    def _clear_item_audio_locked(self, item: HistoryItem, *, remove_file: bool = False) -> None:
        if remove_file and item.audio_path:
            try:
                Path(item.audio_path).unlink(missing_ok=True)
            except OSError:
                pass
        item.audio_state = ""
        item.audio_path = ""
        item.audio_content_type = ""
        item.audio_bytes = 0
        item.audio_error = ""
        item.audio_created_at = 0.0
        item.audio_updated_at = 0.0

    def _prepare_audio_job(self, item_id: str) -> None:
        try:
            with self._lock:
                item = self._find_item(item_id)
                if item is None:
                    return
                item.audio_state = "processing"
                item.audio_error = ""
                item.audio_updated_at = time.time()
                item.updated_at = item.audio_updated_at
                self._upsert_item(item)
                text = _read_text_file(Path(item.source_path))
                read_rate = self._read_rate()

            audio = _synthesize_library_audio(text, rate=read_rate)
            audio_path = self.audio_dir / f"{item_id}.wav"
            temp_path = audio_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temp_path.write_bytes(audio)
            temp_path.replace(audio_path)

            with self._lock:
                item = self._find_item(item_id)
                if item is not None:
                    now = time.time()
                    item.audio_state = "ready"
                    item.audio_path = str(audio_path)
                    item.audio_content_type = "audio/wav"
                    item.audio_bytes = len(audio)
                    item.audio_error = ""
                    item.audio_created_at = item.audio_created_at or now
                    item.audio_updated_at = now
                    item.updated_at = now
                    self._upsert_item(item)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                item = self._find_item(item_id)
                if item is not None:
                    now = time.time()
                    item.audio_state = "failed"
                    item.audio_error = str(exc)
                    item.audio_updated_at = now
                    item.updated_at = now
                    self._upsert_item(item)
        finally:
            with self._lock:
                self._audio_jobs.discard(item_id)

    def _item_payload(self, item: HistoryItem) -> dict[str, Any]:
        payload = asdict(item)
        payload["playing"] = self._active_id == item.id and self._process is not None
        payload["paused"] = self._paused_id == item.id and self._process is None
        payload["sourceMeta"] = item.source_meta
        payload["sourceItemId"] = item.source_item_id
        words = _item_word_count(item)
        channel = _metrics_channel_for_item(item)
        payload["word_count"] = words
        payload["wordCount"] = words
        payload["metrics_channel"] = channel
        payload["metricsChannel"] = channel
        payload["metrics"] = {
            "channel": channel,
            "words": words,
        }
        payload["audio"] = {
            "state": _item_audio_state(item),
            "url": f"/api/library/items/{item.id}/audio" if _item_audio_state(item) == "ready" else "",
            "content_type": item.audio_content_type or "",
            "contentType": item.audio_content_type or "",
            "bytes": max(0, int(item.audio_bytes or 0)),
            "error": item.audio_error or "",
            "created_at": item.audio_created_at or None,
            "updated_at": item.audio_updated_at or None,
        }
        if _is_dictation_item(item):
            payload["text"] = _read_text_file(Path(item.source_path))
        return payload

    def item_text(self, item_id: str) -> dict[str, Any]:
        item = self._find_item(item_id)
        if item is None:
            raise KeyError("History item not found.")
        path = Path(item.source_path)
        if not path.is_file():
            raise FileNotFoundError("Source file not found.")
        if path.suffix.lower() not in {".txt", ".md", ".markdown"} and item.kind != "dictation":
            raise ValueError("History item text is not copyable.")
        return {
            "ok": True,
            "id": item.id,
            "kind": item.kind,
            "text": path.read_text(encoding="utf-8").strip(),
        }

    def update_item_text(self, item_id: str, text: str) -> dict[str, Any]:
        cleaned = str(text or "").strip()
        if not cleaned:
            raise ValueError("No text to save.")
        with self._lock:
            item = self._find_item(item_id)
            if item is None:
                raise KeyError("History item not found.")
            path = Path(item.source_path)
            if path.suffix.lower() not in {".txt", ".md", ".markdown"} and item.kind != "dictation":
                raise ValueError("History item text is not editable.")
            if item.kind == "document" and path.suffix.lower() not in {".txt", ".md", ".markdown"}:
                raise ValueError("Document type is not editable here.")
            if not path.is_file():
                if item.kind == "document":
                    raise FileNotFoundError("Source file not found.")
                path = self.text_dir / f"{item.id}.txt"
                item.source_path = str(path)

            path.write_text(f"{cleaned}\n", encoding="utf-8")
            self._clear_item_audio_locked(item, remove_file=True)
            metrics_text = _metrics_text_for_item_text(item, cleaned)
            now = time.time()
            item.snippet = _snippet(cleaned)
            item.text_hash = _text_hash(cleaned)
            item.word_count = _word_count(metrics_text)
            item.metrics_channel = _metrics_channel_for_item(item)
            item.updated_at = now
            self._status = "Library card saved."
            self._upsert_item(item)
            payload = self._item_payload(item)
        return {"ok": True, "item": payload, "state": self.state()}

    def last_recording_audio(self) -> tuple[bytes, str]:
        settings = self._settings()
        raw_path = str(settings.get("last_recording_path") or "")
        if not raw_path:
            raise FileNotFoundError("No saved dictation recording yet.")
        path = Path(raw_path).expanduser()
        recordings_dir = self.recordings_dir.resolve()
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise FileNotFoundError("Saved recording is not readable.") from exc
        if recordings_dir not in resolved.parents:
            raise PermissionError("Saved recording is outside the dictation recordings folder.")
        if not resolved.is_file():
            raise FileNotFoundError("Saved recording file not found.")
        content_type = str(settings.get("last_recording_content_type") or "audio/mp4")
        return resolved.read_bytes(), content_type

    def _speech_backend(self) -> str:
        configured = self._settings().get("speech_backend")
        if isinstance(configured, str) and configured in SPEECH_BACKENDS:
            return configured
        env_backend = os.getenv("DOC_READER_WEB_SPEECH_BACKEND", DEFAULT_WEB_SPEECH_BACKEND)
        return env_backend if env_backend in SPEECH_BACKENDS else DEFAULT_WEB_SPEECH_BACKEND

    def _stt_enabled(self) -> bool:
        configured = self._settings().get("stt_enabled")
        if isinstance(configured, bool):
            return configured
        env_value = os.getenv("DOC_READER_STT_ENABLED", "").strip().lower()
        if env_value in {"1", "true", "yes", "on"}:
            return True
        if env_value in {"0", "false", "no", "off"}:
            return False
        return DEFAULT_STT_ENABLED

    def _read_rate(self) -> int:
        configured = self._settings().get("read_rate")
        if configured is not None:
            return _normalize_read_rate(configured)
        return _normalize_read_rate(os.getenv("DOC_READER_WEB_RATE", DEFAULT_RATE))

    def _settings_payload(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = settings or self._settings()
        read_rate = (
            _normalize_read_rate(settings.get("read_rate"))
            if settings.get("read_rate") is not None
            else _normalize_read_rate(os.getenv("DOC_READER_WEB_RATE", DEFAULT_RATE))
        )
        return {
            "read_rate": read_rate,
            "readRate": read_rate,
            "read_speed": _speed_for_rate(read_rate),
            "readSpeed": _speed_for_rate(read_rate),
            "min_read_rate": MIN_READ_RATE,
            "max_read_rate": MAX_READ_RATE,
            "read_rate_step": READ_RATE_STEP,
        }

    def _settings(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_settings(self, settings: dict[str, Any]) -> None:
        temp_path = self.settings_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        temp_path.replace(self.settings_path)

    def _write_rate_control(self, rate: int) -> None:
        read_rate = _normalize_read_rate(rate)
        payload = {
            "read_rate": read_rate,
            "readRate": read_rate,
            "read_speed": _speed_for_rate(read_rate),
            "readSpeed": _speed_for_rate(read_rate),
            "updated_at": time.time(),
        }
        temp_path = self.rate_control_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self.rate_control_path)

    def _metrics_snapshot(self, items: list[HistoryItem]) -> dict[str, Any]:
        by_channel: dict[str, dict[str, int]] = {
            "stt": {"items": 0, "words": 0, "completed_words": 0, "open_words": 0},
            "tts": {"items": 0, "words": 0, "completed_words": 0, "open_words": 0},
        }
        by_kind: dict[str, dict[str, int]] = {}
        by_source: dict[str, dict[str, int]] = {}
        prepared_audio_words = 0
        recording_bytes = max(0, int(_clamped_float(self._settings().get("last_recording_bytes"), 0.0, 10_000_000_000.0)))
        for item in items:
            words = _item_word_count(item)
            channel = _metrics_channel_for_item(item)
            channel_bucket = by_channel.setdefault(
                channel,
                {"items": 0, "words": 0, "completed_words": 0, "open_words": 0},
            )
            channel_bucket["items"] += 1
            channel_bucket["words"] += words
            channel_bucket["completed_words" if item.completed else "open_words"] += words
            kind_bucket = by_kind.setdefault(item.kind or "text", {"items": 0, "words": 0})
            kind_bucket["items"] += 1
            kind_bucket["words"] += words
            source = item.source or ("dictation" if channel == "stt" else "doc-reader")
            source_bucket = by_source.setdefault(source, {"items": 0, "words": 0})
            source_bucket["items"] += 1
            source_bucket["words"] += words
            if _item_audio_state(item) == "ready":
                prepared_audio_words += words
        total_words = sum(bucket["words"] for bucket in by_channel.values())
        return {
            "schema": "doc-reader.library-metrics/1",
            "generated_at": time.time(),
            "items": len(items),
            "words": total_words,
            "stt_words": by_channel.get("stt", {}).get("words", 0),
            "tts_words": by_channel.get("tts", {}).get("words", 0),
            "stt_items": by_channel.get("stt", {}).get("items", 0),
            "tts_items": by_channel.get("tts", {}).get("items", 0),
            "completed_tts_words": by_channel.get("tts", {}).get("completed_words", 0),
            "open_tts_words": by_channel.get("tts", {}).get("open_words", 0),
            "prepared_audio_words": prepared_audio_words,
            "last_recording_bytes": recording_bytes,
            "by_channel": by_channel,
            "by_kind": by_kind,
            "by_source": by_source,
        }

    def _load_analysis(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.analysis_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_analysis(self, payload: dict[str, Any]) -> None:
        temp_path = self.analysis_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.analysis_path)

    def _start_analysis_worker(self) -> None:
        if self._analysis_thread is not None:
            return
        self._analysis_thread = threading.Thread(
            target=self._analysis_loop,
            name="doc-reader-library-analysis",
            daemon=True,
        )
        self._analysis_thread.start()

    def _analysis_loop(self) -> None:
        initial_delay = _env_int("DOC_READER_ANALYSIS_INITIAL_DELAY_SECONDS", DEFAULT_ANALYSIS_INITIAL_DELAY_SECONDS)
        if self._analysis_stop.wait(max(0, initial_delay)):
            return
        while not self._analysis_stop.is_set():
            self.run_library_analysis_once(reason="scheduled")
            interval = _env_int("DOC_READER_ANALYSIS_INTERVAL_SECONDS", DEFAULT_ANALYSIS_INTERVAL_SECONDS)
            self._analysis_stop.wait(max(60, interval))

    def _analysis_job(self, *, reason: str) -> None:
        try:
            self._run_library_analysis_batch(reason=reason)
        finally:
            with self._analysis_lock:
                self._analysis_running = False

    def _run_library_analysis_batch(self, *, reason: str) -> dict[str, Any]:
        with self._lock:
            items = self._items()
        metrics = self._metrics_snapshot(items)
        analysis = self._load_analysis()
        item_entries = analysis.get("items", {}) if isinstance(analysis.get("items"), dict) else {}
        batch_size = _env_int("DOC_READER_ANALYSIS_BATCH_SIZE", DEFAULT_ANALYSIS_BATCH_SIZE)
        candidates = _analysis_candidates(items, item_entries, limit=batch_size)
        backend, model, url = _analysis_backend_config()
        now = time.time()

        if not candidates:
            analysis.update(
                {
                    "schema": "doc-reader.library-analysis/1",
                    "updated_at": now,
                    "backend": analysis.get("backend") or backend,
                    "model": analysis.get("model") or model,
                    "url": analysis.get("url") or url,
                    "metrics": metrics,
                    "style_map": _style_map_from_analysis(item_entries, metrics),
                }
            )
            self._save_analysis(analysis)
            self._analysis_last_error = ""
            return self.analysis_status(items=items, metrics=metrics)

        try:
            batch_result = _analyze_batch_with_local_model(candidates, backend=backend, model=model, url=url)
            self._analysis_last_error = ""
        except Exception as exc:  # noqa: BLE001
            batch_result = _heuristic_batch_analysis(candidates)
            batch_result["backend"] = "heuristic"
            batch_result["model"] = "local-rules"
            batch_result["error"] = str(exc)
            self._analysis_last_error = str(exc)

        batch_id = f"{_timestamp_utc()}-{uuid.uuid4().hex[:8]}"
        item_results = _normalized_item_analyses(batch_result, candidates)
        for entry in item_results:
            item_entries[entry["id"]] = entry

        batches = analysis.get("batches")
        if not isinstance(batches, list):
            batches = []
        batch_record = {
            "id": batch_id,
            "reason": reason,
            "created_at": now,
            "backend": batch_result.get("backend") or backend,
            "model": batch_result.get("model") or model,
            "url": url,
            "item_ids": [candidate["id"] for candidate in candidates],
            "summary": str(batch_result.get("summary") or ""),
            "error": str(batch_result.get("error") or ""),
        }
        batches.append(batch_record)

        analysis = {
            "schema": "doc-reader.library-analysis/1",
            "updated_at": now,
            "backend": batch_record["backend"],
            "model": batch_record["model"],
            "url": url,
            "summary": batch_record["summary"],
            "metrics": metrics,
            "items": item_entries,
            "batches": batches,
            "style_map": _style_map_from_analysis(item_entries, metrics),
        }
        self._save_analysis(analysis)
        batch_path = self.analysis_batch_dir / f"{batch_id}.json"
        batch_path.write_text(
            json.dumps(
                {
                    "schema": "doc-reader.library-analysis-batch/1",
                    "batch": batch_record,
                    "items": item_results,
                    "metrics": metrics,
                    "style_map": analysis["style_map"],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return self.analysis_status(items=items, metrics=metrics)


def _is_reader_error_line(line: str) -> bool:
    lowered = line.lower()
    return (
        line.startswith("RuntimeError:")
        or line.startswith("Traceback ")
        or "openai speech request failed" in lowered
        or "insufficient_quota" in lowered
        or "http tts" in lowered
        or "audio playback failed" in lowered
    )


class DocReaderHandler(BaseHTTPRequestHandler):
    server_version = "DocReaderWeb/1.0"

    @property
    def reader(self) -> ReaderService:
        return self.server.reader  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route_path = parsed.path
        if route_path == "/":
            self._send_html(INDEX_HTML)
            return
        asset = _web_metadata_asset(route_path)
        if asset is not None:
            data, content_type = asset
            self._send_binary(data, content_type=content_type)
            return
        if route_path == "/healthz":
            self._send_json(self.reader.health())
            return
        if route_path == "/api/native/status":
            self._send_json(self.reader.native_status())
            return
        if route_path == "/api/state":
            self._send_json(self.reader.state())
            return
        if route_path == "/api/metrics":
            self._send_json({"ok": True, "metrics": self.reader.metrics_snapshot()})
            return
        if route_path == "/api/library/analysis":
            self._send_json({"ok": True, "analysis": self.reader.analysis_status()})
            return
        if route_path == "/api/library/items":
            filters = {
                key: values[-1]
                for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
                if values
            }
            self._send_json({"ok": True, "items": self.reader.library_items(filters)})
            return
        library_prefix = "/api/library/items/"
        if route_path.startswith(library_prefix) and route_path.endswith("/audio"):
            try:
                item_id = unquote(route_path[len(library_prefix) : -len("/audio")])
                data, content_type = self.reader.library_audio(item_id)
                self._send_binary(data, content_type=content_type)
            except (PermissionError, FileNotFoundError, KeyError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if route_path.startswith(library_prefix):
            try:
                item_id = unquote(route_path[len(library_prefix) :])
                self._send_json({"ok": True, "item": self.reader.library_item_payload(item_id=item_id)})
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        if route_path == "/api/dictation/last-recording":
            try:
                data, content_type = self.reader.last_recording_audio()
                self._send_binary(data, content_type=content_type)
            except (PermissionError, FileNotFoundError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        text_prefix = "/api/items/"
        if route_path.startswith(text_prefix) and route_path.endswith("/text"):
            try:
                item_id = unquote(route_path[len(text_prefix) : -len("/text")])
                self._send_json(self.reader.item_text(item_id))
            except (ValueError, KeyError, FileNotFoundError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route_path = parsed.path
        if route_path == "/":
            self._send_headers_only(
                content_type="text/html; charset=utf-8",
                content_length=len(INDEX_HTML.encode("utf-8")),
            )
            return
        asset = _web_metadata_asset(route_path)
        if asset is not None:
            data, content_type = asset
            self._send_headers_only(content_type=content_type, content_length=len(data))
            return
        self._send_headers_only(
            status=HTTPStatus.NOT_FOUND,
            content_type="application/json; charset=utf-8",
            content_length=0,
        )

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            route_path = parsed.path
            if route_path == "/api/text":
                payload = self._read_json()
                item = self.reader.add_text(
                    str(payload.get("text", "")),
                    label=str(payload.get("label", "Text")),
                )
                self.reader.play(item.id)
                self._send_json(self.reader.state())
                return

            if route_path == "/api/library/items":
                payload = self._read_json()
                item, prepare_audio = self.reader.upsert_library_item(payload)
                self._send_json(
                    {"ok": True, "item": self.reader.library_item_payload(item_id=item.id)},
                    status=HTTPStatus.ACCEPTED if prepare_audio else HTTPStatus.OK,
                )
                return

            if route_path == "/api/library/analysis/run":
                payload = self.reader.queue_library_analysis(reason="manual")
                self._send_json({"ok": True, "analysis": payload}, status=HTTPStatus.ACCEPTED)
                return

            library_prefix = "/api/library/items/"
            if route_path.startswith(library_prefix) and route_path.endswith("/prepare-audio"):
                item_id = unquote(route_path[len(library_prefix) : -len("/prepare-audio")])
                payload = self.reader.prepare_library_audio(
                    item_id,
                    retry=bool(self._read_json().get("retry")),
                )
                self._send_json({"ok": True, "item": payload}, status=HTTPStatus.ACCEPTED)
                return

            if route_path == "/api/upload":
                filename, data, _content_type = self._read_upload()
                item = self.reader.add_document(filename, data)
                self.reader.play(item.id)
                self._send_json(self.reader.state())
                return

            if route_path == "/api/audio/transcribe":
                filename, data, content_type = self._read_upload()
                self._send_json(
                    self.reader.transcribe_audio_file(
                        filename,
                        data,
                        content_type=content_type,
                        language=_optional_string(self.headers.get("X-Doc-Reader-Language")),
                        timestamped=_header_flag(self.headers.get("X-Doc-Reader-Timestamps")),
                    )
                )
                return

            if route_path == "/api/pause":
                self._send_json(self.reader.pause())
                return

            if route_path == "/api/stop":
                self._send_json(self.reader.stop())
                return

            if route_path == "/api/settings":
                self._send_json(self.reader.update_settings(self._read_json()))
                return

            text_prefix = "/api/items/"
            if route_path.startswith(text_prefix) and route_path.endswith("/text"):
                item_id = unquote(route_path[len(text_prefix) : -len("/text")])
                payload = self._read_json()
                self._send_json(self.reader.update_item_text(item_id, str(payload.get("text", ""))))
                return

            if route_path == "/api/native/start":
                self._send_json(self.reader.start_native_helper())
                return

            if route_path == "/api/native/stop":
                self._send_json(self.reader.stop_native_helper())
                return

            if route_path == "/api/native/reset":
                self._send_json(self.reader.reset_native_helper())
                return

            if route_path == "/api/native/dictation":
                self._send_json(self.reader.update_native_dictation_status(self._read_json()))
                return

            if route_path == "/api/transcribe":
                self._send_json(
                    self.reader.transcribe_audio(
                        self._read_body(),
                        content_type=self.headers.get("Content-Type", "audio/wav"),
                        elapsed_seconds=_optional_float(self.headers.get("X-Doc-Reader-Elapsed-Seconds")),
                        language=_optional_string(self.headers.get("X-Doc-Reader-Language")),
                        source=_optional_string(self.headers.get("X-Doc-Reader-Source")),
                        source_item_id=_optional_string(self.headers.get("X-Doc-Reader-Source-Item-Id")),
                        source_meta=_header_source_meta(self.headers),
                    )
                )
                return

            play_prefix = "/api/items/"
            if route_path.startswith(play_prefix) and route_path.endswith("/play"):
                item_id = unquote(route_path[len(play_prefix) : -len("/play")])
                self._send_json(self.reader.play(item_id))
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"Request failed: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("[doc-reader-web] " + (format % args) + "\n")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON payload must be an object.")
        return payload

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_upload(self) -> tuple[str, bytes, str]:
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0") or "0")
        if "multipart/form-data" not in content_type or length <= 0:
            raise ValueError("Expected a multipart file upload.")

        raw = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: "
            + content_type.encode("utf-8")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + raw
        )
        for part in message.iter_parts():
            disposition = part.get_content_disposition()
            filename = part.get_filename()
            if disposition == "form-data" and filename:
                payload = part.get_payload(decode=True)
                if payload is None:
                    payload = b""
                return filename, payload, part.get_content_type()
        raise ValueError("No uploaded file found.")

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_binary(self, data: bytes, *, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_headers_only(
        self,
        *,
        content_type: str,
        content_length: int,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(max(0, content_length)))
        self.end_headers()


class DocReaderHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler, reader: ReaderService) -> None:
        super().__init__(server_address, handler)
        self.reader = reader


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if IS_WINDOWS:
        # taskkill /T stops the reader and the ffplay child it may be waiting on.
        kill_process_tree(process.pid, force=True)
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()

    deadline = time.monotonic() + 0.9
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.wait(timeout=0.5)


def _safe_filename(filename: str) -> str:
    cleaned = Path(filename or "document.txt").name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", cleaned).strip(" .-")
    return cleaned or "document.txt"


def _format_timestamped_transcript(segments: object, *, fallback_text: str) -> str:
    phrases = _timestamp_phrases(segments)
    if not phrases:
        return fallback_text
    return "\n".join(
        f"[{_phrase_timestamp(start)} - {_phrase_timestamp(end)}] {text}"
        for start, end, text in phrases
        if text
    )


def _timestamp_phrases(segments: object) -> list[tuple[float, float, str]]:
    if not isinstance(segments, list):
        return []
    phrases: list[tuple[float, float, str]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        word_phrases = _timestamp_phrases_from_words(segment.get("words"))
        if word_phrases:
            phrases.extend(word_phrases)
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = _timestamp_float(segment.get("start"))
        end = _timestamp_float(segment.get("end"))
        start_value = start if start is not None else 0.0
        end_value = end if end is not None else start_value
        phrases.append((start_value, max(start_value, end_value), text))
    return phrases


def _timestamp_phrases_from_words(words: object) -> list[tuple[float, float, str]]:
    if not isinstance(words, list):
        return []
    phrases: list[tuple[float, float, str]] = []
    current_words: list[str] = []
    phrase_start: float | None = None
    phrase_end: float | None = None
    for word in words:
        if not isinstance(word, dict):
            continue
        text = str(word.get("word") or "").strip()
        if not text:
            continue
        start = _timestamp_float(word.get("start"))
        end = _timestamp_float(word.get("end"))
        if phrase_start is None:
            phrase_start = start if start is not None else phrase_end
        if end is not None:
            phrase_end = end
        elif start is not None:
            phrase_end = start
        current_words.append(text)
        duration = (phrase_end or 0.0) - (phrase_start or 0.0)
        if (
            re.search(r"[.!?;:]$|,$", text)
            or len(current_words) >= 14
            or duration >= 7.0
        ):
            phrase = _joined_word_text(current_words)
            if phrase:
                start_value = phrase_start if phrase_start is not None else 0.0
                end_value = phrase_end if phrase_end is not None else start_value
                phrases.append((max(0.0, start_value), max(start_value, end_value), phrase))
            current_words = []
            phrase_start = None
            phrase_end = None
    phrase = _joined_word_text(current_words)
    if phrase:
        start_value = phrase_start if phrase_start is not None else 0.0
        end_value = phrase_end if phrase_end is not None else start_value
        phrases.append((max(0.0, start_value), max(start_value, end_value), phrase))
    return phrases


def _joined_word_text(words: list[str]) -> str:
    text = " ".join(word.strip() for word in words if word.strip())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def _phrase_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    tenths = int(round(seconds * 10))
    total_seconds, tenth = divmod(tenths, 10)
    minutes, second = divmod(total_seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minute:02d}:{second:02d}.{tenth}"
    return f"{minute:02d}:{second:02d}.{tenth}"


def _timestamp_float(value: object) -> float | None:
    try:
        parsed = float("" if value is None else str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed < 0:
        return None
    return parsed


def _is_audio_content_type(content_type: str) -> bool:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    return normalized.startswith("audio/") or normalized in {"video/mp4", "video/webm"}


def _audio_upload_content_type(filename: str, content_type: str) -> str:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    if _is_audio_content_type(normalized):
        return normalized

    guessed, _encoding = mimetypes.guess_type(filename)
    if guessed and _is_audio_content_type(guessed):
        return guessed

    suffix = Path(filename).suffix.lower()
    suffix_types = {
        ".aac": "audio/aac",
        ".aif": "audio/aiff",
        ".aiff": "audio/aiff",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }
    return suffix_types.get(suffix, normalized or "audio/wav")


def _snippet(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= 140:
        return collapsed
    return collapsed[:137] + "..."


def _title(label: str, snippet: str) -> str:
    prefix = label.strip() or "Text"
    raw = f"{prefix}: {snippet}" if snippet else prefix
    if len(raw) <= 90:
        return raw
    return raw[:87] + "..."


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _safe_source(value: object) -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9_.:-]+", "-", normalized)
    return normalized.strip("-")[:80]


def _safe_source_item_id(value: object) -> str:
    normalized = str(value or "").strip()
    normalized = normalized.replace("\0", "")
    return normalized[:240]


def _safe_library_kind(value: object, *, source: str = "") -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9_.:-]+", "-", normalized).strip("-")
    if normalized:
        return normalized[:80]
    return "clawdad-message" if source == "clawdad" else "text"


def _safe_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for tag in value:
        cleaned = str(tag or "").strip()
        if cleaned and cleaned not in tags:
            tags.append(cleaned[:80])
    return tags[:20]


def _source_meta_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("source_meta")
    if raw is None:
        raw = payload.get("sourceMeta")
    source_meta = raw if isinstance(raw, dict) and not isinstance(raw, list) else {}
    allowed = {}
    for key, value in source_meta.items():
        cleaned_key = str(key or "").strip()
        if not cleaned_key:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            allowed[cleaned_key[:80]] = value
    for key in ["projectPath", "project", "sessionId", "requestId", "messageKind", "status"]:
        if key in payload and key not in allowed:
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                allowed[key] = value
    return allowed


def _library_title(payload: dict[str, Any], *, kind: str, source: str, snippet: str) -> str:
    explicit = str(payload.get("title") or "").strip()
    if explicit:
        return explicit[:120]
    label = str(payload.get("label") or "").strip()
    if label:
        return _title(label, snippet)
    if source == "clawdad":
        if kind.endswith("response"):
            return _title("Clawdad Response", snippet)
        if kind.endswith("message"):
            return _title("Clawdad Message", snippet)
        return _title("Clawdad", snippet)
    if kind == "dictation":
        return _title("Dictation", snippet)
    return _title("Text", snippet)


def _is_dictation_item(item: HistoryItem) -> bool:
    return item.kind == "dictation" or item.title.startswith("Dictation:")


def _is_clawdad_item(item: HistoryItem) -> bool:
    return item.source == "clawdad" or item.kind.startswith("clawdad-")


def _item_audio_state(item: HistoryItem) -> str:
    state = str(item.audio_state or "").strip().lower()
    if state in {"queued", "processing", "ready", "failed"}:
        return state
    return "none"


def _normalize_history_entry_payload(entry: dict[str, Any], item_fields: set[str]) -> dict[str, Any]:
    normalized = dict(entry)
    if "sourceMeta" in normalized and "source_meta" not in normalized:
        normalized["source_meta"] = normalized.pop("sourceMeta")
    if "sourceItemId" in normalized and "source_item_id" not in normalized:
        normalized["source_item_id"] = normalized.pop("sourceItemId")
    return {key: value for key, value in normalized.items() if key in item_fields}


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(str(text or "")))


def _metrics_text_for_item_text(item: HistoryItem, text: str) -> str:
    if _is_dictation_item(item):
        return _strip_timestamp_line_prefixes(text)
    return text


def _strip_timestamp_line_prefixes(text: str) -> str:
    return "\n".join(
        TIMESTAMP_LINE_PREFIX_RE.sub("", line)
        for line in str(text or "").splitlines()
    )


def _metrics_channel_for_kind(kind: str) -> str:
    return "stt" if str(kind or "").strip().lower() == "dictation" else "tts"


def _metrics_channel_for_item(item: HistoryItem) -> str:
    configured = str(item.metrics_channel or "").strip().lower()
    if configured in {"stt", "tts"}:
        return configured
    return "stt" if _is_dictation_item(item) else "tts"


def _item_word_count(item: HistoryItem) -> int:
    try:
        words = int(item.word_count or 0)
    except (TypeError, ValueError):
        words = 0
    if words > 0:
        return words
    return _word_count(_analysis_text_for_item(item))


def _refresh_item_metrics(item: HistoryItem) -> None:
    item.metrics_channel = _metrics_channel_for_item(item)
    if item.word_count <= 0 or not item.text_hash:
        text = _analysis_text_for_item(item)
        if item.word_count <= 0:
            item.word_count = _word_count(text)
        if not item.text_hash and text:
            item.text_hash = _text_hash(text)


def _document_word_count(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        return _word_count(_read_text_file(path))
    try:
        return _word_count(" ".join(iter_document_blocks(path)))
    except Exception:  # noqa: BLE001
        return 0


def _analysis_text_for_item(item: HistoryItem) -> str:
    path = Path(item.source_path)
    suffix = path.suffix.lower()
    text = ""
    if suffix in {".txt", ".md", ".markdown"} or item.kind != "document":
        text = _read_text_file(path)
    return text or item.snippet or item.title


def _analysis_candidates(
    items: list[HistoryItem],
    item_entries: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    max_chars = _env_int("DOC_READER_ANALYSIS_ITEM_MAX_CHARS", DEFAULT_ANALYSIS_ITEM_MAX_CHARS)
    candidates: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda entry: entry.updated_at):
        text = _analysis_text_for_item(item)
        if not text.strip():
            continue
        text_hash = item.text_hash or _text_hash(text)
        existing = item_entries.get(item.id)
        if isinstance(existing, dict) and existing.get("text_hash") == text_hash:
            continue
        candidates.append(
            {
                "id": item.id,
                "kind": item.kind,
                "source": item.source or "",
                "channel": _metrics_channel_for_item(item),
                "title": item.title,
                "snippet": item.snippet,
                "text": text[:max(400, max_chars)],
                "full_word_count": _item_word_count(item),
                "text_hash": text_hash,
                "completed": bool(item.completed),
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
        )
        if len(candidates) >= max(1, limit):
            break
    return candidates


def _analysis_backend_config() -> tuple[str, str, str]:
    backend = _env("DOC_READER_ANALYSIS_BACKEND", "auto").lower()
    if backend not in {"auto", "ollama", "openai-compatible", "heuristic"}:
        backend = "auto"
    model = _env("DOC_READER_ANALYSIS_MODEL", DEFAULT_ANALYSIS_MODEL)
    url = _env("DOC_READER_ANALYSIS_URL", _default_analysis_url())
    return backend, model, url.rstrip("/")


def _default_analysis_url() -> str:
    parsed = urlparse(_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL))
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    return f"{scheme}://{host}:11434"


def _analyze_batch_with_local_model(
    candidates: list[dict[str, Any]],
    *,
    backend: str,
    model: str,
    url: str,
) -> dict[str, Any]:
    if backend == "heuristic":
        return _heuristic_batch_analysis(candidates)

    prompt = _analysis_prompt(candidates)
    errors: list[str] = []
    if backend in {"auto", "ollama"}:
        try:
            endpoint = url if url.endswith("/api/generate") else f"{url}/api/generate"
            payload = _post_json(
                endpoint,
                {
                    "model": model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            doc = _parse_jsonish(payload.get("response") if isinstance(payload, dict) else payload)
            doc["backend"] = "ollama"
            doc["model"] = model
            return doc
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ollama: {exc}")
            if backend == "ollama":
                raise

    if backend in {"auto", "openai-compatible"}:
        try:
            endpoint = url if url.endswith("/v1/chat/completions") else f"{url}/v1/chat/completions"
            payload = _post_json(
                endpoint,
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Return compact JSON only. Do not include markdown.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                },
            )
            choices = payload.get("choices") if isinstance(payload, dict) else None
            message = choices[0].get("message", {}) if isinstance(choices, list) and choices else {}
            content = message.get("content") if isinstance(message, dict) else ""
            doc = _parse_jsonish(content)
            doc["backend"] = "openai-compatible"
            doc["model"] = model
            return doc
        except Exception as exc:  # noqa: BLE001
            errors.append(f"openai-compatible: {exc}")
            if backend == "openai-compatible":
                raise

    raise RuntimeError("local analysis model unavailable: " + " | ".join(errors))


def _analysis_prompt(candidates: list[dict[str, Any]]) -> str:
    records = [
        {
            "id": candidate["id"],
            "channel": candidate["channel"],
            "kind": candidate["kind"],
            "source": candidate["source"],
            "title": candidate["title"],
            "completed": candidate["completed"],
            "words": candidate["full_word_count"],
            "text": candidate["text"],
        }
        for candidate in candidates
    ]
    return (
        "Analyze these Doc Reader Library entries. STT means dictated speech-to-text; "
        "TTS means text prepared for listening. Return JSON with keys summary and items. "
        "Each item must include id, summary, topics, tone, intent, completion_state, "
        "action_items, and style_notes. Keep each summary under 35 words. "
        "Use completion_state values completed, open, discussed, or unknown.\n\n"
        f"Entries:\n{json.dumps(records, ensure_ascii=True)}"
    )


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = os.getenv("DOC_READER_ANALYSIS_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    timeout = _env_int("DOC_READER_ANALYSIS_TIMEOUT_SECONDS", DEFAULT_ANALYSIS_TIMEOUT_SECONDS)
    try:
        with urlrequest.urlopen(request, timeout=max(5, timeout)) as response:
            return json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"analysis service failed ({exc.code}): {detail[:500]}") from exc


def _parse_jsonish(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty analysis response")
    try:
        parsed = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("analysis response was not an object")
    return parsed


def _normalized_item_analyses(
    batch_result: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_items = batch_result.get("items")
    by_id = {}
    if isinstance(raw_items, list):
        for raw in raw_items:
            if isinstance(raw, dict) and raw.get("id"):
                by_id[str(raw.get("id"))] = raw

    normalized = []
    for candidate in candidates:
        raw = by_id.get(candidate["id"])
        fallback = _heuristic_item_analysis(candidate)
        if not isinstance(raw, dict):
            raw = {}
        analyzed_at = time.time()
        normalized.append(
            {
                "id": candidate["id"],
                "kind": candidate["kind"],
                "source": candidate["source"],
                "channel": candidate["channel"],
                "words": int(candidate["full_word_count"]),
                "text_hash": candidate["text_hash"],
                "title": candidate["title"],
                "summary": _compact_string(raw.get("summary") or fallback["summary"], 360),
                "topics": _string_list(raw.get("topics") or fallback["topics"], limit=8),
                "tone": _string_list(raw.get("tone") or fallback["tone"], limit=6),
                "intent": _compact_string(raw.get("intent") or fallback["intent"], 160),
                "completion_state": _completion_value(raw.get("completion_state") or fallback["completion_state"]),
                "action_items": _string_list(raw.get("action_items") or fallback["action_items"], limit=8, max_chars=180),
                "style_notes": _string_list(raw.get("style_notes") or fallback["style_notes"], limit=8, max_chars=160),
                "top_terms": fallback["top_terms"],
                "question_count": fallback["question_count"],
                "created_at": candidate["created_at"],
                "updated_at": candidate["updated_at"],
                "analyzed_at": analyzed_at,
                "backend": batch_result.get("backend") or "heuristic",
                "model": batch_result.get("model") or "local-rules",
            }
        )
    return normalized


def _heuristic_batch_analysis(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    items = [_heuristic_item_analysis(candidate) for candidate in candidates]
    topics = Counter(topic for item in items for topic in item["topics"])
    top_topics = ", ".join(topic for topic, _count in topics.most_common(4))
    summary = f"Batch covered {len(items)} item(s)"
    if top_topics:
        summary = f"{summary}: {top_topics}"
    return {
        "backend": "heuristic",
        "model": "local-rules",
        "summary": summary,
        "items": items,
    }


def _heuristic_item_analysis(candidate: dict[str, Any]) -> dict[str, Any]:
    text = str(candidate.get("text") or "")
    sentences = _sentences(text)
    top_terms = _top_terms(text, limit=8)
    action_items = _action_sentences(sentences)
    question_count = text.count("?")
    avg_sentence_words = (
        round(sum(_word_count(sentence) for sentence in sentences) / len(sentences), 1)
        if sentences
        else 0
    )
    style_notes = [
        f"{question_count} question(s)",
        f"{avg_sentence_words} words per sentence",
    ]
    if action_items:
        style_notes.append(f"{len(action_items)} action-oriented sentence(s)")
    return {
        "id": candidate["id"],
        "summary": _first_sentence_summary(text),
        "topics": top_terms[:6],
        "tone": _tone_tags(text),
        "intent": _intent_label(text, action_items=action_items),
        "completion_state": _completion_state(candidate, text),
        "action_items": action_items,
        "style_notes": style_notes,
        "top_terms": top_terms,
        "question_count": question_count,
    }


def _first_sentence_summary(text: str) -> str:
    sentences = _sentences(text)
    summary = sentences[0] if sentences else str(text or "").strip()
    words = summary.split()
    if len(words) > 32:
        summary = " ".join(words[:32]) + "..."
    return summary[:300]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", str(text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def _top_terms(text: str, *, limit: int) -> list[str]:
    words = [
        word.lower()
        for word in WORD_RE.findall(str(text or ""))
        if len(word) > 2 and word.lower() not in STYLE_STOP_WORDS
    ]
    return [word for word, _count in Counter(words).most_common(limit)]


def _tone_tags(text: str) -> list[str]:
    lowered = str(text or "").lower()
    tags: list[str] = []
    if any(token in lowered for token in ["need", "should", "must", "fix", "add"]):
        tags.append("directive")
    if any(token in lowered for token in ["maybe", "sort of", "kind of", "wonder"]):
        tags.append("exploratory")
    if "?" in lowered:
        tags.append("questioning")
    if any(token in lowered for token in ["completed", "done", "finished"]):
        tags.append("completion-aware")
    if not tags:
        tags.append("descriptive")
    return tags


def _intent_label(text: str, *, action_items: list[str]) -> str:
    lowered = str(text or "").lower()
    if action_items:
        return "action planning"
    if "?" in lowered:
        return "question or exploration"
    if any(token in lowered for token in ["summary", "summarize", "what happened", "discussed"]):
        return "sensemaking"
    return "reference"


def _completion_state(candidate: dict[str, Any], text: str) -> str:
    if bool(candidate.get("completed")):
        return "completed"
    lowered = str(text or "").lower()
    if any(token in lowered for token in ["completed", "done", "finished", "shipped"]):
        return "completed"
    if str(candidate.get("channel")) == "stt":
        return "discussed"
    if any(token in lowered for token in ["todo", "next", "need", "should", "add", "fix"]):
        return "open"
    return "unknown"


def _action_sentences(sentences: list[str]) -> list[str]:
    action_words = ["need", "should", "must", "add", "fix", "run", "write", "make", "ship", "verify", "check"]
    actions = []
    for sentence in sentences:
        lowered = sentence.lower()
        if any(word in lowered for word in action_words):
            actions.append(_compact_string(sentence, 180))
    return actions[:5]


def _completion_value(value: object) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "-")
    if normalized in {"completed", "complete", "done"}:
        return "completed"
    if normalized in {"open", "pending", "todo"}:
        return "open"
    if normalized in {"discussed", "discussion"}:
        return "discussed"
    return "unknown"


def _compact_string(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _string_list(value: object, *, limit: int, max_chars: int = 80) -> list[str]:
    if isinstance(value, str):
        raw_items: list[object] = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    items = []
    for raw in raw_items:
        text = _compact_string(raw, max_chars)
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _style_map_from_analysis(item_entries: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    entries = [entry for entry in item_entries.values() if isinstance(entry, dict)]
    topics = Counter(topic for entry in entries for topic in _string_list(entry.get("topics"), limit=12))
    tones = Counter(tone for entry in entries for tone in _string_list(entry.get("tone"), limit=8))
    completions = Counter(_completion_value(entry.get("completion_state")) for entry in entries)
    action_count = sum(len(_string_list(entry.get("action_items"), limit=20)) for entry in entries)
    question_count = sum(int(entry.get("question_count") or 0) for entry in entries)
    by_channel = metrics.get("by_channel", {}) if isinstance(metrics.get("by_channel"), dict) else {}
    stt_items = max(1, int(by_channel.get("stt", {}).get("items", 0) or 0))
    tts_items = max(1, int(by_channel.get("tts", {}).get("items", 0) or 0))
    return {
        "schema": "doc-reader.style-map/1",
        "items_analyzed": len(entries),
        "top_topics": [{"term": term, "count": count} for term, count in topics.most_common(12)],
        "tone": [{"term": term, "count": count} for term, count in tones.most_common(8)],
        "completion": dict(completions),
        "action_items": action_count,
        "question_count": question_count,
        "average_words": {
            "stt": round(float(by_channel.get("stt", {}).get("words", 0) or 0) / stt_items, 1),
            "tts": round(float(by_channel.get("tts", {}).get("words", 0) or 0) / tts_items, 1),
        },
    }


def _latest_batch(analysis: dict[str, Any]) -> dict[str, Any]:
    batches = analysis.get("batches")
    if not isinstance(batches, list) or not batches:
        return {}
    latest = batches[-1]
    return latest if isinstance(latest, dict) else {}


def _timestamp_utc() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _launch_agent_loaded(target: str) -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _kickstart_launch_agent(target: str) -> tuple[int, str]:
    process = subprocess.Popen(
        ["/bin/launchctl", "kickstart", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _stdout, stderr = process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0, ""
    return process.returncode, (stderr or "").strip()


def _terminate_native_helper_processes() -> None:
    if sys.platform != "darwin":
        return
    pids = _native_helper_process_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if not pids:
        return
    time.sleep(0.4)
    remaining = set(_native_helper_process_pids())
    for pid in pids:
        if pid not in remaining:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _native_helper_process_pids() -> list[int]:
    result = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)$", line)
        if not match:
            continue
        pid = int(match.group(1))
        command = match.group(2)
        if pid == os.getpid():
            continue
        if (
            "/Doc Reader.app/Contents/MacOS/DocReader" in command
            or "/DocReader.app/Contents/MacOS/DocReader" in command
        ):
            pids.append(pid)
    return sorted(set(pids))


def _clamped_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = minimum
    return min(maximum, max(minimum, number))


def _microphone_match_tokens() -> list[str]:
    configured = _env("DOC_READER_DEFAULT_MICROPHONE_MATCH", DEFAULT_MICROPHONE_MATCH)
    tokens = [token.strip().lower() for token in configured.split(",")]
    return [token for token in tokens if token]


def _sanitized_microphone_devices(raw_devices: Any) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    if not isinstance(raw_devices, list):
        return devices
    for device in raw_devices:
        if not isinstance(device, dict):
            continue
        device_id = str(device.get("id") or "").strip()
        name = str(device.get("name") or "").strip()
        if device_id and name:
            devices.append({"id": device_id, "name": name})
    return devices


def _microphone_device_by_id(
    devices: list[dict[str, str]],
    microphone_id: str,
) -> dict[str, str] | None:
    if not microphone_id:
        return None
    for device in devices:
        if device["id"] == microphone_id:
            return device
    return None


def _preferred_microphone_device(
    devices: list[dict[str, str]],
) -> dict[str, str] | None:
    tokens = _microphone_match_tokens()
    for token in tokens:
        for device in devices:
            if token in device["name"].lower():
                return device
    for token in tokens:
        for device in devices:
            if token in device["id"].lower():
                return device
    return None


def _pin_preferred_microphone(
    settings: dict[str, Any],
    devices: list[dict[str, str]],
) -> dict[str, str] | None:
    microphone_id = str(settings.get("microphone_id") or "").strip()
    if _microphone_device_by_id(devices, microphone_id):
        return None
    preferred_device = _preferred_microphone_device(devices)
    if preferred_device:
        settings["microphone_id"] = preferred_device["id"]
    return preferred_device


def _microphone_payload(settings: dict[str, Any]) -> dict[str, Any]:
    raw_devices = _sanitized_microphone_devices(settings.get("microphones"))
    preferred_device = _preferred_microphone_device(raw_devices)
    configured_id = str(settings.get("microphone_id") or "").strip()
    selected_device = _microphone_device_by_id(raw_devices, configured_id)
    if selected_device is None and preferred_device is not None:
        selected_device = preferred_device
    selected_id = selected_device["id"] if selected_device is not None else ""
    status_at = float(settings.get("native_dictation_status_at") or 0.0)
    native_age_seconds = max(0.0, time.time() - status_at) if status_at else None
    native_helper_online = (
        native_age_seconds is not None
        and native_age_seconds <= NATIVE_HELPER_STALE_SECONDS
    )
    devices = [] if preferred_device is not None else [{"id": "", "name": "System Default"}]
    devices.extend(raw_devices)
    selected_name = selected_device["name"] if selected_device is not None else "System Default"
    return {
        "selected_id": selected_id,
        "selected_name": selected_name,
        "preferred_id": preferred_device["id"] if preferred_device is not None else "",
        "preferred_name": preferred_device["name"] if preferred_device is not None else "",
        "active_id": str(settings.get("active_microphone_id") or ""),
        "native_helper_online": native_helper_online,
        "native_status_age_seconds": native_age_seconds,
        "recording": bool(settings.get("recording")),
        "recording_start_pending": bool(settings.get("recording_start_pending")),
        "last_event": str(settings.get("last_dictation_event") or ""),
        "audio_level": _clamped_float(settings.get("audio_level"), 0.0, 1.0),
        "audio_peak_level": _clamped_float(settings.get("audio_peak_level"), 0.0, 1.0),
        "last_recording": {
            "path": str(settings.get("last_recording_path") or ""),
            "bytes": max(0, int(_clamped_float(settings.get("last_recording_bytes"), 0.0, 10_000_000_000.0))),
            "seconds": _clamped_float(settings.get("last_recording_seconds"), 0.0, 86_400.0),
            "content_type": str(settings.get("last_recording_content_type") or "audio/mp4"),
            "peak_level": _clamped_float(settings.get("last_recording_peak_level"), 0.0, 1.0),
            "created_at": _clamped_float(settings.get("last_recording_created_at"), 0.0, 4_102_444_800.0),
        },
        "devices": devices,
        "authorization": str(settings.get("microphone_authorization") or "unknown"),
        "input_monitoring_trusted": bool(settings.get("input_monitoring_trusted")),
        "accessibility_trusted": bool(settings.get("accessibility_trusted")),
    }


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


_HEALTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_HEALTH_CACHE_LOCK = threading.Lock()
DEFAULT_HEALTH_CACHE_OK_SECONDS = 1.5
DEFAULT_HEALTH_CACHE_FAIL_SECONDS = 10.0


def _service_health_cache_clear() -> None:
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE.clear()


def _service_health(base_url: str, *, timeout: float | None = None) -> dict[str, Any]:
    """Probe a speech service, caching the answer briefly.

    `state()` is polled by the page about once a second and calls this for every
    configured service while holding the reader lock. An unreachable remote
    service (for example the author's Tailscale box when running elsewhere) would
    otherwise cost a full connect timeout on every poll and stall dictation and
    playback requests behind the lock.
    """
    ok_ttl = _env_float("DOC_READER_SERVICE_HEALTH_CACHE_SECONDS", DEFAULT_HEALTH_CACHE_OK_SECONDS)
    fail_ttl = _env_float("DOC_READER_SERVICE_HEALTH_FAIL_CACHE_SECONDS", DEFAULT_HEALTH_CACHE_FAIL_SECONDS)
    now = time.monotonic()
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get(base_url)
    if cached is not None:
        cached_at, payload = cached
        ttl = ok_ttl if payload.get("ok") else fail_ttl
        if now - cached_at < ttl:
            return dict(payload)
    payload = _service_health_uncached(base_url, timeout=timeout)
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE[base_url] = (time.monotonic(), dict(payload))
    return payload


def _service_health_uncached(base_url: str, *, timeout: float | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    timeout_seconds = timeout
    if timeout_seconds is None:
        timeout_seconds = _env_float(
            "DOC_READER_SERVICE_HEALTH_TIMEOUT_SECONDS",
            DEFAULT_SERVICE_HEALTH_TIMEOUT_SECONDS,
        )
    timeout_seconds = max(0.1, min(5.0, timeout_seconds))
    try:
        with urlrequest.urlopen(f"{base_url.rstrip('/')}/healthz", timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "ok": bool(payload.get("ok")),
            "url": base_url,
            "ms": round((time.perf_counter() - started) * 1000),
            "device": payload.get("device", {}),
            "engines": payload.get("engines", {}),
        }
    except (OSError, ValueError, urlerror.URLError) as exc:
        return {
            "ok": False,
            "url": base_url,
            "ms": round((time.perf_counter() - started) * 1000),
            "error": str(exc),
        }


def _stt_service_candidates() -> list[tuple[str, str]]:
    override = _env("DOC_READER_STT_URL", "").strip()
    if override:
        return [("custom-whisper", override)]
    return [
        ("mac-whisper", _env("DOC_READER_TTS_MAC_URL", DEFAULT_TTS_MAC_URL)),
        ("tailscale-4090-whisper", _env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL)),
    ]


def _stt_default_url() -> str:
    candidates = _stt_service_candidates()
    return candidates[0][1] if candidates else _env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL)


def _stt_service_label(backend: str) -> str:
    if backend == "mac-whisper":
        return LOCAL_STT_LABEL
    if backend == "custom-whisper":
        return "Speech-to-text"
    return "Remote speech-to-text"


def _service_whisper_status(service: dict[str, Any]) -> dict[str, Any]:
    engines = service.get("engines", {}) if isinstance(service, dict) else {}
    whisper = engines.get("whisper", {}) if isinstance(engines, dict) else {}
    return whisper if isinstance(whisper, dict) else {}


def _service_has_whisper(service: dict[str, Any]) -> bool:
    whisper = _service_whisper_status(service)
    return bool(service.get("ok")) and bool(whisper.get("enabled"))


def _stt_service_health() -> tuple[str, dict[str, Any]]:
    fallback: tuple[str, dict[str, Any]] | None = None
    for backend, url in _stt_service_candidates():
        service = _service_health(url)
        service["stt_backend"] = backend
        if fallback is None:
            fallback = (backend, service)
        if _service_has_whisper(service):
            return backend, service
    if fallback is not None:
        return fallback
    backend = "tailscale-4090-whisper"
    service = _service_health(_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL))
    service["stt_backend"] = backend
    return backend, service


def _require_stt_service() -> tuple[str, dict[str, Any]]:
    backend, service = _stt_service_health()
    if _service_has_whisper(service):
        return backend, service
    attempts: list[str] = []
    for candidate_backend, candidate_url in _stt_service_candidates():
        candidate = _service_health(candidate_url)
        candidate["stt_backend"] = candidate_backend
        if _service_has_whisper(candidate):
            return candidate_backend, candidate
        whisper = _service_whisper_status(candidate)
        detail = str(candidate.get("error") or whisper.get("error") or "Whisper disabled")
        attempts.append(f"{_stt_service_label(candidate_backend)} at {candidate_url}: {detail}")
    if not attempts:
        attempts.append("No STT services configured")
    raise RuntimeError(
        "No Whisper speech-to-text service is ready. "
        "Use Reset Helper or run `read-docs tts-mac-start` to restore the Mac fallback. "
        + " | ".join(attempts)
    )


def _synthesize_library_audio(text: str, *, rate: int = DEFAULT_RATE) -> bytes:
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("No text to synthesize.")
    backend = _env("DOC_READER_WEB_SPEECH_BACKEND", DEFAULT_WEB_SPEECH_BACKEND)
    engine = "kokoro"
    urls: list[str] = []
    if backend in {"local-kokoro", "auto"}:
        urls.append(_env("DOC_READER_TTS_MAC_URL", DEFAULT_TTS_MAC_URL))
    if backend in {"tailscale-4090", "tailscale-kokoro", "auto"}:
        urls.append(_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL))
    if backend == "tailscale-chatterbox":
        urls.append(_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL))
        engine = "chatterbox"
    if backend == "http-tts":
        urls.append(_env("DOC_READER_HTTP_TTS_URL", DEFAULT_TTS_MAC_URL))
        engine = _env("DOC_READER_HTTP_TTS_ENGINE", "kokoro")
    if not urls:
        urls.extend([
            _env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL),
            _env("DOC_READER_TTS_MAC_URL", DEFAULT_TTS_MAC_URL),
        ])

    urls = list(dict.fromkeys(url.rstrip("/") for url in urls if url.strip()))
    failures: list[str] = []
    for base_url in urls:
        try:
            return _synthesize_library_audio_from_url(
                base_url,
                text=cleaned,
                engine=engine,
                voice=_env("DOC_READER_HTTP_TTS_VOICE", ""),
                speed=_speed_for_rate(_normalize_read_rate(rate)),
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{base_url}: {exc}")
    raise RuntimeError("Doc Reader local TTS failed: " + " | ".join(failures))


def _synthesize_library_audio_from_url(
    base_url: str,
    *,
    text: str,
    engine: str,
    voice: str,
    speed: float,
) -> bytes:
    payload = {
        "engine": engine,
        "text": text,
        "speed": speed,
    }
    if voice:
        payload["voice"] = voice
    data = json.dumps(payload).encode("utf-8")
    request = urlrequest.Request(
        f"{base_url.rstrip('/')}/v1/audio/speech",
        data=data,
        method="POST",
        headers={
            "Accept": "audio/wav",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=DEFAULT_LIBRARY_AUDIO_TIMEOUT_SECONDS) as response:
            audio = response.read()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"speech service failed ({exc.code}): {detail[:500]}") from exc
    except (OSError, urlerror.URLError) as exc:
        raise RuntimeError(f"speech service network error: {exc}") from exc
    if not audio:
        raise RuntimeError("speech service returned empty audio")
    return audio


def _normalize_stt_audio(
    audio: bytes,
    *,
    content_type: str,
    elapsed_seconds: float | None,
) -> tuple[bytes, str, dict[str, Any]]:
    ffmpeg = _local_tool("ffmpeg")
    if not ffmpeg:
        return audio, content_type, {"ok": False, "reason": "ffmpeg unavailable"}

    source_suffix = _suffix_from_content_type(content_type)
    with tempfile.TemporaryDirectory(prefix="doc-reader-stt-") as directory:
        temp_dir = Path(directory)
        source_path = temp_dir / f"input{source_suffix}"
        output_path = temp_dir / "normalized.wav"
        source_path.write_bytes(audio)

        source_duration = _probe_audio_duration(source_path)
        tempo = 1.0
        if elapsed_seconds and elapsed_seconds > 0.25 and source_duration:
            ratio = source_duration / elapsed_seconds
            if 1.15 <= ratio <= 4.0:
                tempo = ratio

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
        ]
        filters = [
            "highpass=f=80",
            "lowpass=f=7800",
            "loudnorm=I=-18:TP=-2:LRA=7",
        ]
        if abs(tempo - 1.0) >= 0.08:
            filters.insert(0, _atempo_filter(tempo))
        command.extend(["-filter:a", ",".join(filters)])
        command.extend(["-f", "wav", str(output_path)])

        try:
            subprocess.run(command, check=True, capture_output=True, timeout=90)
            normalized = output_path.read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            return audio, content_type, {
                "ok": False,
                "reason": f"normalization failed: {exc}",
                "source_duration": source_duration,
                "elapsed_seconds": elapsed_seconds,
                "tempo": tempo,
            }

    if not normalized:
        return audio, content_type, {"ok": False, "reason": "normalization produced no audio"}
    return normalized, "audio/wav", {
        "ok": True,
        "source_content_type": content_type,
        "source_duration": source_duration,
        "elapsed_seconds": elapsed_seconds,
        "tempo": tempo,
        "filters": filters,
    }


def _transcribe_on_stt_service(
    audio: bytes,
    *,
    content_type: str,
    base_url: str,
    service_label: str,
    language: str | None = None,
    word_timestamps: bool = False,
) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    timeout_seconds = max(10, _env_int("DOC_READER_STT_TIMEOUT_SECONDS", 90))
    headers = {
        "Content-Type": content_type or "audio/wav",
        "X-Doc-Reader-Filename": "dictation.wav",
    }
    stt_language = _optional_string(language) or _env("DOC_READER_STT_LANGUAGE", "en")
    if stt_language:
        headers["X-Doc-Reader-Language"] = stt_language
    if word_timestamps:
        headers["X-Doc-Reader-Word-Timestamps"] = "1"
    request = urlrequest.Request(
        f"{base_url}/v1/audio/transcriptions",
        data=audio,
        method="POST",
        headers=headers,
    )
    started = time.perf_counter()
    try:
        with urlrequest.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{service_label} transcription failed ({exc.code}): {detail}") from exc
    except (OSError, ValueError, urlerror.URLError) as exc:
        raise RuntimeError(f"{service_label} transcription network error: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"{service_label} transcription returned an invalid response.")
    if payload.get("ok") is False:
        raise RuntimeError(str(payload.get("error") or f"{service_label} transcription failed."))
    payload["request_seconds"] = round(time.perf_counter() - started, 3)
    payload["service_url"] = base_url
    payload["service_label"] = service_label
    return payload


def _probe_audio_duration(path: Path) -> float | None:
    ffprobe = _local_tool("ffprobe")
    if not ffprobe:
        return None
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return _optional_float(result.stdout)


def _atempo_filter(tempo: float) -> str:
    factors: list[float] = []
    remaining = max(0.5, min(100.0, tempo))
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.6f}" for factor in factors)


def _suffix_from_content_type(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized in {"audio/aac", "audio/x-aac"}:
        return ".aac"
    if normalized in {"audio/mp4", "audio/m4a", "video/mp4"}:
        return ".m4a"
    if normalized in {"audio/aiff", "audio/x-aiff"}:
        return ".aiff"
    if normalized in {"audio/mpeg", "audio/mp3"}:
        return ".mp3"
    if normalized in {"audio/flac", "audio/x-flac"}:
        return ".flac"
    if normalized in {"audio/ogg", "application/ogg"}:
        return ".ogg"
    if normalized in {"audio/webm", "video/webm"}:
        return ".webm"
    if normalized in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return ".wav"
    return ".wav"


def _local_tool(name: str) -> str:
    return find_tool(name)


def _optional_string(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _header_flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _optional_float(value: object) -> float | None:
    try:
        parsed = float(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    if not parsed == parsed or parsed <= 0:
        return None
    return parsed


def _normalize_read_rate(value: object) -> int:
    try:
        parsed = round(float(str(value).strip()))
    except (TypeError, ValueError):
        parsed = DEFAULT_RATE
    if parsed != parsed:
        parsed = DEFAULT_RATE
    return max(MIN_READ_RATE, min(MAX_READ_RATE, int(parsed)))


def _speed_for_rate(rate: int) -> float:
    return round(max(0.5, min(2.0, float(rate) / DEFAULT_RATE)), 3)


def _header_source_meta(headers: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for header, key in [
        ("X-Doc-Reader-Project", "projectPath"),
        ("X-Doc-Reader-Session-Id", "sessionId"),
        ("X-Doc-Reader-Request-Id", "requestId"),
    ]:
        value = _optional_string(headers.get(header))
        if value:
            meta[key] = value
    return meta


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _managed_root() -> Path:
    value = os.getenv("DOC_READER_MANAGED_ROOT")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".doc-reader-managed"


DOC_READER_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="Doc Reader">
  <rect width="512" height="512" rx="112" fill="#17201c"/>
  <rect x="148" y="84" width="216" height="344" rx="34" fill="#f4f8f4"/>
  <path d="M286 84h44c18 0 34 16 34 34v44z" fill="#dce9e0"/>
  <rect x="190" y="176" width="132" height="20" rx="10" fill="#4d5c54"/>
  <rect x="190" y="226" width="156" height="20" rx="10" fill="#4d5c54"/>
  <rect x="190" y="276" width="108" height="20" rx="10" fill="#4d5c54"/>
  <rect x="190" y="354" width="50" height="18" rx="9" fill="#1f9b68"/>
  <rect x="252" y="330" width="34" height="42" rx="10" fill="#2f7fd2"/>
  <rect x="300" y="360" width="46" height="12" rx="6" fill="#b77a16"/>
</svg>"""


def _doc_reader_manifest() -> str:
    return json.dumps(
        {
            "name": "Doc Reader",
            "short_name": "Doc Reader",
            "description": "Local GPU speech workspace for reading, dictation, and library analysis.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#f7f8f6",
            "theme_color": "#17201c",
            "icons": [
                {
                    "src": "/icons/doc-reader-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/icons/doc-reader-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/icons/doc-reader.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any",
                },
            ],
        },
        separators=(",", ":"),
    )


def _web_metadata_asset(route_path: str) -> tuple[bytes, str] | None:
    if route_path == "/favicon.ico":
        return _doc_reader_favicon_ico(), "image/x-icon"
    if route_path in {"/favicon.svg", "/icons/doc-reader.svg"}:
        return DOC_READER_ICON_SVG.encode("utf-8"), "image/svg+xml; charset=utf-8"
    if route_path in {"/apple-touch-icon.png", "/icons/doc-reader-180.png"}:
        return _doc_reader_icon_png(180), "image/png"
    if route_path == "/icons/doc-reader-192.png":
        return _doc_reader_icon_png(192), "image/png"
    if route_path == "/icons/doc-reader-512.png":
        return _doc_reader_icon_png(512), "image/png"
    if route_path == "/site.webmanifest":
        return _doc_reader_manifest().encode("utf-8"), "application/manifest+json; charset=utf-8"
    return None


@lru_cache(maxsize=8)
def _doc_reader_favicon_ico() -> bytes:
    png = _doc_reader_icon_png(32)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


@lru_cache(maxsize=12)
def _doc_reader_icon_png(size: int) -> bytes:
    size = max(16, min(1024, int(size)))
    pixels = bytearray(size * size * 4)
    _fill_rect(pixels, size, 0, 0, size, size, (23, 32, 28, 255))

    document_x = size * 0.289
    document_y = size * 0.164
    document_w = size * 0.422
    document_h = size * 0.672
    _fill_rounded_rect(pixels, size, document_x, document_y, document_w, document_h, size * 0.066, (244, 248, 244, 255))
    _fill_rect(pixels, size, size * 0.559, document_y, size * 0.086, size * 0.153, (220, 233, 224, 255))

    ink = (77, 92, 84, 255)
    _fill_rounded_rect(pixels, size, size * 0.371, size * 0.344, size * 0.258, size * 0.039, size * 0.020, ink)
    _fill_rounded_rect(pixels, size, size * 0.371, size * 0.441, size * 0.305, size * 0.039, size * 0.020, ink)
    _fill_rounded_rect(pixels, size, size * 0.371, size * 0.539, size * 0.211, size * 0.039, size * 0.020, ink)

    _fill_rounded_rect(pixels, size, size * 0.371, size * 0.691, size * 0.098, size * 0.035, size * 0.018, (31, 155, 104, 255))
    _fill_rounded_rect(pixels, size, size * 0.492, size * 0.645, size * 0.066, size * 0.082, size * 0.020, (47, 127, 210, 255))
    _fill_rounded_rect(pixels, size, size * 0.586, size * 0.703, size * 0.090, size * 0.023, size * 0.012, (183, 122, 22, 255))
    return _encode_png_rgba(size, size, pixels)


def _fill_rect(pixels: bytearray, canvas: int, x: float, y: float, width: float, height: float, color: tuple[int, int, int, int]) -> None:
    x0 = max(0, int(round(x)))
    y0 = max(0, int(round(y)))
    x1 = min(canvas, int(round(x + width)))
    y1 = min(canvas, int(round(y + height)))
    for py in range(y0, y1):
        offset = (py * canvas + x0) * 4
        for _px in range(x0, x1):
            pixels[offset:offset + 4] = bytes(color)
            offset += 4


def _fill_rounded_rect(
    pixels: bytearray,
    canvas: int,
    x: float,
    y: float,
    width: float,
    height: float,
    radius: float,
    color: tuple[int, int, int, int],
) -> None:
    x0 = max(0, int(round(x)))
    y0 = max(0, int(round(y)))
    x1 = min(canvas, int(round(x + width)))
    y1 = min(canvas, int(round(y + height)))
    radius = max(0.0, min(radius, width / 2, height / 2))
    for py in range(y0, y1):
        cy = py + 0.5
        for px in range(x0, x1):
            cx = px + 0.5
            dx = max(x + radius - cx, 0.0, cx - (x + width - radius))
            dy = max(y + radius - cy, 0.0, cy - (y + height - radius))
            if dx * dx + dy * dy <= radius * radius:
                offset = (py * canvas + px) * 4
                pixels[offset:offset + 4] = bytes(color)


def _encode_png_rgba(width: int, height: int, pixels: bytes | bytearray) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = []
    stride = width * 4
    for y in range(height):
        rows.append(b"\x00" + bytes(pixels[y * stride:(y + 1) * stride]))
    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="application-name" content="Doc Reader">
  <meta name="apple-mobile-web-app-title" content="Doc Reader">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="theme-color" content="#17201c">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="shortcut icon" href="/favicon.ico">
  <link rel="alternate icon" href="/favicon.ico" sizes="32x32">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  <link rel="manifest" href="/site.webmanifest">
  <title>Doc Reader</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700&display=swap">
  <style>
    :root {
      color-scheme: light dark;
      --bg: #eef0ea;
      --panel: #fbfbf8;
      --panel-2: #f2f4ee;
      --ink: #141a17;
      --muted: #5c665f;
      --line: #d3d8cf;
      --line-strong: #b9c1b7;
      --accent: #1f5b47;
      --accent-soft: #dcebe2;
      --accent-ink: #f4f8f4;
      --live: #b8651a;
      --live-soft: #f6e5d3;
      --success: #2b7a3e;
      --warn: #a83a2a;
      --shadow: 0 1px 2px rgba(20, 26, 23, 0.06), 0 8px 24px -16px rgba(20, 26, 23, 0.25);
      --font-display: "Bricolage Grotesque", "Segoe UI Variable Display", "Segoe UI", -apple-system, sans-serif;
      --font-body: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", system-ui, Roboto, sans-serif;
      --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
      --ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);
    }
    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) {
        --bg: #121614;
        --panel: #1a201c;
        --panel-2: #212824;
        --ink: #e9ede8;
        --muted: #94a097;
        --line: #2c3630;
        --line-strong: #3d4a42;
        --accent: #7fc9a9;
        --accent-soft: #1d3a2f;
        --accent-ink: #0d1a14;
        --live: #e0994a;
        --live-soft: #3a2a17;
        --success: #6fcf8d;
        --warn: #e07b6b;
        --shadow: 0 1px 2px rgba(0, 0, 0, 0.4), 0 10px 30px -18px rgba(0, 0, 0, 0.8);
      }
    }
    :root[data-theme="dark"] {
      --bg: #121614;
      --panel: #1a201c;
      --panel-2: #212824;
      --ink: #e9ede8;
      --muted: #94a097;
      --line: #2c3630;
      --line-strong: #3d4a42;
      --accent: #7fc9a9;
      --accent-soft: #1d3a2f;
      --accent-ink: #0d1a14;
      --live: #e0994a;
      --live-soft: #3a2a17;
      --success: #6fcf8d;
      --warn: #e07b6b;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.4), 0 10px 30px -18px rgba(0, 0, 0, 0.8);
    }
    * { box-sizing: border-box; }
    html { background: var(--bg); }
    body {
      margin: 0;
      font: 14px/1.5 var(--font-body);
      background: var(--bg);
      color: var(--ink);
      -webkit-font-smoothing: antialiased;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 28px 48px;
    }

    /* ---------------------------------------------------------------- header */
    header {
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin: 0 -28px 20px;
      padding: 14px 28px;
      background: color-mix(in srgb, var(--bg) 78%, transparent);
      -webkit-backdrop-filter: blur(18px) saturate(160%);
      backdrop-filter: blur(18px) saturate(160%);
      border-bottom: 1px solid color-mix(in srgb, var(--line) 70%, transparent);
    }
    @media (prefers-reduced-transparency: reduce) {
      header {
        background: var(--bg);
        -webkit-backdrop-filter: none;
        backdrop-filter: none;
      }
    }
    .brand {
      display: flex;
      align-items: baseline;
      gap: 12px;
      min-width: 0;
    }
    h1 {
      font: 600 24px/1.05 var(--font-display);
      font-variation-settings: "opsz" 40;
      letter-spacing: -0.02em;
      margin: 0;
      text-wrap: balance;
    }
    h2 {
      font: 600 13px/1.25 var(--font-body);
      margin: 0;
      letter-spacing: 0;
      color: var(--ink);
    }
    .status-wrap {
      display: flex;
      align-items: center;
      gap: 9px;
      min-width: 0;
      max-width: 52%;
    }
    .status-dot {
      flex: none;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--line-strong);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--line-strong) 25%, transparent);
    }
    body[data-state="reading"] .status-dot {
      background: var(--accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 25%, transparent);
      animation: pulse 1.6s ease-in-out infinite;
    }
    body[data-state="paused"] .status-dot {
      background: var(--live);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--live) 25%, transparent);
    }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 25%, transparent); }
      50% { box-shadow: 0 0 0 6px color-mix(in srgb, var(--accent) 10%, transparent); }
    }
    @media (prefers-reduced-motion: reduce) {
      body[data-state="reading"] .status-dot { animation: none; }
    }
    .status {
      font: 12.5px/1.35 var(--font-body);
      font-variant-numeric: tabular-nums;
      color: var(--muted);
      text-align: right;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    /* ---------------------------------------------------------------- layout */
    .grid {
      display: grid;
      grid-template-columns: minmax(300px, 372px) minmax(0, 1fr);
      gap: 22px;
      align-items: start;
    }
    section { min-width: 0; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 18px;
      box-shadow: var(--shadow);
    }
    section.panel.stack { gap: 16px; }
    @media (min-height: 980px) {
      section.panel.stack { position: sticky; top: 20px; }
    }
    .stack { display: grid; gap: 14px; }

    /* ---------------------------------------------------------------- fields */
    label {
      display: block;
      font: 500 12.5px/1.25 var(--font-body);
      letter-spacing: 0;
      color: var(--muted);
      margin-bottom: 6px;
    }
    .rule {
      border: 0;
      border-top: 1px solid var(--line);
      margin: 2px 0;
    }
    textarea, select, .library-search {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 11px;
      background: var(--panel-2);
      color: var(--ink);
      font: inherit;
      transition: border-color 140ms ease, box-shadow 140ms ease;
    }
    textarea:focus-visible, select:focus-visible, .library-search:focus-visible,
    input[type="file"]:focus-visible, button:focus-visible, input[type="range"]:focus-visible,
    input[type="checkbox"]:focus-visible {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
    }
    textarea {
      min-height: 150px;
      resize: vertical;
      line-height: 1.5;
    }
    textarea::placeholder { color: var(--muted); }
    select {
      min-height: 36px;
      padding: 7px 34px 7px 11px;
      appearance: none;
      -webkit-appearance: none;
      background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%);
      background-position: calc(100% - 17px) 55%, calc(100% - 12px) 55%;
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
    }
    input[type="range"] {
      width: 100%;
      accent-color: var(--accent);
      margin: 6px 0 0;
    }
    .range-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: baseline;
    }
    .range-head label { margin-bottom: 0; }
    .range-value {
      font: 500 12.5px/1 var(--font-body);
      font-variant-numeric: tabular-nums;
      color: var(--ink);
      white-space: nowrap;
    }
    input[type="file"] {
      width: 100%;
      border: 1px dashed var(--line-strong);
      border-radius: 8px;
      padding: 6px;
      background: transparent;
      color: var(--muted);
      font: 12.5px/1.3 var(--font-body);
    }
    input[type="file"]::file-selector-button {
      font: 500 12.5px/1 var(--font-body);
      color: var(--ink);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 10px;
      margin-right: 10px;
      cursor: pointer;
    }
    input[type="file"]::file-selector-button:active { transform: scale(0.97); }
    @media (hover: hover) and (pointer: fine) {
      input[type="file"]::file-selector-button:hover { border-color: var(--line-strong); }
    }
    .audio-upload-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }
    .audio-upload-row input[type="file"] { min-width: 0; }
    .check-row {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--ink);
    }
    .check-row label {
      margin: 0;
      color: var(--ink);
      font: 500 13px/1.2 var(--font-body);
    }
    input[type="checkbox"] {
      width: 15px;
      height: 15px;
      accent-color: var(--accent);
      margin: 0;
    }
    .row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .service-row { justify-content: space-between; }
    .service-toggle { min-width: 104px; }
    .service-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .service-reset { min-width: 64px; }
    .service-toggle.running {
      border-color: color-mix(in srgb, var(--live) 55%, var(--line));
      color: var(--live);
      background: var(--live-soft);
    }
    .voice-status {
      font: 12px/1.45 var(--font-body);
      font-variant-numeric: tabular-nums;
      color: var(--muted);
      min-height: 17px;
      margin-top: 5px;
      overflow-wrap: anywhere;
    }

    /* ---------------------------------------------------------------- buttons */
    button {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 7px 13px;
      background: var(--panel);
      color: var(--ink);
      font: 500 13px/1.2 var(--font-body);
      min-height: 35px;
      cursor: pointer;
      transition: transform 120ms var(--ease-out), border-color 140ms ease, background 140ms ease, color 140ms ease, box-shadow 140ms ease;
    }
    button:active:not(:disabled) { transform: scale(0.97); }
    @media (hover: hover) and (pointer: fine) {
      button:hover:not(:disabled) { border-color: var(--line-strong); background: var(--panel-2); }
    }
    button.primary {
      background: var(--accent);
      color: var(--accent-ink);
      border-color: var(--accent);
    }
    @media (hover: hover) and (pointer: fine) {
      button.primary:hover:not(:disabled) {
        background: color-mix(in srgb, var(--accent) 88%, var(--ink));
        border-color: color-mix(in srgb, var(--accent) 88%, var(--ink));
      }
    }
    button:disabled { cursor: default; opacity: 0.5; }
    button.icon-button {
      width: 34px;
      min-width: 34px;
      padding: 0;
      display: inline-grid;
      place-items: center;
    }
    button.icon-button svg {
      width: 16px;
      height: 16px;
      stroke: currentColor;
      stroke-width: 1.9;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
    }
    button.icon-button.copied {
      color: var(--success);
      border-color: var(--success);
      background: color-mix(in srgb, var(--success) 12%, transparent);
    }
    .actions { margin-top: 4px; }

    /* ---------------------------------------------------------------- library */
    .list-column { display: grid; gap: 18px; }
    .view-toggle {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel-2);
      padding: 3px;
      gap: 2px;
    }
    .view-toggle button {
      border: 0;
      border-radius: 7px;
      min-height: 32px;
      background: transparent;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      transition: transform 120ms var(--ease-out), background 150ms var(--ease-out), color 150ms ease, box-shadow 150ms ease;
    }
    @media (hover: hover) and (pointer: fine) {
      .view-toggle button:hover:not(:disabled):not(.active) { color: var(--ink); }
    }
    .view-toggle button.active {
      background: var(--panel);
      color: var(--ink);
      font-weight: 600;
      box-shadow: 0 1px 2px rgba(20, 26, 23, 0.10), 0 0 0 1px color-mix(in srgb, var(--line) 80%, transparent);
    }
    .signal-panel {
      display: grid;
      gap: 12px;
      padding: 14px 18px 16px;
    }
    .list-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 30px;
    }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 0;
    }
    .metric-cell {
      min-width: 0;
      padding: 2px 14px 2px 0;
      border-left: 1px solid var(--line);
      padding-left: 14px;
    }
    .metric-cell:first-child { border-left: 0; padding-left: 0; }
    .metric-value {
      font: 600 26px/1.05 var(--font-display);
      font-variation-settings: "opsz" 40;
      font-variant-numeric: tabular-nums;
      letter-spacing: -0.02em;
      color: var(--ink);
    }
    .metric-cell .meta { margin-top: 3px; }
    .topic-map { display: flex; gap: 6px; flex-wrap: wrap; }
    .topic-pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 10px;
      color: var(--muted);
      font: 12px/1.3 var(--font-body);
      font-variant-numeric: tabular-nums;
      background: var(--panel-2);
    }
    .list-block {
      display: grid;
      gap: 12px;
    }
    .list-block h2 {
      font: 600 17px/1.1 var(--font-display);
      font-variation-settings: "opsz" 40;
      letter-spacing: -0.01em;
      text-transform: none;
      color: var(--ink);
    }
    .count {
      font: 12.5px/1 var(--font-body);
      font-variant-numeric: tabular-nums;
      color: var(--muted);
    }
    .library-search {
      min-height: 36px;
      background: var(--panel);
    }
    .history {
      display: grid;
      gap: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    .history:empty { display: none; }
    .card {
      position: relative;
      border: 0;
      border-top: 1px solid var(--line);
      background: transparent;
      padding: 14px 16px 14px 20px;
      display: grid;
      gap: 8px;
    }
    .card:first-child { border-top: 0; }
    .card::before {
      content: "";
      position: absolute;
      left: 0;
      top: 10px;
      bottom: 10px;
      width: 3px;
      border-radius: 0 3px 3px 0;
      background: transparent;
    }
    .card { transition: background 150ms var(--ease-out); }
    .card.active { background: color-mix(in srgb, var(--accent-soft) 45%, var(--panel)); }
    .card.active::before { background: var(--accent); }
    .card-top {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
    }
    .card-actions {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .title {
      font-weight: 600;
      font-size: 14px;
      line-height: 1.35;
      overflow-wrap: anywhere;
      text-wrap: pretty;
    }
    .meta, .snippet {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .meta {
      font: 12px/1.4 var(--font-body);
      font-variant-numeric: tabular-nums;
    }
    .snippet { line-height: 1.5; }
    .dictation-text {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      color: var(--ink);
      font-size: 14px;
      line-height: 1.55;
      max-width: 68ch;
    }
    .dictation-edit {
      min-height: 160px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .empty {
      color: var(--muted);
      border: 1px dashed var(--line-strong);
      border-radius: 12px;
      padding: 26px 18px;
      text-align: center;
      font-size: 13px;
    }
    .history .empty { border: 0; border-radius: 0; }
    .error {
      color: var(--warn);
      font-size: 12.5px;
      min-height: 18px;
    }
    .error:empty { display: none; }

    /* ---------------------------------------------------------------- microphone */
    .mic-meter {
      --level: 0;
      height: 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-2);
      overflow: hidden;
      margin-top: 8px;
    }
    .mic-meter > div {
      width: calc(var(--level) * 100%);
      min-width: 2px;
      max-width: 100%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent) 0%, var(--accent) 60%, var(--live) 100%);
      transition: width 80ms linear;
    }
    .mic-meter.active { border-color: color-mix(in srgb, var(--live) 60%, var(--line)); }
    .recording-debug {
      display: grid;
      gap: 6px;
      border: 1px solid color-mix(in srgb, var(--live) 40%, var(--line));
      border-radius: 10px;
      padding: 10px 12px;
      background: var(--live-soft);
      color: var(--muted);
      font: 12px/1.45 var(--font-body);
      font-variant-numeric: tabular-nums;
    }
    .recording-debug[hidden] { display: none; }
    .recording-debug strong {
      color: var(--ink);
      font: 600 12.5px/1.2 var(--font-body);
    }
    .recording-debug audio { width: 100%; height: 32px; }

    [hidden] { display: none !important; }

    @media (prefers-contrast: more) {
      .panel, .history, .view-toggle, textarea, select, .library-search, button, input[type="file"] {
        border-color: var(--ink);
      }
      header { background: var(--bg); -webkit-backdrop-filter: none; backdrop-filter: none; }
    }

    @media (max-width: 860px) {
      main { padding: 18px 16px 40px; }
      header { align-items: flex-start; flex-direction: column; gap: 8px; margin: 0 -16px 16px; padding: 12px 16px; }
      .status-wrap { max-width: 100%; }
      .status { text-align: left; white-space: normal; }
      .grid { grid-template-columns: 1fr; }
      .audio-upload-row { grid-template-columns: 1fr; }
      .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 12px; }
      .metric-cell:nth-child(3) { border-left: 0; padding-left: 0; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div class="brand">
        <h1>Doc Reader</h1>
      </div>
      <div class="status-wrap">
        <span class="status-dot" aria-hidden="true"></span>
        <div class="status" id="status" aria-live="polite">Ready.</div>
      </div>
    </header>
    <div class="grid">
      <section class="panel stack">
        <div>
          <label for="file">Document</label>
          <input id="file" type="file" accept=".pdf,.docx,.txt,.md,.markdown">
        </div>
        <div>
          <label for="audioFile">Audio</label>
          <div class="audio-upload-row">
            <input id="audioFile" type="file" accept="audio/*,video/mp4,video/webm,.aac,.aif,.aiff,.flac,.m4a,.mp3,.mp4,.ogg,.wav,.webm">
            <div class="check-row">
              <input id="audioTimestamps" type="checkbox">
              <label for="audioTimestamps">Timestamps</label>
            </div>
          </div>
          <div class="voice-status" id="audioFileStatus"></div>
        </div>
        <div>
          <label for="text">Text</label>
          <textarea id="text" placeholder="Paste or type anything to hear it read aloud."></textarea>
        </div>
        <div class="row actions">
          <button class="primary" id="readText">Read text</button>
          <button id="pause">Pause</button>
          <button id="stop">Stop</button>
        </div>
        <div class="error" id="error"></div>
        <hr class="rule">
        <div>
          <label for="voice">Voice</label>
          <select id="voice"></select>
          <div class="voice-status" id="voiceStatus"></div>
        </div>
        <div>
          <div class="range-head">
            <label for="readRate">Read speed</label>
            <output class="range-value" id="readRateValue" for="readRate">180 WPM / 1.00x</output>
          </div>
          <input id="readRate" type="range" min="90" max="300" step="5" value="180">
        </div>
        <hr class="rule">
        <div>
          <div class="row service-row">
            <div class="check-row">
              <input id="dictationEnabled" type="checkbox">
              <label for="dictationEnabled">Dictation</label>
            </div>
            <div class="service-actions">
              <button id="nativeHelperToggle" class="service-toggle" type="button">Start helper</button>
              <button id="nativeHelperReset" class="service-reset" type="button" title="Restart the hotkey helper">Reset</button>
            </div>
          </div>
          <div class="voice-status" id="dictationStatus"></div>
          <div class="mic-meter" id="dictationMeter" aria-label="Microphone level"><div></div></div>
        </div>
        <div>
          <label for="microphone">Microphone</label>
          <select id="microphone"></select>
          <div class="voice-status" id="microphoneStatus"></div>
        </div>
        <div class="recording-debug" id="dictationRecordingDebug" hidden>
          <strong>Latest recording</strong>
          <div id="dictationRecordingStatus"></div>
          <audio id="dictationRecordingAudio" controls preload="none"></audio>
        </div>
      </section>
      <section class="list-column">
        <div class="view-toggle" role="tablist" aria-label="History view">
          <button id="showAll" type="button" role="tab" aria-controls="libraryBlock">All</button>
          <button id="showReadings" type="button" role="tab" aria-controls="libraryBlock">Readings</button>
          <button id="showDictations" type="button" role="tab" aria-controls="libraryBlock">Dictations</button>
          <button id="showClawdad" type="button" role="tab" aria-controls="libraryBlock">Clawdad</button>
        </div>
        <div class="panel signal-panel">
          <div class="list-header">
            <h2>Signal map</h2>
            <button id="runAnalysis" type="button">Analyze</button>
          </div>
          <div class="metric-grid">
            <div class="metric-cell">
              <div class="metric-value" id="sttWords">0</div>
              <div class="meta">STT words</div>
            </div>
            <div class="metric-cell">
              <div class="metric-value" id="ttsWords">0</div>
              <div class="meta">TTS words</div>
            </div>
            <div class="metric-cell">
              <div class="metric-value" id="analyzedItems">0</div>
              <div class="meta">Analyzed</div>
            </div>
            <div class="metric-cell">
              <div class="metric-value" id="openItems">0</div>
              <div class="meta">Open</div>
            </div>
          </div>
          <div class="snippet" id="analysisSummary"></div>
          <div class="topic-map" id="topicMap"></div>
        </div>
        <div class="list-block" id="libraryBlock">
          <div class="list-header">
            <h2 id="libraryTitle">Library</h2>
            <div class="count" id="libraryCount"></div>
          </div>
          <input id="librarySearch" class="library-search" type="search" placeholder="Filter by title or text">
          <div class="history" id="library"></div>
        </div>
      </section>
    </div>
  </main>
  <script>
    const state = {
      data: null,
      editingItemId: "",
      editingText: "",
      editingSavingId: "",
      libraryPointerSelecting: false,
      libraryRenderDeferred: false,
      librarySelectionFlushTimer: null
    };
    const statusEl = document.getElementById("status");
    const libraryEl = document.getElementById("library");
    const libraryCountEl = document.getElementById("libraryCount");
    const libraryTitleEl = document.getElementById("libraryTitle");
    const librarySearchEl = document.getElementById("librarySearch");
    const errorEl = document.getElementById("error");
    const textEl = document.getElementById("text");
    const fileEl = document.getElementById("file");
    const audioFileEl = document.getElementById("audioFile");
    const audioTimestampsEl = document.getElementById("audioTimestamps");
    const audioFileStatusEl = document.getElementById("audioFileStatus");
    const pauseBtn = document.getElementById("pause");
    const stopBtn = document.getElementById("stop");
    const voiceEl = document.getElementById("voice");
    const voiceStatusEl = document.getElementById("voiceStatus");
    const readRateEl = document.getElementById("readRate");
    const readRateValueEl = document.getElementById("readRateValue");
    const dictationEnabledEl = document.getElementById("dictationEnabled");
    const dictationStatusEl = document.getElementById("dictationStatus");
    const dictationMeterEl = document.getElementById("dictationMeter");
    const dictationRecordingDebugEl = document.getElementById("dictationRecordingDebug");
    const dictationRecordingStatusEl = document.getElementById("dictationRecordingStatus");
    const dictationRecordingAudioEl = document.getElementById("dictationRecordingAudio");
    const nativeHelperToggleEl = document.getElementById("nativeHelperToggle");
    const nativeHelperResetEl = document.getElementById("nativeHelperReset");
    const microphoneEl = document.getElementById("microphone");
    const microphoneStatusEl = document.getElementById("microphoneStatus");
    const showAllBtn = document.getElementById("showAll");
    const showReadingsBtn = document.getElementById("showReadings");
    const showDictationsBtn = document.getElementById("showDictations");
    const showClawdadBtn = document.getElementById("showClawdad");
    const runAnalysisBtn = document.getElementById("runAnalysis");
    const sttWordsEl = document.getElementById("sttWords");
    const ttsWordsEl = document.getElementById("ttsWords");
    const analyzedItemsEl = document.getElementById("analyzedItems");
    const openItemsEl = document.getElementById("openItems");
    const analysisSummaryEl = document.getElementById("analysisSummary");
    const topicMapEl = document.getElementById("topicMap");
    state.audioFileAction = "";
    state.nativeHelperAction = "";
    audioTimestampsEl.checked = localStorage.getItem("docReader.audioTimestamps") === "true";
    state.activeView = localStorage.getItem("docReader.historyView") || "all";
    state.libraryQuery = localStorage.getItem("docReader.libraryQuery") || "";
    librarySearchEl.value = state.libraryQuery;

    async function api(path, options = {}) {
      const response = await fetch(path, options);
      const raw = await response.text();
      let payload = {};
      if (raw.trim()) {
        try {
          payload = JSON.parse(raw);
        } catch (_error) {
          throw new Error(raw.slice(0, 180) || `HTTP ${response.status}`);
        }
      } else if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || "Request failed.");
      }
      return payload;
    }

    function timeLabel(seconds) {
      const total = Math.max(0, Math.floor(seconds || 0));
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const rest = total % 60;
      if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
      return `${minutes}m ${String(rest).padStart(2, "0")}s`;
    }

    function byteLabel(bytes) {
      const value = Math.max(0, Number(bytes || 0));
      if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
      if (value >= 1024) return `${Math.round(value / 1024)} KB`;
      return `${Math.round(value)} B`;
    }

    function numberLabel(value) {
      return new Intl.NumberFormat().format(Math.max(0, Math.round(Number(value || 0))));
    }

    function render(data) {
      const previousDictationCount = state.data && state.data.dictations
        ? state.data.dictations.length
        : 0;
      state.data = data;
      statusEl.textContent = data.status || "Ready.";
      document.body.dataset.state = data.running ? "reading" : (data.paused ? "paused" : "idle");
      renderVoice(data.tts || {});
      renderReadRate(data.settings || {});
      renderDictation(data.stt || {});
      renderSignalMap(data.metrics || {}, data.analysis || {});
      pauseBtn.disabled = !data.running && !data.paused;
      pauseBtn.textContent = data.paused ? "Resume" : "Pause";
      stopBtn.disabled = !data.running && !data.paused;

      const library = data.library || data.items || [];
      const dictations = data.dictations || [];
      const preserveLibraryDom = shouldPreserveLibraryDom();
      if (!preserveLibraryDom && dictations.length > previousDictationCount) {
        setActiveView("dictations");
        return;
      }
      if (preserveLibraryDom) {
        state.libraryRenderDeferred = true;
      } else {
        renderLibrary(library);
      }
    }

    function renderSignalMap(metrics, analysis) {
      const styleMap = analysis.style_map || {};
      const completion = styleMap.completion || {};
      sttWordsEl.textContent = numberLabel(metrics.stt_words);
      ttsWordsEl.textContent = numberLabel(metrics.tts_words);
      analyzedItemsEl.textContent = numberLabel(analysis.items_analyzed);
      openItemsEl.textContent = numberLabel(completion.open || 0);
      runAnalysisBtn.disabled = !!analysis.running;
      runAnalysisBtn.textContent = analysis.running ? "Analyzing" : "Analyze";
      const pending = Number(analysis.pending_items || 0);
      const summary = analysis.latest_summary || "";
      const backend = analysis.backend ? `${analysis.backend} / ${analysis.model || "local"}` : "local";
      analysisSummaryEl.textContent = summary
        ? `${summary} / ${backend} / ${numberLabel(pending)} pending`
        : `${backend} / ${numberLabel(pending)} pending`;
      topicMapEl.innerHTML = "";
      const topics = Array.isArray(styleMap.top_topics) ? styleMap.top_topics.slice(0, 8) : [];
      for (const topic of topics) {
        const pill = document.createElement("span");
        pill.className = "topic-pill";
        pill.textContent = `${topic.term} ${topic.count}`;
        topicMapEl.appendChild(pill);
      }
    }

    function setActiveView(view) {
      state.activeView = ["readings", "dictations", "clawdad"].includes(view) ? view : "all";
      localStorage.setItem("docReader.historyView", state.activeView);
      renderLibraryFromState();
    }

    function filteredLibraryItems(items) {
      const query = String(state.libraryQuery || "").trim().toLowerCase();
      return items.filter((item) => {
        if (state.activeView === "readings" && (isDictationItem(item) || isClawdadItem(item))) return false;
        if (state.activeView === "dictations" && !isDictationItem(item)) return false;
        if (state.activeView === "clawdad" && !isClawdadItem(item)) return false;
        if (!query) return true;
        return [item.title, item.snippet, item.kind, item.source]
          .some((value) => String(value || "").toLowerCase().includes(query));
      });
    }

    function renderLibrary(items) {
      state.libraryRenderDeferred = false;
      const allItems = Array.isArray(items) ? items : [];
      const filtered = filteredLibraryItems(allItems);
      libraryEl.innerHTML = "";
      libraryCountEl.textContent = `${countLabel(filtered.length)} / ${allItems.length} total`;
      libraryTitleEl.textContent =
        state.activeView === "readings" ? "Readings" :
        state.activeView === "dictations" ? "Dictations" :
        state.activeView === "clawdad" ? "Clawdad" :
        "Library";
      showAllBtn.classList.toggle("active", state.activeView === "all");
      showReadingsBtn.classList.toggle("active", state.activeView === "readings");
      showDictationsBtn.classList.toggle("active", state.activeView === "dictations");
      showClawdadBtn.classList.toggle("active", state.activeView === "clawdad");
      showAllBtn.setAttribute("aria-selected", String(state.activeView === "all"));
      showReadingsBtn.setAttribute("aria-selected", String(state.activeView === "readings"));
      showDictationsBtn.setAttribute("aria-selected", String(state.activeView === "dictations"));
      showClawdadBtn.setAttribute("aria-selected", String(state.activeView === "clawdad"));
      showAllBtn.textContent = `All ${allItems.length}`;
      showReadingsBtn.textContent = `Readings ${allItems.filter((item) => !isDictationItem(item) && !isClawdadItem(item)).length}`;
      showDictationsBtn.textContent = `Dictations ${allItems.filter(isDictationItem).length}`;
      showClawdadBtn.textContent = `Clawdad ${allItems.filter(isClawdadItem).length}`;
      if (filtered.length === 0) {
        libraryEl.appendChild(emptyCard("No matching library cards."));
        return;
      }

      for (const item of filtered) {
        libraryEl.appendChild(makeLibraryCard(item));
      }
    }

    function renderLibraryFromState() {
      const data = state.data || {};
      renderLibrary(data.library || data.items || []);
    }

    function shouldPreserveLibraryDom() {
      return !!state.editingItemId || state.libraryPointerSelecting || libraryHasTextSelection();
    }

    function libraryHasTextSelection() {
      const selection = window.getSelection ? window.getSelection() : null;
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
      return nodeInsideLibrary(selection.anchorNode) || nodeInsideLibrary(selection.focusNode);
    }

    function nodeInsideLibrary(node) {
      if (!node) return false;
      const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
      return !!element && libraryEl.contains(element);
    }

    function flushDeferredLibraryRender() {
      if (!state.libraryRenderDeferred || shouldPreserveLibraryDom()) return;
      renderLibraryFromState();
    }

    function queueDeferredLibraryFlush(delay = 140) {
      if (state.librarySelectionFlushTimer) {
        window.clearTimeout(state.librarySelectionFlushTimer);
      }
      state.librarySelectionFlushTimer = window.setTimeout(() => {
        state.librarySelectionFlushTimer = null;
        flushDeferredLibraryRender();
      }, delay);
    }

    function isDictationItem(item) {
      return item && (item.kind === "dictation" || String(item.title || "").startsWith("Dictation:"));
    }

    function isClawdadItem(item) {
      return item && (item.source === "clawdad" || String(item.kind || "").startsWith("clawdad-"));
    }

    function countLabel(count) {
      return `${count} ${count === 1 ? "card" : "cards"}`;
    }

    function emptyCard(text) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = text;
      return empty;
    }

    function makeLibraryCard(item) {
      if (isDictationItem(item)) {
        return makeDictationCard(item);
      }
      return makeReadingCard(item);
    }

    function makeReadingCard(item) {
      const card = document.createElement("article");
      card.className = "card" + (item.playing || item.paused ? " active" : "");

      const top = document.createElement("div");
      top.className = "card-top";

      const info = document.createElement("div");
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = item.title;
      const meta = document.createElement("div");
      meta.className = "meta";
      const audio = item.audio || {};
      const audioLabel = audio.state && audio.state !== "none" ? ` / audio ${audio.state}` : "";
      const sourceLabel = isClawdadItem(item) ? "Clawdad" : (item.kind === "document" ? "Document" : "Text");
      const wordsLabel = item.word_count ? ` / ${numberLabel(item.word_count)} words` : "";
      meta.textContent = `${sourceLabel} / ${item.completed ? "Complete" : timeLabel(item.last_seconds)}${wordsLabel}${audioLabel}`;
      info.append(title, meta);

      const play = document.createElement("button");
      play.textContent = item.playing ? "Pause" : (item.paused ? "Resume" : "Play");
      play.className = item.playing || item.paused ? "" : "primary";
      play.addEventListener("click", async () => {
        try {
          errorEl.textContent = "";
          if (item.playing || item.paused) {
            render(await api(item.playing ? "/api/pause" : `/api/items/${encodeURIComponent(item.id)}/play`, { method: "POST" }));
          } else {
            render(await api(`/api/items/${encodeURIComponent(item.id)}/play`, { method: "POST" }));
          }
        } catch (error) {
          errorEl.textContent = error.message;
        }
      });

      top.append(info, play);

      const snippet = document.createElement("div");
      snippet.className = "snippet";
      snippet.textContent = item.snippet || item.source_path || "";
      card.append(top, snippet);
      return card;
    }

    function makeDictationCard(item) {
      const card = document.createElement("article");
      card.className = "card";

      const top = document.createElement("div");
      top.className = "card-top";

      const info = document.createElement("div");
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = item.title;
      const meta = document.createElement("div");
      meta.className = "meta";
      const wordsLabel = item.word_count ? ` / ${numberLabel(item.word_count)} words` : "";
      meta.textContent = `${isClawdadItem(item) ? "Clawdad dictation" : "Dictation"}${wordsLabel}`;
      info.append(title, meta);

      const actions = document.createElement("div");
      actions.className = "card-actions";
      const editing = state.editingItemId === item.id;
      const saving = state.editingSavingId === item.id;
      if (editing) {
        const save = document.createElement("button");
        save.className = "icon-button primary";
        save.type = "button";
        save.title = saving ? "Saving dictation" : "Save dictation";
        save.disabled = saving;
        save.setAttribute("aria-label", save.title);
        save.innerHTML = icon("save");
        save.addEventListener("click", () => saveDictationEdit(item));

        const cancel = document.createElement("button");
        cancel.className = "icon-button";
        cancel.type = "button";
        cancel.title = "Cancel edit";
        cancel.disabled = saving;
        cancel.setAttribute("aria-label", "Cancel edit");
        cancel.innerHTML = icon("x");
        cancel.addEventListener("click", cancelDictationEdit);
        actions.append(save, cancel);
      } else {
        const copy = document.createElement("button");
        copy.className = "icon-button";
        copy.type = "button";
        copy.title = "Copy dictation";
        copy.disabled = !!state.editingItemId;
        copy.setAttribute("aria-label", "Copy dictation");
        copy.innerHTML = icon("copy");
        copy.addEventListener("click", async () => {
          try {
            errorEl.textContent = "";
            const payload = await api(`/api/items/${encodeURIComponent(item.id)}/text`);
            await navigator.clipboard.writeText(payload.text || "");
            showCopied(copy);
          } catch (error) {
            errorEl.textContent = error.message;
          }
        });

        const edit = document.createElement("button");
        edit.className = "icon-button";
        edit.type = "button";
        edit.title = "Edit dictation";
        edit.disabled = !!state.editingItemId;
        edit.setAttribute("aria-label", "Edit dictation");
        edit.innerHTML = icon("edit");
        edit.addEventListener("click", () => beginDictationEdit(item));
        actions.append(copy, edit);
      }

      top.append(info, actions);

      if (editing) {
        const editor = document.createElement("textarea");
        editor.className = "dictation-edit";
        editor.dataset.itemId = item.id;
        editor.value = state.editingText;
        editor.disabled = saving;
        editor.addEventListener("input", () => {
          state.editingText = editor.value;
        });
        editor.addEventListener("keydown", (event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            saveDictationEdit(item);
          }
        });
        card.append(top, editor);
      } else {
        const snippet = document.createElement("div");
        snippet.className = "dictation-text";
        snippet.textContent = item.text || item.snippet || "";
        card.append(top, snippet);
      }
      return card;
    }

    function beginDictationEdit(item) {
      state.editingItemId = item.id;
      state.editingText = item.text || item.snippet || "";
      state.editingSavingId = "";
      renderLibrary((state.data && (state.data.library || state.data.items)) || []);
      window.requestAnimationFrame(() => {
        const editor = Array.from(libraryEl.querySelectorAll("textarea.dictation-edit"))
          .find((element) => element.dataset.itemId === item.id);
        if (!editor) return;
        editor.focus();
        editor.setSelectionRange(editor.value.length, editor.value.length);
      });
    }

    function cancelDictationEdit() {
      state.editingItemId = "";
      state.editingText = "";
      state.editingSavingId = "";
      renderLibrary((state.data && (state.data.library || state.data.items)) || []);
    }

    async function saveDictationEdit(item) {
      const text = String(state.editingText || "").trim();
      if (!text) {
        errorEl.textContent = "Dictation text cannot be empty.";
        return;
      }
      try {
        errorEl.textContent = "";
        state.editingSavingId = item.id;
        renderLibrary((state.data && (state.data.library || state.data.items)) || []);
        const payload = await api(`/api/items/${encodeURIComponent(item.id)}/text`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text })
        });
        state.editingItemId = "";
        state.editingText = "";
        state.editingSavingId = "";
        render(payload.state || state.data || {});
      } catch (error) {
        state.editingSavingId = "";
        renderLibrary((state.data && (state.data.library || state.data.items)) || []);
        errorEl.textContent = error.message;
      }
    }

    function showCopied(button) {
      button.classList.add("copied");
      button.innerHTML = icon("check");
      window.setTimeout(() => {
        button.classList.remove("copied");
        button.innerHTML = icon("copy");
      }, 1100);
    }

    function icon(name) {
      if (name === "check") {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
      }
      if (name === "edit") {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
      }
      if (name === "save") {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>';
      }
      if (name === "x") {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
      }
      return '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="10" height="10" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1"/></svg>';
    }

    function renderVoice(tts) {
      const current = tts.backend || "auto";
      const options = tts.options || [];
      if (voiceEl.dataset.loaded !== "true") {
        voiceEl.innerHTML = "";
        for (const option of options) {
          const entry = document.createElement("option");
          entry.value = option.value;
          entry.textContent = option.label;
          voiceEl.appendChild(entry);
        }
        voiceEl.dataset.loaded = "true";
      }
      voiceEl.value = current;
      const services = tts.services || {};
      const remoteSpeech = services.umbra && services.umbra.ok ? "remote speech online" : "remote speech offline";
      const localSpeech = services.mac && services.mac.ok ? "local speech online" : "local speech offline";
      voiceStatusEl.textContent = `${tts.label || current} / ${localSpeech} / ${remoteSpeech}`;
    }

    function normalizeReadRate(value) {
      const min = Number(readRateEl.min || 90);
      const max = Number(readRateEl.max || 300);
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return 180;
      return Math.max(min, Math.min(max, Math.round(parsed)));
    }

    function readRateLabel(rate) {
      return `${rate} WPM / ${(rate / 180).toFixed(2)}x`;
    }

    function renderReadRate(settings) {
      const rate = normalizeReadRate(settings.read_rate || settings.readRate || 180);
      if (document.activeElement !== readRateEl) {
        readRateEl.value = String(rate);
      }
      readRateValueEl.value = readRateLabel(normalizeReadRate(readRateEl.value));
      readRateValueEl.textContent = readRateValueEl.value;
    }

    function renderDictation(stt) {
      dictationEnabledEl.checked = !!stt.enabled;
      const service = stt.service || {};
      const backendLabel = stt.backend === "mac-whisper"
        ? "local speech"
        : (stt.backend === "custom-whisper" ? "speech service" : "remote speech");
      const serviceLabel = service.ok ? `${backendLabel} online` : `${backendLabel} offline`;
      const modelLabel = stt.loaded ? "speech loaded" : (stt.ready ? "speech ready" : "speech unavailable");
      const mic = stt.microphone || {};
      renderMicrophones(mic);
      renderNativeHelperToggle(mic);
      const helperLabel = mic.recording
        ? "recording"
        : (
          mic.recording_start_pending
            ? "starting recorder"
            : (state.nativeHelperAction || (mic.native_helper_online ? "helper online" : "helper offline"))
        );
      const inputLabel = mic.input_monitoring_trusted ? "hotkey allowed" : "allow Input Monitoring";
      dictationStatusEl.textContent = `${stt.label || "Speech-to-text"} / ${serviceLabel} / ${modelLabel} / ${helperLabel} / ${inputLabel}`;
      const level = Math.max(0, Math.min(1, Number(mic.audio_level || 0)));
      const peak = Math.max(0, Math.min(1, Number(mic.audio_peak_level || 0)));
      dictationMeterEl.style.setProperty("--level", String(level));
      dictationMeterEl.classList.toggle("active", !!mic.recording || !!mic.recording_start_pending);
      dictationMeterEl.title = `Mic level ${Math.round(level * 100)}%, peak ${Math.round(peak * 100)}%`;
      renderLastRecording(mic.last_recording || {});
      renderAudioFileStatus(stt);
    }

    function renderAudioFileStatus(stt) {
      const busy = !!state.audioFileAction;
      const available = !!stt.enabled && !!stt.ready;
      const label = stt.label || "Speech-to-text";
      audioFileEl.disabled = busy || !available;
      audioFileStatusEl.textContent = state.audioFileAction || (
        available ? `${label} ready` : (stt.enabled ? `${label} unavailable` : "Speech-to-text off")
      );
    }

    function renderNativeHelperToggle(mic) {
      const busy = !!state.nativeHelperAction;
      const online = !!mic.native_helper_online;
      nativeHelperToggleEl.disabled = busy;
      nativeHelperResetEl.disabled = busy;
      nativeHelperToggleEl.classList.toggle("running", online);
      if (state.nativeHelperAction === "starting helper") {
        nativeHelperToggleEl.textContent = "Starting...";
      } else if (state.nativeHelperAction === "stopping helper") {
        nativeHelperToggleEl.textContent = "Stopping...";
      } else if (state.nativeHelperAction === "resetting helper") {
        nativeHelperToggleEl.textContent = "Resetting...";
      } else {
        nativeHelperToggleEl.textContent = online ? "Stop Helper" : "Start Helper";
      }
      nativeHelperResetEl.textContent = state.nativeHelperAction === "resetting helper"
        ? "Resetting..."
        : "Reset";
    }

    function renderLastRecording(recording) {
      const hasRecording = !!recording.path && Number(recording.bytes || 0) > 0;
      dictationRecordingDebugEl.hidden = !hasRecording;
      if (!hasRecording) {
        dictationRecordingAudioEl.removeAttribute("src");
        dictationRecordingAudioEl.dataset.path = "";
        return;
      }
      const peak = Math.round(Math.max(0, Math.min(1, Number(recording.peak_level || 0))) * 100);
      dictationRecordingStatusEl.textContent =
        `${byteLabel(recording.bytes)} / ${timeLabel(recording.seconds)} / peak ${peak}%`;
      if (dictationRecordingAudioEl.dataset.path !== recording.path) {
        dictationRecordingAudioEl.src = `/api/dictation/last-recording?t=${encodeURIComponent(String(recording.created_at || Date.now()))}`;
        dictationRecordingAudioEl.dataset.path = recording.path;
      }
    }

    function renderMicrophones(mic) {
      const devices = mic.devices || [{ id: "", name: "System Default" }];
      const signature = JSON.stringify(devices);
      if (microphoneEl.dataset.signature !== signature) {
        microphoneEl.innerHTML = "";
        for (const device of devices) {
          const entry = document.createElement("option");
          entry.value = device.id || "";
          entry.textContent = device.name || "System Default";
          microphoneEl.appendChild(entry);
        }
        microphoneEl.dataset.signature = signature;
      }
      microphoneEl.value = mic.selected_id || "";
      const selected = mic.selected_name || "System Default";
      const permission = mic.authorization === "authorized" ? "mic allowed" : `mic ${mic.authorization || "unknown"}`;
      const accessibility = mic.accessibility_trusted ? "paste allowed" : "allow Accessibility";
      const helper = mic.native_helper_online ? "native helper online" : "native helper offline";
      const lastEvent = mic.last_event ? ` / ${mic.last_event}` : "";
      microphoneStatusEl.textContent = `${selected} / ${permission} / ${accessibility} / ${helper}${lastEvent}`;
    }

    async function refresh() {
      try {
        render(await api("/api/state"));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    }

    document.getElementById("readText").addEventListener("click", async () => {
      try {
        errorEl.textContent = "";
        render(await api("/api/text", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: "Text", text: textEl.value })
        }));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    });

    fileEl.addEventListener("change", async () => {
      const file = fileEl.files && fileEl.files[0];
      if (!file) return;
      const body = new FormData();
      body.append("file", file);
      try {
        errorEl.textContent = "";
        render(await api("/api/upload", { method: "POST", body }));
      } catch (error) {
        errorEl.textContent = error.message;
      } finally {
        fileEl.value = "";
      }
    });

    audioFileEl.addEventListener("change", async () => {
      const file = audioFileEl.files && audioFileEl.files[0];
      if (!file) return;
      const body = new FormData();
      body.append("file", file);
      try {
        errorEl.textContent = "";
        state.audioFileAction = `Transcribing ${file.name}`;
        renderAudioFileStatus((state.data && state.data.stt) || {});
        const payload = await api("/api/audio/transcribe", {
          method: "POST",
          headers: { "X-Doc-Reader-Timestamps": audioTimestampsEl.checked ? "1" : "0" },
          body
        });
        render(payload.state || payload);
      } catch (error) {
        errorEl.textContent = error.message;
      } finally {
        state.audioFileAction = "";
        audioFileEl.value = "";
        renderAudioFileStatus((state.data && state.data.stt) || {});
      }
    });

    audioTimestampsEl.addEventListener("change", () => {
      localStorage.setItem("docReader.audioTimestamps", String(audioTimestampsEl.checked));
    });

    pauseBtn.addEventListener("click", async () => {
      try {
        errorEl.textContent = "";
        const paused = state.data && state.data.paused;
        const active = state.data && state.data.active_id;
        render(await api(paused && active ? `/api/items/${encodeURIComponent(active)}/play` : "/api/pause", { method: "POST" }));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    });

    stopBtn.addEventListener("click", async () => {
      try {
        errorEl.textContent = "";
        render(await api("/api/stop", { method: "POST" }));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    });

    voiceEl.addEventListener("change", async () => {
      try {
        errorEl.textContent = "";
        render(await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ speech_backend: voiceEl.value })
        }));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    });

    let readRateSaveTimer = null;
    async function saveReadRate() {
      if (readRateSaveTimer) {
        window.clearTimeout(readRateSaveTimer);
        readRateSaveTimer = null;
      }
      try {
        errorEl.textContent = "";
        renderReadRate({ read_rate: readRateEl.value });
        render(await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ read_rate: normalizeReadRate(readRateEl.value) })
        }));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    }

    readRateEl.addEventListener("input", () => {
      renderReadRate({ read_rate: readRateEl.value });
      if (readRateSaveTimer) {
        window.clearTimeout(readRateSaveTimer);
      }
      readRateSaveTimer = window.setTimeout(saveReadRate, 350);
    });

    readRateEl.addEventListener("change", saveReadRate);

    dictationEnabledEl.addEventListener("change", async () => {
      try {
        errorEl.textContent = "";
        render(await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ stt_enabled: dictationEnabledEl.checked })
        }));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    });

    microphoneEl.addEventListener("change", async () => {
      try {
        errorEl.textContent = "";
        render(await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ microphone_id: microphoneEl.value })
        }));
      } catch (error) {
        errorEl.textContent = error.message;
      }
    });

    nativeHelperToggleEl.addEventListener("click", async () => {
      try {
        errorEl.textContent = "";
        const mic = state.data && state.data.stt && state.data.stt.microphone
          ? state.data.stt.microphone
          : {};
        const online = !!mic.native_helper_online;
        state.nativeHelperAction = online ? "stopping helper" : "starting helper";
        renderNativeHelperToggle(mic);
        await api(online ? "/api/native/stop" : "/api/native/start", { method: "POST" });
        render(await api("/api/state"));
      } catch (error) {
        errorEl.textContent = error.message;
      } finally {
        state.nativeHelperAction = "";
        if (state.data && state.data.stt) {
          renderDictation(state.data.stt);
        }
      }
    });

    nativeHelperResetEl.addEventListener("click", async () => {
      try {
        errorEl.textContent = "";
        const mic = state.data && state.data.stt && state.data.stt.microphone
          ? state.data.stt.microphone
          : {};
        state.nativeHelperAction = "resetting helper";
        renderNativeHelperToggle(mic);
        await api("/api/native/reset", { method: "POST" });
        render(await api("/api/state"));
      } catch (error) {
        errorEl.textContent = error.message;
      } finally {
        state.nativeHelperAction = "";
        if (state.data && state.data.stt) {
          renderDictation(state.data.stt);
        }
      }
    });

    runAnalysisBtn.addEventListener("click", async () => {
      try {
        errorEl.textContent = "";
        const payload = await api("/api/library/analysis/run", { method: "POST" });
        if (state.data) {
          state.data.analysis = payload.analysis || state.data.analysis;
          renderSignalMap(state.data.metrics || {}, state.data.analysis || {});
        }
      } catch (error) {
        errorEl.textContent = error.message;
      }
    });

    showAllBtn.addEventListener("click", () => setActiveView("all"));
    showReadingsBtn.addEventListener("click", () => setActiveView("readings"));
    showDictationsBtn.addEventListener("click", () => setActiveView("dictations"));
    showClawdadBtn.addEventListener("click", () => setActiveView("clawdad"));
    librarySearchEl.addEventListener("input", () => {
      state.libraryQuery = librarySearchEl.value;
      localStorage.setItem("docReader.libraryQuery", state.libraryQuery);
      renderLibrary((state.data && (state.data.library || state.data.items)) || []);
    });

    libraryEl.addEventListener("pointerdown", (event) => {
      if (event.target && event.target.closest && event.target.closest("button, input, textarea, select, a")) {
        return;
      }
      state.libraryPointerSelecting = true;
    });

    document.addEventListener("pointerup", () => {
      if (!state.libraryPointerSelecting) return;
      window.setTimeout(() => {
        state.libraryPointerSelecting = false;
        queueDeferredLibraryFlush();
      }, 80);
    });

    document.addEventListener("pointercancel", () => {
      state.libraryPointerSelecting = false;
      queueDeferredLibraryFlush();
    });

    document.addEventListener("selectionchange", () => {
      if (!state.libraryRenderDeferred) return;
      queueDeferredLibraryFlush();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (state.editingItemId) {
          event.preventDefault();
          cancelDictationEdit();
          return;
        }
        if (document.activeElement && document.activeElement.blur) {
          document.activeElement.blur();
        }
      }
    });

    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Doc Reader web app.")
    parser.add_argument("--host", default=os.getenv("DOC_READER_WEB_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DOC_READER_WEB_PORT", DEFAULT_PORT)))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    reader = ReaderService(_managed_root())
    stop_event = threading.Event()
    server = DocReaderHTTPServer((args.host, args.port), DocReaderHandler, reader)

    def drain_loop() -> None:
        while not stop_event.is_set():
            reader.drain_service_inbox()
            stop_event.wait(1.0)

    threading.Thread(target=drain_loop, name="doc-reader-service-inbox", daemon=True).start()

    def handle_signal(_signum, _frame) -> None:
        stop_event.set()
        reader.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print(f"[doc-reader-web] listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        reader.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
