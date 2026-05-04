"""Byte-for-byte parity check against teach-me-eng-bot's live skills.

Renders each fabric-shipped template against `examples/teach-me-eng-bot.config.yaml`
and compares to the file in the sibling teach-me-eng-bot checkout. Skips when
`$TEACH_ME_ENG_BOT_PATH` is unset so CI without the sibling repo doesn't fail;
hard-fails on any byte difference when the env var is set.

`dev-flow/SKILL.md` is intentionally NOT compared — it is project-internal and
not managed by the fabric.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

import pytest

from fabric.config import load_config
from fabric.render import (
    SKILL_NAMES,
    SKILL_OUTPUT_FILENAME,
    render_skill,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "teach-me-eng-bot.config.yaml"


def _project_root() -> Path | None:
    raw = os.environ.get("TEACH_ME_ENG_BOT_PATH")
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.is_dir() else None


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_template_matches_teach_me_eng_bot(name: str, tmp_path: Path) -> None:
    project_root = _project_root()
    if project_root is None:
        pytest.skip("TEACH_ME_ENG_BOT_PATH not set; skipping parity check")

    expected_file = project_root / ".claude" / "skills" / name / SKILL_OUTPUT_FILENAME
    if not expected_file.exists():
        pytest.fail(f"missing expected skill file: {expected_file}")
    expected = expected_file.read_text()

    fake_project = tmp_path / "fake-project"
    fake_project.mkdir()
    config = load_config(EXAMPLE_CONFIG)
    rendered = render_skill(name, fake_project, config, fabric_root=REPO_ROOT)

    if rendered == expected:
        return

    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(expected_file),
            tofile=f"<rendered {name}>",
        )
    )
    pytest.fail(f"parity mismatch for {name}:\n{diff}")
