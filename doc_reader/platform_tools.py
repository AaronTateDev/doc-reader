"""Cross-platform helpers shared by the reader, the web app, and the Windows helper.

macOS ships `afplay`, `say`, `pbpaste`, and launchd. Windows has none of those, so the
modules that used to reach for them directly now go through this file instead.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

LOCAL_KOKORO_LABEL = "Mac Kokoro" if IS_MACOS else "Local Kokoro"
LOCAL_STT_LABEL = "Mac speech-to-text" if IS_MACOS else "Local speech-to-text"
DEFAULT_WINDOWS_DICTATION_KEY = "ctrl_r"
DEFAULT_WINDOWS_SELECTION_HOTKEY = "<ctrl>+<alt>+r"


# Quick-swap presets offered in the web page and the tray menu (pynput names).
DICTATION_KEY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("ctrl_r", "Right Ctrl"),
    ("alt_r", "Right Alt"),
    ("shift_r", "Right Shift"),
    ("f8", "F8"),
    ("f9", "F9"),
    ("scroll_lock", "Scroll Lock"),
    ("pause", "Pause"),
)
SELECTION_SHORTCUT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("<ctrl>+<alt>+r", "Ctrl+Alt+R"),
    ("<ctrl>+<shift>+r", "Ctrl+Shift+R"),
    ("<ctrl>+<alt>+s", "Ctrl+Alt+S"),
    ("<alt>+<shift>+r", "Alt+Shift+R"),
    ("<ctrl>+<alt>+<space>", "Ctrl+Alt+Space"),
)
_DICTATION_KEY_LABELS = {
    "ctrl_r": "Right Ctrl",
    "ctrl_l": "Left Ctrl",
    "alt_r": "Right Alt",
    "alt_l": "Left Alt",
    "alt": "Alt",
    "shift_r": "Right Shift",
    "scroll_lock": "Scroll Lock",
    "pause": "Pause",
    "f8": "F8",
    "f9": "F9",
}
_DICTATION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SELECTION_SHORTCUT_RE = re.compile(r"^(<(ctrl|alt|shift|cmd)>\+){1,3}(<[a-z_0-9]+>|[a-z0-9])$")


def default_dictation_key() -> str:
    return os.getenv("DOC_READER_DICTATION_KEY", DEFAULT_WINDOWS_DICTATION_KEY).strip().lower() or DEFAULT_WINDOWS_DICTATION_KEY


def default_selection_shortcut() -> str:
    return os.getenv("DOC_READER_SELECTION_SHORTCUT", DEFAULT_WINDOWS_SELECTION_HOTKEY).strip().lower() or DEFAULT_WINDOWS_SELECTION_HOTKEY


def normalize_dictation_key(value: object) -> str:
    """Return a safe pynput key name, or an empty string when the value is not one."""
    candidate = str(value or "").strip().lower()
    return candidate if _DICTATION_KEY_RE.match(candidate) else ""


def normalize_selection_shortcut(value: object) -> str:
    """Return a safe pynput chord such as ``<ctrl>+<alt>+r``, or an empty string."""
    candidate = str(value or "").strip().lower().replace(" ", "")
    return candidate if _SELECTION_SHORTCUT_RE.match(candidate) else ""


def dictation_hotkey_label(key: str | None = None) -> str:
    if IS_MACOS:
        return "Option"
    name = (key or default_dictation_key()).strip().lower()
    return _DICTATION_KEY_LABELS.get(name, name.replace("_", " ").title())


def selection_hotkey_label(shortcut: str | None = None) -> str:
    if IS_MACOS:
        return "Control+Option+Command+R"
    hotkey = shortcut or default_selection_shortcut()
    return "+".join(part.strip("<>").title() for part in hotkey.split("+"))


def hotkey_options() -> dict[str, list[dict[str, str]]]:
    return {
        "dictation": [{"value": value, "label": label} for value, label in DICTATION_KEY_OPTIONS],
        "selection": [{"value": value, "label": label} for value, label in SELECTION_SHORTCUT_OPTIONS],
    }


def _windows_tool_candidates(name: str) -> list[str]:
    exe = name if name.lower().endswith(".exe") else f"{name}.exe"
    local_app_data = os.getenv("LOCALAPPDATA", "")
    program_files = os.getenv("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    patterns = [
        os.path.join(local_app_data, "Microsoft", "WinGet", "Links", exe),
        os.path.join(local_app_data, "Microsoft", "WinGet", "Packages", "*", "*", "bin", exe),
        os.path.join(local_app_data, "Microsoft", "WinGet", "Packages", "*", "bin", exe),
        os.path.join(local_app_data, "Microsoft", "WinGet", "Packages", "*", exe),
        os.path.join(program_files, "ffmpeg", "bin", exe),
        os.path.join(program_files, "eSpeak NG", exe),
        os.path.join(program_files_x86, "eSpeak NG", exe),
        os.path.join("C:\\", "ffmpeg", "bin", exe),
        os.path.join(os.getenv("ChocolateyInstall", r"C:\ProgramData\chocolatey"), "bin", exe),
        os.path.join(os.getenv("USERPROFILE", ""), "scoop", "shims", exe),
    ]
    found: list[str] = []
    for pattern in patterns:
        if not pattern or pattern.startswith(os.sep):
            continue
        for match in sorted(glob.glob(pattern)):
            if match not in found:
                found.append(match)
    return found


def find_tool(name: str) -> str:
    """Return an absolute path for a CLI tool, or an empty string."""
    resolved = shutil.which(name)
    if resolved and Path(resolved).is_file():
        return resolved
    candidates: list[str] = []
    if IS_WINDOWS:
        candidates.extend(_windows_tool_candidates(name))
    else:
        candidates.extend(
            [
                f"/opt/homebrew/bin/{name}",
                f"/usr/local/bin/{name}",
                f"/usr/bin/{name}",
            ]
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def tool_dirs_for_path() -> list[str]:
    """Directories worth prepending to PATH so child processes find ffmpeg/espeak."""
    dirs: list[str] = []
    for name in ("ffplay", "ffmpeg", "espeak-ng"):
        resolved = find_tool(name)
        if resolved:
            parent = str(Path(resolved).parent)
            if parent not in dirs:
                dirs.append(parent)
    return dirs


def find_audio_player() -> list[str]:
    """Return a command prefix that plays one audio file and exits."""
    afplay = find_tool("afplay") if IS_MACOS else ""
    if afplay:
        return [afplay]
    ffplay = find_tool("ffplay")
    if ffplay:
        return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet"]
    if IS_WINDOWS:
        # Last resort: a tiny Python player using the standard library, so playback
        # still works before ffmpeg is installed.
        return [sys.executable, "-m", "doc_reader.winplay"]
    raise RuntimeError(
        "No audio player found. Install ffmpeg (ffplay) or, on macOS, use afplay."
    )


def popen_hidden_kwargs() -> dict[str, object]:
    """Popen kwargs that keep child console windows from flashing on Windows."""
    if not IS_WINDOWS:
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def popen_process_group_kwargs() -> dict[str, object]:
    """Popen kwargs so the child can be stopped as a group (POSIX) or tree (Windows)."""
    if IS_WINDOWS:
        return {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        }
    return {"start_new_session": True}


def kill_process_tree(pid: int, *, force: bool = True) -> None:
    """Terminate a process and everything it spawned."""
    if pid <= 0:
        return
    if IS_WINDOWS:
        args = ["taskkill", "/T", "/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **popen_hidden_kwargs(),
        )
        return
    import signal

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            check=False,
            **popen_hidden_kwargs(),
        )
        return f'"{pid}"' in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def configure_windows_dll_search() -> None:
    """Make CUDA/cuDNN DLLs bundled with torch visible to ctranslate2 (faster-whisper).

    The Mac installer never needs this. On Windows, torch ships cuDNN inside
    `torch/lib`, and faster-whisper's ctranslate2 only finds them if that folder is
    on the DLL search path before it loads.
    """
    if not IS_WINDOWS:
        return
    try:
        import torch  # noqa: F401

        lib_dir = Path(torch.__file__).resolve().parent / "lib"
    except Exception:  # noqa: BLE001
        return
    if not lib_dir.is_dir():
        return
    try:
        os.add_dll_directory(str(lib_dir))
    except (AttributeError, OSError):
        pass
    current = os.environ.get("PATH", "")
    if str(lib_dir) not in current:
        os.environ["PATH"] = f"{lib_dir}{os.pathsep}{current}"


def configure_espeak() -> None:
    """Point phonemizer at a system espeak-ng on Windows when the bundled one is missing."""
    if not IS_WINDOWS:
        return
    if os.getenv("PHONEMIZER_ESPEAK_LIBRARY"):
        return
    try:
        import espeakng_loader  # noqa: F401

        return  # misaki/kokoro bundle their own espeak-ng library
    except Exception:  # noqa: BLE001
        pass
    for base in (
        os.getenv("ProgramFiles", r"C:\Program Files"),
        os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        dll = Path(base) / "eSpeak NG" / "libespeak-ng.dll"
        if dll.is_file():
            os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = str(dll)
            os.environ.setdefault("PHONEMIZER_ESPEAK_PATH", str(dll.parent))
            return


def managed_root() -> Path:
    value = os.getenv("DOC_READER_MANAGED_ROOT")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".doc-reader-managed"
