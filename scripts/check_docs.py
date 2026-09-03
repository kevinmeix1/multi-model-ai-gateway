"""Validate local Markdown links and fenced Mermaid blocks."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_FILES = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    *sorted((ROOT / "docs").rglob("*.md")),
]
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _local_target(raw: str) -> str | None:
    target = raw.strip().split(maxsplit=1)[0].strip("<>")
    if not target or target.startswith(("#", "http://", "https://", "mailto:")):
        return None
    return unquote(target.split("#", 1)[0].split("?", 1)[0])


def _mermaid_errors(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    in_mermaid = False
    opened_at = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        marker = line.strip()
        if not in_mermaid and marker == "```mermaid":
            in_mermaid = True
            opened_at = line_number
        elif in_mermaid and marker == "```":
            in_mermaid = False
    if in_mermaid:
        errors.append(f"{path.relative_to(ROOT)}:{opened_at}: unclosed Mermaid fence")
    return errors


def main() -> None:
    errors: list[str] = []
    links_checked = 0
    diagrams_checked = 0
    for path in MARKDOWN_FILES:
        if not path.exists():
            errors.append(f"missing documentation entry point: {path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        diagrams_checked += text.count("```mermaid")
        errors.extend(_mermaid_errors(path, text))
        for raw in LINK.findall(text):
            target = _local_target(raw)
            if target is None:
                continue
            links_checked += 1
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(ROOT):
                errors.append(f"{path.relative_to(ROOT)}: local link escapes repository: {target}")
            elif not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)}: missing local link target: {target}")
    if errors:
        raise SystemExit("documentation validation failed:\n" + "\n".join(errors))
    print(
        f"Documentation validation passed: {len(MARKDOWN_FILES)} files, "
        f"{links_checked} local links, {diagrams_checked} Mermaid diagrams."
    )


if __name__ == "__main__":
    main()
