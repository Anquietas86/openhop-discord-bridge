"""The README generator must be idempotent.

It used to locate `## Configuration` and `## Discord setup` independently; since
Discord setup comes first in the document, each run re-inserted the whole
install/config block and five runs produced a 990-line README with the
instructions repeated five times.
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_generator_is_idempotent():
    readme = ROOT / "README.md"
    original = readme.read_text()
    try:
        for _ in range(2):
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "gen_config_table.py")],
                cwd=ROOT, check=True, capture_output=True,
            )
        assert readme.read_text() == original, "generator changed the README on a repeat run"
    finally:
        readme.write_text(original)


def test_readme_documents_each_section_once():
    text = (ROOT / "README.md").read_text()
    for heading in ("## Discord setup", "## Install", "## Configuration", "## Troubleshooting"):
        assert text.count(heading) == 1, f"{heading} appears {text.count(heading)} times"
