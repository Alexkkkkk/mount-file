import csv
import hashlib
import json

import mountfile.cli as cli
from mountfile.cli import main
from mountfile.dfir import detect_timestomp, grep_image, sha256_file


def test_hash_and_literal_search(tmp_path, capsys):
    image = tmp_path / "sample.bin"
    image.write_bytes(b"prefix password=secret suffix")
    assert sha256_file(image) == hashlib.sha256(image.read_bytes()).hexdigest()
    hits = list(grep_image(image, "password=", context=4))
    assert len(hits) == 1
    assert hits[0]["offset"] == 7
    assert main(["hash", str(image)]) == 0
    assert "SHA-256:" in capsys.readouterr().out


def test_empty_image_search_is_safe(tmp_path):
    image = tmp_path / "empty.bin"
    image.write_bytes(b"")
    assert list(grep_image(image, "x")) == []


def test_timeline_csv_and_timestomp_cli(tmp_path, capsys):
    csv_path = tmp_path / "timeline.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["path", "mtime", "atime", "ctime", "birth"])
        writer.writeheader()
        writer.writerow({"path": "/suspicious", "mtime": "1", "atime": "1", "ctime": "10", "birth": ""})
    records = list(__import__("mountfile.dfir", fromlist=["read_timeline_csv"]).read_timeline_csv(csv_path))
    assert len(list(detect_timestomp(records))) == 1
    assert main(["timeline", "image.raw", "--csv", str(csv_path)]) == 0
    assert "Records: 1" in capsys.readouterr().out
    assert main(["timestomp", "image.raw", "--csv", str(csv_path)]) == 0
    assert "Findings: 1" in capsys.readouterr().out


def test_utf16_search(tmp_path):
    image = tmp_path / "utf16.bin"
    image.write_bytes("пароль".encode("utf-16le"))
    assert len(list(grep_image(image, "пароль", encoding="utf-16le"))) == 1


def test_timeline_export_cli_normalizes_timestamps_to_utc(tmp_path, capsys):
    source = tmp_path / "timeline.csv"
    source.write_text("path,mtime,atime,ctime,birth\n/evidence/a,1700000000,,,,\n", encoding="utf-8")
    output = tmp_path / "normalized.csv"

    assert main(["timeline-export", "--csv", str(source), "--output", str(output)]) == 0

    contents = output.read_text(encoding="utf-8")
    assert '"timestamp"' in contents
    assert "Z" in contents
    assert "Records:" not in capsys.readouterr().out


def test_unallocated_scan_cli_uses_only_explicit_ranges(tmp_path, capsys):
    image = tmp_path / "image.bin"
    image.write_bytes(b"prefix\x89PNG\r\n\x1a\nsuffix")
    ranges = tmp_path / "ranges.json"
    ranges.write_text(json.dumps([[6, 8]]), encoding="utf-8")

    assert main(["unallocated-scan", str(image), "--ranges", str(ranges)]) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(rows) == 1
    assert json.loads(rows[0])["offset"] == 6


def test_yara_cli_uses_user_rules_and_prints_json(tmp_path, monkeypatch, capsys):
    image = tmp_path / "image.bin"
    image.write_bytes(b"evidence")
    rules = tmp_path / "user.yar"
    rules.write_text("rule sample { condition: true }", encoding="utf-8")
    calls = []

    def fake_scan(image_path, rules_path):
        calls.append((image_path, rules_path))
        return [{"rule": "sample", "tags": (), "meta": {}, "offsets": ()}]

    monkeypatch.setattr(cli, "scan_yara", fake_scan)
    assert main(["yara", str(image), "--rules", str(rules)]) == 0
    assert calls == [(str(image), str(rules))]
    assert '"rule": "sample"' in capsys.readouterr().out
