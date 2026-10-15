"""Integrity gates for the vendored third-party skills under ``skills/third_party/``.

These tests exist because the vendor set is a *reviewed* artifact, not a
dependency. Nothing in the Python packaging system tracks it: there is no lock
entry, no version resolver, and no upstream CI watching it. The only thing
standing between "six audited skill directories" and "someone dropped a 34 MB
directory in here with a network call in it" is this file.

Two invariants matter most:

1. **The reviewed-file allowlist.** ``REVIEWED_PY_FILES`` lists every executable
   file that was read line by line before vendoring (see
   ``skills/third_party/PROVENANCE.md``). A re-vendor that adds a single new
   ``.py`` fails here, which forces the review to actually happen again instead
   of being assumed.

2. **Sibling path resolution.** Vendored skills reach shared reference material
   via ``../nature-shared/core/<file>.md``. Those relative paths only resolve
   while all six directories stay siblings. Reorganising this tree one level
   deeper would break every reference at *prompt assembly time*, not at import
   time — a silent failure that would surface as degraded output quality.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDOR_ROOT = REPO_ROOT / "skills" / "third_party"

UPSTREAM_COMMIT = "19be8d45e0d5233aa44be6d4572e655b9914ff54"

VENDORED_SKILLS = (
    "nature-shared",
    "nature-polishing",
    "nature-ref-verifier",
    "nature-response",
    "nature-statistics",
    "nature-social-science",
)

# Every executable file present at the reviewed commit. Adding a file here
# without reading it defeats the purpose of the list.
REVIEWED_PY_FILES = (
    "nature-shared/scripts/check_consistency.py",
    "nature-shared/tests/test_check_consistency.py",
    "nature-response/scripts/check_package_consistency.py",
    "nature-response/tests/test_package_consistency.py",
    "nature-response/tests/test_response_instruction_contracts.py",
)

# Patterns that would mean the file is no longer a passive knowledge asset.
FORBIDDEN_PATTERNS = (
    r"\bsubprocess\b",
    r"\bos\.system\b",
    r"\bos\.popen\b",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\b__import__\s*\(",
    r"\bsocket\b",
    r"\brequests\b",
    r"\burllib\b",
    r"\bhttp\.client\b",
    r"\bhttpx\b",
    r"\bftplib\b",
    r"\bsmtplib\b",
    r"\bshutil\.rmtree\b",
    r"\bos\.environ\b",
    r"\bos\.getenv\b",
)

RESIDUE_GLOBS = ("install.sh", "*.pyc", "__pycache__", ".DS_Store", "*.egg-info")


def _vendor_root() -> Path:
    if not VENDOR_ROOT.is_dir():
        pytest.skip(f"vendored skills not present at {VENDOR_ROOT}")
    return VENDOR_ROOT


@pytest.fixture(scope="module")
def vendor_root() -> Path:
    return _vendor_root()


def test_license_is_apache_2(vendor_root: Path) -> None:
    """The full Apache-2.0 text must ship with the vendored subset (§4(a))."""
    text = (vendor_root / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0" in text


def test_notice_pins_the_upstream_commit(vendor_root: Path) -> None:
    """Attribution is worthless if it does not name the exact revision taken."""
    notice = (vendor_root / "NOTICE").read_text(encoding="utf-8")
    assert UPSTREAM_COMMIT in notice
    assert "Apache-2.0" in notice


def test_notice_and_provenance_agree_on_the_commit(vendor_root: Path) -> None:
    """Two files record the same SHA; updating one and not the other is the
    failure mode this catches."""
    provenance = (vendor_root / "PROVENANCE.md").read_text(encoding="utf-8")
    assert UPSTREAM_COMMIT in provenance


def test_vendored_set_is_exactly_the_reviewed_set(vendor_root: Path) -> None:
    """Guards both directions: an unreviewed skill appearing, and a skill being
    deleted while NOTICE still claims it was vendored."""
    present = {p.name for p in vendor_root.iterdir() if p.is_dir()}
    assert present == set(VENDORED_SKILLS)


@pytest.mark.parametrize("skill", VENDORED_SKILLS)
def test_skill_has_entrypoint_and_manifest(vendor_root: Path, skill: str) -> None:
    directory = vendor_root / skill
    assert (directory / "SKILL.md").is_file(), f"{skill} lost its SKILL.md"
    # manifest.yaml carries the on-demand loading rules; without it the loader
    # cannot do progressive disclosure and would inline whole reference trees.
    assert (directory / "manifest.yaml").is_file(), f"{skill} lost its manifest.yaml"


def test_shared_sibling_references_resolve(vendor_root: Path) -> None:
    """Every ``../nature-shared/...`` path must point at a real file.

    Collected from markdown and YAML rather than hardcoded, so a future
    upstream revision that adds a reference is checked too.

    Scoped to the six skill directories: NOTICE and PROVENANCE.md discuss these
    paths in prose (``../nature-shared/core/<file>.md``) and would otherwise be
    parsed as references. The pattern also demands a real file extension, which
    is what keeps an illustrative ellipsis from matching.
    """
    pattern = re.compile(r"\.\./nature-shared/[A-Za-z0-9/_-]+(?:\.[A-Za-z0-9]+)+")
    found: set[str] = set()
    for skill in VENDORED_SKILLS:
        for path in (vendor_root / skill).rglob("*"):
            if path.suffix not in {".md", ".yaml"} or not path.is_file():
                continue
            for match in pattern.findall(path.read_text(encoding="utf-8", errors="replace")):
                found.add(match)

    assert found, "no ../nature-shared references found; did the layout change?"
    for ref in sorted(found):
        # Refs are relative to a skill directory, so resolve against any one of
        # them; all six are siblings by construction.
        target = (vendor_root / "nature-polishing" / ref).resolve()
        assert target.is_file(), f"dangling shared reference: {ref}"
        assert target.is_relative_to(vendor_root.resolve()), f"{ref} escapes the vendor tree"


def test_python_files_match_the_reviewed_allowlist(vendor_root: Path) -> None:
    """The core security gate.

    A re-vendor that silently introduces a new script must fail here, because
    "no dangerous patterns found" is only a meaningful statement about files
    somebody actually read.
    """
    present = {
        p.relative_to(vendor_root).as_posix()
        for p in vendor_root.rglob("*.py")
        if "__pycache__" not in p.parts
    }
    expected = set(REVIEWED_PY_FILES)

    added = present - expected
    removed = expected - present
    assert not added, f"unreviewed Python files vendored: {sorted(added)}"
    assert not removed, (
        f"reviewed files vanished (update the allowlist deliberately): {sorted(removed)}"
    )


def test_vendored_python_has_no_dangerous_patterns(vendor_root: Path) -> None:
    """Belt-and-braces alongside the allowlist: even a reviewed file must not
    gain a network call or a subprocess when it is re-copied."""
    combined = re.compile("|".join(FORBIDDEN_PATTERNS))
    for relative in REVIEWED_PY_FILES:
        source = (vendor_root / relative).read_text(encoding="utf-8", errors="replace")
        hits = {
            f"{lineno}: {line.strip()}"
            for lineno, line in enumerate(source.splitlines(), start=1)
            if combined.search(line) and not line.lstrip().startswith("#")
        }
        assert not hits, f"{relative} contains forbidden constructs:\n" + "\n".join(sorted(hits))


def test_no_installer_or_build_residue(vendor_root: Path) -> None:
    """`install.sh` self-activates into a host agent's config and pip-installs
    unpinned dependencies. It must never be copied in, even accidentally."""
    for glob in RESIDUE_GLOBS:
        hits = [p.relative_to(vendor_root).as_posix() for p in vendor_root.rglob(glob)]
        assert not hits, f"residue matching {glob!r}: {hits}"


def test_no_skill_declares_a_conflicting_license(vendor_root: Path) -> None:
    """All six are covered by the repository-root Apache-2.0 grant. Upstream has
    MIT-licensed skills elsewhere; if one ever lands here, NOTICE is wrong and
    the per-skill LICENSE must be vendored alongside it."""
    for skill in VENDORED_SKILLS:
        text = (vendor_root / skill / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
        assert not re.search(r"^\s*license\s*:", frontmatter, re.MULTILINE), (
            f"{skill} declares its own license — check NOTICE and vendor its LICENSE file"
        )
        assert not (vendor_root / skill / "LICENSE").exists(), (
            f"{skill} ships a per-skill LICENSE — reconcile it with NOTICE"
        )


def test_upstream_tests_are_outside_the_default_testpaths() -> None:
    """Vendored skills ship their own unittest files. They are outside
    ``testpaths`` on purpose: they are upstream's tests, not ours, and collecting
    them would couple our suite to code we do not maintain."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    assert not any(str(p).startswith("skills") for p in testpaths), (
        f"skills/ must stay out of testpaths, got {testpaths}"
    )


def test_ruff_cannot_rewrite_the_vendored_tree() -> None:
    """NOTICE claims these are verbatim copies, so the formatter must never
    touch them — reformatting is a modification, and Apache-2.0 §4(b) then
    requires us to say so in every changed file.

    ``extend-exclude`` alone is not enough: it only applies while ruff walks the
    tree from its start point. An editor's format-on-save, a pre-commit hook, or
    a plain ``ruff format skills/`` passes the path explicitly and bypasses the
    exclusion. ``force-exclude`` is what makes it unconditional. Both settings
    are asserted here because either one missing reopens the hole.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = config["tool"]["ruff"]
    assert "skills/third_party" in ruff["extend-exclude"], ruff["extend-exclude"]
    assert ruff.get("force-exclude") is True, (
        "force-exclude must be true, otherwise explicitly passed paths bypass extend-exclude"
    )
