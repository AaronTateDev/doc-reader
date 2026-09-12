"""Fallback WAV player for Windows when ffplay is not installed.

Usage: python -m doc_reader.winplay <file.wav>
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: winplay <file.wav>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    try:
        import winsound
    except ImportError:
        print("winplay only works on Windows.", file=sys.stderr)
        return 1
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME)
    except RuntimeError as exc:
        print(f"winplay failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
