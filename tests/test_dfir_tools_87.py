# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for the issue #87 DFIR modules: timeline, content search, YARA,
timestomp, slack and live diff.

Everything runs against fakes or tiny synthetic images, so the suite passes
with and without ``pytsk3`` / ``yara-python`` installed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from mountfile import live_diff, search_content, slack, timeline, timestomp
from mountfile.dfir_yara import YaraUnavailableError

UTC = timezone.utc


# --------------------------------------------------------------------- helpers
class FakeMeta:
    def __init__(self, *, size=0, addr=1, mtime=None, atime=None, ctime=None, crtime=None,
                 mtime_nano=0, atime_nano=0, ctime_nano=0, crtime_nano=0, kind=1):
        self.size = size
        self.addr = addr
        self.mtime = mtime
        self.atime = atime
        self.ctime = ctime
        self.crtime = crtime
        self.mtime_nano = mtime_nano
        self.atime_nano = atime_nano
        self.ctime_nano = ctime_nano
        self.crtime_nano = crtime_nano
        self.type = kind


class FakeName:
    def __init__(self, name):
        self.name = name


class FakeEntry:
    def __init__(self, name, data=b"", meta=None, children=None):
        self._data = data
        self.info = type("Info", (), {})()
        self.info.name = FakeName(name if isinstance(name, bytes) else name.encode())
        self.info.meta = meta or FakeMeta(size=len(data))
        self._children = children or []

    def read_random(self, offset, length):
        return self._data[offset:offset + length]

    def as_directory(self):
        return FakeDirectory(self._children)


class FakeDirectory:
    def __init__(self, entries):
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)


class FakeRun:
    def __init__(self, addr, length):
        self.addr = addr
        self.len = length


class FakeAttr:
    def __init__(self, runs):
        self._runs = runs

    def __iter__(self):
        return iter(self._runs)


class FakeFs:
    """Minimal stand-in for a pytsk3 filesystem."""

    def __init__(self, files, *, block_size=512, runs=None):
        self.info = type("FsInfo", (), {"block_size": block_size})()
        self._runs = runs or {}
        root = [
            FakeEntry(name, data, meta=FakeMeta(size=len(data), addr=index + 2))
            for index, (name, data) in enumerate(files)
        ]
        self._root = FakeDirectory(root)
        self._by_addr = {index + 2: name for index, (name, _) in enumerate(files)}

    def open_dir(self, _path):
        return self._root

    def open_meta(self, inode):
        return [FakeAttr(self._runs.get(inode, []))]


# ------------------------------------------------------------------- timeline
def test_collect_events_from_fs_builds_four_timestamp_events():
    fs = FakeFs([("a.txt", b"hello")])
    events = list(timeline.collect_events_from_fs(
        fs, hash_limit=None,
    ))
    # no timestamps set on the fake -> nothing collected
    assert events == []

    fs2 = FakeFs([])
    fs2._root = FakeDirectory([
        FakeEntry("b.txt", b"x", meta=FakeMeta(
            size=1, mtime=1700000000, atime=1700000001, ctime=1700000002, crtime=1699999999,
            mtime_nano=123456700, atime_nano=1, ctime_nano=1, crtime_nano=1,
        ))
    ])
    events = list(timeline.collect_events_from_fs(fs2, hash_limit=None))
    assert [e.event for e in events] == ["created", "mtime", "atime", "ctime"]
    assert all(e.timestamp.tzinfo is not None for e in events)
    assert all(e.path == "/b.txt" for e in events)


def test_timeline_event_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        timeline.TimelineEvent(
            timestamp=datetime(2024, 1, 1), source="t", path="/a", event="mtime"
        )


def test_sqlite_and_csv_roundtrip(tmp_path):
    events = [
        timeline.TimelineEvent(datetime(2024, 1, 1, 12, 0, tzinfo=UTC), "pytsk3", "/a.txt", "mtime", "aa"),
        timeline.TimelineEvent(datetime(2024, 1, 1, 13, 0, tzinfo=UTC), "pytsk3", "/a.txt", "atime", None),
    ]
    db = tmp_path / "timeline.db"
    csv_path = tmp_path / "timeline.csv"
    assert timeline.write_sqlite(events, db) == 2
    assert timeline.write_csv(events, csv_path) == 2

    rows = timeline.query_timeline(db)
    assert len(rows) == 2
    assert rows[0]["path"] == "/a.txt"
    assert rows[0]["sha256"] == "aa"

    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header == "timestamp,source,path,event,sha256"

    with sqlite3.connect(db) as connection:
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 2


def test_query_timeline_filters_by_window_and_event(tmp_path):
    db = tmp_path / "t.db"
    timeline.write_sqlite([
        timeline.TimelineEvent(datetime(2024, 3, 12, 14, 30, tzinfo=UTC), "s", "/x", "mtime"),
        timeline.TimelineEvent(datetime(2024, 3, 12, 16, 30, tzinfo=UTC), "s", "/x", "atime"),
    ], db)
    window = timeline.query_timeline(
        db, start=datetime(2024, 3, 12, 14, 0, tzinfo=UTC), end=datetime(2024, 3, 12, 15, 0, tzinfo=UTC)
    )
    assert len(window) == 1
    assert window[0]["event"] == "mtime"
    assert len(timeline.query_timeline(db, event="atime")) == 1


def test_summarise_counts_events_and_files():
    events = [
        timeline.TimelineEvent(datetime(2024, 1, 1, tzinfo=UTC), "s", "/a", "mtime"),
        timeline.TimelineEvent(datetime(2024, 1, 1, tzinfo=UTC), "s", "/a", "atime"),
        timeline.TimelineEvent(datetime(2024, 1, 1, tzinfo=UTC), "s", "/b", "mtime"),
    ]
    summary = timeline.summarise(events)
    assert summary["events"] == 3
    assert summary["files"] == 2
    assert summary["by_event"]["mtime"] == 2


# --------------------------------------------------------------- content search
def test_search_blob_plain_and_utf16le():
    data = b"password=secret\x00\x00"
    plain = search_content.search_blob(data, "password=")
    assert len(plain) == 1
    assert plain[0].offset == 0
    assert "password=" in plain[0].context

    utf16 = "password=".encode("utf-16-le")
    blob = b"\x00" * 8 + utf16 + b"\x00" * 8
    hits = search_content.search_blob(blob, "password=", encoding="utf-16le")
    assert len(hits) == 1
    assert hits[0].offset == 8


def test_search_blob_regex():
    hits = search_content.search_blob(b"id=42 id=77", r"id=\d+", regex=True)
    assert [h.context for h in hits] == ["id=42", "id=77"]


def test_search_image_streams_with_overlap(tmp_path):
    needle = b"NEEDLE-MARKER"
    image = tmp_path / "img.bin"
    # place the marker so it straddles a chunk boundary
    chunk = 1024
    image.write_bytes(b"\x00" * (chunk - 4) + needle + b"\x00" * 4096)
    hits = search_content.search_image(image, "NEEDLE-MARKER", chunk=chunk, overlap=64)
    assert len(hits) == 1
    assert hits[0].offset == chunk - 4


def test_search_image_respects_max_hits(tmp_path):
    image = tmp_path / "img.bin"
    image.write_bytes(b"ab" * 1000)
    hits = search_content.search_image(image, "ab", max_hits=5, chunk=4096)
    assert len(hits) == 5


def test_search_fs_reports_paths():
    fs = FakeFs([("one.txt", b"secret data"), ("two.txt", b"nothing here")])
    hits = search_content.search_fs(fs, "secret")
    assert len(hits) == 1
    assert hits[0].path == "/one.txt"


def test_search_regions_limits_scope(tmp_path):
    image = tmp_path / "img.bin"
    image.write_bytes(b"TARGET" + b"\x00" * 100 + b"TARGET")
    hits = search_content.search_regions(image, [(0, 6)], "TARGET")
    assert len(hits) == 1
    assert hits[0].offset == 0


def test_encode_pattern_rejects_empty():
    with pytest.raises(search_content.SearchError):
        search_content.encode_pattern("")


# ---------------------------------------------------------------------- YARA
def test_rule_files_expands_directory(tmp_path):
    from mountfile import yara_scan

    (tmp_path / "a.yar").write_text("rule a { condition: true }", encoding="utf-8")
    (tmp_path / "b.yara").write_text("rule b { condition: true }", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert set(yara_scan.rule_files(tmp_path)) == {"a", "b"}


def test_rule_files_missing_raises(tmp_path):
    from mountfile import yara_scan

    with pytest.raises(yara_scan.YaraScanError):
        yara_scan.rule_files(tmp_path / "nope")


def test_load_rules_without_yara_python_raises_or_compiles(tmp_path):
    from mountfile import yara_scan

    rules = tmp_path / "r.yar"
    rules.write_text('rule t { strings: $a = "hello" condition: $a }', encoding="utf-8")
    try:
        import yara  # noqa: F401
    except ImportError:
        with pytest.raises(YaraUnavailableError):
            yara_scan.load_rules(rules)
    else:
        compiled = yara_scan.load_rules(rules)
        assert compiled is not None


def test_match_blob_and_shipped_rules_compile():
    from mountfile import yara_scan

    rules_dir = __import__("pathlib").Path(__file__).resolve().parent.parent / "rules"
    assert (rules_dir / "pii.yar").exists()
    assert (rules_dir / "anti_forensics.yar").exists()
    try:
        import yara  # noqa: F401
    except ImportError:
        pytest.skip("yara-python not installed")
    compiled = yara_scan.load_rules(rules_dir)
    hits = yara_scan.match_blob(compiled, b"-----BEGIN RSA PRIVATE KEY-----\n")
    assert any(hit.rule == "PII_PrivateKeyPEM" for hit in hits)


def test_scan_files_from_fs_in_memory():
    from mountfile import yara_scan

    try:
        import yara
    except ImportError:
        pytest.skip("yara-python not installed")

    compiled = yara.compile(source='rule t { strings: $a = "MAGIC" condition: $a }')
    fs = FakeFs([("hit.bin", b"MAGIC"), ("clean.bin", b"nothing")])
    hits = yara_scan.scan_files_from_fs(fs, compiled)
    assert [h.path for h in hits] == ["/hit.bin"]
    assert hits[0].as_dict()["target"] == "files"


def test_unallocated_ranges_is_the_complement():
    from mountfile import yara_scan

    # block_size 512; file at addr=2 len=1 -> bytes 1024..1536 allocated
    fs = FakeFs([("a.bin", b"x")], block_size=512, runs={2: [FakeRun(2, 1)]})
    allocated = yara_scan.allocated_ranges(fs, 2048)
    assert allocated == [(1024, 1536)]
    assert yara_scan.unallocated_ranges(fs, 2048) == [(0, 1024), (1536, 2048)]


# ------------------------------------------------------------------ timestomp
def test_timestomp_flags_future_timestamp():
    now = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    finding = timestomp.analyse_timestamps(
        {"path": "/evil.exe", "mtime": now + 86400, "mtime_nano": 1}, now=now
    )
    assert finding is not None
    assert timestomp.REASON_FUTURE in finding.reasons


def test_timestomp_flags_equal_mac():
    now = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    stamp = now - 100
    finding = timestomp.analyse_timestamps(
        {"path": "/x", "mtime": stamp, "atime": stamp, "ctime": stamp, "mtime_nano": 5},
        now=now,
    )
    assert finding is not None
    assert timestomp.REASON_EQUAL_MAC in finding.reasons


def test_timestomp_flags_si_lt_fn():
    now = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    finding = timestomp.analyse_timestamps(
        {"path": "/x", "mtime": now - 500, "mtime_nano": 5, "si_mtime": now - 500, "fn_mtime": now - 100},
        now=now,
    )
    assert finding is not None
    assert timestomp.REASON_SI_LT_FN in finding.reasons


def test_timestomp_flags_zero_nanoseconds():
    now = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    finding = timestomp.analyse_timestamps(
        {"path": "/x", "mtime": now - 500, "mtime_nano": 0}, now=now
    )
    assert finding is not None
    assert timestomp.REASON_ZERO_NANO in finding.reasons


def test_timestomp_clean_record_is_not_flagged():
    now = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    assert timestomp.analyse_timestamps(
        {"path": "/ok", "mtime": now - 500, "atime": now - 300, "ctime": now - 200,
         "mtime_nano": 1234, "atime_nano": 999, "ctime_nano": 42},
        now=now,
    ) is None


def test_timestomp_rejects_negative_skew():
    with pytest.raises(timestomp.TimestompError):
        timestomp.analyse_timestamps({"path": "/x"}, skew=-1)


def test_timestomp_summarise_counts_reasons():
    findings = [
        timestomp.TimestompFinding("/a", (timestomp.REASON_FUTURE,)),
        timestomp.TimestompFinding("/b", (timestomp.REASON_FUTURE, timestomp.REASON_EQUAL_MAC)),
    ]
    assert timestomp.summarise(findings) == {timestomp.REASON_FUTURE: 2, timestomp.REASON_EQUAL_MAC: 1}


# ---------------------------------------------------------------------- slack
def test_slack_region_offset_is_absolute():
    # 4 blocks of 512 allocated, real size 1000 -> slack 1048 bytes,
    # last run ends at block 20 -> byte 10240, so slack starts at 10240-1048
    region = slack.slack_region_for("/big.bin", 1000, [(10, 2), (18, 2)], 512)
    assert region is not None
    assert region.allocated_size == 2048
    assert region.length == 1048
    assert region.offset == 20 * 512 - 1048


def test_slack_region_none_when_cluster_aligned():
    assert slack.slack_region_for("/exact.bin", 1024, [(0, 2)], 512) is None
    assert slack.slack_region_for("/empty.bin", 0, [(0, 2)], 512) is None
    assert slack.slack_region_for("/no-runs.bin", 10, [], 512) is None


def test_find_slack_regions_from_fs_and_extract(tmp_path):
    fs = FakeFs([("f.bin", b"\x00" * 1000)], block_size=512, runs={2: [FakeRun(4, 2)]})
    regions = slack.find_slack_regions_from_fs(fs)
    assert len(regions) == 1
    assert regions[0].path == "/f.bin"
    assert regions[0].length == 24  # 1024 allocated - 1000 real
    # the slack sits at the tail of the last allocated run
    assert regions[0].offset == (4 + 2) * 512 - 24

    payload = bytearray(4096)
    payload[regions[0].offset:regions[0].offset + 24] = b"S" * 24
    image = tmp_path / "img.bin"
    image.write_bytes(bytes(payload))
    written = slack.extract_slack(image, tmp_path / "out", regions=regions)
    assert len(written) == 1
    files = list((tmp_path / "out").iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"S" * 24


def test_iter_slack_bytes_streams(tmp_path):
    image = tmp_path / "img.bin"
    image.write_bytes(b"A" * 100)
    regions = [slack.SlackRegion(path="/x", offset=10, length=50, allocated_size=100, real_size=50)]
    chunks = list(slack.iter_slack_bytes(image, regions, chunk=20))
    assert b"".join(data for _r, data in chunks) == b"A" * 50


def test_extract_slack_missing_image_raises(tmp_path):
    with pytest.raises(slack.SlackError):
        slack.extract_slack(tmp_path / "nope.img", tmp_path / "out")


# ------------------------------------------------------------------ live diff
def test_diff_snapshots_classifies_added_modified_removed():
    before = {"/a": ("aa", 1), "/b": ("bb", 2), "/c": ("cc", 3)}
    after = {"/a": ("aa", 1), "/b": ("zz", 2), "/d": ("dd", 4)}
    entries = {e.path: e for e in live_diff.diff_snapshots(before, after)}
    assert entries["/b"].status == live_diff.MODIFIED
    assert entries["/d"].status == live_diff.ADDED
    assert entries["/c"].status == live_diff.REMOVED
    assert "/a" not in entries


def test_live_diff_take_and_compare():
    tracker = live_diff.LiveDiff()
    baseline = {"/a": ("aa", 1)}
    assert tracker.has_snapshot is False
    tracker._snapshot = baseline
    assert tracker.has_snapshot is True
    entries = live_diff.diff_snapshots(baseline, {"/a": ("bb", 1)})
    assert entries[0].status == live_diff.MODIFIED
    tracker.reset()
    assert tracker.has_snapshot is False


def test_live_diff_compare_without_baseline_raises():
    tracker = live_diff.LiveDiff()
    with pytest.raises(live_diff.LiveDiffError):
        tracker.compare("whatever.img")


def test_markdown_report_contains_sections():
    entries = [
        live_diff.DiffEntry("/new.txt", live_diff.ADDED, after_sha="a" * 64, after_size=10),
        live_diff.DiffEntry("/gone.txt", live_diff.REMOVED, before_sha="b" * 64, before_size=20),
    ]
    report = live_diff.to_markdown(entries, title="Session diff")
    assert "# Session diff" in report
    assert "## Added (1)" in report
    assert "## Modified (0)" in report
    assert "## Removed (1)" in report
    assert "`/new.txt`" in report
    assert live_diff.summarise(entries) == {live_diff.ADDED: 1, live_diff.MODIFIED: 0, live_diff.REMOVED: 1}


def test_write_markdown_writes_file(tmp_path):
    entries = [live_diff.DiffEntry("/x", live_diff.ADDED, after_sha="c" * 64, after_size=1)]
    out = tmp_path / "nested" / "diff.md"
    assert live_diff.write_markdown(entries, out) == 1
    assert out.read_text(encoding="utf-8").startswith("# Live diff")


def test_diff_entry_rejects_unknown_status():
    with pytest.raises(ValueError):
        live_diff.DiffEntry("/x", "bogus")


# --------------------------------------------------------------- CLI integration
def test_cli_exposes_new_dfir_commands():
    from mountfile.cli import COMMANDS, build_parser

    for name in ("timeline-build", "timeline-query", "search", "yara-scan",
                 "timestomp-scan", "slack-extract", "live-diff"):
        assert name in COMMANDS, name

    parser = build_parser()
    args = parser.parse_args(["timeline-build", "dump.img", "-o", "t.db"])
    assert args.func is not None
    args = parser.parse_args(["search", "dump.img", "password=", "--regex"])
    assert args.regex is True
    args = parser.parse_args(["yara-scan", "dump.img", "--rules", "rules/", "--json"])
    assert args.json is True
    args = parser.parse_args(["live-diff", "before.img", "after.img", "-o", "d.md"])
    assert args.output == "d.md"


def test_cli_timeline_build_end_to_end(tmp_path, monkeypatch):
    from mountfile import cli

    events = [timeline.TimelineEvent(datetime(2024, 1, 1, tzinfo=UTC), "pytsk3", "/a", "mtime", "ff")]

    def fake_build(image, *, db_path=None, csv_path=None, **kwargs):
        timeline.write_sqlite(events, db_path)
        return len(events), events

    monkeypatch.setattr(cli, "build_timeline", fake_build)
    image = tmp_path / "img.bin"
    image.write_bytes(b"\x00" * 16)
    db = tmp_path / "t.db"
    parser = cli.build_parser()
    args = parser.parse_args(["timeline-build", str(image), "-o", str(db)])
    assert cli.cmd_timeline_build(args) == 0
    assert db.exists()
    assert len(timeline.query_timeline(db)) == 1


def test_cli_search_reports_matches(tmp_path, capsys):
    from mountfile import cli

    image = tmp_path / "img.bin"
    image.write_bytes(b"password=hunter2")
    parser = cli.build_parser()
    args = parser.parse_args(["search", str(image), "password="])
    assert cli.cmd_search(args) == 0
    assert "password=hunter2" in capsys.readouterr().out


def test_cli_live_diff_writes_markdown(tmp_path, monkeypatch):
    from mountfile import cli

    monkeypatch.setattr(
        cli, "diff_images",
        lambda *a, **k: [live_diff.DiffEntry("/a", live_diff.ADDED, after_sha="d" * 64, after_size=1)],
        raising=False,
    )
    out = tmp_path / "diff.md"
    parser = cli.build_parser()
    args = parser.parse_args(["live-diff", "b.img", "a.img", "-o", str(out)])
    assert cli.cmd_live_diff(args) == 0
    assert out.exists()
