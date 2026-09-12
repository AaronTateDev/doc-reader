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


# ------------------------------------------------------------------ hotkey specs
#
# A hotkey is stored as a small platform-neutral string that both helpers read:
#   dictation (hold to talk): one key, pynput style: "ctrl_r", "alt", "f8",
#       "scroll_lock", or a side mouse button "mouse:x1" / "mouse:x2".
#   read selection: a chord, pynput style: "<ctrl>+<alt>+r", "<ctrl>+<shift>+<f8>".
# The Windows helper feeds these to pynput; the macOS helper maps them to
# NSEvent modifier flags and key codes (Option for alt, Control for ctrl).

DEFAULT_MAC_DICTATION_KEY = "alt"
DEFAULT_MAC_SELECTION_HOTKEY = "<ctrl>+<alt>+<cmd>+r"

# Quick-swap presets offered in the web page and the tray menu.
DICTATION_KEY_OPTIONS: tuple[str, ...] = ("ctrl_r", "alt_r", "shift_r", "f8", "f9", "scroll_lock", "pause")
MAC_DICTATION_KEY_OPTIONS: tuple[str, ...] = ("alt", "alt_r", "ctrl", "shift_r", "f8", "f9")
SELECTION_SHORTCUT_OPTIONS: tuple[str, ...] = (
    "<ctrl>+<alt>+r",
    "<ctrl>+<shift>+r",
    "<ctrl>+<alt>+s",
    "<alt>+<shift>+r",
    "<ctrl>+<alt>+<space>",
)

_DICTATION_MODIFIERS = {"ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", "alt_gr", "shift", "shift_l", "shift_r"}
_DICTATION_SPECIAL = {"scroll_lock", "pause", "insert", "caps_lock", "num_lock", "print_screen", "menu", "home", "end", "page_up", "page_down"}
_DICTATION_MOUSE = {"mouse:x1", "mouse:x2"}
_WIN_KEYS = {"cmd", "cmd_l", "cmd_r", "win", "meta", "super"}
_FUNCTION_KEY_RE = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")
_KEY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_:]{0,31}$")
_SELECTION_MODIFIERS = ("ctrl", "alt", "shift")

WIN_KEY_MESSAGE = "The Windows key (Command on a Mac) can't be used."
TYPING_KEY_MESSAGE = (
    "Use a key you don't type with: Ctrl, Alt, Shift, F1 to F12, Scroll Lock, Pause, Insert, "
    "or a side mouse button."
)
MOUSE_BUTTON_MESSAGE = "Left, right, and middle click can't be used. The side buttons work."
NO_MODIFIER_MESSAGE = "Add Ctrl, Alt, or Shift to it."
BAD_FINAL_KEY_MESSAGE = "Finish with a letter, number, function key, or Space."


def validate_dictation_key(value: object) -> tuple[str, str]:
    """Return (normalized key, "") or ("", reason) for a hold-to-dictate key."""
    candidate = str(value or "").strip().lower().replace(" ", "")
    if not candidate:
        return "", "Press a key to use for dictation."
    if candidate in _WIN_KEYS:
        return "", WIN_KEY_MESSAGE
    if candidate.startswith("mouse:"):
        return (candidate, "") if candidate in _DICTATION_MOUSE else ("", MOUSE_BUTTON_MESSAGE)
    if candidate in _DICTATION_MODIFIERS or candidate in _DICTATION_SPECIAL or _FUNCTION_KEY_RE.match(candidate):
        return candidate, ""
    if _KEY_NAME_RE.match(candidate):
        return "", TYPING_KEY_MESSAGE
    return "", "That key isn't recognized."


def validate_selection_shortcut(value: object) -> tuple[str, str]:
    """Return (normalized chord, "") or ("", reason) for the read-selection shortcut."""
    candidate = str(value or "").strip().lower().replace(" ", "")
    if not candidate:
        return "", "Press a key combination for reading the selection."
    modifiers: list[str] = []
    final = ""
    for part in candidate.split("+"):
        token = part[1:-1] if part.startswith("<") and part.endswith(">") and len(part) > 2 else None
        if token in _WIN_KEYS:
            return "", WIN_KEY_MESSAGE
        if token in _SELECTION_MODIFIERS:
            if token not in modifiers:
                modifiers.append(token)
            continue
        if final:
            return "", "Use one key after the modifiers."
        if token is not None and (_FUNCTION_KEY_RE.match(token) or token == "space"):
            final = f"<{token}>"
        elif token is None and re.fullmatch(r"[a-z0-9]", part):
            final = part
        else:
            return "", BAD_FINAL_KEY_MESSAGE
    if not modifiers:
        return "", NO_MODIFIER_MESSAGE
    if not final:
        return "", BAD_FINAL_KEY_MESSAGE
    ordered = [name for name in _SELECTION_MODIFIERS if name in modifiers]
    return "+".join(f"<{name}>" for name in ordered) + "+" + final, ""


def normalize_dictation_key(value: object) -> str:
    return validate_dictation_key(value)[0]


def normalize_selection_shortcut(value: object) -> str:
    return validate_selection_shortcut(value)[0]


def default_dictation_key() -> str:
    fallback = DEFAULT_MAC_DICTATION_KEY if IS_MACOS else DEFAULT_WINDOWS_DICTATION_KEY
    return os.getenv("DOC_READER_DICTATION_KEY", fallback).strip().lower() or fallback


def default_selection_shortcut() -> str:
    fallback = DEFAULT_MAC_SELECTION_HOTKEY if IS_MACOS else DEFAULT_WINDOWS_SELECTION_HOTKEY
    return os.getenv("DOC_READER_SELECTION_SHORTCUT", fallback).strip().lower() or fallback


def _key_word(token: str) -> str:
    """Human label for one key token in the current platform's vocabulary."""
    base = {
        "ctrl": "Control" if IS_MACOS else "Ctrl",
        "alt": "Option" if IS_MACOS else "Alt",
        "alt_gr": "AltGr",
        "shift": "Shift",
        "cmd": "Command" if IS_MACOS else "Win",
        "space": "Space",
        "scroll_lock": "Scroll Lock",
        "num_lock": "Num Lock",
        "caps_lock": "Caps Lock",
        "print_screen": "Print Screen",
        "page_up": "Page Up",
        "page_down": "Page Down",
        "menu": "Menu",
        "mouse:x1": "Mouse 4",
        "mouse:x2": "Mouse 5",
    }
    if token in base:
        return base[token]
    for side, word in (("_l", "Left "), ("_r", "Right ")):
        if token.endswith(side) and token[: -len(side)] in base:
            return word + base[token[: -len(side)]]
    if _FUNCTION_KEY_RE.match(token):
        return token.upper()
    return token.replace("_", " ").title()


def dictation_hotkey_label(key: str | None = None) -> str:
    name = (key or default_dictation_key()).strip().lower()
    return _key_word(name)


def selection_hotkey_label(shortcut: str | None = None) -> str:
    hotkey = (shortcut or default_selection_shortcut()).strip().lower()
    return "+".join(_key_word(part.strip("<>")) for part in hotkey.split("+") if part)


def hotkey_options() -> dict[str, list[dict[str, str]]]:
    dictation = MAC_DICTATION_KEY_OPTIONS if IS_MACOS else DICTATION_KEY_OPTIONS
    return {
        "dictation": [{"value": value, "label": dictation_hotkey_label(value)} for value in dictation],
        "selection": [{"value": value, "label": selection_hotkey_label(value)} for value in SELECTION_SHORTCUT_OPTIONS],
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
