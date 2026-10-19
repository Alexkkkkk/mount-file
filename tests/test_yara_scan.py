# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.

from types import SimpleNamespace

import pytest

from mountfile.yara_scan import YaraScanError, YaraUnavailableError, scan_image


class FakeRules:
    def match(self, *, data):
        assert data == b"MALWARE"
        return [
            SimpleNamespace(
                rule="demo_rule",
                tags=("test",),
                meta={"severity": "high"},
                strings=[SimpleNamespace(identifier="$needle")],
            )
        ]


class FakeYara:
    def compile(self, *, filepath):
        assert filepath.endswith("rules.yar")
        return FakeRules()


def test_scan_image_is_bounded_and_returns_structured_findings(tmp_path):
    image = tmp_path / "sample.img"
    image.write_bytes(b"MALWARE" + b"x" * 100)
    rules = tmp_path / "rules.yar"
    rules.write_text("rule demo_rule { condition: true }", encoding="utf-8")

    findings = scan_image(image, rules, max_bytes=7, yara_module=FakeYara())

    assert findings[0].rule == "demo_rule"
    assert findings[0].tags == ("test",)
    assert findings[0].strings == ("$needle",)


def test_scan_image_rejects_invalid_limit(tmp_path):
    image = tmp_path / "sample.img"
    image.write_bytes(b"data")
    rules = tmp_path / "rules.yar"
    rules.write_text("rule x { condition: true }", encoding="utf-8")

    with pytest.raises(YaraScanError, match="max_bytes"):
        scan_image(image, rules, max_bytes=0, yara_module=FakeYara())


def test_scan_image_reports_missing_optional_dependency(tmp_path):
    image = tmp_path / "sample.img"
    image.write_bytes(b"data")
    rules = tmp_path / "rules.yar"
    rules.write_text("rule x { condition: true }", encoding="utf-8")

    with pytest.raises(YaraUnavailableError):
        scan_image(image, rules, yara_module=None)
