from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from .platform_tools import LOCAL_KOKORO_LABEL, find_audio_player, popen_hidden_kwargs


class Speaker(Protocol):
    def set_rate(self, rate: int) -> None:
        ...

    def prefetch(self, text: str, key: int | None = None) -> None:
        ...

    def speak(self, text: str, key: int | None = None) -> None:
        ...

    def close(self) -> None:
        ...


@dataclass
class ConsoleSpeaker:
    def set_rate(self, rate: int) -> None:
        return

    def prefetch(self, text: str, key: int | None = None) -> None:
        return

    def speak(self, text: str, key: int | None = None) -> None:
        print(text)

    def close(self) -> None:
        return


class MacSaySpeaker:
    def __init__(self, *, rate: int = 180, voice_hint: str | None = None) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("Mac 'say' backend is only available on macOS.")

        self.say_bin = shutil.which("say")
        if not self.say_bin:
            raise RuntimeError("macOS 'say' command was not found.")

        self.rate = max(90, min(rate, 500))
        self.voice = self._resolve_voice(voice_hint)
        self._active: subprocess.Popen[str] | None = None

    def set_rate(self, rate: int) -> None:
        self.rate = max(90, min(int(rate), 500))

    def prefetch(self, text: str, key: int | None = None) -> None:
        return

    def _resolve_voice(self, voice_hint: str | None) -> str | None:
        if not voice_hint:
            return None

        hint = voice_hint.strip().lower()
        if not hint:
            return None

        try:
            raw = subprocess.check_output(
                [self.say_bin, "-v", "?"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:  # noqa: BLE001
            return voice_hint

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if hint in stripped.lower():
                return stripped.split()[0]

        return voice_hint

    def speak(self, text: str, key: int | None = None) -> None:
        if not text:
            return

        args = [self.say_bin, "-r", str(self.rate)]
        if self.voice:
            args.extend(["-v", self.voice])
        args.append(text)

        self._active = subprocess.Popen(args, text=True)
        self._active.wait()
        self._active = None

    def close(self) -> None:
        if self._active and self._active.poll() is None:
            self._active.terminate()
        self._active = None


class Pyttsx3Speaker:
    def __init__(self, *, rate: int = 180, voice_hint: str | None = None) -> None:
        try:
            import pyttsx3
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Speech backend requires pyttsx3. Install it with: pip install pyttsx3"
            ) from exc

        self.engine = pyttsx3.init()
        self.rate = 0
        self.set_rate(rate)
        if voice_hint:
            self._select_voice(voice_hint)

    def set_rate(self, rate: int) -> None:
        self.rate = max(90, min(int(rate), 500))
        self.engine.setProperty("rate", self.rate)

    def prefetch(self, text: str, key: int | None = None) -> None:
        return

    def _select_voice(self, voice_hint: str) -> None:
        voice_hint = voice_hint.lower()
        voices = self.engine.getProperty("voices")
        for voice in voices:
            voice_blob = f"{voice.id} {getattr(voice, 'name', '')}".lower()
            if voice_hint in voice_blob:
                self.engine.setProperty("voice", voice.id)
                return

    def speak(self, text: str, key: int | None = None) -> None:
        if not text:
            return
        self.engine.say(text)
        self.engine.runAndWait()

    def close(self) -> None:
        self.engine.stop()


OPENAI_TTS_BASE_URL = "https://api.openai.com/v1"
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "marin"
OPENAI_TTS_RESPONSE_FORMAT = "wav"
OPENAI_TTS_INSTRUCTIONS = (
    "Read clearly at a natural pace with a calm, focused delivery for document narration."
)
OPENAI_TTS_MODELS = {"gpt-4o-mini-tts", "tts-1", "tts-1-hd"}
OPENAI_TTS_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
}
OPENAI_LEGACY_TTS_VOICES = {
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "onyx",
    "nova",
    "sage",
    "shimmer",
}
PLAYABLE_OPENAI_FORMATS = {"mp3", "opus", "aac", "flac", "wav"}
DEFAULT_TTS_UMBRA_URL = "http://100.72.151.28:8771"
DEFAULT_TTS_MAC_URL = "http://127.0.0.1:8772"
DEFAULT_HTTP_TTS_TIMEOUT_SECONDS = 180.0
DEFAULT_RATE = 180


class OpenAITTSSpeaker:
    def __init__(
        self,
        *,
        api_key: str | None,
        voice: str | None = None,
        model: str = OPENAI_TTS_MODEL,
        response_format: str = OPENAI_TTS_RESPONSE_FORMAT,
        instructions: str | None = OPENAI_TTS_INSTRUCTIONS,
        rate: int = DEFAULT_RATE,
        request_timeout_seconds: float = 120.0,
    ) -> None:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "OpenAI speech backend requires requests. Install with: pip install requests"
            ) from exc

        resolved_key = resolve_openai_api_key(api_key)
        resolved_model = (model or OPENAI_TTS_MODEL).strip()
        resolved_voice = (voice or os.getenv("DOC_READER_OPENAI_VOICE") or OPENAI_TTS_VOICE).strip()
        resolved_format = (
            response_format
            or os.getenv("DOC_READER_OPENAI_RESPONSE_FORMAT")
            or OPENAI_TTS_RESPONSE_FORMAT
        ).strip().lower()

        if not resolved_key:
            raise RuntimeError(
                "OpenAI API key missing. Set OPENAI_API_KEY, store openai-primary in ORP secrets, "
                "or pass --openai-api-key."
            )
        if resolved_model not in OPENAI_TTS_MODELS:
            raise RuntimeError(
                "Unsupported OpenAI speech model. Use one of: "
                + ", ".join(sorted(OPENAI_TTS_MODELS))
            )
        if resolved_voice not in OPENAI_TTS_VOICES:
            raise RuntimeError(
                "Unsupported OpenAI speech voice. Use one of: "
                + ", ".join(sorted(OPENAI_TTS_VOICES))
            )
        if resolved_model in {"tts-1", "tts-1-hd"} and resolved_voice not in OPENAI_LEGACY_TTS_VOICES:
            resolved_voice = "alloy"
        if resolved_format not in PLAYABLE_OPENAI_FORMATS:
            raise RuntimeError(
                "Unsupported OpenAI speech response format for playback. Use one of: "
                + ", ".join(sorted(PLAYABLE_OPENAI_FORMATS))
            )

        self.requests = requests
        self.api_key = resolved_key
        self.base_url = (os.getenv("OPENAI_BASE_URL") or os.getenv("DOC_READER_OPENAI_BASE_URL") or OPENAI_TTS_BASE_URL).rstrip("/")
        self.voice = resolved_voice
        self.model = resolved_model
        self.response_format = resolved_format
        self.instructions = (instructions or os.getenv("DOC_READER_OPENAI_INSTRUCTIONS") or "").strip()
        self.speed = _speed_for_rate(rate)
        self.timeout = max(10.0, request_timeout_seconds)
        self._player = self._resolve_player()
        self._active: subprocess.Popen[bytes] | None = None
        self._prefetch_lock = threading.Lock()
        self._prefetched_audio: dict[int, Future[bytes]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="doc-reader-openai-tts",
        )

    def _resolve_player(self) -> list[str]:
        return find_audio_player()

    def set_rate(self, rate: int) -> None:
        speed = _speed_for_rate(rate)
        with self._prefetch_lock:
            if speed == self.speed:
                return
            self.speed = speed
            prefetched = list(self._prefetched_audio.values())
            self._prefetched_audio.clear()
        for future in prefetched:
            future.cancel()

    def _synthesize_audio(self, text: str) -> bytes:
        if not text:
            return b""

        try:
            with self._prefetch_lock:
                speed = self.speed
            url = f"{self.base_url}/audio/speech"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "voice": self.voice,
                "input": text,
                "response_format": self.response_format,
                "speed": speed,
            }
            if self.instructions and self.model == "gpt-4o-mini-tts":
                payload["instructions"] = self.instructions

            with self.requests.post(
                url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=(10.0, self.timeout),
            ) as response:
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"OpenAI speech request failed ({response.status_code}): "
                        f"{_extract_error_message(response)}"
                    )

                audio_chunks = [
                    chunk
                    for chunk in response.iter_content(chunk_size=16384)
                    if chunk
                ]

            audio = b"".join(audio_chunks)
            if not audio:
                raise RuntimeError("OpenAI returned no audio data.")
            return audio
        except self.requests.RequestException as exc:
            raise RuntimeError(f"OpenAI speech network error: {exc}") from exc

    def prefetch(self, text: str, key: int | None = None) -> None:
        if not text or key is None:
            return

        with self._prefetch_lock:
            if key in self._prefetched_audio:
                return
            self._prefetched_audio[key] = self._executor.submit(self._synthesize_audio, text)

    def speak(self, text: str, key: int | None = None) -> None:
        if not text:
            return

        future: Future[bytes] | None = None
        if key is not None:
            with self._prefetch_lock:
                future = self._prefetched_audio.pop(key, None)

        temp_audio_path: str | None = None

        try:
            audio = future.result() if future is not None else self._synthesize_audio(text)
            with tempfile.NamedTemporaryFile(suffix=f".{self.response_format}", delete=False) as handle:
                temp_audio_path = handle.name
                handle.write(audio)

            command = [*self._player, temp_audio_path]
            self._active = subprocess.Popen(command, **popen_hidden_kwargs())
            return_code = self._active.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Audio playback failed with exit code {exc.returncode}") from exc
        finally:
            self._active = None
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.remove(temp_audio_path)
                except OSError:
                    pass

    def close(self) -> None:
        if self._active and self._active.poll() is None:
            self._active.terminate()
        with self._prefetch_lock:
            prefetched = list(self._prefetched_audio.values())
            self._prefetched_audio.clear()
        for future in prefetched:
            future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        return


class HttpTTSSpeaker:
    def __init__(
        self,
        *,
        base_url: str,
        engine: str,
        voice: str | None = None,
        rate: int = DEFAULT_RATE,
        request_timeout_seconds: float = DEFAULT_HTTP_TTS_TIMEOUT_SECONDS,
    ) -> None:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "HTTP speech backend requires requests. Install with: pip install requests"
            ) from exc

        self.requests = requests
        self.base_url = base_url.rstrip("/")
        self.engine = engine
        self.voice = voice
        self.speed = _speed_for_rate(rate)
        self.timeout = max(10.0, request_timeout_seconds)
        self._player = self._resolve_player()
        self._active: subprocess.Popen[bytes] | None = None
        self._prefetch_lock = threading.Lock()
        self._prefetched_audio: dict[int, Future[tuple[bytes, str]]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix=f"doc-reader-http-tts-{engine}",
        )

    def _resolve_player(self) -> list[str]:
        return find_audio_player()

    def set_rate(self, rate: int) -> None:
        speed = _speed_for_rate(rate)
        with self._prefetch_lock:
            if speed == self.speed:
                return
            self.speed = speed
            prefetched = list(self._prefetched_audio.values())
            self._prefetched_audio.clear()
        for future in prefetched:
            future.cancel()

    def _synthesize_audio(self, text: str) -> tuple[bytes, str]:
        if not text:
            return b"", "wav"
        with self._prefetch_lock:
            speed = self.speed
        payload: dict[str, object] = {
            "engine": self.engine,
            "text": text,
            "format": "wav",
            "speed": speed,
        }
        if self.voice:
            payload["voice"] = self.voice
        try:
            response = self.requests.post(
                f"{self.base_url}/v1/audio/speech",
                json=payload,
                timeout=(2.0, self.timeout),
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP TTS {self.engine} failed ({response.status_code}): "
                    f"{_extract_error_message(response)}"
                )
            content_type = response.headers.get("Content-Type", "")
            extension = "wav" if "wav" in content_type else "audio"
            if not response.content:
                raise RuntimeError(f"HTTP TTS {self.engine} returned no audio data.")
            return response.content, extension
        except self.requests.RequestException as exc:
            raise RuntimeError(f"HTTP TTS {self.engine} network error: {exc}") from exc

    def prefetch(self, text: str, key: int | None = None) -> None:
        if not text or key is None:
            return
        with self._prefetch_lock:
            if key in self._prefetched_audio:
                return
            self._prefetched_audio[key] = self._executor.submit(self._synthesize_audio, text)

    def speak(self, text: str, key: int | None = None) -> None:
        if not text:
            return

        future: Future[tuple[bytes, str]] | None = None
        if key is not None:
            with self._prefetch_lock:
                future = self._prefetched_audio.pop(key, None)

        temp_audio_path: str | None = None
        try:
            audio, extension = future.result() if future is not None else self._synthesize_audio(text)
            with tempfile.NamedTemporaryFile(suffix=f".{extension}", delete=False) as handle:
                temp_audio_path = handle.name
                handle.write(audio)

            command = [*self._player, temp_audio_path]
            self._active = subprocess.Popen(command, **popen_hidden_kwargs())
            return_code = self._active.wait()
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, command)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Audio playback failed with exit code {exc.returncode}") from exc
        finally:
            self._active = None
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.remove(temp_audio_path)
                except OSError:
                    pass

    def close(self) -> None:
        if self._active and self._active.poll() is None:
            self._active.terminate()
        with self._prefetch_lock:
            prefetched = list(self._prefetched_audio.values())
            self._prefetched_audio.clear()
        for future in prefetched:
            future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


class FallbackSpeaker:
    def __init__(
        self,
        *,
        primary: Speaker,
        fallback: Speaker,
        fallback_name: str,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.fallback_name = fallback_name
        self._using_fallback = False

    def set_rate(self, rate: int) -> None:
        primary_setter = getattr(self.primary, "set_rate", None)
        if callable(primary_setter):
            primary_setter(rate)
        fallback_setter = getattr(self.fallback, "set_rate", None)
        if callable(fallback_setter):
            fallback_setter(rate)

    def prefetch(self, text: str, key: int | None = None) -> None:
        if self._using_fallback:
            self.fallback.prefetch(text, key)
            return
        try:
            self.primary.prefetch(text, key)
        except RuntimeError as exc:
            self._activate_fallback(exc)
            self.fallback.prefetch(text, key)

    def speak(self, text: str, key: int | None = None) -> None:
        if self._using_fallback:
            self.fallback.speak(text, key)
            return
        try:
            self.primary.speak(text, key)
        except RuntimeError as exc:
            self._activate_fallback(exc)
            self.fallback.speak(text, key)

    def close(self) -> None:
        self.primary.close()
        self.fallback.close()

    def _activate_fallback(self, exc: RuntimeError) -> None:
        if self._using_fallback:
            return
        self._using_fallback = True
        self.primary.close()
        print(
            f"[doc-reader] OpenAI speech unavailable; using {self.fallback_name}: {exc}",
            file=sys.stderr,
        )


class FallbackChainSpeaker:
    def __init__(self, speakers: list[tuple[str, Speaker]]) -> None:
        if not speakers:
            raise RuntimeError("Fallback chain requires at least one speaker.")
        self._speakers = speakers
        self._index = 0
        self._lock = threading.RLock()
        self._prefetch_ready = False

    def set_rate(self, rate: int) -> None:
        with self._lock:
            speakers = [speaker for _name, speaker in self._speakers]
        for speaker in speakers:
            setter = getattr(speaker, "set_rate", None)
            if callable(setter):
                setter(rate)

    def prefetch(self, text: str, key: int | None = None) -> None:
        if not self._prefetch_ready:
            return
        with self._lock:
            speaker = self._speakers[self._index][1]
        try:
            speaker.prefetch(text, key)
        except RuntimeError as exc:
            self._activate_next(exc)

    def speak(self, text: str, key: int | None = None) -> None:
        while True:
            with self._lock:
                speaker = self._speakers[self._index][1]
            try:
                speaker.speak(text, key)
                with self._lock:
                    self._prefetch_ready = True
                return
            except RuntimeError as exc:
                self._activate_next(exc)

    def close(self) -> None:
        for _name, speaker in self._speakers:
            speaker.close()

    def _activate_next(self, exc: RuntimeError) -> None:
        with self._lock:
            current_name, current_speaker = self._speakers[self._index]
            current_speaker.close()
            if self._index >= len(self._speakers) - 1:
                raise exc
            self._index += 1
            self._prefetch_ready = False
            next_name = self._speakers[self._index][0]
        print(
            f"[doc-reader] {current_name} unavailable; using {next_name}: {exc}",
            file=sys.stderr,
        )


def _pick_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_keychain_password(service: str, account: str | None = None) -> str:
    security_bin = shutil.which("security")
    if sys.platform != "darwin" or not security_bin or not service:
        return ""

    args = [security_bin, "find-generic-password", "-s", service]
    if account:
        args.extend(["-a", account])
    args.append("-w")

    try:
        return subprocess.check_output(
            args,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


def _plaintext_from_orp_secret_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""

    nested = [
        payload.get("secret"),
        payload.get("item"),
        payload.get("result"),
        payload.get("resolved"),
    ]
    values: list[object] = [
        payload.get("value"),
        payload.get("plaintext"),
        payload.get("plaintextValue"),
        payload.get("secretValue"),
        payload.get("password"),
        payload.get("apiKey"),
    ]
    for entry in nested:
        if isinstance(entry, dict):
            values.extend(
                [
                    entry.get("value"),
                    entry.get("plaintext"),
                    entry.get("plaintextValue"),
                    entry.get("secretValue"),
                    entry.get("password"),
                    entry.get("apiKey"),
                ]
            )
    return _pick_string(*values)


def _resolve_openai_api_key_from_orp() -> str:
    orp_bin = (
        os.getenv("DOC_READER_ORP")
        or os.getenv("ORP_BINARY")
        or shutil.which("orp")
        or ""
    )
    if not orp_bin:
        return ""

    refs = [
        os.getenv("DOC_READER_OPENAI_ORP_SECRET_REF"),
        "openai-primary",
        "openai-api-key",
        "openai",
        "OPENAI_API_KEY",
    ]
    deduped_refs = [ref for i, ref in enumerate(refs) if ref and ref not in refs[:i]]

    for mode in ("--local-only", "--local-first"):
        for ref in deduped_refs:
            try:
                output = subprocess.check_output(
                    [
                        orp_bin,
                        "secrets",
                        "resolve",
                        ref,
                        mode,
                        "--reveal",
                        "--json",
                    ],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3.0,
                ).strip()
            except Exception:  # noqa: BLE001
                continue

            if not output:
                continue
            try:
                value = _plaintext_from_orp_secret_payload(json.loads(output))
            except ValueError:
                value = "" if output.startswith("{") else output
            if value:
                return value

    return ""


def resolve_openai_api_key(explicit_api_key: str | None = None) -> str:
    direct = _pick_string(
        explicit_api_key,
        os.getenv("DOC_READER_OPENAI_API_KEY"),
        os.getenv("OPENAI_API_KEY"),
        os.getenv("CLAWDAD_OPENAI_API_KEY"),
    )
    if direct:
        return direct

    configured_service = os.getenv("DOC_READER_OPENAI_KEYCHAIN_SERVICE")
    configured_account = os.getenv("DOC_READER_OPENAI_KEYCHAIN_ACCOUNT")
    keychain_pairs: list[tuple[str, str | None]] = [
        (configured_service or "", configured_account),
        ("orp.secret.openai", "openai-primary"),
        ("com.sproutseeds.read-docs", "openai-api-key"),
        ("OPENAI_API_KEY", None),
        ("OpenAI", "api-key"),
        ("openai", "api-key"),
    ]
    for service, account in keychain_pairs:
        value = _read_keychain_password(service, account)
        if value:
            return value

    return _resolve_openai_api_key_from_orp()


def _extract_error_message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = (response.text or "").strip()
        return body[:200] if body else "Unknown error"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
            return json.dumps(error)[:200]
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str):
                return message
            return json.dumps(detail)[:200]
        return json.dumps(payload)[:200]

    return str(payload)[:200]


def build_speaker(
    *,
    rate: int = 180,
    voice_hint: str | None = None,
    backend: str = "auto",
    openai_api_key: str | None = None,
    openai_model: str = OPENAI_TTS_MODEL,
    openai_voice: str | None = None,
    openai_response_format: str = OPENAI_TTS_RESPONSE_FORMAT,
    openai_instructions: str | None = OPENAI_TTS_INSTRUCTIONS,
    http_tts_url: str | None = None,
    http_tts_engine: str | None = None,
    http_tts_voice: str | None = None,
) -> Speaker:
    normalized_backend = backend.strip().lower()
    supported = {
        "auto",
        "macsay",
        "pyttsx3",
        "openai",
        "http-tts",
        "tailscale-4090",
        "tailscale-chatterbox",
        "tailscale-kokoro",
        "local-kokoro",
    }
    if normalized_backend not in supported:
        raise RuntimeError(
            "Unknown speech backend. Use one of: auto, tailscale-4090, tailscale-chatterbox, "
            "tailscale-kokoro, local-kokoro, http-tts, openai, macsay, pyttsx3"
        )

    errors: list[str] = []

    if normalized_backend == "openai":
        return OpenAITTSSpeaker(
            api_key=openai_api_key,
            model=openai_model,
            voice=openai_voice,
            response_format=openai_response_format,
            instructions=openai_instructions,
            rate=rate,
        )

    if normalized_backend == "macsay":
        return MacSaySpeaker(rate=rate, voice_hint=voice_hint)

    if normalized_backend == "pyttsx3":
        return Pyttsx3Speaker(rate=rate, voice_hint=voice_hint)

    if normalized_backend == "http-tts":
        return HttpTTSSpeaker(
            base_url=http_tts_url or _env("DOC_READER_HTTP_TTS_URL", DEFAULT_TTS_MAC_URL),
            engine=http_tts_engine or _env("DOC_READER_HTTP_TTS_ENGINE", "kokoro"),
            voice=http_tts_voice or voice_hint,
            rate=rate,
        )

    if normalized_backend == "tailscale-chatterbox":
        return HttpTTSSpeaker(
            base_url=_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL),
            engine="chatterbox",
            voice=http_tts_voice or voice_hint,
            rate=rate,
        )

    if normalized_backend in {"tailscale-4090", "tailscale-kokoro"}:
        return HttpTTSSpeaker(
            base_url=_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL),
            engine="kokoro",
            voice=http_tts_voice or voice_hint,
            rate=rate,
        )

    if normalized_backend == "local-kokoro":
        return HttpTTSSpeaker(
            base_url=_env("DOC_READER_TTS_MAC_URL", DEFAULT_TTS_MAC_URL),
            engine="kokoro",
            voice=http_tts_voice or voice_hint,
            rate=rate,
        )

    # Auto mode: keep API spend opt-in and prefer stable private local/Tailnet engines.
    chain: list[tuple[str, Speaker]] = []
    for name, speaker in _auto_http_speakers(voice_hint=http_tts_voice or voice_hint, rate=rate):
        chain.append((name, speaker))

    if sys.platform == "darwin":
        try:
            chain.append(("macOS system speech", MacSaySpeaker(rate=rate, voice_hint=voice_hint)))
        except RuntimeError as exc:
            errors.append(str(exc))

    try:
        chain.append(("pyttsx3", Pyttsx3Speaker(rate=rate, voice_hint=voice_hint)))
    except RuntimeError as exc:
        errors.append(str(exc))

    if os.getenv("DOC_READER_AUTO_ALLOW_OPENAI", "").strip().lower() in {"1", "true", "yes"}:
        resolved_key = resolve_openai_api_key(openai_api_key)
        if resolved_key:
            try:
                chain.append(
                    (
                        "OpenAI TTS",
                        OpenAITTSSpeaker(
                            api_key=resolved_key,
                            model=openai_model,
                            voice=openai_voice,
                            response_format=openai_response_format,
                            instructions=openai_instructions,
                            rate=rate,
                        ),
                    )
                )
            except RuntimeError as exc:
                errors.append(str(exc))

    if chain:
        return FallbackChainSpeaker(chain)

    raise RuntimeError("No speech backend available. " + " | ".join(errors))


def _auto_http_speakers(*, voice_hint: str | None, rate: int) -> list[tuple[str, Speaker]]:
    return [
        (
            LOCAL_KOKORO_LABEL,
            HttpTTSSpeaker(
                base_url=_env("DOC_READER_TTS_MAC_URL", DEFAULT_TTS_MAC_URL),
                engine="kokoro",
                voice=voice_hint,
                rate=rate,
            ),
        ),
        (
            "Remote Kokoro",
            HttpTTSSpeaker(
                base_url=_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL),
                engine="kokoro",
                voice=voice_hint,
                rate=rate,
            ),
        ),
        (
            "Remote Chatterbox",
            HttpTTSSpeaker(
                base_url=_env("DOC_READER_TTS_UMBRA_URL", DEFAULT_TTS_UMBRA_URL),
                engine="chatterbox",
                voice=voice_hint,
                rate=rate,
            ),
        ),
    ]


def _speed_for_rate(rate: int) -> float:
    try:
        parsed = float(rate)
    except (TypeError, ValueError):
        parsed = DEFAULT_RATE
    if parsed != parsed:
        parsed = DEFAULT_RATE
    return round(max(0.5, min(2.0, parsed / DEFAULT_RATE)), 3)


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default
