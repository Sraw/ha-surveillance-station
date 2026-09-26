"""Every translation has exactly the strings strings.json has, with the same placeholders."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

HERE = Path(__file__).parent.parent / "custom_components/surveillance_station"
SOURCE = json.loads((HERE / "strings.json").read_text())


def flat(tree: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            out.update(flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


@pytest.mark.parametrize("path", sorted((HERE / "translations").glob("*.json")), ids=lambda p: p.stem)
def test_translation_matches_strings(path: Path) -> None:
    source, translated = flat(SOURCE), flat(json.loads(path.read_text()))
    assert translated.keys() == source.keys()
    for key, text in source.items():
        assert set(re.findall(r"\{\w+\}", translated[key])) == set(re.findall(r"\{\w+\}", text)), key
