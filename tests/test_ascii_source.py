"""Guard: every source file we own is pure ASCII.

Non-ASCII punctuation (em-dashes, arrows, ellipses, micro/ohm signs, emoji)
creeps in easily from editors and pasted text. It then breaks in terminals,
in Qt Designer strings and in the Windows console, and makes diffs noisy.

Third-party and EDA-generated trees are exempt (see ``EXEMPT_DIRS``): KiCad
writes its own files and legitimately uses characters like the ohm sign.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Files we author. Anything else (binaries, EDA, 3D models) is not checked.
SOURCE_SUFFIXES = {
    ".py", ".pyi", ".ui", ".h", ".hpp", ".c", ".cpp", ".ino",
    ".md", ".sh", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".js", ".spec",
}

# Generated or vendored by other tools; not ours to keep ASCII.
EXEMPT_DIRS = ("hardware/", "3Dmodels/")


def _tracked_sources() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [
        ROOT / name
        for name in out.split("\0")
        if name
        and Path(name).suffix in SOURCE_SUFFIXES
        and not name.startswith(EXEMPT_DIRS)
    ]


def _offenders(path: Path) -> list[str]:
    """``file:line:col: <char>`` for every non-ASCII character in a file."""
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for col, char in enumerate(line, 1):
            if ord(char) > 127:
                rel = path.relative_to(ROOT)
                hits.append(f"{rel}:{lineno}:{col}: {char!r} (U+{ord(char):04X})")
    return hits


def test_sources_are_ascii():
    """No non-ASCII characters in the code, comments, docs or .ui strings.

    Use ASCII equivalents: ``-`` for dashes, ``->`` for arrows, ``...`` for an
    ellipsis, ``uT``/``ohm`` for units, ``+-`` shapes for diagrams (or a
    mermaid block in a ``.md``, which GitHub renders), plain words instead of
    emoji.
    """
    files = _tracked_sources()
    assert files, "no source files found - is this a git checkout?"

    offenders = [hit for path in files for hit in _offenders(path)]
    if offenders:
        shown = "\n  ".join(offenders[:40])
        more = f"\n  ... and {len(offenders) - 40} more" if len(offenders) > 40 else ""
        pytest.fail(f"non-ASCII characters in source files:\n  {shown}{more}")
