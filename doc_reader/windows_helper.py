"""Windows tray helper for Doc Reader.

This is the Windows counterpart of the macOS menu-bar app: it lives in the system
tray, listens for the read-selection hotkey and the hold-to-dictate key, records the
microphone, sends audio to the web app for local Whisper transcription, pastes the
result into the active text field, and reports its state to the web app so the page
can show "helper online".

Run with ``python -m doc_reader.windows_helper`` inside the project venv, or let
``run-doc-reader.cmd start`` launch it.
"""

from __future__ import annotations

import io
import json
import os
import random
import sys
import threading
import time
import wave
import webbrowser
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .platform_tools import (
    DEFAULT_WINDOWS_DICTATION_KEY,
    DEFAULT_WINDOWS_SELECTION_HOTKEY,
    IS_WINDOWS,
    dictation_hotkey_label,
    hotkey_options,
    managed_root,
    normalize_dictation_key,
    normalize_selection_shortcut,
    pid_is_running,
    selection_hotkey_label,
)

WEB_URL = os.getenv("DOC_READER_WEB_URL", "http://127.0.0.1:8766").rstrip("/")
DICTATION_KEY_NAME = os.getenv("DOC_READER_DICTATION_KEY", DEFAULT_WINDOWS_DICTATION_KEY).strip().lower()
SELECTION_HOTKEY = os.getenv("DOC_READER_SELECTION_SHORTCUT", DEFAULT_WINDOWS_SELECTION_HOTKEY).strip()
SAMPLE_RATE = 16000
HEARTBEAT_SECONDS = 2.0
MIN_DICTATION_SECONDS = 0.35
MAX_DICTATION_SECONDS = 180.0
TRANSCRIBE_TIMEOUT_SECONDS = 120.0
PID_FILE_NAME = "windows-helper.pid"


class _TimestampedLog:
    """Prefix each helper log line with the wall-clock time so events can be placed."""

    def __init__(self, stream) -> None:  # noqa: ANN001
        self._stream = stream
        self._at_line_start = True

    def write(self, text: str) -> int:
        out = []
        for piece in text.splitlines(keepends=True):
            if self._at_line_start and piece.strip():
                out.append(time.strftime("%Y-%m-%d %H:%M:%S ") + piece)
            else:
                out.append(piece)
            self._at_line_start = piece.endswith("\n")
        return self._stream.write("".join(out))

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._stream, name)


# ------------------------------------------------------------------ HTTP helpers


def _request_json(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 2.0) -> dict[str, Any]:
    data = None
    headers = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urlrequest.Request(f"{WEB_URL}{path}", data=data, headers=headers, method=method)
    with urlrequest.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body) if body else {}
    return parsed if isinstance(parsed, dict) else {}


def _post_empty(path: str, timeout: float = 3.0) -> dict[str, Any]:
    request = urlrequest.Request(f"{WEB_URL}{path}", data=b"", method="POST")
    with urlrequest.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body) if body else {}
    return parsed if isinstance(parsed, dict) else {}


def _web_reachable() -> bool:
    try:
        return bool(_request_json("/healthz", timeout=1.0).get("ok"))
    except (OSError, ValueError, urlerror.URLError):
        return False


# ------------------------------------------------------------------ keyboard helpers


def _parse_key(name: str):
    from pynput import keyboard

    if hasattr(keyboard.Key, name):
        return getattr(keyboard.Key, name)
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    raise ValueError(f"Unknown dictation key: {name}")


# Keys we have seen go down but not yet come up, with the time they went down.
# Only modifiers matter for a clean Ctrl+V, and a key that "went down" long ago
# is a phantom: its release happened on the lock screen, during sleep, or in an
# elevated window where the hook cannot see it.
_pressed_keys: dict[Any, float] = {}
_pressed_lock = threading.Lock()
_MODIFIER_NAMES = {"ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", "alt_gr", "shift", "shift_l", "shift_r", "cmd", "cmd_l", "cmd_r"}
_PHANTOM_KEY_SECONDS = 5.0


def _held_modifiers(now: float | None = None) -> list[str]:
    now = time.monotonic() if now is None else now
    with _pressed_lock:
        stale = [key for key, since in _pressed_keys.items() if now - since > _PHANTOM_KEY_SECONDS]
        for key in stale:
            _pressed_keys.pop(key, None)
        return sorted(
            getattr(key, "name", str(key))
            for key in _pressed_keys
            if getattr(key, "name", "") in _MODIFIER_NAMES
        )


def _wait_for_keys_released(max_seconds: float = 0.5) -> float:
    """Wait (briefly) until modifier keys are up so the injected Ctrl+V is clean.

    Returns the time spent waiting. Never blocks on keys that are not modifiers,
    and gives up after `max_seconds` so a missed key-up cannot stall every paste.
    """
    started = time.monotonic()
    deadline = started + max_seconds
    while time.monotonic() < deadline:
        held = _held_modifiers()
        if not held:
            break
        _process_qt_events()
        time.sleep(0.02)
    waited = time.monotonic() - started
    if waited >= max_seconds:
        print(f"[doc-reader] paste went ahead after {waited:.2f}s; still reported down: {_held_modifiers()}", flush=True)
    return waited


def _forget_pressed_keys() -> None:
    with _pressed_lock:
        _pressed_keys.clear()


def _process_qt_events() -> None:
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.processEvents()
    except Exception:  # noqa: BLE001
        pass


def _clipboard():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    return app.clipboard() if app is not None else None


def capture_selected_text() -> str:
    """Copy the current selection in the foreground app without losing the clipboard.

    Must be called on the Qt GUI thread (the tray helper marshals hotkey events there).
    """
    from pynput import keyboard

    clipboard = _clipboard()
    if clipboard is None:
        return ""
    previous = clipboard.text()
    marker = f"__DOC_READER_NO_SELECTION__{random.randint(100000, 999999)}"
    clipboard.setText(marker)
    _process_qt_events()
    _wait_for_keys_released()

    controller = keyboard.Controller()
    with controller.pressed(keyboard.Key.ctrl):
        controller.press("c")
        controller.release("c")

    selected = ""
    deadline = time.monotonic() + 0.9
    while time.monotonic() < deadline:
        _process_qt_events()
        current = clipboard.text()
        if current != marker:
            selected = current
            break
        time.sleep(0.03)

    clipboard.setText(previous)
    _process_qt_events()
    return selected.strip()


def paste_text(text: str) -> float:
    """Insert text into the active field via clipboard paste, then restore the clipboard.

    Returns the seconds spent waiting for modifier keys to come up.
    """
    from PySide6.QtCore import QTimer
    from pynput import keyboard

    clipboard = _clipboard()
    if clipboard is None:
        return 0.0
    previous = clipboard.text()
    clipboard.setText(text)
    _process_qt_events()
    waited = _wait_for_keys_released()
    controller = keyboard.Controller()
    with controller.pressed(keyboard.Key.ctrl):
        controller.press("v")
        controller.release("v")

    def restore() -> None:
        try:
            if clipboard.text() == text:
                clipboard.setText(previous)
        except Exception:  # noqa: BLE001
            pass

    QTimer.singleShot(600, restore)
    return waited


# ------------------------------------------------------------------ microphone


def _input_devices() -> list[dict[str, Any]]:
    """List input devices once each, preferring the WASAPI host API."""
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001
        return []
    preferred_api = None
    for index, api in enumerate(hostapis):
        if "WASAPI" in str(api.get("name", "")):
            preferred_api = index
            break
    if preferred_api is None and hostapis:
        preferred_api = sd.default.hostapi if sd.default.hostapi is not None else 0
    entries: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) <= 0:
            continue
        if preferred_api is not None and int(device.get("hostapi", -1)) != preferred_api:
            continue
        name = str(device.get("name", "")).strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        entries.append({"id": f"win-{index}", "name": name, "index": index})
    return entries


class Recorder:
    """Microphone capture with a pre-armed stream.

    Opening a Windows audio device costs a few hundred milliseconds, which is exactly
    the lag you feel between pressing the dictation key and recording starting. So
    while dictation is enabled the stream stays open and idle; pressing the key only
    flips a flag. A short pre-roll ring buffer is kept so the first syllable spoken
    right as the key goes down is not lost.
    """

    BLOCKSIZE = 512  # 32 ms at 16 kHz
    # A live microphone in a quiet room still has a noise floor. Exact digital
    # silence for a long stretch means we are reading an endpoint that is not the
    # one Windows is actually using (a headset that switched from its dongle to
    # Bluetooth after a lock/unlock, for example).
    SILENCE_RMS = 0.000015
    SILENT_RECHECK_SECONDS = 30.0
    # An open stream delivers a block every 32 ms. After sleep, screen lock, or an
    # audio-device reset, PortAudio keeps the stream object but the callbacks stop,
    # and every recording comes back empty. Treat a longer silence as a dead stream.
    STALE_SECONDS = 1.5

    def __init__(self, *, prearm: bool = True, preroll_seconds: float = 0.3) -> None:
        import collections

        self._stream = None
        self._stream_device: int | None = None
        self._stream_lock = threading.Lock()
        self._frames: list[bytes] = []
        self._preroll: collections.deque[bytes] = collections.deque(
            maxlen=max(1, int(preroll_seconds * SAMPLE_RATE / self.BLOCKSIZE))
        )
        self._lock = threading.Lock()
        self._capturing = False
        self.prearm = prearm
        self.level = 0.0
        self.peak = 0.0
        self.started_at = 0.0
        self.active = False
        self.last_error = ""
        self.last_data_at = 0.0
        self.reopen_count = 0
        self.captured_seconds = 0.0
        self.captured_rms = 0.0
        self.last_signal_at = 0.0
        self.device_name = ""
        self.device_index_used: int | None = None

    def _callback(self, indata, _frames, _time_info, _status) -> None:  # noqa: ANN001
        import numpy as np

        now = time.monotonic()
        self.last_data_at = now
        chunk = bytes(indata)
        with self._lock:
            capturing = self._capturing
            if capturing:
                self._frames.append(chunk)
            else:
                self._preroll.append(chunk)
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if not samples.size:
            return
        rms = float(np.sqrt(np.mean(samples * samples)))
        if rms > self.SILENCE_RMS:
            self.last_signal_at = now
        if capturing:
            self.level = min(1.0, rms * 6.0)
            self.peak = max(self.peak, self.level)

    def is_stale(self) -> bool:
        """True when the stream is open but has stopped delivering audio."""
        if self._stream is None:
            return False
        return time.monotonic() - self.last_data_at > self.STALE_SECONDS

    def silent_for(self) -> float:
        """Seconds of exact digital silence from an open stream (0 when a signal is present)."""
        if self._stream is None:
            return 0.0
        return time.monotonic() - self.last_signal_at

    @staticmethod
    def _wasapi_default_input(sd) -> int | None:  # noqa: ANN001
        """PortAudio index of the microphone Windows currently calls the default."""
        try:
            for api in sd.query_hostapis():
                if "WASAPI" in str(api.get("name", "")):
                    index = int(api.get("default_input_device", -1))
                    return index if index >= 0 else None
        except Exception:  # noqa: BLE001
            pass
        return None

    def _candidates(self, sd, device_index: int | None) -> list[tuple[int | None, object]]:  # noqa: ANN001
        """Devices to try, best first: the request via WASAPI (with automatic sample-rate
        conversion, which the wireless headsets need), then Windows' default input via
        WASAPI, then PortAudio's plain default."""
        wasapi = None
        try:
            wasapi = sd.WasapiSettings(auto_convert=True)
        except Exception:  # noqa: BLE001
            pass
        candidates: list[tuple[int | None, object]] = []
        if device_index is not None:
            candidates.append((device_index, wasapi))
            candidates.append((device_index, None))
        default_wasapi = self._wasapi_default_input(sd)
        if default_wasapi is not None:
            candidates.append((default_wasapi, wasapi))
        candidates.append((None, None))
        return candidates

    def _open_stream(self, device_index: int | None, *, force: bool = False) -> None:
        import sounddevice as sd

        with self._stream_lock:
            if self._stream is not None and self._stream_device == device_index and not force:
                return
            self._close_stream_locked()
            if force:
                # Re-scan devices: after sleep, a lock/unlock, or a headset switching
                # between its dongle and Bluetooth, the endpoint list and Windows'
                # default change, and PortAudio only notices on re-initialisation.
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:  # noqa: BLE001
                    pass
                self.reopen_count += 1
            last_error: Exception | None = None
            stream = None
            used: int | None = None
            for candidate, extra in self._candidates(sd, device_index):
                try:
                    kwargs = {"extra_settings": extra} if extra is not None else {}
                    stream = sd.InputStream(
                        samplerate=SAMPLE_RATE,
                        channels=1,
                        dtype="int16",
                        device=candidate,
                        callback=self._callback,
                        blocksize=self.BLOCKSIZE,
                        **kwargs,
                    )
                    stream.start()
                    used = candidate
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    stream = None
            if stream is None:
                raise last_error or RuntimeError("no microphone could be opened")
            self._stream = stream
            self._stream_device = device_index
            self.device_index_used = used
            try:
                resolved = used if used is not None else sd.default.device[0]
                self.device_name = str(sd.query_devices(resolved).get("name", "")) if resolved is not None and resolved >= 0 else ""
            except Exception:  # noqa: BLE001
                self.device_name = ""
            now = time.monotonic()
            self.last_data_at = now
            self.last_signal_at = now
            with self._lock:
                self._preroll.clear()

    def _close_stream_locked(self) -> None:
        stream = self._stream
        self._stream = None
        self._stream_device = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def is_armed(self) -> bool:
        return self._stream is not None

    def arm(self, device_index: int | None) -> bool:
        """Open the microphone ahead of time so the next key press starts instantly.

        Called on every heartbeat, so a stream that died while the PC slept is
        reopened within a couple of seconds of waking.
        """
        if self.active:
            return True
        try:
            self._open_stream(device_index, force=self.is_stale())
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False

    def reopen(self, device_index: int | None) -> bool:
        """Drop and reopen the stream (used right after the PC wakes)."""
        if self.active:
            return True
        try:
            self._open_stream(device_index, force=True)
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False

    def disarm(self) -> None:
        if self.active:
            return
        with self._stream_lock:
            self._close_stream_locked()

    def start(self, device_index: int | None) -> None:
        # Instant when already armed on this device; reopened first if the armed
        # stream went quiet (a dead stream would record nothing at all).
        self._open_stream(device_index, force=self.is_stale())
        with self._lock:
            self._frames = list(self._preroll)
            self._preroll.clear()
            self._capturing = True
        self.level = 0.0
        self.peak = 0.0
        self.started_at = time.monotonic()
        self.active = True

    def stop(self) -> tuple[bytes, float]:
        elapsed = time.monotonic() - self.started_at if self.started_at else 0.0
        self.active = False
        with self._lock:
            self._capturing = False
            raw = b"".join(self._frames)
            self._frames = []
        self.captured_seconds = len(raw) / 2 / SAMPLE_RATE
        self.captured_rms = 0.0
        if raw:
            import numpy as np

            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            self.captured_rms = float(np.sqrt(np.mean(samples * samples)))
        if elapsed >= 0.2 and self.captured_seconds < min(0.1, elapsed / 2):
            # The device delivered (almost) nothing for the whole hold: the stream is
            # dead. Mark it stale so the next arm/start reopens it.
            self.last_data_at = 0.0
            self.last_error = "the microphone delivered no audio"
        elif elapsed >= 0.5 and self.captured_rms <= self.SILENCE_RMS:
            # Frames arrived but they are exact silence: we are on the wrong endpoint.
            self.last_data_at = 0.0
            self.last_error = f"the microphone delivered only silence ({self.device_name or 'default device'})"
        if not self.prearm:
            with self._stream_lock:
                self._close_stream_locked()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(raw)
        return buffer.getvalue(), elapsed


# ------------------------------------------------------------------ Qt tray app


def _build_icon(recording: bool = False):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#17201c"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 28, 28, 7, 7)
    pen = QPen(QColor("#f4f8f4"))
    pen.setWidth(3)
    painter.setPen(pen)
    painter.drawLine(9, 11, 23, 11)
    painter.drawLine(9, 16, 23, 16)
    painter.drawLine(9, 21, 19, 21)
    if recording:
        painter.setBrush(QColor("#e5484d"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(20, 18, 9, 9)
    painter.end()
    return QIcon(pixmap)


def main() -> int:
    if not IS_WINDOWS:
        print("[doc-reader] The Windows helper only runs on Windows.")
        return 1
    try:
        from PySide6.QtCore import QObject, Qt, QTimer, Signal
        from PySide6.QtGui import QAction, QActionGroup, QFont
        from PySide6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon
        from pynput import keyboard, mouse
    except ModuleNotFoundError as exc:
        print(f"[doc-reader] Missing dependency for the Windows helper: {exc}")
        return 1

    root = managed_root()
    root.mkdir(parents=True, exist_ok=True)
    pid_path = root / PID_FILE_NAME
    try:
        existing = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        existing = 0
    if existing and existing != os.getpid() and pid_is_running(existing):
        print(f"[doc-reader] Windows helper already running (pid {existing}).")
        return 0
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    sys.stdout = _TimestampedLog(sys.stdout)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    power_events: list[str] = []
    try:
        from PySide6.QtCore import QAbstractNativeEventFilter

        class _PowerFilter(QAbstractNativeEventFilter):
            """Note Windows sleep/resume so the microphone stream is reopened on wake."""

            WM_POWERBROADCAST = 0x0218
            PBT_APMSUSPEND = 0x0004
            PBT_APMRESUMESUSPEND = 0x0007
            PBT_APMRESUMEAUTOMATIC = 0x0012
            WM_WTSSESSION_CHANGE = 0x02B1
            WTS_CONSOLE_CONNECT = 0x1
            WTS_SESSION_LOGON = 0x5
            WTS_SESSION_UNLOCK = 0x8

            def nativeEventFilter(self, event_type, message):  # noqa: ANN001, N802
                try:
                    import ctypes
                    import ctypes.wintypes as wintypes

                    msg = wintypes.MSG.from_address(int(message))
                    if msg.message == self.WM_POWERBROADCAST:
                        if msg.wParam in (self.PBT_APMRESUMESUSPEND, self.PBT_APMRESUMEAUTOMATIC):
                            power_events.append("resume")
                        elif msg.wParam == self.PBT_APMSUSPEND:
                            power_events.append("suspend")
                    elif msg.message == self.WM_WTSSESSION_CHANGE:
                        if msg.wParam in (self.WTS_CONSOLE_CONNECT, self.WTS_SESSION_LOGON, self.WTS_SESSION_UNLOCK):
                            power_events.append("unlock")
                except Exception:  # noqa: BLE001
                    pass
                return False, 0

        power_filter = _PowerFilter()
        app.installNativeEventFilter(power_filter)
    except Exception:  # noqa: BLE001
        pass
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("[doc-reader] System tray is not available.")
        return 1

    class Bridge(QObject):
        selectionRequested = Signal()
        dictationStarted = Signal()
        dictationStopped = Signal()
        transcriptReady = Signal(str)
        statusText = Signal(str)
        hudText = Signal(str)
        stateUpdated = Signal(dict)
        hotkeysChanged = Signal(dict)

    bridge = Bridge()
    prearm = os.getenv("DOC_READER_DICTATION_PREARM", "1").strip().lower() not in {"0", "false", "no", "off"}
    recorder = Recorder(prearm=prearm)
    state: dict[str, Any] = {
        "stt_enabled": True,
        "selected_microphone_id": "",
        "running": False,
        "paused": False,
        "active_id": "",
        "last_event": "native helper started",
        "last_recording": {},
        "web_ok": False,
    }
    devices_cache: list[dict[str, Any]] = []

    # ---------------------------------------------------------- HUD
    hud = QLabel()
    hud.setWindowFlags(
        Qt.WindowType.Tool
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.WindowDoesNotAcceptFocus
    )
    hud.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    hud.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    hud.setStyleSheet(
        "QLabel { background: rgba(23, 32, 28, 235); color: #f4f8f4; padding: 10px 18px;"
        " border-radius: 12px; font-size: 14px; }"
    )
    hud.setFont(QFont("Segoe UI", 11))

    def show_hud(text: str) -> None:
        if not text:
            hud.hide()
            return
        hud.setText(text)
        hud.adjustSize()
        screen = app.primaryScreen()
        if screen is not None:
            geometry = screen.availableGeometry()
            x = geometry.center().x() - hud.width() // 2
            y = geometry.bottom() - hud.height() - 60
            if not hud.isVisible() or abs(hud.x() - x) > 40:
                hud.move(x, y)
        hud.show()

    bridge.hudText.connect(show_hud)
    try:
        import ctypes

        ctypes.windll.wtsapi32.WTSRegisterSessionNotification(int(hud.winId()), 0)  # NOTIFY_FOR_THIS_SESSION
    except Exception:  # noqa: BLE001
        pass

    # ---------------------------------------------------------- tray
    tray = QSystemTrayIcon(_build_icon(), app)
    tray.setToolTip("Doc Reader")
    menu = QMenu()
    open_action = QAction("Open Doc Reader", menu)
    selection_action = QAction(f"Read Selection ({selection_hotkey_label()})", menu)
    clipboard_action = QAction("Read Clipboard", menu)
    pause_action = QAction("Pause", menu)
    stop_action = QAction("Stop Reading", menu)
    dictation_action = QAction(f"Dictation: hold {dictation_hotkey_label()}", menu)
    dictation_action.setCheckable(True)
    dictation_action.setChecked(True)
    hotkeys_menu = QMenu("Hotkeys", menu)
    dictation_key_menu = hotkeys_menu.addMenu("Dictation key")
    selection_key_menu = hotkeys_menu.addMenu("Read selection")
    dictation_key_group = QActionGroup(menu)
    selection_key_group = QActionGroup(menu)
    hotkey_actions: dict[str, dict[str, QAction]] = {"dictation": {}, "selection": {}}

    def _save_hotkey(field: str, value: str) -> None:
        def run() -> None:
            try:
                _request_json("/api/settings", payload={field: value}, timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                bridge.statusText.emit(f"Could not save hotkey: {exc}")

        threading.Thread(target=run, name="doc-reader-hotkey-save", daemon=True).start()

    for option in hotkey_options()["dictation"]:
        action = QAction(option["label"], dictation_key_menu)
        action.setCheckable(True)
        action.triggered.connect(lambda _checked=False, value=option["value"]: _save_hotkey("dictation_key", value))
        dictation_key_group.addAction(action)
        dictation_key_menu.addAction(action)
        hotkey_actions["dictation"][option["value"]] = action
    for option in hotkey_options()["selection"]:
        action = QAction(option["label"], selection_key_menu)
        action.setCheckable(True)
        action.triggered.connect(lambda _checked=False, value=option["value"]: _save_hotkey("selection_shortcut", value))
        selection_key_group.addAction(action)
        selection_key_menu.addAction(action)
        hotkey_actions["selection"][option["value"]] = action
    status_action = QAction("Starting...", menu)
    status_action.setEnabled(False)
    quit_action = QAction("Quit Helper", menu)
    for action in (open_action, selection_action, clipboard_action):
        menu.addAction(action)
    menu.addSeparator()
    menu.addAction(pause_action)
    menu.addAction(stop_action)
    menu.addSeparator()
    menu.addAction(dictation_action)
    menu.addMenu(hotkeys_menu)
    menu.addAction(status_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)

    def set_status(text: str) -> None:
        status_action.setText(text[:80])
        tray.setToolTip(f"Doc Reader - {text[:120]}")

    bridge.statusText.connect(set_status)

    def notify(title: str, text: str) -> None:
        try:
            tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 2500)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- web actions
    def ensure_web(then=None) -> None:
        if _web_reachable():
            if then:
                then()
            return
        set_status("Starting Doc Reader services...")

        def worker() -> None:
            try:
                from .windows_app import ensure_service

                ensure_service("tts", quiet=True)
                ensure_service("web", quiet=True)
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline and not _web_reachable():
                    time.sleep(0.5)
            except Exception as exc:  # noqa: BLE001
                bridge.statusText.emit(f"Could not start services: {exc}")
                return
            if then:
                QTimer.singleShot(0, then)

        threading.Thread(target=worker, name="doc-reader-ensure-web", daemon=True).start()

    def open_web() -> None:
        ensure_web(lambda: webbrowser.open(WEB_URL))

    def read_text(label: str, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            notify("Doc Reader", f"No {label.lower()} text to read.")
            return

        def send() -> None:
            try:
                _request_json("/api/text", payload={"label": label, "text": cleaned}, timeout=15.0)
                bridge.statusText.emit(f"Reading {label.lower()} text.")
            except Exception as exc:  # noqa: BLE001
                bridge.statusText.emit(f"Could not send text: {exc}")

        ensure_web(lambda: threading.Thread(target=send, daemon=True).start())

    def on_read_selection() -> None:
        text = capture_selected_text()
        if not text:
            clipboard = _clipboard()
            text = clipboard.text().strip() if clipboard is not None else ""
            if text:
                read_text("Clipboard", text)
                return
            notify("Doc Reader", f"No selected text detected. Highlight text, then press {selection_hotkey_label()}.")
            return
        read_text("Highlighted", text)

    def on_read_clipboard() -> None:
        clipboard = _clipboard()
        read_text("Clipboard", clipboard.text() if clipboard is not None else "")

    def on_pause() -> None:
        def worker() -> None:
            try:
                if state["paused"] and state["active_id"]:
                    _post_empty(f"/api/items/{state['active_id']}/play")
                else:
                    _post_empty("/api/pause")
            except Exception as exc:  # noqa: BLE001
                bridge.statusText.emit(f"Pause failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def on_stop() -> None:
        def worker() -> None:
            try:
                _post_empty("/api/stop")
            except Exception as exc:  # noqa: BLE001
                bridge.statusText.emit(f"Stop failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def on_toggle_dictation(checked: bool) -> None:
        state["stt_enabled"] = checked

        def worker() -> None:
            try:
                _request_json("/api/settings", payload={"stt_enabled": checked}, timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                bridge.statusText.emit(f"Could not update dictation setting: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    open_action.triggered.connect(open_web)
    selection_action.triggered.connect(on_read_selection)
    clipboard_action.triggered.connect(on_read_clipboard)
    pause_action.triggered.connect(on_pause)
    stop_action.triggered.connect(on_stop)
    dictation_action.toggled.connect(on_toggle_dictation)
    quit_action.triggered.connect(app.quit)
    bridge.selectionRequested.connect(on_read_selection)

    def on_tray_activated(reason) -> None:  # noqa: ANN001
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            open_web()

    tray.activated.connect(on_tray_activated)

    # ---------------------------------------------------------- dictation
    def selected_device_index() -> int | None:
        wanted = state.get("selected_microphone_id") or ""
        for device in devices_cache:
            if device["id"] == wanted:
                return int(device["index"])
        return None

    def on_dictation_started() -> None:
        if recorder.active:
            return
        if not state["stt_enabled"]:
            set_status("Dictation is disabled in Doc Reader settings.")
            return
        try:
            recorder.start(selected_device_index())
        except Exception as exc:  # noqa: BLE001
            state["last_event"] = f"recording failed: {exc}"
            set_status(f"Microphone error: {exc}")
            notify("Doc Reader", f"Could not start recording: {exc}")
            return
        hud_state["recording_since"] = time.monotonic()
        render_recording_hud()
        level_timer.start()
        state["last_event"] = "recording started"
        tray.setIcon(_build_icon(recording=True))
        set_status("Recording dictation...")

    def render_recording_hud() -> None:
        """Recording HUD: key to release, which microphone, and a live level bar."""
        key_name = dictation_hotkey_label(bindings["dictation_name"])
        device = recorder.device_name or "default microphone"
        filled = max(0, min(10, int(round(recorder.level * 10))))
        quiet_for = time.monotonic() - max(recorder.last_signal_at, hud_state.get("recording_since", 0.0))
        if recorder.level <= 0.0 and quiet_for > 1.5:
            meter = "<span style='color:#F0B45C'>no sound from the mic</span>"
        else:
            meter = (
                f"<span style='color:#62D0BC'>{'▮' * filled}</span>"
                f"<span style='color:#4b5a55'>{'▮' * (10 - filled)}</span>"
            )
        show_hud(
            f"<span style='color:#e5484d'>●</span>&nbsp; Recording &nbsp;{meter}&nbsp; "
            f"<span style='color:#b0b8c4'>{device}</span> &nbsp;·&nbsp; release {key_name} to transcribe"
        )

    def on_dictation_stopped() -> None:
        if not recorder.active:
            return
        level_timer.stop()
        audio, elapsed = recorder.stop()
        tray.setIcon(_build_icon())
        peak = recorder.peak
        if elapsed < MIN_DICTATION_SECONDS:
            show_hud("")
            state["last_event"] = "recording too short"
            set_status("Dictation too short.")
            return
        if recorder.captured_seconds < min(0.1, elapsed / 2) or (elapsed >= 0.5 and recorder.captured_rms <= recorder.SILENCE_RMS):
            # Nothing, or exact silence, came from the microphone (typical right after
            # sleep, or when the headset moved between its dongle and Bluetooth).
            # Re-scan devices and reopen on Windows' current default so the next
            # attempt works, and say so instead of sending silence to Whisper.
            was = recorder.device_name or "default device"
            reopened = recorder.reopen(selected_device_index())
            now_on = recorder.device_name or "default device"
            show_hud("")
            state["last_event"] = (
                f"microphone gave no sound on {was}; reopened on {now_on}" if reopened
                else f"microphone unavailable: {recorder.last_error}"
            )
            set_status("No sound from the microphone. Try again." if reopened else f"Microphone error: {recorder.last_error}")
            notify("Doc Reader", f"No sound came from the microphone ({was}). Reopened on {now_on}; please try again.")
            print(f"[doc-reader] {state['last_event']} (hold {elapsed:.1f}s)", flush=True)
            return
        show_hud("Transcribing…")
        state["last_event"] = "transcribing"
        set_status("Transcribing dictation...")
        released_at = time.monotonic()
        saved = _save_recording(root, audio, elapsed, peak)
        if saved:
            state["last_recording"] = saved
        timing["released_at"] = released_at
        timing["saved_seconds"] = time.monotonic() - released_at

        def worker() -> None:
            try:
                sent_at = time.monotonic()
                request = urlrequest.Request(
                    f"{WEB_URL}/api/transcribe",
                    data=audio,
                    method="POST",
                    headers={
                        "Content-Type": "audio/wav",
                        "X-Doc-Reader-Language": os.getenv("DOC_READER_STT_LANGUAGE", "en"),
                        "X-Doc-Reader-Elapsed-Seconds": f"{elapsed:.6f}",
                    },
                )
                with urlrequest.urlopen(request, timeout=TRANSCRIBE_TIMEOUT_SECONDS) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                timing["request_seconds"] = time.monotonic() - sent_at
                text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
                if text:
                    state["last_event"] = "transcription received"
                    bridge.transcriptReady.emit(text)
                else:
                    state["last_event"] = "transcription produced no text"
                    bridge.statusText.emit("Dictation produced no text.")
                    bridge.hudText.emit("")
            except urlerror.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
                state["last_event"] = f"transcription failed: {detail}"
                bridge.statusText.emit(f"Transcription failed: {detail}")
                bridge.hudText.emit("")
            except Exception as exc:  # noqa: BLE001
                state["last_event"] = f"transcription failed: {exc}"
                bridge.statusText.emit(f"Transcription failed: {exc}")
                bridge.hudText.emit("")

        threading.Thread(target=worker, name="doc-reader-transcribe", daemon=True).start()

    def on_transcript_ready(text: str) -> None:
        show_hud("")
        paste_started = time.monotonic()
        waited = paste_text(text)
        state["last_event"] = "transcription inserted"
        set_status(f"Dictation inserted ({len(text)} chars).")
        released_at = timing.get("released_at") or paste_started
        print(
            "[doc-reader] dictation timing: "
            f"release->text {time.monotonic() - released_at:.2f}s "
            f"(save {timing.get('saved_seconds', 0.0):.2f}s, transcribe request {timing.get('request_seconds', 0.0):.2f}s, "
            f"key wait {waited:.2f}s, paste {time.monotonic() - paste_started - waited:.2f}s)",
            flush=True,
        )

    bridge.dictationStarted.connect(on_dictation_started)
    bridge.dictationStopped.connect(on_dictation_stopped)
    bridge.transcriptReady.connect(on_transcript_ready)

    # ---------------------------------------------------------- keyboard listener
    hotkey_state = {"dictation_down": False, "last_selection": 0.0}
    timing: dict[str, float] = {}
    hud_state: dict[str, float] = {}
    level_timer = QTimer()
    level_timer.setInterval(70)
    level_timer.timeout.connect(lambda: render_recording_hud() if recorder.active else level_timer.stop())

    def on_hotkey() -> None:
        now = time.monotonic()
        if now - hotkey_state["last_selection"] < 0.8:
            return
        hotkey_state["last_selection"] = now
        bridge.selectionRequested.emit()

    # The bindings live in a dict so the heartbeat can swap them without restarting
    # the listener when the web page or tray menu picks a different key.
    bindings: dict[str, Any] = {
        "dictation_name": "",
        "selection_name": "",
        "dictation_keys": frozenset(),   # pynput Key objects that count as the dictation key
        "dictation_mouse": None,         # pynput mouse.Button for a side-button dictation key
        "selection": None,
    }

    def _dictation_keys_for(name: str) -> frozenset:
        """Generic modifiers ("ctrl") match either side; everything else is one key."""
        if name in ("ctrl", "alt", "shift"):
            return frozenset(_parse_key(candidate) for candidate in (name, f"{name}_l", f"{name}_r"))
        return frozenset([_parse_key(name)])

    def apply_hotkeys(dictation_name: str, selection_name: str) -> bool:
        changed = False
        dictation_name = normalize_dictation_key(dictation_name) or DEFAULT_WINDOWS_DICTATION_KEY
        selection_name = normalize_selection_shortcut(selection_name) or DEFAULT_WINDOWS_SELECTION_HOTKEY
        if dictation_name != bindings["dictation_name"]:
            if dictation_name.startswith("mouse:"):
                bindings["dictation_keys"] = frozenset()
                bindings["dictation_mouse"] = mouse.Button.x1 if dictation_name == "mouse:x1" else mouse.Button.x2
            else:
                bindings["dictation_mouse"] = None
                try:
                    bindings["dictation_keys"] = _dictation_keys_for(dictation_name)
                except (ValueError, AttributeError) as exc:
                    print(f"[doc-reader] {exc}; falling back to {DEFAULT_WINDOWS_DICTATION_KEY}", flush=True)
                    dictation_name = DEFAULT_WINDOWS_DICTATION_KEY
                    bindings["dictation_keys"] = _dictation_keys_for(dictation_name)
            bindings["dictation_name"] = dictation_name
            changed = True
        if selection_name != bindings["selection_name"]:
            try:
                bindings["selection"] = keyboard.HotKey(keyboard.HotKey.parse(selection_name), on_hotkey)
            except ValueError as exc:
                print(f"[doc-reader] {exc}; falling back to {DEFAULT_WINDOWS_SELECTION_HOTKEY}", flush=True)
                selection_name = DEFAULT_WINDOWS_SELECTION_HOTKEY
                bindings["selection"] = keyboard.HotKey(keyboard.HotKey.parse(selection_name), on_hotkey)
            bindings["selection_name"] = selection_name
            changed = True
        return changed

    apply_hotkeys(DICTATION_KEY_NAME, SELECTION_HOTKEY)

    def on_hotkeys_changed(payload: dict) -> None:
        dictation_name = str(payload.get("dictation_key") or bindings["dictation_name"])
        selection_name = str(payload.get("selection_shortcut") or bindings["selection_name"])
        selection_action.setText(f"Read Selection ({selection_hotkey_label(selection_name)})")
        dictation_action.setText(f"Dictation: hold {dictation_hotkey_label(dictation_name)}")
        for value, action in hotkey_actions["dictation"].items():
            action.setChecked(value == dictation_name)
        for value, action in hotkey_actions["selection"].items():
            action.setChecked(value == selection_name)

    bridge.hotkeysChanged.connect(on_hotkeys_changed)
    bridge.hotkeysChanged.emit({"dictation_key": bindings["dictation_name"], "selection_shortcut": bindings["selection_name"]})

    listener_holder: dict[str, Any] = {}

    def canonical(key):  # noqa: ANN001
        listener = listener_holder.get("listener")
        return listener.canonical(key) if listener is not None else key

    def on_press(key) -> None:  # noqa: ANN001
        with _pressed_lock:
            _pressed_keys.setdefault(key, time.monotonic())
        try:
            bindings["selection"].press(canonical(key))
        except Exception:  # noqa: BLE001
            pass
        if key in bindings["dictation_keys"] and not hotkey_state["dictation_down"]:
            hotkey_state["dictation_down"] = True
            bridge.dictationStarted.emit()

    def on_release(key) -> None:  # noqa: ANN001
        with _pressed_lock:
            _pressed_keys.pop(key, None)
        try:
            bindings["selection"].release(canonical(key))
        except Exception:  # noqa: BLE001
            pass
        if key in bindings["dictation_keys"] and hotkey_state["dictation_down"]:
            hotkey_state["dictation_down"] = False
            bridge.dictationStopped.emit()

    def on_click(_x, _y, button, pressed) -> None:  # noqa: ANN001
        wanted = bindings["dictation_mouse"]
        if wanted is None or button != wanted:
            return
        if pressed and not hotkey_state["dictation_down"]:
            hotkey_state["dictation_down"] = True
            bridge.dictationStarted.emit()
        elif not pressed and hotkey_state["dictation_down"]:
            hotkey_state["dictation_down"] = False
            bridge.dictationStopped.emit()

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener_holder["listener"] = listener
    listener.daemon = True
    listener.start()
    # Side mouse buttons (Mouse 4 / Mouse 5) can be the dictation key too.
    mouse_listener = mouse.Listener(on_click=on_click)
    mouse_listener.daemon = True
    mouse_listener.start()

    # Safety: never record longer than MAX_DICTATION_SECONDS even if a release is missed.
    def enforce_max_recording() -> None:
        if recorder.active and time.monotonic() - recorder.started_at > MAX_DICTATION_SECONDS:
            hotkey_state["dictation_down"] = False
            on_dictation_stopped()

    guard_timer = QTimer()
    guard_timer.setInterval(1000)
    guard_timer.timeout.connect(enforce_max_recording)
    guard_timer.start()

    # ---------------------------------------------------------- heartbeat
    reopen_deadline: dict[str, Any] = {"at": 0.0, "why": ""}

    def heartbeat_loop() -> None:
        first = True
        while True:
            try:
                if power_events:
                    events = list(power_events)
                    power_events.clear()
                    _forget_pressed_keys()
                    if "resume" in events or "unlock" in events:
                        # Give Bluetooth headsets a moment to reconnect, then re-scan and
                        # reopen on whatever Windows now calls the default microphone.
                        reopen_deadline["at"] = time.monotonic() + 4.0
                        reopen_deadline["why"] = "PC resumed" if "resume" in events else "session unlocked"
                    elif "suspend" in events:
                        recorder.disarm()
                if reopen_deadline["at"] and time.monotonic() >= reopen_deadline["at"] and not recorder.active:
                    why = reopen_deadline["why"]
                    reopen_deadline["at"] = 0.0
                    recorder.reopen(selected_device_index())
                    print(f"[doc-reader] {why}; microphone reopened on {recorder.device_name or 'default device'}.", flush=True)
                    state["last_event"] = f"microphone reopened after {why}"
                devices = _input_devices()
                devices_cache[:] = devices
                active_id = ""
                for device in devices:
                    if recorder.device_name and device["name"] == recorder.device_name:
                        active_id = device["id"]
                payload: dict[str, Any] = {
                    "devices": [{"id": d["id"], "name": d["name"]} for d in devices],
                    "microphone_authorization": "authorized",
                    "input_monitoring_trusted": True,
                    "accessibility_trusted": True,
                    "active_microphone_id": active_id,
                    "active_microphone_name": recorder.device_name,
                    "recording": bool(recorder.active),
                    "recording_start_pending": False,
                    "last_dictation_event": "native helper started" if first else state["last_event"],
                    "audio_level": float(recorder.level if recorder.active else 0.0),
                    "audio_peak_level": float(recorder.peak),
                }
                if state.get("last_recording"):
                    payload.update(state["last_recording"])
                _request_json("/api/native/dictation", payload=payload, timeout=1.0)
                status = _request_json("/api/native/status", timeout=1.0)
                stt = status.get("stt") if isinstance(status.get("stt"), dict) else {}
                microphone = stt.get("microphone") if isinstance(stt.get("microphone"), dict) else {}
                state["web_ok"] = True
                state["stt_enabled"] = bool(stt.get("enabled", True))
                state["selected_microphone_id"] = str(microphone.get("selected_id") or "")
                state["running"] = bool(status.get("running"))
                state["paused"] = bool(status.get("paused"))
                state["active_id"] = str(status.get("active_id") or "")
                hotkeys = stt.get("hotkeys") if isinstance(stt.get("hotkeys"), dict) else {}
                if hotkeys and apply_hotkeys(str(hotkeys.get("dictation_key") or ""), str(hotkeys.get("selection_shortcut") or "")):
                    bridge.hotkeysChanged.emit({
                        "dictation_key": bindings["dictation_name"],
                        "selection_shortcut": bindings["selection_name"],
                    })
                    print(
                        f"[doc-reader] Hotkeys now: read selection {selection_hotkey_label(bindings['selection_name'])}, "
                        f"dictation hold {dictation_hotkey_label(bindings['dictation_name'])}.",
                        flush=True,
                    )
                # Keep the microphone stream open (idle) so the dictation key starts
                # capturing immediately instead of waiting for the device to open.
                if recorder.prearm and state["stt_enabled"]:
                    was_stale = recorder.is_stale()
                    if not recorder.arm(selected_device_index()) and recorder.last_error:
                        state["last_event"] = f"microphone unavailable: {recorder.last_error}"
                    elif was_stale:
                        state["last_event"] = "microphone stream reopened after it went quiet"
                        print(f"[doc-reader] microphone stream went quiet (sleep or device reset); reopened on {recorder.device_name or 'default device'}.", flush=True)
                    elif not recorder.active and recorder.silent_for() > recorder.SILENT_RECHECK_SECONDS:
                        was = recorder.device_name or "default device"
                        if recorder.reopen(selected_device_index()):
                            print(f"[doc-reader] {was} gave exact silence for {recorder.SILENT_RECHECK_SECONDS:.0f}s; re-scanned, now on {recorder.device_name or 'default device'}.", flush=True)
                elif recorder.prearm:
                    recorder.disarm()
                bridge.stateUpdated.emit(
                    {
                        "status": str(status.get("status") or "Doc Reader ready."),
                        "running": state["running"],
                        "paused": state["paused"],
                        "stt_enabled": state["stt_enabled"],
                    }
                )
                first = False
            except Exception:  # noqa: BLE001
                state["web_ok"] = False
                bridge.stateUpdated.emit({"status": "Doc Reader web app is not running.", "running": False, "paused": False, "stt_enabled": state["stt_enabled"]})
            time.sleep(HEARTBEAT_SECONDS)

    def on_state_updated(payload: dict) -> None:
        if not recorder.active:
            set_status(str(payload.get("status") or ""))
        pause_action.setText("Resume" if payload.get("paused") else "Pause")
        pause_action.setEnabled(bool(payload.get("running")) or bool(payload.get("paused")))
        stop_action.setEnabled(bool(payload.get("running")) or bool(payload.get("paused")))
        wanted = bool(payload.get("stt_enabled"))
        if dictation_action.isChecked() != wanted:
            dictation_action.blockSignals(True)
            dictation_action.setChecked(wanted)
            dictation_action.blockSignals(False)

    bridge.stateUpdated.connect(on_state_updated)
    threading.Thread(target=heartbeat_loop, name="doc-reader-heartbeat", daemon=True).start()

    tray.show()
    set_status("Doc Reader helper online.")
    print(
        f"[doc-reader] Windows helper online. Read selection: {selection_hotkey_label()}. "
        f"Dictation: hold {dictation_hotkey_label()}.",
        flush=True,
    )
    try:
        return app.exec()
    finally:
        try:
            listener.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass


def _save_recording(root: Path, audio: bytes, elapsed: float, peak: float) -> dict[str, Any]:
    recordings_dir = root / "dictation-recordings"
    try:
        recordings_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        path = recordings_dir / f"dictation-{stamp}.wav"
        path.write_bytes(audio)
    except OSError:
        return {}
    return {
        "last_recording_path": str(path),
        "last_recording_bytes": len(audio),
        "last_recording_seconds": round(elapsed, 3),
        "last_recording_content_type": "audio/wav",
        "last_recording_peak_level": round(peak, 3),
        "last_recording_created_at": time.time(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
