from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .platform_tools import configure_espeak, configure_windows_dll_search


DEFAULT_ENGINES = ("chatterbox", "kokoro")
DEFAULT_KOKORO_VOICE = "af_heart"
DEFAULT_CHATTERBOX_VOICE = "stock"
DEFAULT_STT_MODEL = "large-v3"
DEFAULT_STT_BEAM_SIZE = 1
WAV_MIME = "audio/wav"
DEFAULT_CHATTERBOX_MAX_SEGMENT_CHARS = 260
DEFAULT_KOKORO_MAX_SEGMENT_CHARS = 700
KOKORO_RETRY_MAX_SEGMENT_CHARS = 240
DEFAULT_SEGMENT_PAUSE_SECONDS = 0.14
MIN_TTS_SPEED = 0.5
MAX_TTS_SPEED = 2.0
URL_RE = re.compile(r"https?://\S+")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")
PHONEMIZER_LINE_MISMATCH = "number of lines in input and output must be equal"


@dataclass
class SynthesisResult:
    audio: bytes
    sample_rate: int
    audio_seconds: float
    engine: str
    voice: str
    generation_seconds: float


@dataclass
class TranscriptionResult:
    text: str
    language: str | None
    language_probability: float | None
    duration: float | None
    model: str
    generation_seconds: float
    segments: list[dict[str, Any]]


class EngineRegistry:
    def __init__(self, *, enabled_engines: set[str], device: str) -> None:
        self.enabled_engines = enabled_engines
        self.device = device
        self._lock = threading.RLock()
        self._chatterbox_model: Any | None = None
        self._kokoro_pipeline: Any | None = None
        self._whisper_model: Any | None = None
        self._load_errors: dict[str, str] = {}

    def start_background_preload(self) -> None:
        preload_kokoro = "kokoro" in self.enabled_engines and _env_flag("DOC_READER_KOKORO_PRELOAD", True)
        preload_whisper = "whisper" in self.enabled_engines and _env_flag("DOC_READER_STT_PRELOAD", True)
        if not preload_kokoro and not preload_whisper:
            return
        thread = threading.Thread(
            target=self._preload_models,
            args=(preload_kokoro, preload_whisper),
            name="doc-reader-model-preload",
            daemon=True,
        )
        thread.start()

    def _preload_models(self, preload_kokoro: bool, preload_whisper: bool) -> None:
        # Kokoro/Torch must claim its cuDNN DLLs before faster-whisper/ctranslate2.
        # Loading ctranslate2's cudnn64_9.dll first can crash Kokoro on Windows CUDA.
        if preload_kokoro:
            self._preload_kokoro()
        if preload_whisper:
            self._preload_whisper()

    def _preload_kokoro(self) -> None:
        started = time.perf_counter()
        try:
            self._synthesize_kokoro("Doc Reader ready.", DEFAULT_KOKORO_VOICE)
            print(
                f"[doc-reader-tts] preloaded kokoro seconds={time.perf_counter() - started:.2f}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[doc-reader-tts] kokoro preload failed: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def _preload_whisper(self) -> None:
        model_name = _env("DOC_READER_STT_MODEL", DEFAULT_STT_MODEL)
        started = time.perf_counter()
        try:
            self._load_whisper(model_name)
            print(
                f"[doc-reader-tts] preloaded whisper model={model_name} "
                f"seconds={time.perf_counter() - started:.2f}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[doc-reader-tts] whisper preload failed: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "app": "doc-reader-tts",
                "host": platform.node(),
                "device": self._device_payload(),
                "engines": {
                    "chatterbox": self._engine_payload("chatterbox", self._chatterbox_model),
                    "kokoro": self._engine_payload("kokoro", self._kokoro_pipeline),
                    "whisper": self._engine_payload("whisper", self._whisper_model),
                },
            }

    def synthesize(
        self,
        *,
        engine: str,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> SynthesisResult:
        normalized = _normalize_engine(engine)
        cleaned = _clean_text_for_tts(text)
        normalized_speed = _normalize_speed(speed)
        if not cleaned:
            raise ValueError("No text to synthesize.")
        if normalized not in self.enabled_engines:
            raise ValueError(f"Engine is not enabled: {normalized}")

        if normalized == "chatterbox":
            return self._synthesize_chatterbox(cleaned, voice or DEFAULT_CHATTERBOX_VOICE)
        if normalized == "kokoro":
            return self._synthesize_kokoro(cleaned, voice or DEFAULT_KOKORO_VOICE, speed=normalized_speed)
        raise ValueError(f"Unsupported engine: {normalized}")

    def transcribe(
        self,
        *,
        audio: bytes,
        suffix: str = ".wav",
        language: str | None = None,
        word_timestamps: bool = False,
    ) -> TranscriptionResult:
        if "whisper" not in self.enabled_engines:
            raise ValueError("Speech-to-text is not enabled on this sidecar.")
        if not audio:
            raise ValueError("No audio to transcribe.")
        model_name = _env("DOC_READER_STT_MODEL", DEFAULT_STT_MODEL)
        started = time.perf_counter()
        model = self._load_whisper(model_name)
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        with tempfile.NamedTemporaryFile(suffix=suffix or ".wav", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(audio)
        try:
            transcribe_kwargs: dict[str, Any] = {
                "language": language,
                "vad_filter": True,
                "beam_size": _env_int("DOC_READER_STT_BEAM_SIZE", DEFAULT_STT_BEAM_SIZE),
            }
            if word_timestamps:
                transcribe_kwargs["word_timestamps"] = True
            try:
                segments_iter, info = model.transcribe(str(temp_path), **transcribe_kwargs)
            except TypeError:
                if not word_timestamps:
                    raise
                transcribe_kwargs.pop("word_timestamps", None)
                segments_iter, info = model.transcribe(str(temp_path), **transcribe_kwargs)
            segments: list[dict[str, Any]] = []
            text_parts: list[str] = []
            for segment in segments_iter:
                segment_text = str(getattr(segment, "text", "")).strip()
                if segment_text:
                    text_parts.append(segment_text)
                segment_payload = {
                    "start": float(getattr(segment, "start", 0.0)),
                    "end": float(getattr(segment, "end", 0.0)),
                    "text": segment_text,
                }
                words = _segment_words_payload(segment)
                if words:
                    segment_payload["words"] = words
                segments.append(segment_payload)
            generation_seconds = time.perf_counter() - started
            return TranscriptionResult(
                text=" ".join(text_parts).strip(),
                language=getattr(info, "language", None),
                language_probability=(
                    float(getattr(info, "language_probability"))
                    if getattr(info, "language_probability", None) is not None
                    else None
                ),
                duration=(
                    float(getattr(info, "duration"))
                    if getattr(info, "duration", None) is not None
                    else None
                ),
                model=model_name,
                generation_seconds=generation_seconds,
                segments=segments,
            )
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def bench(
        self,
        *,
        engine: str,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> dict[str, Any]:
        result = self.synthesize(engine=engine, text=text, voice=voice, speed=speed)
        chars = len(text)
        return {
            "ok": True,
            "engine": result.engine,
            "voice": result.voice,
            "speed": _normalize_speed(speed),
            "sample_rate": result.sample_rate,
            "audio_seconds": result.audio_seconds,
            "generation_seconds": result.generation_seconds,
            "real_time_factor": (
                result.generation_seconds / result.audio_seconds
                if result.audio_seconds > 0
                else None
            ),
            "chars": chars,
            "chars_per_second": chars / result.generation_seconds
            if result.generation_seconds > 0
            else None,
            "bytes": len(result.audio),
            "device": self._device_payload(),
        }

    def _synthesize_chatterbox(self, text: str, voice: str) -> SynthesisResult:
        started = time.perf_counter()
        model = self._load_chatterbox()
        try:
            sample_rate = int(getattr(model, "sr", 24000))
            parts = [
                model.generate(segment)
                for segment in _tts_segments(
                    text,
                    max_chars=_env_int(
                        "DOC_READER_CHATTERBOX_MAX_SEGMENT_CHARS",
                        DEFAULT_CHATTERBOX_MAX_SEGMENT_CHARS,
                    ),
                )
            ]
            if not parts:
                raise RuntimeError("Chatterbox returned no audio.")
            wav = _concat_torch_audio(parts, sample_rate)
            audio = _torch_audio_to_wav_bytes(wav, sample_rate)
            generation_seconds = time.perf_counter() - started
            return SynthesisResult(
                audio=audio,
                sample_rate=sample_rate,
                audio_seconds=_audio_seconds_from_torch(wav, sample_rate),
                engine="chatterbox",
                voice=voice,
                generation_seconds=generation_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Chatterbox synthesis failed: {exc}") from exc

    def _synthesize_kokoro(self, text: str, voice: str, *, speed: float = 1.0) -> SynthesisResult:
        started = time.perf_counter()
        pipeline = self._load_kokoro()
        try:
            import numpy as np
            import soundfile as sf

            chunks = []
            silence = np.zeros(
                int(24000 * _env_float("DOC_READER_TTS_SEGMENT_PAUSE_SECONDS", DEFAULT_SEGMENT_PAUSE_SECONDS)),
                dtype="float32",
            )
            for segment in _tts_segments(
                text,
                max_chars=_env_int(
                    "DOC_READER_KOKORO_MAX_SEGMENT_CHARS",
                    DEFAULT_KOKORO_MAX_SEGMENT_CHARS,
                ),
            ):
                segment_chunks = _kokoro_audio_chunks_for_segment(
                    pipeline,
                    segment,
                    voice=voice,
                    speed=speed,
                    np_module=np,
                )
                if chunks and segment_chunks and silence.size:
                    chunks.append(silence)
                chunks.extend(segment_chunks)
            if not chunks:
                raise RuntimeError("Kokoro returned no audio.")
            combined = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
            buffer = io.BytesIO()
            sf.write(buffer, combined, 24000, format="WAV")
            generation_seconds = time.perf_counter() - started
            return SynthesisResult(
                audio=buffer.getvalue(),
                sample_rate=24000,
                audio_seconds=float(len(combined) / 24000),
                engine="kokoro",
                voice=voice,
                generation_seconds=generation_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Kokoro synthesis failed: {exc}") from exc

    def _load_chatterbox(self) -> Any:
        with self._lock:
            if self._chatterbox_model is not None:
                return self._chatterbox_model
            try:
                _patch_chatterbox_watermarker()
                from chatterbox.tts import ChatterboxTTS

                self._chatterbox_model = ChatterboxTTS.from_pretrained(device=self.device)
                self._load_errors.pop("chatterbox", None)
                return self._chatterbox_model
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                self._load_errors["chatterbox"] = message
                raise RuntimeError(f"Unable to load Chatterbox: {message}") from exc

    def _load_kokoro(self) -> Any:
        with self._lock:
            if self._kokoro_pipeline is not None:
                return self._kokoro_pipeline
            try:
                configure_espeak()
                from kokoro import KPipeline

                try:
                    self._kokoro_pipeline = KPipeline(lang_code="a", device=self.device)
                except TypeError:
                    self._kokoro_pipeline = KPipeline(lang_code="a")
                self._load_errors.pop("kokoro", None)
                return self._kokoro_pipeline
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                self._load_errors["kokoro"] = message
                raise RuntimeError(f"Unable to load Kokoro: {message}") from exc

    def _load_whisper(self, model_name: str) -> Any:
        with self._lock:
            if self._whisper_model is not None:
                return self._whisper_model
            try:
                configure_windows_dll_search()
                from faster_whisper import WhisperModel

                compute_type = _env(
                    "DOC_READER_STT_COMPUTE_TYPE",
                    "float16" if self.device == "cuda" else "int8",
                )
                self._whisper_model = WhisperModel(
                    model_name,
                    device=self.device,
                    compute_type=compute_type,
                )
                self._load_errors.pop("whisper", None)
                return self._whisper_model
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                self._load_errors["whisper"] = message
                raise RuntimeError(f"Unable to load Whisper STT: {message}") from exc

    def _engine_payload(self, engine: str, model: Any | None) -> dict[str, Any]:
        return {
            "enabled": engine in self.enabled_engines,
            "loaded": model is not None,
            "error": self._load_errors.get(engine, ""),
        }

    def _device_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "requested": self.device,
            "platform": sys.platform,
            "python": sys.version.split()[0],
        }
        try:
            import torch

            payload.update(
                {
                    "torch": getattr(torch, "__version__", ""),
                    "cuda_available": bool(torch.cuda.is_available()),
                    "mps_available": bool(
                        getattr(getattr(torch, "backends", None), "mps", None)
                        and torch.backends.mps.is_available()
                    ),
                }
            )
            if torch.cuda.is_available():
                payload["cuda_device"] = torch.cuda.get_device_name(0)
        except Exception as exc:  # noqa: BLE001
            payload["torch_error"] = f"{type(exc).__name__}: {exc}"
        return payload


class TTSHandler(BaseHTTPRequestHandler):
    server_version = "DocReaderTTS/1.0"

    @property
    def registry(self) -> EngineRegistry:
        return self.server.registry  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json(self.registry.health())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/v1/audio/speech":
                payload = self._read_json()
                result = self.registry.synthesize(
                    engine=str(payload.get("engine") or "kokoro"),
                    voice=_optional_string(payload.get("voice")),
                    text=str(payload.get("text") or payload.get("input") or ""),
                    speed=_normalize_speed(payload.get("speed", 1.0)),
                )
                self._send_audio(result)
                return
            if self.path == "/v1/bench":
                payload = self._read_json()
                result = self.registry.bench(
                    engine=str(payload.get("engine") or "kokoro"),
                    voice=_optional_string(payload.get("voice")),
                    text=str(payload.get("text") or payload.get("input") or ""),
                    speed=_normalize_speed(payload.get("speed", 1.0)),
                )
                self._send_json(result)
                return
            if self.path == "/v1/audio/transcriptions":
                audio = self._read_body()
                result = self.registry.transcribe(
                    audio=audio,
                    suffix=_suffix_from_content_type(self.headers.get("Content-Type", "")),
                    language=_optional_string(self.headers.get("X-Doc-Reader-Language")),
                    word_timestamps=_header_flag(self.headers.get("X-Doc-Reader-Word-Timestamps")),
                )
                self._send_json(
                    {
                        "ok": True,
                        "text": result.text,
                        "language": result.language,
                        "language_probability": result.language_probability,
                        "duration": result.duration,
                        "model": result.model,
                        "generation_seconds": result.generation_seconds,
                        "segments": result.segments,
                    }
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("[doc-reader-tts] " + (format % args) + "\n")

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

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_audio(self, result: SynthesisResult) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", WAV_MIME)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(result.audio)))
        self.send_header("X-Doc-Reader-Engine", result.engine)
        self.send_header("X-Doc-Reader-Voice", result.voice)
        self.send_header("X-Doc-Reader-Sample-Rate", str(result.sample_rate))
        self.send_header("X-Doc-Reader-Audio-Seconds", f"{result.audio_seconds:.6f}")
        self.send_header("X-Doc-Reader-Generation-Seconds", f"{result.generation_seconds:.6f}")
        self.end_headers()
        self.wfile.write(result.audio)


class TTSServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler,
        registry: EngineRegistry,
    ) -> None:
        super().__init__(server_address, handler)
        self.registry = registry

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        # Clients that give up while a model is loading produce noisy stack traces
        # on Windows (WinError 10053/10054). Log one line instead.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            print(f"[doc-reader-tts] client {client_address[0]} disconnected early", flush=True)
            return
        super().handle_error(request, client_address)


def _torch_audio_to_wav_bytes(wav: Any, sample_rate: int) -> bytes:
    import torch
    import torchaudio as ta

    tensor = _normalize_torch_audio(wav).cpu()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        ta.save(str(temp_path), tensor, sample_rate)
        return temp_path.read_bytes()
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def _audio_seconds_from_torch(wav: Any, sample_rate: int) -> float:
    shape = getattr(wav, "shape", None)
    if not shape:
        return 0.0
    samples = int(shape[-1])
    return float(samples / max(1, sample_rate))


def _normalize_torch_audio(wav: Any):
    import torch

    tensor = wav.detach() if hasattr(wav, "detach") else torch.as_tensor(wav)
    if tensor.ndim == 1:
        tensor = tensor.unsqueeze(0)
    elif tensor.ndim == 2 and tensor.shape[0] > tensor.shape[1]:
        tensor = tensor.transpose(0, 1)
    return tensor


def _concat_torch_audio(parts: list[Any], sample_rate: int):
    import torch

    tensors = [_normalize_torch_audio(part) for part in parts]
    if len(tensors) == 1:
        return tensors[0]

    pause_samples = int(
        max(0.0, _env_float("DOC_READER_TTS_SEGMENT_PAUSE_SECONDS", DEFAULT_SEGMENT_PAUSE_SECONDS))
        * max(1, sample_rate)
    )
    merged = []
    for index, tensor in enumerate(tensors):
        if index > 0 and pause_samples > 0:
            merged.append(
                torch.zeros(
                    (tensor.shape[0], pause_samples),
                    dtype=tensor.dtype,
                    device=tensor.device,
                )
            )
        merged.append(tensor)
    return torch.cat(merged, dim=-1)


def _clean_text_for_tts(text: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _replace_tts_control_chars(cleaned)
    cleaned = re.sub(r"```.*?```", " code block omitted. ", cleaned, flags=re.DOTALL)
    cleaned = MARKDOWN_LINK_RE.sub(r"\1", cleaned)
    cleaned = URL_RE.sub(" a web link ", cleaned)

    lines = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*+•]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        line = re.sub(r"^\[[ xX]\]\s+", "", line)
        lines.append(line)
    cleaned = "\n".join(lines)

    replacements = {
        "`": "",
        "*": "",
        "_": " ",
        "|": ", ",
        "&": " and ",
        "@": " at ",
        "→": " to ",
        "=>": " to ",
        "->": " to ",
        ">=": " at least ",
        "<=": " at most ",
        "\u2013": "-",
        "\u2014": "-",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)

    cleaned = re.sub(r"(?<!\w)/(?!\w)", " ", cleaned)
    cleaned = re.sub(r"(?<=\w)/(?=\w)", " or ", cleaned)
    cleaned = re.sub(r"[{}[\]<>]", " ", cleaned)
    cleaned = re.sub(r"={2,}", ". ", cleaned)
    cleaned = re.sub(r"-{2,}", ". ", cleaned)
    cleaned = re.sub(r"[#~^]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,.;:!?]){2,}", r"\1", cleaned)
    cleaned = cleaned.strip(" ,;:")
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def _kokoro_audio_chunks_for_segment(
    pipeline: Any,
    segment: str,
    *,
    voice: str,
    speed: float,
    np_module: Any,
) -> list[Any]:
    try:
        return _collect_kokoro_audio_chunks(
            pipeline,
            segment,
            voice=voice,
            speed=speed,
            np_module=np_module,
        )
    except Exception as exc:  # noqa: BLE001
        if not _is_phonemizer_line_mismatch(exc):
            raise

        last_error: Exception = exc
        for retry_segment in _kokoro_retry_segments(segment):
            try:
                chunks = _collect_kokoro_audio_chunks(
                    pipeline,
                    retry_segment,
                    voice=voice,
                    speed=speed,
                    np_module=np_module,
                )
            except Exception as retry_exc:  # noqa: BLE001
                last_error = retry_exc
                continue
            if chunks:
                print(
                    "[doc-reader-tts] recovered kokoro phonemizer line mismatch "
                    f"chars={len(segment)} retry_chars={len(retry_segment)}",
                    file=sys.stderr,
                    flush=True,
                )
                return chunks
        raise RuntimeError(f"Kokoro phonemizer line mismatch after retry: {last_error}") from exc


def _collect_kokoro_audio_chunks(
    pipeline: Any,
    segment: str,
    *,
    voice: str,
    speed: float,
    np_module: Any,
) -> list[Any]:
    chunks = []
    for _graphemes, _phonemes, audio in pipeline(segment, voice=voice, speed=speed):
        if audio is None:
            continue
        chunks.append(np_module.asarray(audio, dtype="float32"))
    return chunks


def _kokoro_retry_segments(text: str) -> list[str]:
    safe_text = _safe_text_for_kokoro_retry(text)
    if not safe_text:
        return []
    segments = _tts_segments(safe_text, max_chars=KOKORO_RETRY_MAX_SEGMENT_CHARS)
    return list(dict.fromkeys(segment for segment in segments if segment.strip()))


def _safe_text_for_kokoro_retry(text: str) -> str:
    safe = _replace_tts_control_chars(text or "")
    safe = "".join(
        char if char.isalnum() or char.isspace() or char in ".,!?'" else " "
        for char in safe
    )
    safe = re.sub(r"([.,!?]){2,}", r"\1", safe)
    safe = re.sub(r"\s+", " ", safe)
    safe = safe.strip(" ,.!?'")
    if not re.search(r"\w", safe):
        return ""
    if safe[-1] not in ".!?":
        safe += "."
    return safe


def _replace_tts_control_chars(text: str) -> str:
    return "".join(
        char
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"}
        else " "
        for char in text
    )


def _is_phonemizer_line_mismatch(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if PHONEMIZER_LINE_MISMATCH in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _tts_segments(text: str, *, max_chars: int) -> list[str]:
    max_chars = max(120, int(max_chars))
    sentences = [part.strip() for part in SENTENCE_RE.split(text) if part.strip()]
    if not sentences:
        sentences = [text.strip()] if text.strip() else []

    segments: list[str] = []
    current = ""
    for sentence in sentences:
        pieces = _split_long_sentence(sentence, max_chars=max_chars)
        for piece in pieces:
            if not current:
                current = piece
                continue
            if len(current) + 1 + len(piece) <= max_chars:
                current = f"{current} {piece}"
            else:
                segments.append(_ensure_sentence_end(current))
                current = piece
    if current:
        segments.append(_ensure_sentence_end(current))
    return segments


def _split_long_sentence(sentence: str, *, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]

    pieces: list[str] = []
    current = ""
    for word in sentence.split():
        if not current:
            current = word
            continue
        if len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            pieces.append(current)
            current = word
    if current:
        pieces.append(current)
    return pieces


def _ensure_sentence_end(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in ".!?":
        return f"{text}."
    return text


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


def _patch_chatterbox_watermarker() -> None:
    try:
        import perth
    except Exception:  # noqa: BLE001
        return
    if getattr(perth, "PerthImplicitWatermarker", None) is not None:
        return

    class _NoopWatermarker:
        def apply_watermark(self, wav, sample_rate: int | None = None):  # noqa: ANN001
            return wav

    perth.PerthImplicitWatermarker = _NoopWatermarker


def _normalize_engine(value: str) -> str:
    normalized = (value or "").strip().lower()
    aliases = {
        "chatterbox-4090": "chatterbox",
        "tailscale-chatterbox": "chatterbox",
        "remote-chatterbox": "chatterbox",
        "tailscale-4090": "kokoro",
        "kokoro-4090": "kokoro",
        "tailscale-kokoro": "kokoro",
        "local-kokoro": "kokoro",
    }
    return aliases.get(normalized, normalized)


def _normalize_speed(value: object) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        parsed = 1.0
    if parsed != parsed:
        parsed = 1.0
    return max(MIN_TTS_SPEED, min(MAX_TTS_SPEED, parsed))


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _header_flag(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _segment_words_payload(segment: Any) -> list[dict[str, Any]]:
    words = getattr(segment, "words", None)
    if not words:
        return []
    payload: list[dict[str, Any]] = []
    for word in words:
        text = str(getattr(word, "word", "") or "").strip()
        if not text:
            continue
        entry: dict[str, Any] = {"word": text}
        start = _float_or_none(getattr(word, "start", None))
        end = _float_or_none(getattr(word, "end", None))
        probability = _float_or_none(getattr(word, "probability", None))
        if start is not None:
            entry["start"] = start
        if end is not None:
            entry["end"] = end
        if probability is not None:
            entry["probability"] = probability
        payload.append(entry)
    return payload


def _float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _suffix_from_content_type(content_type: str) -> str:
    normalized = content_type.lower()
    if "mp4" in normalized or "m4a" in normalized:
        return ".m4a"
    if "mpeg" in normalized or "mp3" in normalized:
        return ".mp3"
    if "ogg" in normalized:
        return ".ogg"
    if "flac" in normalized:
        return ".flac"
    return ".wav"


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _default_device() -> str:
    configured = os.getenv("DOC_READER_TTS_DEVICE")
    if configured:
        return configured
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if sys.platform == "darwin" and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a private DocReader TTS sidecar.")
    parser.add_argument("--host", default=os.getenv("DOC_READER_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DOC_READER_TTS_PORT", "8772")))
    parser.add_argument(
        "--engines",
        default=os.getenv("DOC_READER_TTS_ENGINES", ",".join(DEFAULT_ENGINES)),
        help="Comma-separated engines to enable: chatterbox,kokoro",
    )
    parser.add_argument("--device", default=_default_device())
    return parser


def main() -> int:
    args = build_parser().parse_args()
    engines = {
        _normalize_engine(engine)
        for engine in str(args.engines).split(",")
        if _normalize_engine(engine)
    }
    registry = EngineRegistry(enabled_engines=engines, device=args.device)
    registry.start_background_preload()
    server = TTSServer((args.host, args.port), TTSHandler, registry)
    print(
        f"[doc-reader-tts] listening on http://{args.host}:{args.port} "
        f"engines={','.join(sorted(engines))} device={args.device}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
