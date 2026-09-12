from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from PySide6.QtCore import (
        QObject,
        QPoint,
        QProcess,
        QProcessEnvironment,
        QSettings,
        Qt,
        QTimer,
        Signal,
    )
    from PySide6.QtGui import QAction, QCursor, QIcon, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMenu,
        QPushButton,
        QSystemTrayIcon,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - runtime dependency guard
    raise SystemExit(
        "[doc-reader] PySide6 is required for the tray app. "
        "Install dependencies with: pip install -r requirements.txt"
    ) from exc

from .extract import iter_document_blocks
from .platform_tools import (
    DEFAULT_WINDOWS_SELECTION_HOTKEY,
    IS_WINDOWS,
    LOCAL_KOKORO_LABEL,
    kill_process_tree,
    selection_hotkey_label,
)
from .speech import (
    OPENAI_TTS_INSTRUCTIONS,
    OPENAI_TTS_MODEL,
    OPENAI_TTS_RESPONSE_FORMAT,
    OPENAI_TTS_VOICE,
    OPENAI_TTS_VOICES,
    resolve_openai_api_key,
)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".markdown"}
DEFAULT_MODE = "full"
DEFAULT_STYLE = "balanced"
DEFAULT_RATE = 180
MAX_SYSTEM_VOICES = 8
OPENAI_TTS_VOICE_LABELS = [
    "marin",
    "cedar",
    "coral",
    "alloy",
    "ash",
    "ballad",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
]
SYSTEM_BACKEND = "macsay" if sys.platform == "darwin" else "pyttsx3"
LOCAL_TTS_OPTIONS = [
    ("Remote Kokoro (strict)", "tailscale-4090"),
    ("Remote Kokoro", "tailscale-kokoro"),
    ("Remote Chatterbox (experimental)", "tailscale-chatterbox"),
    (LOCAL_KOKORO_LABEL, "local-kokoro"),
]
DEFAULT_SELECTION_SHORTCUT = (
    DEFAULT_WINDOWS_SELECTION_HOTKEY if IS_WINDOWS else "<ctrl>+<alt>+<cmd>+r"
)
SELECTION_SHORTCUT = os.getenv("DOC_READER_SELECTION_SHORTCUT", DEFAULT_SELECTION_SHORTCUT)
SELECTION_SHORTCUT_LABEL = selection_hotkey_label()
SERVICE_INBOX_DIR = Path.home() / ".doc-reader-managed" / "service-inbox"
LIBRARY_SETTINGS_KEY = "library/items_v1"
LIBRARY_MAX_ITEMS = 50
CHAPTER_MAX_ITEMS = 60
CHAPTER_TITLE_MAX_WORDS = 14
CHAPTER_WORD_OFFSET_MIN_GAP = 50
VOICE_BACKEND_SETTINGS_KEY = "voice/backend"
VOICE_VALUE_SETTINGS_KEY = "voice/value"

CHAPTER_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(chapter|chap\.?)\s+([0-9]+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)\b[:.\-\s]*",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(part|book|section)\s+([0-9]+|[ivxlcdm]+|[a-z]+)\b[:.\-\s]*",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(prologue|epilogue|introduction|preface|foreword|afterword|appendix(?:\s+[a-z0-9]+)?)\b",
        flags=re.IGNORECASE,
    ),
)
CHUNK_START_RE = re.compile(r"^\[doc-reader\]\s+chunk-start\s+index=(\d+)\s*$")
CHUNK_DONE_RE = re.compile(r"^\[doc-reader\]\s+chunk-done\s+index=(\d+)\s*$")
CHUNK_PAGE_RE = re.compile(r"^\[doc-reader\]\s+page\s+number=(\d+)(?:\s+chunk=(\d+))?\s*$")


def _is_supported_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES


def _set_macos_accessory_activation_policy() -> None:
    if sys.platform != "darwin":
        return

    try:
        import ctypes

        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        ns_application = objc.objc_getClass(b"NSApplication")
        shared_application = objc.sel_registerName(b"sharedApplication")
        objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        app = objc.objc_msgSend(ns_application, shared_application)

        set_activation_policy = objc.sel_registerName(b"setActivationPolicy:")
        objc.objc_msgSend.restype = ctypes.c_bool
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        objc.objc_msgSend(app, set_activation_policy, 1)
    except Exception:
        return


def _build_fallback_icon() -> QIcon:
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(Qt.GlobalColor.white)
    pen.setWidth(2)
    painter.setPen(pen)
    painter.drawRoundedRect(2, 2, 16, 16, 4, 4)
    painter.drawLine(6, 8, 14, 8)
    painter.drawLine(6, 11, 14, 11)
    painter.drawLine(6, 14, 12, 14)
    painter.end()

    return QIcon(pixmap)


def _tray_icon() -> QIcon:
    icon = QIcon.fromTheme("audio-x-generic")
    if not icon.isNull():
        return icon
    return _build_fallback_icon()


def _load_system_voices(limit: int = MAX_SYSTEM_VOICES) -> list[str]:
    if sys.platform != "darwin":
        return ["Default"]

    say_bin = shutil.which("say")
    if not say_bin:
        return ["Default"]

    try:
        raw = subprocess.check_output([say_bin, "-v", "?"], stderr=subprocess.DEVNULL, text=True)
    except Exception:  # noqa: BLE001
        return ["Default"]

    voices: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        name = stripped.split()[0]
        if name not in voices:
            voices.append(name)

    preferred = ["Samantha", "Alex", "Allison", "Ava", "Daniel", "Karen", "Moira", "Tessa"]
    ordered: list[str] = [name for name in preferred if name in voices]
    ordered.extend(name for name in voices if name not in ordered)

    return ordered[: max(1, limit)]


def _word_count_inline(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _normalize_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("-:;,. ")
    return text


def _is_explicit_chapter_marker(line: str) -> bool:
    for pattern in CHAPTER_LINE_PATTERNS:
        if pattern.match(line):
            return True
    return False


def _extract_explicit_chapter_label(line: str) -> str | None:
    for pattern in CHAPTER_LINE_PATTERNS:
        match = pattern.match(line)
        if not match:
            continue
        label = line[: match.end()].strip()
        label = label.strip(":-. ")
        return label or None
    return None


def _chapter_label_from_block(block: str) -> str | None:
    line = _normalize_title(block)
    if not line:
        return None

    explicit_label = _extract_explicit_chapter_label(line)
    if explicit_label:
        return explicit_label

    if len(line) > 120:
        return None

    words = line.split()
    if len(words) > CHAPTER_TITLE_MAX_WORDS:
        return None

    lower = line.lower()

    # Fallback for short title-style lines often used as headings in books.
    if len(words) >= 2 and len(words) <= 8:
        has_terminal_punctuation = line.endswith((".", "!", "?", ";", ","))
        if not has_terminal_punctuation:
            title_tokens = 0
            for token in words:
                stripped = token.strip("()[]{}\"'`")
                if not stripped:
                    continue
                if stripped.isupper() or stripped[:1].isupper():
                    title_tokens += 1
            if title_tokens >= max(2, len(words) - 1):
                banned = {"table of contents", "contents", "copyright", "title page"}
                if lower not in banned:
                    return line

    return None


def _detect_chapters_for_path(path: Path, rate_wpm: int = DEFAULT_RATE) -> list[dict[str, object]]:
    chapters: list[dict[str, object]] = [{"label": "Start of document", "seconds": 0.0}]
    seen_labels: set[str] = {"start of document"}
    words_seen = 0
    last_added_words = -10_000

    try:
        for block_index, block in enumerate(iter_document_blocks(path)):
            text = _normalize_title(block)
            if not text:
                continue

            label = _chapter_label_from_block(text)
            if label:
                normalized = label.lower()
                explicit = _is_explicit_chapter_marker(label)
                if (
                    normalized not in seen_labels
                    and (
                        explicit
                        or (words_seen - last_added_words) >= CHAPTER_WORD_OFFSET_MIN_GAP
                    )
                ):
                    seconds = (words_seen / max(60, int(rate_wpm))) * 60.0
                    chapters.append({"label": label, "seconds": max(0.0, seconds)})
                    seen_labels.add(normalized)
                    last_added_words = words_seen
                    if len(chapters) >= CHAPTER_MAX_ITEMS:
                        break

            words_seen += _word_count_inline(text)
            if block_index > 20000:
                break
    except Exception:  # noqa: BLE001
        return chapters

    return chapters


def _capture_selected_text_from_frontmost_app() -> str:
    if IS_WINDOWS:
        from .windows_helper import capture_selected_text

        return capture_selected_text()
    if sys.platform != "darwin":
        return ""

    script = """
set previousClipboard to missing value
set hadPrevious to true
try
    set previousClipboard to the clipboard
on error
    set hadPrevious to false
end try

set marker to "__DOC_READER_NO_SELECTION__" & (random number from 100000 to 999999) as text
set the clipboard to marker

tell application "System Events"
    keystroke "c" using command down
end tell

delay 0.18

set selectedText to ""
try
    set selectedText to the clipboard as text
end try

if hadPrevious then
    set the clipboard to previousClipboard
else
    set the clipboard to ""
end if

if selectedText is marker then
    return ""
end if

return selectedText
"""

    try:
        output = subprocess.check_output(
            ["osascript", "-e", script],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3.0,
        )
    except Exception:  # noqa: BLE001
        return ""

    return output.strip()


def _read_clipboard_text() -> str:
    if sys.platform != "darwin":
        clipboard = QApplication.clipboard()
        return (clipboard.text() if clipboard is not None else "").strip()
    try:
        output = subprocess.check_output(
            ["pbpaste"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except Exception:  # noqa: BLE001
        return ""
    return output.strip()


class GlobalSelectionShortcut(QObject):
    triggered = Signal()
    statusChanged = Signal(str)

    def __init__(self, hotkey: str) -> None:
        super().__init__()
        self.hotkey = hotkey
        self._listener = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return

        try:
            from pynput import keyboard
        except ModuleNotFoundError:
            self.statusChanged.emit(
                "[doc-reader] Global shortcut disabled: install pynput to enable selection hotkey."
            )
            return

        def on_activate() -> None:
            self.triggered.emit()

        try:
            self._listener = keyboard.GlobalHotKeys({self.hotkey: on_activate})
            self._listener.start()
            self._started = True
            display_hotkey = (
                SELECTION_SHORTCUT_LABEL if self.hotkey == DEFAULT_SELECTION_SHORTCUT else self.hotkey
            )
            self.statusChanged.emit(
                f"[doc-reader] Selection shortcut ready: {display_hotkey}."
            )
        except Exception as exc:  # noqa: BLE001
            self.statusChanged.emit(f"[doc-reader] Global shortcut unavailable: {exc}")

    def stop(self) -> None:
        if not self._listener:
            return
        try:
            self._listener.stop()
        except Exception:  # noqa: BLE001
            pass
        self._listener = None
        self._started = False


class DropTarget(QFrame):
    fileDropped = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame { border: 1px dashed #7a7a7a; border-radius: 8px; background: #1f1f1f; }"
            "QLabel { color: #f0f0f0; }"
        )

        layout = QVBoxLayout(self)
        label = QLabel("Drop a document here")
        sub = QLabel("or use Browse to pick a file")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        layout.addWidget(sub)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if not mime.hasUrls():
            event.ignore()
            return

        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_file() and _is_supported_file(path):
                event.acceptProposedAction()
                return

        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_file() and _is_supported_file(path):
                self.fileDropped.emit(str(path))
                event.acceptProposedAction()
                return
        event.ignore()


class ReaderRunner(QObject):
    outputLine = Signal(str)
    statusChanged = Signal(str)
    runningChanged = Signal(bool)
    pausedChanged = Signal(bool)
    pageChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._buffer = ""
        self._process = QProcess(self)
        self._wire_process(self._process)

        self._is_paused = False
        self._start_monotonic: float | None = None
        self._paused_at: float | None = None
        self._paused_total_seconds = 0.0
        self._start_offset_seconds = 0.0
        self._last_position_seconds = 0.0
        self._paused_resume_seconds = 0.0
        self._start_chunk_index = 0
        self._active_chunk_index: int | None = None
        self._last_completed_chunk_index = -1
        self._resume_chunk_index = 0
        self._current_page_number: int | None = None
        self._start_options: dict[str, object] | None = None
        self._pause_in_progress = False

    def _wire_process(self, process: QProcess) -> None:
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)

    def _replace_process(self) -> None:
        old = self._process
        try:
            old.readyReadStandardOutput.disconnect(self._read_output)
        except Exception:  # noqa: BLE001
            pass
        try:
            old.finished.disconnect(self._on_finished)
        except Exception:  # noqa: BLE001
            pass
        try:
            old.errorOccurred.disconnect(self._on_error)
        except Exception:  # noqa: BLE001
            pass
        old.deleteLater()

        self._process = QProcess(self)
        self._wire_process(self._process)

    def is_running(self) -> bool:
        return self._process.state() != QProcess.ProcessState.NotRunning

    def is_paused(self) -> bool:
        return self._is_paused

    def start(
        self,
        source_file: Path,
        *,
        speech_backend: str,
        voice_value: str | None,
        openai_api_key: str | None,
        openai_model: str,
        openai_response_format: str,
        openai_instructions: str,
        start_seconds: float = 0.0,
        start_chunk_index: int = 0,
        track_progress: bool = True,
    ) -> bool:
        if self.is_running():
            self.stop()

        args = [
            "-m",
            "doc_reader",
            str(source_file),
            "--mode",
            DEFAULT_MODE,
            "--style",
            DEFAULT_STYLE,
            "--rate",
            str(DEFAULT_RATE),
            "--speech-backend",
            speech_backend,
            "--start-chunk-index",
            str(max(0, int(start_chunk_index))),
            "--start-seconds",
            f"{max(0.0, start_seconds):.2f}",
            "--verbose",
        ]

        if speech_backend == "openai":
            args.extend(["--openai-model", openai_model])
            args.extend(["--openai-response-format", openai_response_format])
            if voice_value:
                args.extend(["--openai-voice", voice_value])
            if openai_instructions:
                args.extend(["--openai-instructions", openai_instructions])
        elif speech_backend in {"macsay", "pyttsx3"} and voice_value:
            args.extend(["--voice", voice_value])

        env = QProcessEnvironment.systemEnvironment()
        if speech_backend == "openai" and openai_api_key:
            env.insert("OPENAI_API_KEY", openai_api_key)
        elif speech_backend != "openai":
            env.insert("DOC_READER_AUTO_ALLOW_OPENAI", "0")
            for key in ("OPENAI_API_KEY", "DOC_READER_OPENAI_API_KEY"):
                if env.contains(key):
                    env.remove(key)
        self._process.setProcessEnvironment(env)

        self.statusChanged.emit(f"Starting: {source_file.name}")
        self._buffer = ""
        self._process.start(sys.executable, args)

        if not self._process.waitForStarted(2500):
            self.statusChanged.emit("Could not start reader process.")
            self.runningChanged.emit(False)
            return False

        self._start_monotonic = time.monotonic()
        self._paused_at = None
        self._paused_total_seconds = 0.0
        self._is_paused = False
        self._pause_in_progress = False
        self._start_offset_seconds = max(0.0, float(start_seconds))
        self._last_position_seconds = self._start_offset_seconds
        self._paused_resume_seconds = self._start_offset_seconds
        self._start_chunk_index = max(0, int(start_chunk_index))
        self._active_chunk_index = None
        self._last_completed_chunk_index = self._start_chunk_index - 1
        self._resume_chunk_index = self._start_chunk_index
        self._current_page_number = None
        self._start_options = {
            "source_file": Path(source_file),
            "speech_backend": speech_backend,
            "voice_value": voice_value,
            "openai_api_key": openai_api_key,
            "openai_model": openai_model,
            "openai_response_format": openai_response_format,
            "openai_instructions": openai_instructions,
            "start_chunk_index": self._start_chunk_index,
            "track_progress": bool(track_progress),
        }

        self.runningChanged.emit(True)
        self.pausedChanged.emit(False)
        return True

    def stop(self) -> None:
        if not self.is_running():
            if self._is_paused:
                self._is_paused = False
                self._pause_in_progress = False
                self._paused_resume_seconds = self._last_position_seconds
                self._resume_chunk_index = max(
                    self._resume_chunk_index,
                    self._last_completed_chunk_index + 1,
                )
                self.statusChanged.emit("Stopped.")
                self.pausedChanged.emit(False)
            return

        self._pause_in_progress = False
        self.statusChanged.emit("Stopping...")
        root_pid = int(self._process.processId())

        self._resume_tree(root_pid)
        # Stop child audio processes first, then kill the reader quickly so UI unblocks.
        self._kill_process_tree(root_pid, include_root=False, force=True)
        self._process.terminate()
        if not self._process.waitForFinished(220):
            self._kill_process_tree(root_pid, include_root=True, force=True)
            self._process.kill()
            self._process.waitForFinished(420)

        if self.is_running():
            # Last-resort reset so controls recover even if QProcess gets wedged.
            try:
                fallback_pid = int(self._process.processId())
            except Exception:  # noqa: BLE001
                fallback_pid = 0
            if fallback_pid > 0:
                self._kill_process_tree(fallback_pid, include_root=True, force=True)
            self._replace_process()
            self.runningChanged.emit(False)
            self.statusChanged.emit("Stopped.")

        self._is_paused = False
        self._pause_in_progress = False
        self._paused_resume_seconds = self._last_position_seconds
        self._resume_chunk_index = max(
            self._resume_chunk_index,
            self._last_completed_chunk_index + 1,
        )
        self.pausedChanged.emit(False)

    def toggle_pause(self) -> bool | None:
        if self._is_paused and not self.is_running():
            return self._resume_from_pause()

        if not self.is_running():
            return None

        # Pause by stopping playback and resuming from an offset. This avoids
        # low-level SIGSTOP audio glitches (stuck/repeating syllables) on macOS.
        self._paused_resume_seconds = self.current_position_seconds()
        if self._active_chunk_index is not None:
            self._resume_chunk_index = max(0, self._active_chunk_index)
        else:
            self._resume_chunk_index = max(
                self._resume_chunk_index,
                self._last_completed_chunk_index + 1,
            )
        self._is_paused = True
        self._pause_in_progress = True
        self.statusChanged.emit("Pausing...")
        self.pausedChanged.emit(True)

        root_pid = int(self._process.processId())
        self._resume_tree(root_pid)
        self._kill_process_tree(root_pid, include_root=False, force=True)
        self._process.terminate()
        if not self._process.waitForFinished(220):
            self._kill_process_tree(root_pid, include_root=True, force=True)
            self._process.kill()
            self._process.waitForFinished(420)

        if self.is_running():
            self._replace_process()
            self.runningChanged.emit(False)
            self.statusChanged.emit("Paused.")
            self._pause_in_progress = False

        return True

    def _resume_from_pause(self) -> bool | None:
        if not self._is_paused:
            return None
        if self._start_options is None:
            self._is_paused = False
            self.pausedChanged.emit(False)
            return None

        options = dict(self._start_options)
        source_file = options.get("source_file")
        speech_backend = options.get("speech_backend")
        if not isinstance(source_file, Path) or not isinstance(speech_backend, str):
            self._is_paused = False
            self.pausedChanged.emit(False)
            return None

        voice_value = options.get("voice_value")
        if not isinstance(voice_value, str):
            voice_value = None
        openai_api_key = options.get("openai_api_key")
        if not isinstance(openai_api_key, str):
            openai_api_key = None
        openai_model = options.get("openai_model")
        if not isinstance(openai_model, str):
            openai_model = OPENAI_TTS_MODEL
        openai_response_format = options.get("openai_response_format")
        if not isinstance(openai_response_format, str):
            openai_response_format = OPENAI_TTS_RESPONSE_FORMAT
        openai_instructions = options.get("openai_instructions")
        if not isinstance(openai_instructions, str):
            openai_instructions = OPENAI_TTS_INSTRUCTIONS
        track_progress = bool(options.get("track_progress", True))

        target = max(0.0, self._paused_resume_seconds)
        resume_chunk = max(0, int(self._resume_chunk_index))
        started = self.start(
            source_file,
            speech_backend=speech_backend,
            voice_value=voice_value,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            openai_response_format=openai_response_format,
            openai_instructions=openai_instructions,
            start_seconds=0.0,
            start_chunk_index=resume_chunk,
            track_progress=track_progress,
        )
        if not started:
            return None

        if target > 0.0:
            self.statusChanged.emit(f"Resumed at about {int(target)}s.")
        else:
            self.statusChanged.emit("Resumed.")
        return False

    def rewind_seconds(self, seconds: float = 15.0) -> bool:
        if not self._start_options:
            return False

        seconds = max(0.0, seconds)
        current = self.current_position_seconds()
        target = max(0.0, current - seconds)

        options = dict(self._start_options)
        source_file = options.get("source_file")
        speech_backend = options.get("speech_backend")
        if not isinstance(source_file, Path) or not isinstance(speech_backend, str):
            return False

        voice_value = options.get("voice_value")
        if not isinstance(voice_value, str):
            voice_value = None
        openai_api_key = options.get("openai_api_key")
        if not isinstance(openai_api_key, str):
            openai_api_key = None
        openai_model = options.get("openai_model")
        if not isinstance(openai_model, str):
            openai_model = OPENAI_TTS_MODEL
        openai_response_format = options.get("openai_response_format")
        if not isinstance(openai_response_format, str):
            openai_response_format = OPENAI_TTS_RESPONSE_FORMAT
        openai_instructions = options.get("openai_instructions")
        if not isinstance(openai_instructions, str):
            openai_instructions = OPENAI_TTS_INSTRUCTIONS
        track_progress = bool(options.get("track_progress", True))

        started = self.start(
            source_file,
            speech_backend=speech_backend,
            voice_value=voice_value,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            openai_response_format=openai_response_format,
            openai_instructions=openai_instructions,
            start_seconds=target,
            start_chunk_index=0,
            track_progress=track_progress,
        )
        if started:
            self.statusChanged.emit(f"Rewound to about {int(target)}s.")
        return started

    def current_position_seconds(self) -> float:
        if self.is_running() and self._start_monotonic is not None:
            now = time.monotonic()
            paused_seconds = self._paused_total_seconds
            if self._is_paused and self._paused_at is not None:
                paused_seconds += max(0.0, now - self._paused_at)
            elapsed = max(0.0, now - self._start_monotonic - paused_seconds)
            self._last_position_seconds = self._start_offset_seconds + elapsed
            return self._last_position_seconds

        return self._last_position_seconds

    def current_source_file(self) -> Path | None:
        if not self._start_options:
            return None
        source = self._start_options.get("source_file")
        if isinstance(source, Path):
            return source
        return None

    def should_track_progress(self) -> bool:
        if not self._start_options:
            return False
        return bool(self._start_options.get("track_progress", True))

    def current_resume_chunk_index(self) -> int:
        return max(0, int(self._resume_chunk_index))

    def _send_signal_tree(self, pid: int, sig: signal.Signals, *, include_root: bool) -> None:
        descendants = self._descendant_pids(pid)
        targets = descendants + ([pid] if include_root else [])
        for child_pid in reversed(targets):
            try:
                os.kill(child_pid, sig)
            except OSError:
                continue

    def _resume_tree(self, pid: int) -> None:
        sigcont = getattr(signal, "SIGCONT", None)
        if sigcont is None:
            return
        self._send_signal_tree(pid, sigcont, include_root=True)

    def _kill_process_tree(self, pid: int, *, include_root: bool, force: bool) -> None:
        if IS_WINDOWS:
            # taskkill /T always includes the root; the caller terminates it anyway.
            kill_process_tree(pid, force=force)
            return
        sig = signal.SIGKILL if force else signal.SIGTERM
        self._send_signal_tree(pid, sig, include_root=include_root)

    def _descendant_pids(self, root_pid: int) -> list[int]:
        if root_pid <= 0:
            return []

        descendants: list[int] = []
        queue = [root_pid]
        seen = {root_pid}

        while queue:
            parent = queue.pop(0)
            for child in self._child_pids(parent):
                if child in seen:
                    continue
                seen.add(child)
                descendants.append(child)
                queue.append(child)

        return descendants

    def _child_pids(self, parent_pid: int) -> list[int]:
        try:
            output = subprocess.check_output(
                ["pgrep", "-P", str(parent_pid)],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:  # noqa: BLE001
            return []

        pids: list[int] = []
        for token in output.split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        return pids

    def _on_error(self, _error) -> None:
        self.statusChanged.emit("Reader process error.")

    def _on_finished(self, code: int, _status) -> None:
        self.current_position_seconds()
        self._start_monotonic = None
        self._paused_at = None
        self._paused_total_seconds = 0.0
        self._start_offset_seconds = 0.0
        self.runningChanged.emit(False)

        if self._pause_in_progress:
            self._pause_in_progress = False
            self._is_paused = True
            self._paused_resume_seconds = self._last_position_seconds
            if self._active_chunk_index is not None:
                self._resume_chunk_index = max(0, self._active_chunk_index)
            else:
                self._resume_chunk_index = max(
                    self._resume_chunk_index,
                    self._last_completed_chunk_index + 1,
                )
            self.pausedChanged.emit(True)
            self.statusChanged.emit("Paused.")
            return

        self._is_paused = False
        self._active_chunk_index = None
        self._resume_chunk_index = max(
            self._resume_chunk_index,
            self._last_completed_chunk_index + 1,
        )
        self.pausedChanged.emit(False)
        if code == 0:
            self.statusChanged.emit("Ready.")
        elif code == 130:
            self.statusChanged.emit("Stopped.")
        else:
            self.statusChanged.emit(f"Reader exited with code {code}.")

    def _read_output(self) -> None:
        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not chunk:
            return

        self._buffer += chunk
        lines = self._buffer.splitlines(keepends=True)
        self._buffer = ""

        for line in lines:
            if line.endswith("\n") or line.endswith("\r"):
                clean = line.strip()
                if self._track_chunk_progress(clean):
                    continue
                self.outputLine.emit(clean)
            else:
                self._buffer = line

    def _track_chunk_progress(self, line: str) -> bool:
        if not line:
            return False

        match_page = CHUNK_PAGE_RE.match(line)
        if match_page:
            page_number = int(match_page.group(1))
            self._current_page_number = page_number
            self.pageChanged.emit(str(page_number))
            return True

        match_start = CHUNK_START_RE.match(line)
        if match_start:
            idx = int(match_start.group(1))
            self._active_chunk_index = idx
            self._resume_chunk_index = max(0, idx)
            return True

        match_done = CHUNK_DONE_RE.match(line)
        if match_done:
            idx = int(match_done.group(1))
            self._last_completed_chunk_index = max(self._last_completed_chunk_index, idx)
            if self._active_chunk_index == idx:
                self._active_chunk_index = None
            self._resume_chunk_index = max(self._resume_chunk_index, idx + 1)
            return True

        return False


class ReaderPanel(QWidget):
    stopRequested = Signal()

    def __init__(self, runner: ReaderRunner) -> None:
        super().__init__()
        self.runner = runner
        self.selected_file: Path | None = None
        self._source_preference = "file"
        self._inline_text_file = Path(tempfile.gettempdir()) / f"doc_reader_inline_{os.getpid()}.txt"
        self._chapter_cache: dict[str, list[dict[str, object]]] = {}
        self._chapter_path: str | None = None
        self._chapter_populating = False
        self._chapter_jump_pending = False
        self.settings = QSettings("DocReader", "DocReader")
        self.openai_api_key = resolve_openai_api_key()
        self.openai_model = os.getenv("DOC_READER_OPENAI_MODEL", OPENAI_TTS_MODEL).strip() or OPENAI_TTS_MODEL
        self.openai_response_format = (
            os.getenv("DOC_READER_OPENAI_RESPONSE_FORMAT", OPENAI_TTS_RESPONSE_FORMAT).strip()
            or OPENAI_TTS_RESPONSE_FORMAT
        )
        self.openai_instructions = os.getenv(
            "DOC_READER_OPENAI_INSTRUCTIONS",
            OPENAI_TTS_INSTRUCTIONS,
        ).strip()
        self._voice_populating = False

        self.setWindowTitle("Doc Reader")
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)

        self.status_label = QLabel("Ready. Choose a document or paste text to begin.")
        root.addWidget(self.status_label)
        self.page_label = QLabel("Current page: n/a")
        root.addWidget(self.page_label)

        self.drop_target = DropTarget()
        self.drop_target.fileDropped.connect(self.load_and_start)
        root.addWidget(self.drop_target)

        self.path_input = QLineEdit()
        self.path_input.setReadOnly(True)
        self.path_input.setPlaceholderText("No document selected")
        root.addWidget(self.path_input)

        library_row = QHBoxLayout()
        library_row.addWidget(QLabel("Library"))
        self.library_combo = QComboBox()
        library_row.addWidget(self.library_combo)
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.clicked.connect(self._resume_from_library)
        library_row.addWidget(self.resume_btn)
        root.addLayout(library_row)

        chapter_row = QHBoxLayout()
        chapter_row.addWidget(QLabel("Chapter"))
        self.chapter_combo = QComboBox()
        self.chapter_combo.currentIndexChanged.connect(self._on_chapter_changed)
        chapter_row.addWidget(self.chapter_combo)
        root.addLayout(chapter_row)
        self._clear_chapter_combo()

        self.text_input = QTextEdit()
        self.text_input.setAcceptRichText(False)
        self.text_input.setPlaceholderText("Or paste text here, then click Start.")
        self.text_input.setFixedHeight(120)
        self.text_input.textChanged.connect(self._on_text_input_changed)
        root.addWidget(self.text_input)

        api_row = QHBoxLayout()
        api_row.addWidget(QLabel("OpenAI Key"))
        self.api_key_status = QLabel("")
        self.api_key_status.setMinimumWidth(140)
        api_row.addWidget(self.api_key_status)
        root.addLayout(api_row)

        voice_row = QHBoxLayout()
        voice_row.addWidget(QLabel("Voice"))
        self.voice_combo = QComboBox()
        self.voice_combo.currentIndexChanged.connect(self._on_voice_changed)
        voice_row.addWidget(self.voice_combo)
        root.addLayout(voice_row)

        buttons = QHBoxLayout()
        self.browse_btn = QPushButton("Browse...")
        self.start_btn = QPushButton("Start")
        self.pause_btn = QPushButton("Pause")
        self.back_btn = QPushButton("Back 15s")
        self.stop_btn = QPushButton("Stop")
        self.pause_btn.setEnabled(False)
        self.back_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        self.browse_btn.clicked.connect(self.pick_file_and_start)
        self.start_btn.clicked.connect(self.start_current)
        self.pause_btn.clicked.connect(self._pause_or_resume)
        self.back_btn.clicked.connect(self._rewind_15)
        self.stop_btn.clicked.connect(self._request_stop)

        buttons.addWidget(self.browse_btn)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.pause_btn)
        buttons.addWidget(self.back_btn)
        buttons.addWidget(self.stop_btn)
        root.addLayout(buttons)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Reader output will appear here.")
        root.addWidget(self.log)

        self.runner.outputLine.connect(self._append_log)
        self.runner.statusChanged.connect(self.status_label.setText)
        self.runner.statusChanged.connect(self._append_log)
        self.runner.runningChanged.connect(self._update_running_state)
        self.runner.pausedChanged.connect(self._on_paused_changed)
        self.runner.pageChanged.connect(self._on_page_changed)

        self._populate_voice_options()
        self.refresh_library_list()
        if self.openai_api_key:
            self._set_api_key_status("success", "Available")
        else:
            self._set_api_key_status("neutral", "Not found")

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()

    def pick_file_and_start(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a document",
            str(Path.home()),
            "Documents (*.pdf *.docx *.txt *.md *.markdown);;All files (*)",
        )
        if not path:
            return
        self.load_and_start(path)

    def load_and_start(self, path_str: str) -> None:
        path = Path(path_str).expanduser().resolve()
        if not path.exists() or not path.is_file():
            self._append_log(f"File not found: {path}")
            return
        if not _is_supported_file(path):
            self._append_log(f"Unsupported file type: {path.suffix}")
            return

        self.selected_file = path
        self._source_preference = "file"
        self.path_input.setText(str(path))
        self._populate_chapters_for_file(path)
        self.start_current()

    def load_text_and_start(self, text: str) -> bool:
        cleaned = text.strip()
        if not cleaned:
            return False
        self.text_input.setPlainText(cleaned)
        self._source_preference = "text"
        self.start_current()
        return True

    def start_current(self, *, start_seconds: float = 0.0) -> None:
        source_file, source_label, track_progress = self._resolve_source_for_start()
        if source_file is None:
            self._append_log("Pick/drop a file or paste text first.")
            return

        effective_start = max(0.0, float(start_seconds))
        if track_progress and effective_start <= 0.0:
            chapter_seconds = self._selected_chapter_seconds_for_file(source_file)
            effective_start = max(effective_start, chapter_seconds)
        self._start_source(
            source_file,
            source_label=source_label,
            track_progress=track_progress,
            start_seconds=effective_start,
        )

    def _start_source(
        self,
        source_file: Path,
        *,
        source_label: str,
        track_progress: bool,
        start_seconds: float,
        start_chunk_index: int = 0,
    ) -> None:
        start_seconds = max(0.0, float(start_seconds))
        start_chunk_index = max(0, int(start_chunk_index))

        choice = self.voice_combo.currentData()
        if not isinstance(choice, dict) or not choice.get("backend"):
            self._append_log("Pick a voice first.")
            return

        backend = str(choice["backend"])
        voice_value = choice.get("value")
        if isinstance(voice_value, str):
            voice_value = voice_value.strip() or None
        else:
            voice_value = None

        if backend == "openai" and not self.openai_api_key:
            self.openai_api_key = resolve_openai_api_key()
        if backend == "openai" and not self.openai_api_key:
            self._append_log("[doc-reader] OpenAI API key not found in env, Keychain, or ORP secrets.")
            return

        self._set_page_for_source(source_file)

        started = self.runner.start(
            source_file,
            speech_backend=backend,
            voice_value=voice_value,
            openai_api_key=self.openai_api_key or None,
            openai_model=self.openai_model,
            openai_response_format=self.openai_response_format,
            openai_instructions=self.openai_instructions,
            start_seconds=start_seconds,
            start_chunk_index=start_chunk_index,
            track_progress=track_progress,
        )
        if started:
            if track_progress:
                self.save_library_progress(
                    source_file,
                    start_seconds,
                    chunk_index=start_chunk_index,
                    refresh_ui=True,
                )
            if start_chunk_index > 0 and track_progress:
                self._append_log(f"Starting {source_label} from saved position.")
            elif start_seconds > 0.0 and track_progress:
                self._append_log(f"Starting {source_label} at about {int(start_seconds)}s")
            else:
                self._append_log(f"Reading {source_label}")
        else:
            self._set_page_for_idle()

    def _on_text_input_changed(self) -> None:
        if self.text_input.toPlainText().strip():
            self._source_preference = "text"
            return
        if self.selected_file is not None:
            self._source_preference = "file"

    def _resolve_source_for_start(self) -> tuple[Path | None, str, bool]:
        pasted_text = self.text_input.toPlainText()
        has_pasted_text = bool(pasted_text.strip())

        if self._source_preference == "text" and has_pasted_text:
            inline_path = self._prepare_inline_text_source(pasted_text)
            if inline_path is None:
                return None, "", False
            return inline_path, "pasted text", False

        if self.selected_file is not None:
            return self.selected_file, self.selected_file.name, True

        if has_pasted_text:
            inline_path = self._prepare_inline_text_source(pasted_text)
            if inline_path is None:
                return None, "", False
            return inline_path, "pasted text", False

        return None, "", False

    def _prepare_inline_text_source(self, text: str) -> Path | None:
        cleaned = text.strip()
        if not cleaned:
            return None

        try:
            self._inline_text_file.write_text(f"{cleaned}\n", encoding="utf-8")
        except OSError as exc:
            self._append_log(f"Could not prepare pasted text: {exc}")
            return None

        self.path_input.setText("(Using pasted text)")
        self._source_preference = "text"
        return self._inline_text_file

    def _clear_chapter_combo(self, text: str = "Start of document") -> None:
        self._chapter_populating = True
        self.chapter_combo.clear()
        self.chapter_combo.addItem(text, {"seconds": 0.0})
        self._chapter_populating = False

    def _populate_chapters_for_file(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        key = str(resolved)
        self._chapter_path = key

        entries = self._chapter_cache.get(key)
        if entries is None:
            self._append_log("[doc-reader] Detecting chapters...")
            entries = _detect_chapters_for_path(resolved, rate_wpm=DEFAULT_RATE)
            self._chapter_cache[key] = entries
            detected = max(0, len(entries) - 1)
            if detected > 0:
                self._append_log(f"[doc-reader] Detected {detected} chapter markers.")
            else:
                self._append_log("[doc-reader] No chapter headings detected; using start of document.")

        self._chapter_populating = True
        self.chapter_combo.clear()
        for entry in entries:
            label = str(entry.get("label", "Start of document"))
            seconds = max(0.0, float(entry.get("seconds", 0.0)))
            if seconds <= 0.0:
                display = label
            else:
                display = f"{label} • {self._format_time(seconds)}"
            self.chapter_combo.addItem(display, {"seconds": seconds, "label": label})
        self._chapter_populating = False

        if self.chapter_combo.count() == 0:
            self._clear_chapter_combo()

    def _selected_chapter_seconds_for_file(self, source_file: Path) -> float:
        if self._source_preference != "file":
            return 0.0
        if self._chapter_path is None:
            return 0.0
        try:
            selected = source_file.expanduser().resolve()
        except OSError:
            return 0.0
        if str(selected) != self._chapter_path:
            return 0.0

        data = self.chapter_combo.currentData()
        if not isinstance(data, dict):
            return 0.0
        try:
            return max(0.0, float(data.get("seconds", 0.0)))
        except (TypeError, ValueError):
            return 0.0

    def _select_closest_chapter_for_seconds(self, seconds: float) -> None:
        target = max(0.0, float(seconds))
        if self.chapter_combo.count() <= 0:
            return

        best_index = 0
        best_seconds = -1.0
        for idx in range(self.chapter_combo.count()):
            data = self.chapter_combo.itemData(idx)
            if not isinstance(data, dict):
                continue
            try:
                item_seconds = max(0.0, float(data.get("seconds", 0.0)))
            except (TypeError, ValueError):
                item_seconds = 0.0
            if item_seconds <= target and item_seconds >= best_seconds:
                best_seconds = item_seconds
                best_index = idx
        self._chapter_populating = True
        self.chapter_combo.setCurrentIndex(best_index)
        self._chapter_populating = False

    def _selected_chapter_data(self) -> tuple[str, float]:
        data = self.chapter_combo.currentData()
        if not isinstance(data, dict):
            return "Start of document", 0.0
        label = str(data.get("label", "Start of document"))
        try:
            seconds = max(0.0, float(data.get("seconds", 0.0)))
        except (TypeError, ValueError):
            seconds = 0.0
        return label, seconds

    def _on_chapter_changed(self, _index: int) -> None:
        if self._chapter_populating:
            return
        if self.selected_file is None:
            return
        if not self.runner.is_running():
            label, _ = self._selected_chapter_data()
            self._append_log(f"[doc-reader] Chapter selected: {label}. Press Start to listen there.")
            return

        if self._chapter_jump_pending:
            return

        label, seconds = self._selected_chapter_data()
        if seconds <= 0.0:
            return

        self._chapter_jump_pending = True
        try:
            self._start_source(
                self.selected_file,
                source_label=self.selected_file.name,
                track_progress=True,
                start_seconds=seconds,
            )
            self._append_log(f"[doc-reader] Jumped to {label}.")
        finally:
            self._chapter_jump_pending = False

    def refresh_library_list(self) -> None:
        current_path = ""
        current_data = self.library_combo.currentData()
        if isinstance(current_data, dict):
            value = current_data.get("path")
            if isinstance(value, str):
                current_path = value

        self.library_combo.clear()
        items = self._load_library_items()
        if not items:
            self.library_combo.addItem("No saved books yet", {"kind": "empty"})
            model = self.library_combo.model()
            item = getattr(model, "item", lambda _i: None)(0)
            if item is not None and hasattr(item, "setEnabled"):
                item.setEnabled(False)
            self.resume_btn.setEnabled(False)
            return

        selected_index = 0
        for idx, entry in enumerate(items):
            path = str(entry.get("path", ""))
            name = str(entry.get("name", Path(path).name if path else "Untitled"))
            seconds = float(entry.get("last_seconds", 0.0))
            label = f"{name} • {self._format_time(seconds)}"
            self.library_combo.addItem(label, entry)
            if current_path and path == current_path:
                selected_index = idx

        self.library_combo.setCurrentIndex(selected_index)
        self.resume_btn.setEnabled(not self.runner.is_running())

    def save_library_progress(
        self,
        source_file: Path,
        seconds: float,
        *,
        chunk_index: int,
        refresh_ui: bool,
    ) -> None:
        resolved = source_file.expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            return
        if not _is_supported_file(resolved):
            return

        items = self._load_library_items()
        target_path = str(resolved)
        seconds = max(0.0, float(seconds))
        chunk_index = max(0, int(chunk_index))
        updated = False
        now = int(time.time())

        for entry in items:
            if str(entry.get("path", "")) != target_path:
                continue
            entry["name"] = resolved.name
            entry["last_seconds"] = seconds
            entry["resume_chunk_index"] = chunk_index
            entry["updated_at"] = now
            updated = True
            break

        if not updated:
            items.append(
                {
                    "path": target_path,
                    "name": resolved.name,
                    "last_seconds": seconds,
                    "resume_chunk_index": chunk_index,
                    "updated_at": now,
                }
            )

        items.sort(key=lambda entry: int(entry.get("updated_at", 0)), reverse=True)
        items = items[:LIBRARY_MAX_ITEMS]
        self._save_library_items(items)
        if refresh_ui:
            self.refresh_library_list()

    def _resume_from_library(self) -> None:
        data = self.library_combo.currentData()
        if not isinstance(data, dict) or not data.get("path"):
            self._append_log("No library item selected.")
            return

        path = Path(str(data.get("path", ""))).expanduser()
        if not path.exists() or not path.is_file():
            self._append_log(f"Library file not found: {path}")
            self._remove_library_path(path)
            self.refresh_library_list()
            return

        resolved = path.resolve()
        self.selected_file = resolved
        self._source_preference = "file"
        self.path_input.setText(str(resolved))
        self._populate_chapters_for_file(resolved)
        start_seconds = float(data.get("last_seconds", 0.0))
        try:
            resume_chunk_index = max(0, int(data.get("resume_chunk_index", 0)))
        except (TypeError, ValueError):
            resume_chunk_index = 0
        self._select_closest_chapter_for_seconds(start_seconds)
        # Older library entries may only have second-based progress; backtrack a bit
        # to avoid skipping content on first resume after upgrade.
        start_seconds_for_resume = 0.0 if resume_chunk_index > 0 else max(0.0, start_seconds - 20.0)
        self._start_source(
            resolved,
            source_label=resolved.name,
            track_progress=True,
            start_seconds=start_seconds_for_resume,
            start_chunk_index=resume_chunk_index,
        )

    def _load_library_items(self) -> list[dict[str, object]]:
        raw = self.settings.value(LIBRARY_SETTINGS_KEY, "")
        if not raw:
            return []
        if isinstance(raw, list):
            return []
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []

        items: list[dict[str, object]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not path.strip():
                continue
            try:
                last_seconds = max(0.0, float(entry.get("last_seconds", 0.0)))
            except (TypeError, ValueError):
                last_seconds = 0.0
            try:
                updated_at = int(entry.get("updated_at", 0))
            except (TypeError, ValueError):
                updated_at = 0
            try:
                resume_chunk_index = max(0, int(entry.get("resume_chunk_index", 0)))
            except (TypeError, ValueError):
                resume_chunk_index = 0
            item: dict[str, object] = {
                "path": path.strip(),
                "name": str(entry.get("name", Path(path).name)),
                "last_seconds": last_seconds,
                "resume_chunk_index": resume_chunk_index,
                "updated_at": updated_at,
            }
            items.append(item)
        return items

    def _save_library_items(self, items: list[dict[str, object]]) -> None:
        self.settings.setValue(LIBRARY_SETTINGS_KEY, json.dumps(items))

    def _remove_library_path(self, path: Path) -> None:
        target = str(path.expanduser().resolve())
        items = [entry for entry in self._load_library_items() if str(entry.get("path", "")) != target]
        self._save_library_items(items)

    def _format_time(self, seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes:02d}m"
        return f"{minutes}m {secs:02d}s"

    def _append_log(self, message: str) -> None:
        if not message:
            return
        self.log.append(message)

    def _set_page_for_idle(self) -> None:
        self.page_label.setText("Current page: n/a")

    def _set_page_for_source(self, source_file: Path) -> None:
        if source_file.suffix.lower() == ".pdf":
            self.page_label.setText("Current page: loading...")
            return
        self._set_page_for_idle()

    def _on_page_changed(self, page_text: str) -> None:
        page = page_text.strip()
        if not page:
            self.page_label.setText("Current page: loading...")
            return
        self.page_label.setText(f"Current page: {page}")

    def _update_running_state(self, running: bool) -> None:
        paused = self.runner.is_paused()
        active = running or paused
        self.stop_btn.setEnabled(active)
        self.pause_btn.setEnabled(active)
        self.back_btn.setEnabled(active)
        self.start_btn.setEnabled(not active)
        has_library_items = self.library_combo.count() > 0 and isinstance(
            self.library_combo.itemData(0), dict
        ) and bool(self.library_combo.itemData(0).get("path"))
        self.resume_btn.setEnabled((not active) and has_library_items)
        if not paused:
            self.pause_btn.setText("Pause")
        else:
            self.pause_btn.setText("Resume")

    def _on_paused_changed(self, paused: bool) -> None:
        self.pause_btn.setText("Resume" if paused else "Pause")
        self._update_running_state(self.runner.is_running())

    def _pause_or_resume(self) -> None:
        result = self.runner.toggle_pause()
        if result is None:
            self._append_log("Nothing is currently playing.")

    def _rewind_15(self) -> None:
        rewound = self.runner.rewind_seconds(15.0)
        if not rewound:
            self._append_log("Could not rewind.")

    def _request_stop(self) -> None:
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.back_btn.setEnabled(False)
        self.stopRequested.emit()

    def _set_api_key_status(self, level: str, text: str) -> None:
        if level == "success":
            self.api_key_status.setText(f"✓ {text}")
            self.api_key_status.setStyleSheet("QLabel { color: #22c55e; font-weight: 600; }")
            return
        if level == "error":
            self.api_key_status.setText(f"✗ {text}")
            self.api_key_status.setStyleSheet("QLabel { color: #ef4444; font-weight: 600; }")
            return
        if level == "info":
            self.api_key_status.setText(text)
            self.api_key_status.setStyleSheet("QLabel { color: #d1d5db; }")
            return
        self.api_key_status.setText(text)
        self.api_key_status.setStyleSheet("QLabel { color: #9ca3af; }")

    def _populate_voice_options(self) -> None:
        self._voice_populating = True
        try:
            self.voice_combo.clear()

            self._add_disabled_item("Local Voices")
            for label, backend in LOCAL_TTS_OPTIONS:
                self.voice_combo.addItem(label, {"backend": backend, "value": None})

            self._add_disabled_item("OpenAI Voices")
            for voice in OPENAI_TTS_VOICE_LABELS:
                if voice not in OPENAI_TTS_VOICES:
                    continue
                self.voice_combo.addItem(
                    f"OpenAI • {voice}",
                    {"backend": "openai", "value": voice},
                )

            self._add_disabled_item("Operating System Voices")
            system_voices = _load_system_voices(MAX_SYSTEM_VOICES)
            for voice in system_voices:
                value = None if voice == "Default" else voice
                label = f"System • {voice}"
                self.voice_combo.addItem(label, {"backend": SYSTEM_BACKEND, "value": value})

            self._select_preferred_voice()
        finally:
            self._voice_populating = False

    def _add_disabled_item(self, label: str) -> None:
        index = self.voice_combo.count()
        self.voice_combo.addItem(label, {"kind": "label"})
        model = self.voice_combo.model()
        item = getattr(model, "item", lambda _i: None)(index)
        if item is not None and hasattr(item, "setEnabled"):
            item.setEnabled(False)

    def _select_first_valid_voice(self) -> None:
        for i in range(self.voice_combo.count()):
            data = self.voice_combo.itemData(i)
            if isinstance(data, dict) and data.get("backend"):
                self.voice_combo.setCurrentIndex(i)
                return

    def _saved_voice_choice(self) -> tuple[str | None, str | None]:
        backend = str(self.settings.value(VOICE_BACKEND_SETTINGS_KEY, "") or "").strip().lower()
        raw_value = self.settings.value(VOICE_VALUE_SETTINGS_KEY, None)
        value = raw_value.strip() if isinstance(raw_value, str) else None
        return (backend or None), (value or None)

    def _find_exact_voice_index(self, backend: str, value: str | None) -> int:
        expected_backend = backend.strip().lower()
        expected_value = value.strip() if isinstance(value, str) else None
        expected_value = expected_value or None

        for i in range(self.voice_combo.count()):
            data = self.voice_combo.itemData(i)
            if not isinstance(data, dict) or not data.get("backend"):
                continue
            item_backend = str(data.get("backend", "")).strip().lower()
            item_value = data.get("value")
            item_value = item_value.strip() if isinstance(item_value, str) else None
            item_value = item_value or None
            if item_backend == expected_backend and item_value == expected_value:
                return i
        return -1

    def _find_first_voice_index(self, backend: str | None = None) -> int:
        expected_backend = backend.strip().lower() if isinstance(backend, str) else None
        for i in range(self.voice_combo.count()):
            data = self.voice_combo.itemData(i)
            if not isinstance(data, dict) or not data.get("backend"):
                continue
            if expected_backend is None:
                return i
            item_backend = str(data.get("backend", "")).strip().lower()
            if item_backend == expected_backend:
                return i
        return -1

    def _select_preferred_voice(self) -> None:
        index = -1
        saved_backend, saved_value = self._saved_voice_choice()
        if saved_backend:
            index = self._find_exact_voice_index(saved_backend, saved_value)
            if index < 0:
                index = self._find_first_voice_index(saved_backend)

        if index < 0:
            index = self._find_first_voice_index("local-kokoro")

        if index < 0:
            index = self._find_first_voice_index(SYSTEM_BACKEND)

        if index < 0:
            index = self._find_first_voice_index()

        if index >= 0:
            self.voice_combo.setCurrentIndex(index)

    def _on_voice_changed(self, _index: int) -> None:
        if self._voice_populating:
            return

        choice = self.voice_combo.currentData()
        if not isinstance(choice, dict) or not choice.get("backend"):
            return

        backend = str(choice.get("backend", "")).strip().lower()
        value = choice.get("value")
        value = value.strip() if isinstance(value, str) else None
        value = value or None

        self.settings.setValue(VOICE_BACKEND_SETTINGS_KEY, backend)
        if value:
            self.settings.setValue(VOICE_VALUE_SETTINGS_KEY, value)
        else:
            self.settings.remove(VOICE_VALUE_SETTINGS_KEY)


class TrayController(QObject):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.runner = ReaderRunner()
        self.panel = ReaderPanel(self.runner)
        self._service_block_until = 0.0
        self.service_inbox_dir = SERVICE_INBOX_DIR
        try:
            self.service_inbox_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fallback_dir = Path(tempfile.gettempdir()) / "doc-reader-service-inbox"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            self.service_inbox_dir = fallback_dir
            self.panel._append_log(
                f"[doc-reader] Service inbox fallback active ({self.service_inbox_dir}): {exc}"
            )
        self.selection_shortcuts: list[GlobalSelectionShortcut] = []
        for hotkey in [SELECTION_SHORTCUT, DEFAULT_SELECTION_SHORTCUT]:
            normalized = hotkey.strip()
            if not normalized:
                continue
            if any(existing.hotkey == normalized for existing in self.selection_shortcuts):
                continue
            shortcut = GlobalSelectionShortcut(normalized)
            shortcut.triggered.connect(self.read_selected_text)
            shortcut.statusChanged.connect(self.panel._append_log)
            self.selection_shortcuts.append(shortcut)

        self.tray = QSystemTrayIcon(_tray_icon(), self)
        self.tray.setToolTip("Doc Reader")

        menu = QMenu()
        self.open_action = QAction("Open Reader", self)
        self.pick_action = QAction("Browse and Read...", self)
        self.stop_action = QAction("Stop Reading", self)
        quit_action = QAction("Quit", self)

        menu.addAction(self.open_action)
        menu.addAction(self.pick_action)
        menu.addAction(self.stop_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)

        self.open_action.triggered.connect(self.toggle_panel)
        self.pick_action.triggered.connect(self.pick_file_from_menu)
        self.stop_action.triggered.connect(self.stop_reading)
        quit_action.triggered.connect(self.quit)

        self.tray.activated.connect(self._on_tray_activated)
        self.panel.stopRequested.connect(self.stop_reading)
        self.stop_action.setEnabled(False)
        self._service_poll = QTimer(self)
        self._service_poll.setInterval(450)
        self._service_poll.timeout.connect(self._drain_service_inbox)
        self._progress_poll = QTimer(self)
        self._progress_poll.setInterval(2000)
        self._progress_poll.timeout.connect(self._persist_current_progress)
        self.runner.runningChanged.connect(self._on_runner_running_changed)

    def show(self) -> None:
        self.tray.show()
        for shortcut in self.selection_shortcuts:
            shortcut.start()
        self._service_poll.start()
        self._progress_poll.start()

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
            return

        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

        cursor = QCursor.pos()
        x = max(cursor.x() - (self.panel.width() // 2), 20)
        y = cursor.y() + 20
        self.panel.move(QPoint(x, y))

    def pick_file_from_menu(self) -> None:
        self.toggle_panel()
        self.panel.pick_file_and_start()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.toggle_panel()

    def stop_reading(self) -> None:
        self._persist_current_progress(refresh_ui=False)
        self._service_block_until = time.monotonic() + 2.0
        self.runner.stop()

    def read_selected_text(self) -> None:
        text = _capture_selected_text_from_frontmost_app()
        if not text:
            text = _read_clipboard_text()
        if not text:
            self.panel._append_log(
                "[doc-reader] No selected text detected. Highlight text, then press "
                f"{SELECTION_SHORTCUT_LABEL}."
            )
            self.tray.showMessage("Doc Reader", "No selected text detected.")
            return

        started = self.panel.load_text_and_start(text)
        if started:
            self.tray.showMessage("Doc Reader", "Reading selected text.")

    def _drain_service_inbox(self) -> None:
        if self.runner.is_running() or self.runner.is_paused():
            return
        if time.monotonic() < self._service_block_until:
            return

        try:
            queued = sorted(
                self.service_inbox_dir.glob("*.txt"),
                key=lambda path: path.stat().st_mtime,
            )
        except Exception:  # noqa: BLE001
            return
        if not queued:
            return

        latest = queued[-1]
        stale = queued[:-1]

        for stale_path in stale:
            try:
                stale_path.unlink()
            except OSError:
                pass

        try:
            text = latest.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            text = ""
        try:
            latest.unlink()
        except OSError:
            pass

        cleaned = text.strip()
        if not cleaned:
            return

        started = self.panel.load_text_and_start(cleaned)
        if started:
            self.tray.showMessage("Doc Reader", "Reading selected text from Services.")

    def _persist_current_progress(self, *, refresh_ui: bool = False) -> None:
        if not self.runner.should_track_progress():
            return
        source = self.runner.current_source_file()
        if source is None:
            return
        seconds = self.runner.current_position_seconds()
        chunk_index = self.runner.current_resume_chunk_index()
        self.panel.save_library_progress(
            source,
            seconds,
            chunk_index=chunk_index,
            refresh_ui=refresh_ui,
        )

    def _on_runner_running_changed(self, running: bool) -> None:
        self.stop_action.setEnabled(running or self.runner.is_paused())
        if running:
            return
        self._persist_current_progress(refresh_ui=True)

    def quit(self) -> None:
        self._persist_current_progress(refresh_ui=False)
        self.runner.stop()
        self._service_poll.stop()
        self._progress_poll.stop()
        for shortcut in self.selection_shortcuts:
            shortcut.stop()
        self.tray.hide()
        self.app.quit()


def main() -> int:
    app = QApplication(sys.argv)
    _set_macos_accessory_activation_policy()
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("[doc-reader] System tray is not available on this system.")
        return 1

    controller = TrayController(app)
    controller.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
