#!/usr/bin/env python3
"""Fail if the code references pipeline.<module>.<name> that does not exist.

This guards a real bug: `collector.py` called `db.utc_from_ms()`, but `db.py`
never defined it. Every sample therefore failed to persist and the pipeline
stored *nothing* -- while every offline test passed, because none of them touch
the database. A missing function is a whole-pipeline outage that no unit test
notices, so check it statically, in CI, with no DB.

    python tests/check_module_refs.py
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "src", "pipeline")


def _top_level_names(path: str) -> set[str]:
    """Every name a module defines or imports at the top level."""
    names: set[str] = set()
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


def _module_names() -> dict[str, set[str]]:
    mods: dict[str, set[str]] = {}
    for base in (PKG, os.path.join(PKG, "sparkplug")):
        if not os.path.isdir(base):
            continue
        for fn in os.listdir(base):
            if fn.endswith(".py"):
                mods.setdefault(fn[:-3], _top_level_names(os.path.join(base, fn)))
    return mods


def main() -> int:
    modules = _module_names()
    missing: list[str] = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                    continue
                mod, attr = node.value.id, node.attr
                if mod in modules and attr not in modules[mod]:
                    missing.append(f"  {rel}:{node.lineno}: {mod}.{attr} does not exist")

    print(f"modules checked: {', '.join(sorted(modules))}")
    if missing:
        print("\nReferences to names that do not exist:")
        print("\n".join(sorted(set(missing))))
        return 1
    print("OK: every pipeline.<module>.<name> reference resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
