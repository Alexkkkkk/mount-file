from __future__ import annotations

from types import SimpleNamespace

import pytest

import mountfile.dfir_yara as dfir_yara
from mountfile.dfir_yara import YaraUnavailableError, scan_file, scan_yara


class FakeRules:
    def __init__(self, matches):
        self.matches = matches
        self.paths = []

    def match(self, *, filepath):
        self.paths.append(filepath)
        return self.matches


class FakeYara:
    def __init__(self, compiled):
        self.compiled = compiled
        self.calls = []

    def compile(self, **kwargs):
        self.calls.append(kwargs)
        return self.compiled


def test_compiled_rules_are_scanned_without_importing_yara_and_evidence_is_unchanged(
    tmp_path, monkeypatch
):
    image = tmp_path / "evidence.img"
    original = b"prefix\x00marker\x00suffix"
    image.write_bytes(original)
    rules = FakeRules(
        [
            SimpleNamespace(
                rule="suspicious",
                tags=("malware", "triage"),
                meta={"severity": "high"},
                strings=[(7, "$a", b"marker")],
            )
        ]
    )
    monkeypatch.setattr(
        dfir_yara.importlib,
        "import_module",
        lambda name: pytest.fail("yara-python was imported for compiled rules"),
    )

    findings = scan_file(image, rules)

    assert findings == [
        {
            "rule": "suspicious",
            "tags": ("malware", "triage"),
            "meta": {"severity": "high"},
            "offsets": (7,),
        }
    ]
    assert rules.paths == [str(image)]
    assert image.read_bytes() == original


def test_source_rules_compile_lazily_and_normalise_current_string_instances(tmp_path):
    image = tmp_path / "evidence.raw"
    image.write_bytes(b"evidence")
    rules = FakeRules(
        [
            SimpleNamespace(
                rule="two_hits",
                tags=["test"],
                meta={"author": "unit"},
                strings=[
                    SimpleNamespace(
                        identifier="$a",
                        instances=[
                            SimpleNamespace(offset=18),
                            SimpleNamespace(offset=3),
                        ],
                    )
                ],
            )
        ]
    )
    yara = FakeYara(rules)

    findings = scan_yara(image, "rule two_hits { condition: true }", yara_module=yara)

    assert yara.calls == [{"source": "rule two_hits { condition: true }"}]
    assert findings[0]["rule"] == "two_hits"
    assert findings[0]["tags"] == ("test",)
    assert findings[0]["meta"] == {"author": "unit"}
    assert findings[0]["offsets"] == (3, 18)


def test_mapping_matches_and_missing_offsets_use_stable_empty_schema(tmp_path):
    image = tmp_path / "evidence.bin"
    image.write_bytes(b"x")
    rules = FakeRules(
        [{"rule": "metadata_only", "tags": [], "meta": {"enabled": True}}]
    )

    assert scan_file(image, rules) == [
        {"rule": "metadata_only", "tags": (), "meta": {"enabled": True}, "offsets": ()}
    ]


def test_missing_optional_dependency_has_actionable_error(tmp_path, monkeypatch):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"evidence")

    def missing(name):
        assert name == "yara"
        raise ModuleNotFoundError("No module named yara")

    monkeypatch.setattr(dfir_yara.importlib, "import_module", missing)
    with pytest.raises(YaraUnavailableError, match="pip install yara-python"):
        scan_yara(image, "rule x { condition: true }")
