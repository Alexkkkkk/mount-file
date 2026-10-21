from datetime import datetime, timezone

from mountfile.dfir_timestomp import (
    TimestampRecord,
    TimestompThresholds,
    analyze_timestomp,
)


def test_ordering_finding_is_structured_and_read_only():
    record = TimestampRecord(
        path="/evidence/report.doc",
        record_id="inode-17",
        created=300.0,
        modified=100.0,
        accessed=200.0,
    )

    findings = analyze_timestomp([record], TimestompThresholds(ordering_tolerance=0.0))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.path == "/evidence/report.doc"
    assert finding.record_id == "inode-17"
    assert finding.severity == "high"
    assert "created occurs after modified" in finding.reason
    assert finding.timestamps == {"created": 300.0, "modified": 100.0}
    assert dict(finding)["path"] == "/evidence/report.doc"
    assert record.created == 300.0
    assert record.modified == 100.0


def test_ordering_tolerance_suppresses_small_synthetic_skew():
    record = TimestampRecord(
        path="/evidence/close.txt",
        record_id="r-close",
        created=100.0,
        modified=99.5,
        accessed=100.0,
    )

    assert (
        analyze_timestomp([record], TimestompThresholds(ordering_tolerance=1.0)) == []
    )
    assert len(analyze_timestomp([record], skew=0.25)) == 1


def test_configured_boundary_and_range_findings_include_involved_values():
    record = TimestampRecord(
        path="/evidence/boundary.bin",
        record_id="r-boundary",
        created=0.5,
        modified=10.0,
        accessed=25.0,
    )
    thresholds = TimestompThresholds(
        ordering_tolerance=0.0,
        boundary_values=(0.0,),
        boundary_tolerance=1.0,
        minimum_timestamp=1.0,
        maximum_timestamp=20.0,
    )

    findings = analyze_timestomp([record], thresholds)

    assert len(findings) == 3
    reasons = {finding.reason for finding in findings}
    assert any("configured boundary" in reason for reason in reasons)
    assert any("configured minimum" in reason for reason in reasons)
    assert any("configured maximum" in reason for reason in reasons)
    assert all(finding.path == "/evidence/boundary.bin" for finding in findings)
    assert all(finding.record_id == "r-boundary" for finding in findings)
    assert {key for finding in findings for key in finding.timestamps} == {
        "created",
        "accessed",
    }
    assert record.created == 0.5
    assert record.accessed == 25.0


def test_legacy_field_aliases_and_datetime_values_are_explicit_and_deterministic():
    records = [
        {
            "path": "/evidence/legacy.txt",
            "id": 9,
            "birth": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "mtime": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "atime": datetime(2024, 1, 3, tzinfo=timezone.utc),
        }
    ]

    findings = analyze_timestomp(records, ordering_tolerance=0.0)

    assert len(findings) == 1
    assert findings[0].record_id == "9"
    assert findings[0].timestamps["created"] == records[0]["birth"]
