"""CLI mutations use the same explicitly bounded native writer facade."""

from __future__ import annotations

from types import SimpleNamespace

from mountfile import cli


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


def test_put_routes_absolute_target_path_through_native_writer(
    monkeypatch, tmp_path
):
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

    assert cli.cmd_mkdir(
        SimpleNamespace(yes=True, image="disk.img", path="/docs/new", backup=False)
    ) == cli.OK
    assert cli.cmd_mv(
        SimpleNamespace(
            yes=True,
            image="disk.img",
            path="/docs/old.txt",
            new_name="new.txt",
            backup=False,
        )
    ) == cli.OK
    assert cli.cmd_rm(
        SimpleNamespace(
            yes=True,
            image="disk.img",
            path="/docs/new",
            recursive=True,
            backup=False,
        )
    ) == cli.OK

    assert writer.calls == [
        ("mkdir", "/docs/new"),
        ("rename", "/docs/old.txt", "/docs/new.txt"),
        ("delete", "/docs/new", True),
    ]
    assert writer.closed
