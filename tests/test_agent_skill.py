import re
from pathlib import Path

import pytest

from d2md import cli

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "d2md" / "SKILL.md"


def test_skill_has_frontmatter_and_readme_links_it():
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = text.split("---\n")[1]

    assert re.search(r"^name: d2md$", frontmatter, re.M)
    assert re.search(r"^description: \S", frontmatter, re.M)
    assert (
        "https://github.com/wtigero/d2md/blob/main/skills/d2md/SKILL.md"
        in (ROOT / "README.md").read_text(encoding="utf-8")
    )


def test_skill_cites_only_real_flags(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    real = set(re.findall(r"--[a-z][a-z-]*", capsys.readouterr().out))
    cited = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", SKILL.read_text(encoding="utf-8")))

    assert cited - real == set()
