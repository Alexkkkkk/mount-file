import csv
import hashlib

from mountfile.cli import main
from mountfile.dfir import TimelineRecord, detect_timestomp, grep_image, sha256_file


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
