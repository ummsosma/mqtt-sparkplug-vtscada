#!/usr/bin/env python3
"""Fail if a real secret, or a `.env`, is present in the tracked tree.

This guards the release-checklist line that nothing else enforces. It tells a real
VALUE apart from a variable name: `MQTT_PASSWORD = _str("MQTT_PASSWORD")` is a
name, and a check that flags names is noise the owner stops trusting.

A value counts as a secret when it is a known credential shape (provider token
prefix, private key block) or when it is high-entropy enough to be a generated
credential (>= 12 characters, >= 3.0 bits per character). Documented convenience
defaults (`emqx`, `public1234`) are below that bar and are reported as defaults,
not as leaks -- they are a configuration finding, not a committed secret.

The report prints *where* a finding is and how it was classified, never the value
and never the setting name. A secret scanner that echoes what it found creates a
second leak in the CI log (and CodeQL flags exactly that, on this file, as
clear-text logging of sensitive data -- fix the reporting, do not suppress the
rule); the same reasoning applies to a name harvested out of a credential-shaped
line, so nothing that came from a match reaches stdout.

    python tests/check_no_secrets.py
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Known credential shapes: provider tokens and key blocks.
KNOWN = [
    ("github token", r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    ("github fine-grained token", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("Slack token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("Google API key", r"AIza[0-9A-Za-z_\-]{30,}"),
    ("private key block", r"BEGIN [A-Z ]*PRIVATE KEY"),
]

# Assignment-shaped candidates: a credential-ish key name with a REAL VALUE.
# Only two shapes count, so code that merely mentions the word is not flagged:
#   password="literal"          (a quoted literal in code)
#   DB_PASSWORD=value           (an env/config line with an UPPERCASE key)
# `password = settings.DB_PASSWORD,` or `token = data.get("nextPageToken")` is a
# variable holding something else, not a committed secret.
CRED_WORD = r"password|passwd|pass|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret"
ASSIGN_CODE = re.compile(r"(?i)\b(" + CRED_WORD + r")\s*[:=]\s*[\"']([^\"']{6,})[\"']")
ASSIGN_ENV = re.compile(r"(?i)^([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL)[A-Z0-9_]*)\s*=\s*(\S+)")

# A value that is obviously not a real credential: shell expansion, placeholder,
# or the key name itself echoed back.
PLACEHOLDER = re.compile(r"(?i)^\$|^<|^\{\{|\.\.\.|changeme|your_|example|placeholder|\bname\b|^x{6,}$")


def entropy(value: str) -> float:
    counts = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(value)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def tracked_files():
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True,
                             text=True, check=True).stdout
        return [f for f in out.split() if f]
    except Exception:
        # No git available (e.g. running inside an image built from an archive).
        files = []
        for base, dirs, names in os.walk(ROOT):
            if ".git" in dirs:
                dirs.remove(".git")
            for n in names:
                files.append(os.path.relpath(os.path.join(base, n), ROOT))
        return files


def main() -> int:
    findings, soft = [], []
    for rel in tracked_files():
        if os.path.basename(rel) == ".env":
            findings.append((rel, 0, ".env file is tracked (must be gitignored)"))
            continue
        try:
            body = open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue

        for label, pattern in KNOWN:
            for lineno, raw_line in enumerate(body.splitlines(), 1):
                if re.search(pattern, raw_line):
                    findings.append((rel, lineno, label))

        for lineno, raw_line in enumerate(body.splitlines(), 1):
            if raw_line.lstrip().startswith("#"):
                continue   # a comment, e.g. this file's own examples
            m = ASSIGN_CODE.search(raw_line) or ASSIGN_ENV.match(raw_line)
            if not m:
                continue
            setting, raw = m.group(1), m.group(2).strip("'\"`")
            # The setting echoed back (`_str("DB_PASSWORD")`), a code expression, or
            # a placeholder is not a value.
            if raw.lower() == setting.lower() or setting.lower() in raw.lower() or "(" in raw:
                continue
            if PLACEHOLDER.search(raw):
                continue
            if len(raw) >= 12 and entropy(raw) >= 3.0:
                findings.append((rel, lineno, "high-entropy value"))
            else:
                soft.append((rel, lineno, "documented default"))

    print(f"scanned {len(tracked_files())} tracked files")
    if findings:
        print("\nCredential-shaped findings, by location. Neither the value nor the "
              "setting name is printed: this report must not leak what it found.")
        for rel, lineno, label in findings:
            print(f"  {rel}:{lineno}  {label}")
        return 1
    print("No committed credential values (names and placeholders are not secrets).")
    if soft:
        print("\nDocumented defaults -- not leaks, but not production credentials "
              "either. Read the cited lines:")
        for rel, lineno, _ in soft:
            print(f"  {rel}:{lineno}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
