#!/usr/bin/env python3
"""Regression guard: the installer/CLI prompt helpers must not leak into $(...).

`ask` / `ask_secret` are always called as::

    VAR="$(ask "Some prompt" "default")"

If the prompt is printed to stdout it is captured *into VAR*, so every prompted
setting is silently corrupted -- VAR becomes "Some prompt [default]: value". That
is what shipped in 1.1.0: install.sh wrote a corrupted VTScada URL, topic,
decoder, tags and password into `.env` and still passed validation, because
nothing compared the value against what the user typed. The prompt must go to
stderr; only the answer goes to stdout.

    python tests/test_install_prompts.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ["deploy/install.sh", "deploy/pipeline"]

# A value with a space and a '#' proves nothing is word-split or truncated, and
# the default-path case proves an empty answer falls back correctly.
PROBE = r"""
a="$(printf 'readback\n' | ask 'Confirm mode (readback|publish)' 'readback')"
[ "$a" = "readback" ] || { echo "ask leaked: [$a]"; exit 1; }

b="$(printf '\n' | ask 'PLC topic' 'plc/#')"
[ "$b" = "plc/#" ] || { echo "ask default leaked: [$b]"; exit 1; }

c="$(printf 'p@ss w0rd\n' | ask_secret 'Password')"
[ "$c" = "p@ss w0rd" ] || { echo "ask_secret leaked: [$c]"; exit 1; }

echo OK
"""


def _definitions(path: str) -> str:
    """Extract the ask()/ask_secret() one-liner definitions from a script."""
    src = open(path, encoding="utf-8").read()
    found = []
    for name in ("ask", "ask_secret"):
        m = re.search(rf"^{name}\(\)[^\n]*$", src, re.M)
        if not m:
            raise AssertionError(f"{path}: no {name}() definition found")
        found.append(m.group(0))
    return "\n".join(found)


def test_prompts_do_not_leak_into_command_substitution() -> None:
    for path in SCRIPTS:
        script = _definitions(os.path.join(ROOT, path)) + "\n" + PROBE
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, f"{path}: stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "OK" in r.stdout, f"{path}: stdout={r.stdout!r} stderr={r.stderr!r}"


def main() -> int:
    try:
        test_prompts_do_not_leak_into_command_substitution()
    except AssertionError as e:  # noqa: BLE001
        print(f"FAIL {e}")
        return 1
    print("PASS prompt helpers do not leak into $(...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
