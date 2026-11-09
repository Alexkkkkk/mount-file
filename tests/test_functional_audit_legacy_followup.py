"""Legacy-only regressions: all mount/ImDisk actions are mocked."""

import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mountfile import universal_fs_rw as rw


@pytest.fixture(autouse=True)
def no_real_mount_actions(monkeypatch):
    monkeypatch.setattr(rw.sys, "platform", "linux")
    monkeypatch.setattr(
        rw.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unexpected real subprocess action"),
    )
    monkeypatch.setattr(
        rw,
        "_imdisk_mount",
        lambda *args: pytest.fail("unexpected ImDisk attach"),
    )
    monkeypatch.setattr(
        rw,
        "_imdisk_unmount",
        lambda *args: pytest.fail("unexpected ImDisk detach"),
    )


def result(code=0, detail=""):
    return SimpleNamespace(returncode=code, stdout="", stderr=detail)


class Reader:
    def __init__(self, image, entries=None):
        self.image = image
        self.offset = 0
        self.entries = entries or {}
        self.payloads = {}
        self.closed = False
        self.reads = []

    def info(self):
        return rw.FSInfo(
            "ext2", self.offset, "mock-reader", total_size=self.image.stat().st_size
        )

    def list(self, path="/"):
        return self.entries.get(path, [])

    def read(self, path):
        self.reads.append(path)
        payload = self.payloads[path]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def close(self):
        self.closed = True

    def reopen(self):
        self.closed = False


def entry(path, payload=b"old", *, is_dir=False, **kwargs):
    return rw.FSEntry(
        Path(path).name,
        path,
        0 if is_dir else len(payload),
        is_dir,
        **kwargs,
    )


@pytest.fixture
def rebuild(tmp_path):
    image = tmp_path / "original.img"
    image.write_bytes(b"original forensic image".ljust(4096, b"!"))
    reader = Reader(image, {"/": [entry("/old.txt")]})
    reader.payloads["/old.txt"] = b"old"
    return rw.RebuildRW(reader, "ext2")


def fake_build(monkeypatch, backend, *, fill_error=None):
    calls = []
    copied = {}

    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == "mkfs.ext2"
        return result()

    def fill(stage, image):
        copied.update(
            (path.relative_to(stage).as_posix(), path.read_bytes())
            for path in stage.rglob("*")
            if path.is_file()
        )
        if fill_error:
            raise fill_error
        image.write_bytes(b"rebuilt".ljust(backend.image.stat().st_size, b"#"))

    monkeypatch.setattr(rw.subprocess, "run", run)
    monkeypatch.setattr(backend, "_fill", fill)
    return calls, copied


@pytest.mark.parametrize("backend_type", [rw.NtfsRW, rw.LoopMountRW, rw.RebuildRW])
@pytest.mark.parametrize(
    "failure", ["returncode", "timeout", "cleanup", "still-mounted"]
)
def test_failed_detach_or_cleanup_never_recursively_removes_mount(
    tmp_path, monkeypatch, backend_type, failure
):
    backend = backend_type.__new__(backend_type)
    mount = tmp_path / "mount"
    mount.mkdir()
    evidence = mount / "do-not-delete.txt"
    evidence.write_bytes(b"mounted evidence")
    monkeypatch.setattr(
        rw.shutil,
        "rmtree",
        lambda *args, **kwargs: pytest.fail("recursive mount cleanup"),
    )

    def run(command, **kwargs):
        assert command == ["umount", str(mount)]
        if failure == "timeout":
            raise rw.subprocess.TimeoutExpired(command, 120)
        return result(1, "busy volume") if failure == "returncode" else result()

    monkeypatch.setattr(rw.subprocess, "run", run)
    monkeypatch.setattr(rw.os.path, "ismount", lambda path: failure == "still-mounted")
    with pytest.raises(RuntimeError, match="retained"):
        backend._unmount(mount)

    assert evidence.read_bytes() == b"mounted evidence"
    assert backend._retained_mount_point == mount
    assert backend._mount_failure
    with pytest.raises(RuntimeError, match="manual inspection"):
        backend._check_pending_mount()


@pytest.mark.parametrize("backend_type", [rw.NtfsRW, rw.LoopMountRW, rw.RebuildRW])
def test_successful_detach_uses_empty_directory_removal_only(
    tmp_path, monkeypatch, backend_type
):
    backend = backend_type.__new__(backend_type)
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setattr(rw.subprocess, "run", lambda *args, **kwargs: result())
    monkeypatch.setattr(
        rw.shutil, "rmtree", lambda *args, **kwargs: pytest.fail("rmtree mount")
    )
    backend._unmount(mount)
    assert not mount.exists()
    assert backend._retained_mount_point is None


@pytest.mark.parametrize("backend_type", [rw.NtfsRW, rw.LoopMountRW])
def test_imdisk_failed_detach_keeps_drive_for_inspection_and_retry(
    tmp_path, monkeypatch, backend_type
):
    backend = backend_type.__new__(backend_type)
    backend._imdisk_drive = "R:"
    mount = tmp_path / "mock-drive"
    mount.mkdir()
    monkeypatch.setattr(rw.sys, "platform", "win32")

    def fail(drive):
        assert drive == "R:"
        raise RuntimeError("device still busy")

    monkeypatch.setattr(rw, "_imdisk_unmount", fail)
    with pytest.raises(RuntimeError, match="retained"):
        backend._unmount(mount)
    assert backend._imdisk_drive == "R:"
    assert mount.is_dir()
    monkeypatch.setattr(rw, "_imdisk_unmount", lambda drive: None)
    backend._unmount(mount)
    assert backend._imdisk_drive is None
    assert mount.is_dir()  # Windows drive roots are never removed.


@pytest.mark.parametrize("backend_type", [rw.NtfsRW, rw.LoopMountRW])
def test_failed_mount_retains_directory_and_blocks_new_mount(
    tmp_path, monkeypatch, backend_type
):
    image = tmp_path / "image.img"
    image.write_bytes(b"original")
    backend = backend_type.__new__(backend_type)
    rw.RWBackend.__init__(backend, image)
    backend.fs_type = "ext2"
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setattr(rw.tempfile, "mkdtemp", lambda **kwargs: str(mount))
    monkeypatch.setattr(rw.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        rw.subprocess, "run", lambda *args, **kwargs: result(1, "mount failed")
    )
    with pytest.raises(RuntimeError, match="mount failed"):
        backend._mount_point()
    assert mount.is_dir()
    assert backend._retained_mount_point == mount
    with pytest.raises(RuntimeError, match="manual inspection"):
        backend._mount_point()
    assert image.read_bytes() == b"original"


BAD_PATHS = [
    "/../escape",
    r"\..\escape",
    "/dir/../../escape",
    "C:/host",
    "file:stream",
    "./file",
    "/dir//file",
    "/bad\x00name",
    "/",
    "",
]
OPERATIONS = ["write_file", "mkdir", "delete", "rename-source", "rename-destination"]


def invoke(backend, operation, path):
    if operation == "write_file":
        backend.write_file(path, b"replacement")
    elif operation == "mkdir":
        backend.mkdir(path)
    elif operation == "delete":
        backend.delete(path, recursive=True)
    elif operation == "rename-source":
        backend.rename(path, "/new.txt")
    else:
        backend.rename("/old.txt", path)


@pytest.mark.parametrize("path", BAD_PATHS)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_rebuild_rejects_unsafe_mutation_paths_before_read_or_mkfs(
    rebuild, operation, path
):
    original = rebuild.image.read_bytes()
    with pytest.raises(ValueError, match="unsafe path"):
        invoke(rebuild, operation, path)
    assert rebuild.reader.reads == []
    assert rebuild.image.read_bytes() == original
    assert not list(rebuild.image.parent.glob(".mountfile-rebuild-*"))


@pytest.mark.parametrize("backend_type", [rw.NtfsRW, rw.LoopMountRW])
@pytest.mark.parametrize("path", ["/../escape", r"\..\escape", "C:/host", "/", ""])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_mount_rejects_unsafe_mutations_before_mount(
    tmp_path, monkeypatch, backend_type, operation, path
):
    backend = backend_type.__new__(backend_type)
    backend.fs_type = "ext2"
    if backend_type is rw.LoopMountRW:
        monkeypatch.setattr(backend, "_assert_writable", lambda: None)
    with pytest.raises(ValueError, match="unsafe path"):
        invoke(backend, operation, path)


@pytest.mark.parametrize("operation", ["read", *OPERATIONS])
def test_rebuild_stage_links_cannot_escape_host(
    rebuild, tmp_path, monkeypatch, operation
):
    outside = tmp_path / "host.txt"
    outside.write_bytes(b"host evidence")
    original = rebuild.image.read_bytes()

    def unsafe_stage(callback):
        stage = tmp_path / "stage"
        stage.mkdir()
        (stage / "escape").symlink_to(outside)
        callback(stage)

    if operation == "read":
        stage = tmp_path / "stage"
        stage.mkdir()
        (stage / "escape").symlink_to(outside)
        with pytest.raises(PermissionError, match="link/reparse"):
            rw._legacy_read_file(stage, "/escape")
    else:
        monkeypatch.setattr(rebuild, "_rebuild_with", unsafe_stage)
        with pytest.raises(PermissionError, match="link/reparse"):
            invoke(rebuild, operation, "/escape")
    assert outside.read_bytes() == b"host evidence"
    assert rebuild.image.read_bytes() == original


@pytest.mark.parametrize("root_link", [True, False])
def test_parent_or_root_symlink_is_refused(tmp_path, root_link):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"host")
    root = tmp_path / "stage"
    if root_link:
        root.symlink_to(outside, target_is_directory=True)
        target = "/file"
    else:
        root.mkdir()
        (root / "dir").symlink_to(outside, target_is_directory=True)
        target = "/dir/file"
    with pytest.raises(PermissionError):
        rw._legacy_write_file(root, target, b"modified")
    assert (outside / "file").read_bytes() == b"host"


def test_hardlinked_stage_file_is_not_truncated(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    outside = tmp_path / "host"
    outside.write_bytes(b"host evidence")
    os.link(outside, stage / "alias")
    with pytest.raises(PermissionError, match="non-linked"):
        rw._legacy_write_file(stage, "/alias", b"damage")
    assert outside.read_bytes() == b"host evidence"


def test_nested_mount_is_rejected_before_path_mutation(tmp_path, monkeypatch):
    root = tmp_path / "stage"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.setattr(rw.os.path, "ismount", lambda path: Path(path) == nested)
    with pytest.raises(PermissionError, match="nested mount"):
        rw._legacy_path(root, "/nested/file")


@pytest.mark.parametrize("operation", ["read", "list", "delete"])
def test_loop_mount_rejects_links_without_reading_or_removing_target(
    tmp_path, monkeypatch, operation
):
    mount = tmp_path / "mounted"
    mount.mkdir()
    outside = tmp_path / "host"
    outside.write_bytes(b"host evidence")
    (mount / "link").symlink_to(outside)
    backend = rw.LoopMountRW(tmp_path / "image.img", 0, "ext2")
    monkeypatch.setattr(backend, "_do", lambda callback: callback(mount))
    monkeypatch.setattr(backend, "_assert_writable", lambda: None)
    with pytest.raises(PermissionError, match="link/reparse"):
        if operation == "list":
            backend.list("/")
        elif operation == "read":
            backend.read("/link")
        else:
            backend.delete("/link", recursive=True)
    assert outside.read_bytes() == b"host evidence"


@pytest.mark.parametrize("failure", [OSError("read failure"), b"sh", None])
def test_rebuild_read_failure_is_explicit_and_preserves_image(rebuild, failure):
    original = rebuild.image.read_bytes()
    rebuild.reader.payloads["/old.txt"] = failure
    with pytest.raises(OSError, match="could not read|short/invalid read"):
        rebuild.write_file("/new.txt", b"new")
    assert rebuild.image.read_bytes() == original
    assert not list(rebuild.image.parent.glob(".mountfile-rebuild-*"))


@pytest.mark.parametrize(
    "bad_entry",
    [
        rw.FSEntry("escape", "/../host", 3, False),
        rw.FSEntry("../escape", "/escape", 3, False),
        rw.FSEntry("file", "/other/file", 3, False),
        entry("/old.txt", mode=stat.S_IFLNK | 0o777),
        entry("/old.txt", mode=stat.S_IFIFO | 0o600),
        entry("/old.txt", deleted=True),
    ],
)
def test_rebuild_untrusted_entries_fail_before_reading(rebuild, bad_entry):
    original = rebuild.image.read_bytes()
    rebuild.reader.entries["/"] = [bad_entry]
    with pytest.raises((ValueError, PermissionError)):
        rebuild.mkdir("/new")
    assert rebuild.reader.reads == []
    assert rebuild.image.read_bytes() == original


def test_rebuild_list_failure_is_not_skipped(rebuild, monkeypatch):
    original = rebuild.image.read_bytes()
    monkeypatch.setattr(
        rebuild.reader,
        "list",
        lambda path: (_ for _ in ()).throw(OSError("listing failed")),
    )
    with pytest.raises(OSError, match="listing failed"):
        rebuild.mkdir("/new")
    assert rebuild.image.read_bytes() == original


def test_rebuild_duplicate_path_and_directory_cycle_are_refused(rebuild):
    original = rebuild.image.read_bytes()
    rebuild.reader.entries["/"] = [entry("/old.txt"), entry("/old.txt")]
    with pytest.raises(ValueError, match="duplicate"):
        rebuild.mkdir("/new")
    rebuild.reader.entries = {
        "/": [entry("/dir", is_dir=True, inode=1)],
        "/dir": [entry("/dir/alias", is_dir=True, inode=1)],
    }
    with pytest.raises(ValueError, match="cycle/alias"):
        rebuild.mkdir("/new")
    assert rebuild.image.read_bytes() == original


@pytest.mark.parametrize(
    "profile", ["offset", "extent", "mismatch", "unsupported", "symlink", "hardlink"]
)
def test_rebuild_whole_image_and_regular_file_gates(rebuild, tmp_path, profile):
    original = rebuild.image.read_bytes()
    if profile == "offset":
        rebuild.offset = 512
    elif profile == "extent":
        rebuild.reader.info = lambda: rw.FSInfo("ext2", 0, "mock", total_size=100)
    elif profile == "mismatch":
        rebuild.fs_type = "ntfs"
    elif profile == "unsupported":
        rebuild.fs_type = "made-up-filesystem"
    elif profile == "symlink":
        link = tmp_path / "link.img"
        link.symlink_to(rebuild.image)
        rebuild.image = link
    else:
        os.link(rebuild.image, tmp_path / "image-alias.img")
    with pytest.raises((ValueError, RuntimeError)):
        rebuild.write_file("/new.txt", b"new")
    assert rebuild.image.read_bytes() == original
    assert rebuild.reader.reads == []


@pytest.mark.parametrize(
    "failure", ["mkfs", "fill", "size", "fsync", "replace", "tree-cleanup"]
)
def test_precommit_failures_preserve_original_image(rebuild, monkeypatch, failure):
    original = rebuild.image.read_bytes()
    fake_build(
        monkeypatch,
        rebuild,
        fill_error=OSError("copy failed") if failure == "fill" else None,
    )
    if failure == "mkfs":
        monkeypatch.setattr(
            rw.subprocess, "run", lambda *args, **kwargs: result(1, "mkfs failed")
        )
    elif failure == "size":
        monkeypatch.setattr(
            rebuild, "_fill", lambda stage, image: image.write_bytes(b"too short")
        )
    elif failure == "fsync":
        monkeypatch.setattr(
            rw,
            "_fsync_file",
            lambda path: (_ for _ in ()).throw(OSError("fsync failed")),
        )
    elif failure == "replace":
        monkeypatch.setattr(
            rw.os,
            "replace",
            lambda *args: (_ for _ in ()).throw(OSError("replace failed")),
        )
    elif failure == "tree-cleanup":
        real_rmtree = shutil.rmtree

        def remove(path, *args, **kwargs):
            if Path(path).name == "tree":
                raise OSError("tree cleanup failed")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(rw.shutil, "rmtree", remove)
    with pytest.raises((OSError, RuntimeError)):
        rebuild.write_file("/new.txt", b"new")
    assert rebuild.image.read_bytes() == original
    assert not list(rebuild.image.parent.glob(".mountfile-rebuild-*"))


def test_rebuild_success_uses_same_filesystem_staging_and_refreshes_reader(
    rebuild, monkeypatch
):
    original_mode = stat.S_IMODE(rebuild.image.stat().st_mode)
    calls, copied = fake_build(monkeypatch, rebuild)
    replacements = []
    real_replace = os.replace

    def replace(source, destination):
        source = Path(source)
        assert source.parent.parent == rebuild.image.parent
        replacements.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(rw.os, "replace", replace)
    rebuild.write_file(r"\new-dir\new.txt", b"new data")
    assert copied == {"old.txt": b"old", "new-dir/new.txt": b"new data"}
    assert len(calls) == len(replacements) == 1
    assert rebuild.image.read_bytes().startswith(b"rebuilt")
    assert stat.S_IMODE(rebuild.image.stat().st_mode) == original_mode
    assert not rebuild.reader.closed
    assert not rebuild._reader_stale
    assert not list(rebuild.image.parent.glob(".mountfile-rebuild-*"))


def test_post_replace_fsync_failure_rolls_back_image(rebuild, monkeypatch):
    original = rebuild.image.read_bytes()
    fake_build(monkeypatch, rebuild)
    count = 0

    def fsync_directory(path):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("directory fsync failed")

    monkeypatch.setattr(rw, "_fsync_directory", fsync_directory)
    with pytest.raises(OSError, match="directory fsync failed"):
        rebuild.write_file("/new.txt", b"new")
    assert count == 2
    assert rebuild.image.read_bytes() == original
    assert not rebuild.reader.closed


def test_reader_without_reopen_is_explicitly_stale_after_commit(rebuild, monkeypatch):
    fake_build(monkeypatch, rebuild)
    rebuild.reader.reopen = None
    rebuild.mkdir("/new")
    with pytest.raises(RuntimeError, match="closed/stale"):
        rebuild.read("/old.txt")
    with pytest.raises(RuntimeError, match="closed/stale"):
        rebuild.write_file("/next.txt", b"next")


def test_source_change_is_not_overwritten_by_staged_rebuild(rebuild, monkeypatch):
    fake_build(monkeypatch, rebuild)
    changed = b"external concurrent writer".ljust(4096, b"?")

    def fill(stage, image):
        rebuild.image.write_bytes(changed)

    monkeypatch.setattr(rebuild, "_fill", fill)
    with pytest.raises(RuntimeError, match="source image changed"):
        rebuild.mkdir("/new")
    assert rebuild.image.read_bytes() == changed


@pytest.mark.parametrize("detach_failure", ["umount", "cleanup", "sync", "copy"])
def test_rebuild_fill_failure_preserves_original_and_mount_evidence(
    rebuild, tmp_path, monkeypatch, detach_failure
):
    original = rebuild.image.read_bytes()
    mount = tmp_path / "mock-mount"
    mount.mkdir()
    real_mkdtemp = rw.tempfile.mkdtemp
    commands = []

    def mkdtemp(**kwargs):
        if kwargs.get("prefix") == "mnt_":
            return str(mount)
        return real_mkdtemp(**kwargs)

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "umount":
            if detach_failure == "umount":
                return result(1, "busy")
            if detach_failure == "cleanup":
                return result()  # fake mounted contents stay in place
            for child in list(mount.iterdir()):
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        if command[0] == "sync" and detach_failure == "sync":
            return result(1, "sync failure")
        return result()

    monkeypatch.setattr(rw.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(rw.subprocess, "run", run)
    if detach_failure == "copy":
        real_write = rw._legacy_write_file

        def write(root, path, data, **kwargs):
            if Path(root) == mount:
                raise OSError("copy failure")
            return real_write(root, path, data, **kwargs)

        monkeypatch.setattr(rw, "_legacy_write_file", write)
    with pytest.raises((RuntimeError, OSError)):
        rebuild.write_file("/new.txt", b"new")
    assert rebuild.image.read_bytes() == original
    assert "umount" in [command[0] for command in commands]
    if detach_failure in ("umount", "cleanup"):
        assert (mount / "old.txt").read_bytes() == b"old"
        assert rebuild._retained_mount_point == mount
        assert rebuild._retained_workspace.is_dir()
        assert (rebuild._retained_workspace / "staged.img").is_file()
        assert (rebuild._retained_workspace / "tree" / "old.txt").read_bytes() == b"old"
    else:
        assert not mount.exists()
        assert rebuild._retained_workspace is None


def test_fill_rejects_source_link_before_mount(rebuild, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "link").symlink_to(rebuild.image)
    with pytest.raises(PermissionError, match="link/reparse"):
        rebuild._fill(stage, tmp_path / "staged.img")


def test_fill_rejects_mounted_destination_link_and_still_detaches(
    rebuild, tmp_path, monkeypatch
):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "file").write_bytes(b"new data")
    outside = tmp_path / "host"
    outside.write_bytes(b"host data")
    mount = tmp_path / "mount"
    mount.mkdir()
    commands = []
    monkeypatch.setattr(rw.tempfile, "mkdtemp", lambda **kwargs: str(mount))

    def run(command, **kwargs):
        commands.append(command[0])
        if command[0] == "mount":
            (mount / "file").symlink_to(outside)
        else:
            (mount / "file").unlink()
        return result()

    monkeypatch.setattr(rw.subprocess, "run", run)
    with pytest.raises(PermissionError, match="link/reparse"):
        rebuild._fill(stage, tmp_path / "staged.img")
    assert commands == ["mount", "umount"]
    assert outside.read_bytes() == b"host data"
    assert not mount.exists()
