#!/usr/bin/env python3
"""Fail if any setting the code reads is not forwarded by docker-compose.yml.

This guards audit finding F4: a setting documented in `.env.example` but dropped
by the Compose env allow-list silently does nothing, which is worse than no
setting at all. Runs offline; used by CI.

    python tests/check_settings_forwarded.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    settings_src = open(os.path.join(ROOT, "src", "pipeline", "settings.py"),
                        encoding="utf-8").read()
    compose_src = open(os.path.join(ROOT, "docker-compose.yml"),
                       encoding="utf-8").read()

    read = set(re.findall(r'_(?:str|int|float|bool|json)\("([A-Z0-9_]+)"', settings_src))
    forwarded = set(re.findall(r"\$\{([A-Z0-9_]+)", compose_src))

    missing = sorted(n for n in read if n not in forwarded)
    print(f"settings read by code: {len(read)}")
    print(f"forwarded by compose:  {len(forwarded)}")
    if missing:
        print("\nNOT forwarded (set in .env -> silently ignored):")
        for name in missing:
            print("  -", name)
        return 1
    print("OK: every setting is forwarded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
