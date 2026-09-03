"""Fail CI on common committed credential shapes without printing candidate values."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
PATTERNS = [
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{24,}"),
    re.compile(r"gh[opurs]_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def main() -> None:
    findings = 0
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".md", ".toml", ".yaml", ".yml", ".json", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        if any(pattern.search(text) for pattern in PATTERNS):
            findings += 1
            print(f"credential-shaped value detected in {path.relative_to(ROOT)}")
    if findings:
        raise SystemExit(f"secret scan failed in {findings} file(s)")
    print(f"Secret scan passed: {scanned} text files, zero credential-shaped values.")


if __name__ == "__main__":
    main()
