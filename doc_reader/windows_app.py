"""Windows orchestration for Doc Reader.

macOS uses launchd agents and a Swift menu-bar app. On Windows this module fills the
same role: it starts and stops the local Kokoro/Whisper speech service, the web app,
and the tray helper, keeps their PID files and logs under the managed root, and
registers an optional login-startup shortcut.

Run it through ``run-doc-reader.cmd`` (which prepares the virtual environment) or
directly with ``python -m doc_reader.windows_app <command>`` from inside the venv.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .platform_tools import (
    IS_WINDOWS,
    configure_windows_dll_search,
    find_tool,
    kill_process_tree,
    managed_root,
    pid_is_running,
    popen_hidden_kwargs,
    tool_dirs_for_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_HOST = os.getenv("DOC_READER_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("DOC_READER_WEB_PORT", "8766"))
TTS_HOST = os.getenv("DOC_READER_TTS_HOST", "127.0.0.1")
TTS_PORT = int(os.getenv("DOC_READER_TTS_PORT", os.getenv("DOC_READER_TTS_MAC_PORT", "8772")))
WEB_URL = os.getenv("DOC_READER_WEB_URL", f"http://{WEB_HOST}:{WEB_PORT}")
TTS_URL = os.getenv("DOC_READER_TTS_MAC_URL", f"http://{TTS_HOST}:{TTS_PORT}")
DEFAULT_ENGINES = os.getenv("DOC_READER_TTS_ENGINES", "kokoro,whisper")
DEFAULT_STT_MODEL = os.getenv("DOC_READER_STT_MODEL", "small")
STARTUP_SHORTCUT_NAME = "Doc Reader.lnk"

SERVICES: dict[str, dict[str, Any]] = {
    "tts": {
        "module": "doc_reader.tts_service",
        "args": ["--host", TTS_HOST, "--port", str(TTS_PORT), "--engines", DEFAULT_ENGINES],
        "label": "Local Kokoro/Whisper service",
        "health": TTS_URL,
    },
    "web": {
        "module": "doc_reader.webapp",
        "args": ["--host", WEB_HOST, "--port", str(WEB_PORT)],
        "label": "Web app",
        "health": WEB_URL,
    },
    "helper": {
        "module": "doc_reader.windows_helper",
        "args": [],
        "label": "Tray helper (hotkeys + dictation)",
        "health": "",
    },
}


# ------------------------------------------------------------------ paths & env


def _root() -> Path:
    root = managed_root()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "pids").mkdir(parents=True, exist_ok=True)
    return root


def _pid_file(name: str) -> Path:
    return _root() / "pids" / f"{name}.pid"


def _log_file(name: str) -> Path:
    return _root() / "logs" / f"{name}.log"


def _venv_python(*, windowed: bool = False) -> str:
    exe_dir = Path(sys.executable).resolve().parent
    if windowed:
        candidate = exe_dir / "pythonw.exe"
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (f"{os.pathsep}{existing}" if existing else "")
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    env["DOC_READER_MANAGED_ROOT"] = str(_root())
    env.setdefault("DOC_READER_TTS_MAC_URL", TTS_URL)
    env.setdefault("DOC_READER_HTTP_TTS_URL", TTS_URL)
    env.setdefault("DOC_READER_TTS_ENGINES", DEFAULT_ENGINES)
    env.setdefault("DOC_READER_STT_MODEL", DEFAULT_STT_MODEL)
    env.setdefault("DOC_READER_WEB_SPEECH_BACKEND", "local-kokoro")
    env.setdefault("DOC_READER_WEB_URL", WEB_URL)
    extra_dirs = tool_dirs_for_path()
    try:
        import torch

        extra_dirs.append(str(Path(torch.__file__).resolve().parent / "lib"))
    except Exception:  # noqa: BLE001
        pass
    if extra_dirs:
        env["PATH"] = os.pathsep.join(extra_dirs + [env.get("PATH", "")])
    return env


# ------------------------------------------------------------------ process control


def _read_pid(name: str) -> int:
    try:
        return int(_pid_file(name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_pid(name: str, pid: int) -> None:
    _pid_file(name).write_text(f"{pid}\n", encoding="utf-8")


def _clear_pid(name: str) -> None:
    try:
        _pid_file(name).unlink()
    except OSError:
        pass


def _running_pid(name: str) -> int:
    pid = _read_pid(name)
    if pid and pid_is_running(pid):
        return pid
    if pid:
        _clear_pid(name)
    return 0


def _health(url: str, timeout: float = 1.5) -> dict[str, Any]:
    if not url:
        return {"ok": False}
    try:
        with urlrequest.urlopen(f"{url.rstrip('/')}/healthz", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {"ok": False}
    except (OSError, ValueError, urlerror.URLError):
        return {"ok": False}


def _spawn(name: str) -> int:
    service = SERVICES[name]
    log_path = _log_file(name)
    log_handle = open(log_path, "ab")  # noqa: SIM115 - handed to the child
    creationflags = 0
    if IS_WINDOWS:
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    process = subprocess.Popen(
        [_venv_python(windowed=name == "helper"), "-m", service["module"], *service["args"]],
        cwd=str(REPO_ROOT),
        env=child_env(),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        close_fds=True,
        creationflags=creationflags,
    )
    log_handle.close()
    _write_pid(name, process.pid)
    return process.pid


def _wait_health(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(url, timeout=1.0).get("ok"):
            return True
        time.sleep(0.5)
    return False


def ensure_service(name: str, *, quiet: bool = False) -> str:
    service = SERVICES[name]
    if service["health"] and _health(service["health"]).get("ok"):
        return f"{service['label']}: already running ({service['health']})"
    pid = _running_pid(name)
    if pid and not service["health"]:
        return f"{service['label']}: already running (pid {pid})"
    if pid and service["health"]:
        # Process is alive but not answering yet (models still loading).
        return f"{service['label']}: starting (pid {pid})"
    pid = _spawn(name)
    if not quiet:
        print(f"[doc-reader] started {service['label']} (pid {pid}) -> {_log_file(name)}")
    return f"{service['label']}: started (pid {pid})"


def stop_service(name: str, *, quiet: bool = False) -> str:
    service = SERVICES[name]
    pid = _running_pid(name)
    stopped: list[int] = []
    if pid:
        kill_process_tree(pid, force=True)
        stopped.append(pid)
    for stray in _find_stray_pids(service["module"]):
        if stray not in stopped:
            kill_process_tree(stray, force=True)
            stopped.append(stray)
    _clear_pid(name)
    if not stopped:
        return f"{service['label']}: not running"
    if not quiet:
        print(f"[doc-reader] stopped {service['label']} (pid {', '.join(map(str, stopped))})")
    return f"{service['label']}: stopped"


def _find_stray_pids(module: str) -> list[int]:
    """Find our own service processes even when the PID file is missing."""
    if not IS_WINDOWS:
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -like '*-m " + module + "*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            **popen_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        token = line.strip()
        if token.isdigit() and int(token) != os.getpid():
            pids.append(int(token))
    return pids


# ------------------------------------------------------------------ helper API (used by webapp)


def start_helper() -> str:
    return ensure_service("helper", quiet=True)


def stop_helper() -> str:
    return stop_service("helper", quiet=True)


def restart_helper() -> str:
    stop_service("helper", quiet=True)
    time.sleep(0.3)
    return ensure_service("helper", quiet=True)


# ------------------------------------------------------------------ startup shortcut


def _startup_dir() -> Path:
    appdata = os.getenv("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_shortcut() -> Path:
    return _startup_dir() / STARTUP_SHORTCUT_NAME


def enable_startup() -> int:
    if not IS_WINDOWS:
        print("[doc-reader] enable-startup is a Windows command.")
        return 1
    shortcut = _startup_shortcut()
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    target = _venv_python(windowed=True)
    arguments = "-m doc_reader.windows_app start --no-open --quiet"
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$s = $shell.CreateShortcut('{shortcut}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '{arguments}'; "
        f"$s.WorkingDirectory = '{REPO_ROOT}'; "
        "$s.WindowStyle = 7; "
        "$s.Description = 'Doc Reader local speech workspace'; "
        "$s.Save()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        **popen_hidden_kwargs(),
    )
    if result.returncode != 0 or not shortcut.exists():
        print(f"[doc-reader] Could not create startup shortcut: {result.stderr.strip()}")
        return 1
    print(f"[doc-reader] Startup shortcut created: {shortcut}")
    return 0


def disable_startup() -> int:
    shortcut = _startup_shortcut()
    if shortcut.exists():
        shortcut.unlink()
        print(f"[doc-reader] Startup shortcut removed: {shortcut}")
    else:
        print("[doc-reader] Startup shortcut was not installed.")
    return 0


# ------------------------------------------------------------------ commands


def cmd_start(*, open_browser: bool = True, quiet: bool = False) -> int:
    for name in ("tts", "web", "helper"):
        message = ensure_service(name, quiet=quiet)
        if not quiet:
            print(f"[doc-reader] {message}")
    ready = _wait_health(WEB_URL, timeout=40.0)
    if not ready:
        print(f"[doc-reader] Web app did not become healthy at {WEB_URL}. See {_log_file('web')}")
        return 1
    if not quiet:
        print(f"[doc-reader] Doc Reader is running at {WEB_URL}")
        tts = _health(TTS_URL)
        device = (tts.get("device") or {}).get("cuda_device") or (tts.get("device") or {}).get("requested")
        if tts.get("ok"):
            print(f"[doc-reader] Speech service is up on {device or 'unknown device'}; models load in the background.")
        else:
            print(f"[doc-reader] Speech service is still starting; check {_log_file('tts')} if playback fails.")
    if open_browser:
        webbrowser.open(WEB_URL)
    return 0


def cmd_stop() -> int:
    for name in ("helper", "web", "tts"):
        print(f"[doc-reader] {stop_service(name, quiet=True)}")
    return 0


def cmd_status() -> int:
    web = _health(WEB_URL)
    tts = _health(TTS_URL)
    device = (tts.get("device") or {}).get("cuda_device") or (tts.get("device") or {}).get("requested") or "unknown"
    engines = tts.get("engines") or {}

    def engine_state(name: str) -> str:
        info = engines.get(name) or {}
        if not info.get("enabled"):
            return "disabled"
        if info.get("loaded"):
            return "loaded"
        return f"loading{' (' + info['error'] + ')' if info.get('error') else ''}"

    helper_pid = _running_pid("helper")
    lines = [
        "Doc Reader status (Windows)",
        f"Platform: {sys.platform}",
        f"Repo: {REPO_ROOT}",
        f"Managed data: {_root()}",
        f"Web app: {'ok' if web.get('ok') else 'not reachable'} ({WEB_URL})",
        f"Speech service: {'ok' if tts.get('ok') else 'not reachable'} ({TTS_URL}) device={device}",
        f"  Kokoro: {engine_state('kokoro')}",
        f"  Whisper: {engine_state('whisper')}",
        f"Tray helper: {'running (pid ' + str(helper_pid) + ')' if helper_pid else 'not running'}",
        f"Startup at login: {'enabled' if _startup_shortcut().exists() else 'disabled'} ({_startup_shortcut()})",
        f"Logs: {_root() / 'logs'}",
    ]
    print("\n".join(lines))
    return 0


def cmd_doctor() -> int:
    ok = True

    def line(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else '!!'}] {label}{(' - ' + detail) if detail else ''}")

    line("Python", sys.version_info[:2] in {(3, 10), (3, 11), (3, 12)}, sys.version.split()[0])
    try:
        import torch

        cuda = torch.cuda.is_available()
        line("PyTorch", True, f"{torch.__version__} cuda={'yes (' + torch.cuda.get_device_name(0) + ')' if cuda else 'no (CPU mode)'}")
    except Exception as exc:  # noqa: BLE001
        line("PyTorch", False, str(exc))
    try:
        import kokoro

        line("Kokoro", True, kokoro.__version__)
    except Exception as exc:  # noqa: BLE001
        line("Kokoro", False, str(exc))
    try:
        configure_windows_dll_search()
        import faster_whisper  # noqa: F401

        line("faster-whisper", True)
    except Exception as exc:  # noqa: BLE001
        line("faster-whisper", False, str(exc))
    ffplay = find_tool("ffplay")
    line("ffplay (audio playback)", bool(ffplay), ffplay or "install ffmpeg: winget install Gyan.FFmpeg")
    ffmpeg = find_tool("ffmpeg")
    line("ffmpeg (dictation audio normalization)", bool(ffmpeg), ffmpeg or "install ffmpeg: winget install Gyan.FFmpeg")
    try:
        import espeakng_loader

        line("espeak-ng (bundled)", True, espeakng_loader.get_library_path())
    except Exception:  # noqa: BLE001
        espeak = find_tool("espeak-ng")
        line("espeak-ng (system)", bool(espeak), espeak or "winget install eSpeak-NG.eSpeak-NG")
    try:
        import sounddevice as sd

        default_input = sd.query_devices(kind="input")
        line("Microphone", True, str(default_input.get("name", "default")))
    except Exception as exc:  # noqa: BLE001
        line("Microphone", False, str(exc))
    try:
        import PySide6  # noqa: F401
        import pynput  # noqa: F401

        line("Tray helper deps (PySide6, pynput)", True)
    except Exception as exc:  # noqa: BLE001
        line("Tray helper deps (PySide6, pynput)", False, str(exc))
    return 0 if ok else 1


def cmd_open() -> int:
    if not _health(WEB_URL).get("ok"):
        code = cmd_start(open_browser=False, quiet=True)
        if code != 0:
            return code
    webbrowser.open(WEB_URL)
    return 0


def cmd_run_foreground(name: str, extra: list[str]) -> int:
    service = SERVICES[name]
    command = [sys.executable, "-m", service["module"], *service["args"], *extra]
    return subprocess.call(command, cwd=str(REPO_ROOT), env=child_env())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-doc-reader",
        description="Start, stop, and inspect Doc Reader on Windows.",
    )
    sub = parser.add_subparsers(dest="command")
    start = sub.add_parser("start", help="Start speech service, web app, and tray helper; open the browser")
    start.add_argument("--no-open", action="store_true", help="Do not open the browser")
    start.add_argument("--quiet", action="store_true")
    sub.add_parser("stop", help="Stop everything")
    sub.add_parser("restart", help="Stop then start")
    sub.add_parser("status", help="Show service health")
    sub.add_parser("doctor", help="Check Python, CUDA, Kokoro, ffmpeg, espeak, microphone")
    sub.add_parser("open", help="Open the web app (starting services if needed)")
    sub.add_parser("enable-startup", help="Start Doc Reader automatically at login")
    sub.add_parser("disable-startup", help="Remove the login startup shortcut")
    for name in SERVICES:
        sub.add_parser(f"{name}-start", help=f"Start only the {SERVICES[name]['label']}")
        sub.add_parser(f"{name}-stop", help=f"Stop only the {SERVICES[name]['label']}")
        run = sub.add_parser(f"run-{name}", help=f"Run the {SERVICES[name]['label']} in the foreground")
        run.add_argument("extra", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "start"
    if command == "start":
        return cmd_start(open_browser=not getattr(args, "no_open", False), quiet=getattr(args, "quiet", False))
    if command == "stop":
        return cmd_stop()
    if command == "restart":
        cmd_stop()
        time.sleep(0.5)
        return cmd_start()
    if command == "status":
        return cmd_status()
    if command == "doctor":
        return cmd_doctor()
    if command == "open":
        return cmd_open()
    if command == "enable-startup":
        return enable_startup()
    if command == "disable-startup":
        return disable_startup()
    for name in SERVICES:
        if command == f"{name}-start":
            print(f"[doc-reader] {ensure_service(name)}")
            return 0
        if command == f"{name}-stop":
            print(f"[doc-reader] {stop_service(name)}")
            return 0
        if command == f"run-{name}":
            return cmd_run_foreground(name, list(getattr(args, "extra", []) or []))
    build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
