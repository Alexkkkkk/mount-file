"""CLI mutations use the same explicitly bounded native writer facade."""

from __future__ import annotations

import json
from types import SimpleNamespace

from mountfile import cli
from mountfile.capabilities import get_native_capability_matrix


class _FakeWriter:
    def __init__(self):
        self.calls = []
        self.closed = False

    def write_file(self, path, data):
        self.calls.append(("write_file", path, data))

    def mkdir(self, path):
        self.calls.append(("mkdir", path))

    def rename(self, source, destination):
        self.calls.append(("rename", source, destination))

    def delete(self, path, recursive=False):
        self.calls.append(("delete", path, recursive))

    def close(self):
        self.closed = True


def test_open_write_uses_bounded_universal_native_writer(monkeypatch):
    captured = {}
    writer = _FakeWriter()

    def factory(image, **kwargs):
        captured["image"] = image
        captured.update(kwargs)
        return writer

    monkeypatch.setattr(cli, "volume_size_for_offset", lambda _image, _offset: 4096)
    monkeypatch.setattr(cli, "UniversalFSRW", factory)

    assert cli._open_write("disk.img", 512) is writer
    assert captured == {
        "image": "disk.img",
        "offset": 512,
        "volume_size": 4096,
        "auto_backup": False,
        "allow_legacy_mount": False,
    }


def test_put_routes_absolute_target_path_through_native_writer(monkeypatch, tmp_path):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")
    writer = _FakeWriter()
    monkeypatch.setattr(cli, "_select", lambda *_args: (4096, "exfat", "volume"))
    monkeypatch.setattr(cli, "_open_write", lambda *_args, **_kwargs: writer)

    result = cli.cmd_put(
        SimpleNamespace(
            yes=True,
            image="disk.img",
            dest="/docs",
            local=[str(source)],
            backup=False,
        )
    )

    assert result == cli.OK
    assert writer.calls == [("write_file", "/docs/payload.bin", b"payload")]
    assert writer.closed


def test_mkdir_mv_and_recursive_rm_route_through_native_writer(monkeypatch):
    writer = _FakeWriter()
    monkeypatch.setattr(cli, "_select", lambda *_args: (4096, "ext2", "volume"))
    monkeypatch.setattr(cli, "_open_write", lambda *_args, **_kwargs: writer)

    assert (
        cli.cmd_mkdir(
            SimpleNamespace(yes=True, image="disk.img", path="/docs/new", backup=False)
        )
        == cli.OK
    )
    assert (
        cli.cmd_mv(
            SimpleNamespace(
                yes=True,
                image="disk.img",
                path="/docs/old.txt",
                new_name="new.txt",
                backup=False,
            )
        )
        == cli.OK
    )
    assert (
        cli.cmd_rm(
            SimpleNamespace(
                yes=True,
                image="disk.img",
                path="/docs/new",
                recursive=True,
                backup=False,
            )
        )
        == cli.OK
    )

    assert writer.calls == [
        ("mkdir", "/docs/new"),
        ("rename", "/docs/old.txt", "/docs/new.txt"),
        ("delete", "/docs/new", True),
    ]
    assert writer.closed


class _FakeReader:
    backend_name = "native-test-ro"

    def __init__(self):
        self.closed = False

    def stat(self, path):
        return {
            "path": path,
            "name": "payload.bin",
            "is_dir": False,
            "size": 7,
        }

    def read_file_range(self, path, offset=0, length=1 << 20):
        assert path == "/payload.bin"
        return b"payload"[offset : offset + length]

    def close(self):
        self.closed = True


def test_stat_reports_native_backend_and_path_metadata(monkeypatch, capsys):
    reader = _FakeReader()
    monkeypatch.setattr(cli, "_select", lambda *_args: (512, "ext4", "volume"))
    monkeypatch.setattr(cli, "_provider", lambda *_args: (reader, "native-test-ro"))

    result = cli.cmd_stat(SimpleNamespace(image="disk.img", path="/payload.bin"))

    assert result == cli.OK
    report = json.loads(capsys.readouterr().out)
    assert report["filesystem"] == "ext4"
    assert report["backend"] == "native-test-ro"
    assert report["path"] == "/payload.bin"
    assert report["size"] == 7
    assert reader.closed


def test_read_range_writes_only_requested_bytes_to_new_output(
    monkeypatch, tmp_path, capsys
):
    reader = _FakeReader()
    output = tmp_path / "range.bin"
    monkeypatch.setattr(cli, "_select", lambda *_args: (512, "ntfs", "volume"))
    monkeypatch.setattr(cli, "_provider", lambda *_args: (reader, "native-test-ro"))

    result = cli.cmd_read_range(
        SimpleNamespace(
            image="disk.img",
            path="/payload.bin",
            file_offset=2,
            length=3,
            output=str(output),
        )
    )

    assert result == cli.OK
    assert output.read_bytes() == b"ylo"
    assert "Wrote 3 bytes" in capsys.readouterr().err
    assert reader.closed


def test_capabilities_command_prints_canonical_matrix(capsys):
    parsed = cli.build_parser().parse_args(["capabilities"])
    assert parsed.func is cli.cmd_capabilities

    result = cli.cmd_capabilities(SimpleNamespace())

    assert result == cli.OK
    assert json.loads(capsys.readouterr().out) == get_native_capability_matrix()
