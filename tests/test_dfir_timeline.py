from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace

from mountfile.dfir import TimelineRecord
from mountfile.dfir_timeline import CSV_FIELDS, export_timeline_csv, normalize_timeline
from mountfile.dfir_timestomp import TimestampRecord


def test_normalizes_existing_metadata_and_event_records_without_opening_paths():
    records = [
        TimelineRecord(
            path="/evidence/image.raw/report.txt",
            mtime=1704067200,
            atime=1704067202,
            ctime=1704067201,
            birth=1704067199,
        ),
        TimestampRecord(
            path="/evidence/image.raw/legacy.txt",
            record_id="inode-7",
            created=datetime(2024, 1, 1, 0, 0, 3, tzinfo=timezone(timedelta(hours=2))),
        ),
        SimpleNamespace(
            kind="read",
            path="/evidence/image.raw/report.txt",
            ts="2023-12-31T23:59:58-01:00",
            source="olma",
        ),
    ]

    normalized = normalize_timeline(records)

    assert [event.event for event in normalized] == [
        "created",
        "birth",
        "mtime",
        "ctime",
        "atime",
        "read",
    ]
    assert normalized[-1].timestamp == datetime(
        2024, 1, 1, 0, 59, 58, tzinfo=timezone.utc
    )
    assert all(event.timestamp.tzinfo == timezone.utc for event in normalized)
    assert normalized[-1].source == "olma"
    assert normalized[-1].evidence_path == "/evidence/image.raw/report.txt"
    metadata_event = next(event for event in normalized if event.record_id == "inode-7")
    assert metadata_event["path"] == "/evidence/image.raw/legacy.txt"


def test_normalization_is_deterministic_and_preserves_metadata_fields():
    records = [
        {
            "timestamp": "2024-01-01T00:00:00Z",
            "event_type": "open",
            "source": "collector,one",
            "evidence_path": '/tmp/a\nquoted".txt',
            "id": 2,
        },
        {
            "timestamp": datetime(
                2023, 12, 31, 19, 0, tzinfo=timezone(timedelta(hours=-5))
            ),
            "event": "close",
            "source": "collector,two",
            "path": "/tmp/z.txt",
            "record_id": "1",
        },
    ]

    first = normalize_timeline(records)
    second = normalize_timeline(list(reversed(records)))

    assert first == second
    assert first[0].event == "close"
    assert first[1].event_type == "open"
    assert first[1].source == "collector,one"
    assert first[1].evidence_path == '/tmp/a\nquoted".txt'
    assert first[1].record_id == "2"


def test_csv_export_quotes_data_and_does_not_read_evidence_paths():
    output = StringIO()
    count = export_timeline_csv(
        [
            {
                "timestamp": "2024-01-01T00:00:00.123456+02:00",
                "event": 'open,"safe"',
                "source": "=not-a-formula",
                "evidence_path": "/does/not/exist,\nfile.raw",
            }
        ],
        output,
    )

    assert count == 1
    lines = output.getvalue().splitlines()
    assert lines[0] == '"timestamp","event","source","evidence_path","record_id"'
    assert '"2023-12-31T22:00:00.123456Z"' in output.getvalue()
    assert '"=not-a-formula"' in output.getvalue()
    assert '"/does/not/exist,\nfile.raw"' in output.getvalue()

    output.seek(0)
    import csv

    rows = list(csv.DictReader(output))
    assert tuple(rows[0]) == CSV_FIELDS
    assert rows[0]["source"] == "=not-a-formula"
    assert rows[0]["evidence_path"] == "/does/not/exist,\nfile.raw"
