from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Spelled in two parts so this file does not hold the word it looks for; "software" is the factory's own name.
PRODUCT_WORD = re.compile(r"\b" + "em" + r"ber\b", re.IGNORECASE)


def test_no_product_name_in_the_roles_rules_procedures_or_docs() -> None:
    found = [
        str(path.relative_to(ROOT))
        for folder in ("plugin", "rules", "procedures", "docs")
        for path in sorted((ROOT / folder).rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and PRODUCT_WORD.search(path.name + "\n" + path.read_text(encoding="utf-8"))
    ]
    assert found == []
