"""Regression coverage for real desktop workflows and safe mutations."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("mountfile.qt_compat", exc_type=ImportError)

from test_gui_end_to_end import build_disk

from mountfile import mainwindow, tc_style, tc_window, text_editor
from mountfile.qt_compat import MB_No, MB_Yes, QApplication, QEvent, QMessageBox


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "SETTINGS_PATH", str(tmp_path / "ui.json"))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: MB_Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: MB_Yes)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args, **_kwargs: None)
    window = tc_window.MainWindow()
    yield window
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, getattr(QEvent, "Type", QEvent).DeferredDelete)
    app.processEvents()


def local_panel(win, path, side="R"):
    panel = win.panels[side]
    win.release_volume(panel)
    panel.mode = "local"
    panel.cd(str(path))
    win.set_active(side)
    return panel


def test_tree_parent_uses_parent_row_and_secondary_column_has_no_children(app):
    tree = {
        "/": [{"name": "first", "is_dir": True}, {"name": "second", "is_dir": True}],
        "/second": [{"name": "nested", "is_dir": True}],
        "/second/nested": [{"name": "file", "is_dir": False}],
    }
    provider = SimpleNamespace(root_name="test", list_dir=lambda path: tree[path])
    model = mainwindow.ImageModel(provider)
    model.fetchMore()
    second = model.index(1, 0)
    model.fetchMore(second)
    nested = model.index(0, 0, second)
    assert model.parent(nested) == second
    model.fetchMore(nested)
    assert model.parent(model.index(0, 0, nested)) == nested
    secondary = model.index(1, 1)
    assert model.rowCount(secondary) == 0
    assert not model.hasChildren(secondary)
    assert not model.canFetchMore(secondary)


def test_classic_open_mounts_partition_only_once(app, monkeypatch, tmp_path):
    monkeypatch.setattr(mainwindow.QMessageBox, "warning", lambda *_args: MB_No)
    window = mainwindow.MainWindow()
    calls = []
    monkeypatch.setattr(window, "_mount_volume", calls.append)
    try:
        window.open_image(build_disk(tmp_path))
        assert calls == [2048 * 512]
    finally:
        window.close()


def test_tc_writer_uses_bounded_facade_and_refreshes_both_image_readers(win, tmp_path):
    image = build_disk(tmp_path)
    assert win.load_image(image)
    left = win.panelL
    assert isinstance(left.writer, tc_window._UniversalImageWriter)
    assert left.writer.filesystem.volume_size == 70000 * 512
    assert win.mount_volume(win.panelR, left.volume_offset)
    right = win.panelR
    left.writer.write_file("/", "new.bin", b"new contents")
    assert left.provider.read_file("/new.bin") == b"new contents"
    assert right.provider.read_file("/new.bin") == b"new contents"
    assert "new.bin" in {entry["name"] for entry in right.entries}


def test_image_hex_save_rejects_changed_contents_even_with_same_provider(win, tmp_path):
    assert win.load_image(build_disk(tmp_path))
    panel = win.panelL
    panel.table.setCurrentCell(panel._row_of("HELLO.TXT"), 0)
    win.f3_hex_view()
    page = win.workspace_stack.currentWidget()
    panel.writer.write_file("/", "HELLO.TXT", b"external edit")
    page.hex_view.insertBytes(b"stale")
    page.save()
    assert page.hex_view.isModified()
    assert panel.provider.read_file("/HELLO.TXT") == b"external edit"
    assert not os.path.exists(win.image_path + ".bak")


def test_image_hex_can_save_twice_and_advance_expected_snapshot(win, tmp_path):
    assert win.load_image(build_disk(tmp_path))
    panel = win.panelL
    assert panel.writer is not None, panel.write_reason
    panel.table.setCurrentCell(panel._row_of("HELLO.TXT"), 0)
    win.f3_hex_view()
    page = win.workspace_stack.currentWidget()
    page.hex_view.insertBytes(b"one")
    assert page.save(), page.last_error
    assert not page.hex_view.isModified()
    page.hex_view.insertBytes(b"two")
    expected = page.hex_view.data()
    assert page.save(), page.last_error
    assert not page.hex_view.isModified()
    assert panel.provider.read_file("/HELLO.TXT") == expected


def test_volume_extent_ignores_signature_hits_inside_a_filesystem(
    win, monkeypatch, tmp_path
):
    """Signature scanning finds headers inside a volume; they are not bounds."""
    from mountfile import vfs
    from mountfile.vfs import TskError

    image = tmp_path / "whole-disk.img"
    image.write_bytes(b"\x00" * 8192)
    win.image_path = str(image)
    win.volumes = [
        SimpleNamespace(
            offset=0, fstype="NTFS", size=None, source="signature", desc="NTFS @ 0x0"
        ),
        SimpleNamespace(
            offset=2048,
            fstype="ext2/3/4",
            size=None,
            source="signature",
            desc="ext @ 0x800",
        ),
    ]
    seen = {}

    def fake_open(path, offset=0, volume_size=None, **_kwargs):
        seen["volume_size"] = volume_size
        raise TskError("stop after recording the extent")

    monkeypatch.setattr(vfs, "open_image_provider", fake_open)
    monkeypatch.setattr(win, "_auto_pick", lambda *_args, **_kwargs: False)

    assert not win.mount_volume(win.panelL, 0)
    assert seen["volume_size"] == len(image.read_bytes())


def test_relative_cd_resolves_against_panel_directory(win, tmp_path):
    root = tmp_path / "browse"
    (root / "child").mkdir(parents=True)
    panel = local_panel(win, root)
    assert win.try_cd(panel, "child")
    assert panel.path == str(root / "child")
    win.cmdline.setText("cd ..")
    win.on_cmdline()
    assert panel.path == str(root)
    panel.path_edit.setText("missing")
    panel.path_entered()
    assert panel.path == str(root)
    assert panel.path_edit.text() == str(root) + os.sep + "*.*"


def test_closing_inactive_tab_preserves_active_directory(win, tmp_path):
    roots = [tmp_path / name for name in ("one", "two", "three")]
    for root in roots:
        root.mkdir()
    panel = local_panel(win, roots[-1])
    panel.tabs = [str(root) for root in roots]
    panel.tab_index = 2
    panel._rebuild_tabs()
    panel.tab_closed(0)
    assert panel.path == str(roots[-1])
    assert panel.tab_index == 1


def test_image_export_preserves_empty_dirs_and_refuses_destination_symlink(
    win, tmp_path
):
    image = build_disk(tmp_path)
    assert win.load_image(image)
    panel = win.panelL
    panel.writer.mkdir("/", "empty")
    destination = tmp_path / "export"
    destination.mkdir()
    other = local_panel(win, destination)
    win._copy_img_to_local(panel, other, [{"name": "empty", "is_dir": True}])
    assert (destination / "empty").is_dir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    link = destination / "HELLO.TXT"
    link.symlink_to(victim)
    win._copy_img_to_local(panel, other, [{"name": "HELLO.TXT", "is_dir": False}])
    assert link.is_symlink()
    assert victim.read_bytes() == b"keep"
    assert "не удалось" in win.log.toPlainText().lower()


def test_image_export_read_failure_preserves_existing_destination(win, tmp_path):
    destination = tmp_path / "export"
    destination.mkdir()
    existing = destination / "file.bin"
    existing.write_bytes(b"keep")
    other = local_panel(win, destination)

    def failed_read(_path):
        raise OSError("bad image data")

    panel = SimpleNamespace(
        provider=SimpleNamespace(read_file=failed_read), join=lambda name: "/" + name
    )
    win._copy_img_to_local(panel, other, [{"name": "file.bin", "is_dir": False}])
    assert existing.read_bytes() == b"keep"
    assert "не удалось: 1" in win.log.toPlainText()


def test_image_export_rejects_traversal_names_before_reading(win, tmp_path):
    destination = tmp_path / "export"
    destination.mkdir()
    other = local_panel(win, destination)
    reads = []
    panel = SimpleNamespace(
        provider=SimpleNamespace(read_file=lambda path: reads.append(path) or b"bad"),
        join=lambda name: "/" + name,
    )
    win._copy_img_to_local(panel, other, [{"name": "../escaped", "is_dir": False}])
    assert not (tmp_path / "escaped").exists()
    assert reads == []


def test_local_copy_file_does_not_follow_destination_symlink(win, tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "file.bin").write_bytes(b"new")
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    (destination / "file.bin").symlink_to(victim)
    panel = local_panel(win, source)
    other = local_panel(win, destination, "L")
    win._copy_local_to_local(
        panel, other, [{"name": "file.bin", "is_dir": False}], False
    )
    assert victim.read_bytes() == b"keep"
    assert not (destination / "file.bin").is_symlink()
    assert (destination / "file.bin").read_bytes() == b"new"


def test_image_import_does_not_follow_source_symlink_or_delete_source(win, tmp_path):
    assert win.load_image(build_disk(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"private")
    link = source / "file.bin"
    link.symlink_to(victim)
    panel = local_panel(win, source)
    win._copy_local_to_img(
        panel, win.panelL, [{"name": "file.bin", "is_dir": False}], True
    )
    assert link.is_symlink()
    assert victim.read_bytes() == b"private"
    assert "file.bin" not in {entry["name"] for entry in win.panelL.entries}


def test_external_text_copy_does_not_overwrite_live_editor_session(win, monkeypatch):
    panel = SimpleNamespace(
        mode="img",
        path="/",
        provider=SimpleNamespace(read_file=lambda _path: b"opened\n"),
        writer=None,
        join=lambda name: "/" + name,
    )
    copies = []
    monkeypatch.setattr(
        text_editor,
        "open_in_editor",
        lambda path: copies.append(path) or ["fake", str(path)],
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    entry = {"name": "same.txt", "size": 7, "is_dir": False}
    win._open_text_entry(panel, entry)
    copies[0].write_bytes(b"unsaved\n")
    win._open_text_entry(panel, entry)
    assert copies[0] != copies[1]
    assert copies[0].read_bytes() == b"unsaved\n"


def test_shutdown_releases_all_handles_even_if_one_provider_close_fails(win):
    closed = []

    def failed_close():
        closed.append("bad")
        raise OSError("failed close")

    win.panelL.provider = SimpleNamespace(close=failed_close)
    win.panelR.provider = SimpleNamespace(close=lambda: closed.append("good"))
    assert win.close()
    assert closed == ["bad", "good"]
    assert win.panelL.provider is None
    assert win.panelR.provider is None


def test_classic_hex_save_survives_refresh_and_repeated_save(
    app, monkeypatch, tmp_path
):
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: MB_Yes)
    window = mainwindow.MainWindow()
    try:
        window.open_image(build_disk(tmp_path))
        window.model.fetchMore()
        row = next(
            index
            for index, node in enumerate(window.model.root.children)
            if node.name == "HELLO.TXT"
        )
        window.tree.setCurrentIndex(window.model.index(row, 0))
        window.hex_view.insertBytes(b"first")
        assert window.act_save_hex.isEnabled()
        window.save_hex()
        assert not window.hex_view.isModified()
        assert window._current_node().path == "/HELLO.TXT"
        window.hex_view.insertBytes(b"second")
        expected = window.hex_view.data()
        window.save_hex()
        assert window.provider.read_file("/HELLO.TXT") == expected
        assert not window.hex_view.isModified()
    finally:
        window.close()


def test_classic_selection_and_shutdown_allow_cancelling_discard(
    app, monkeypatch, tmp_path
):
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: MB_No)
    window = mainwindow.MainWindow()
    try:
        window.open_image(build_disk(tmp_path))
        window.model.fetchMore()
        row = next(
            index
            for index, node in enumerate(window.model.root.children)
            if node.name == "HELLO.TXT"
        )
        original_index = window.model.index(row, 0)
        window.tree.setCurrentIndex(original_index)
        window.hex_view.insertBytes(b"dirty")
        expected = window.hex_view.data()
        window.tree.setCurrentIndex(window.model.index(0, 0))
        assert window.tree.currentIndex() == original_index
        assert window.hex_view.data() == expected
        window.cmb_parts.setCurrentIndex(0)
        assert window.cmb_parts.currentData() == 2048 * 512
        assert not window.close()
        assert window.provider is not None
    finally:
        monkeypatch.setattr(QMessageBox, "warning", lambda *_args: MB_Yes)
        window.close()


def test_f4_rejects_image_content_changed_while_dialog_open(win, monkeypatch, tmp_path):
    assert win.load_image(build_disk(tmp_path))
    panel = win.panelL
    panel.table.setCurrentCell(panel._row_of("HELLO.TXT"), 0)

    def edit(dialog):
        panel.writer.write_file("/", "HELLO.TXT", b"external")
        dialog.editor.setPlainText("stale contents")
        return tc_window.DialogAccepted

    monkeypatch.setattr(tc_window, "exec_dialog", edit)
    win.f4_edit()
    assert panel.provider.read_file("/HELLO.TXT") == b"external"
    assert not os.path.exists(win.image_path + ".bak")


def test_failed_local_folder_copy_keeps_old_destination_and_reports_failure(
    win, monkeypatch, tmp_path
):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "folder").mkdir()
    (source / "folder" / "new.bin").write_bytes(b"new")
    (destination / "folder").mkdir()
    (destination / "folder" / "old.bin").write_bytes(b"keep")
    panel = local_panel(win, source)
    other = local_panel(win, destination, "L")

    def failed_copy(*_args, **_kwargs):
        raise OSError("read failure")

    monkeypatch.setattr(tc_window.shutil, "copytree", failed_copy)
    win._copy_local_to_local(panel, other, [{"name": "folder", "is_dir": True}], True)
    assert (destination / "folder" / "old.bin").read_bytes() == b"keep"
    assert (source / "folder" / "new.bin").read_bytes() == b"new"
    assert "Перемещено: 0 объект(ов); не удалось: 1" in win.log.toPlainText()
    assert not list(destination.glob(".mountfile-copy-*"))


def test_backup_never_overwrites_dangling_symlink(win, tmp_path):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"evidence")
    victim = tmp_path / "not-created"
    backup = tmp_path / "evidence.img.bak"
    backup.symlink_to(victim)
    win.image_path = str(image)
    assert win._backup_before_write(SimpleNamespace(mode="img"))
    assert backup.is_symlink()
    assert not victim.exists()
    assert (tmp_path / "evidence.img.bak.1").read_bytes() == b"evidence"


def test_importing_image_into_itself_is_rejected_before_backup(win, tmp_path):
    image = build_disk(tmp_path)
    assert win.load_image(image)
    panel = local_panel(win, tmp_path)
    win._copy_local_to_img(
        panel, win.panelL, [{"name": os.path.basename(image), "is_dir": False}], False
    )
    assert not os.path.exists(image + ".bak")
    assert "disk.img" not in {entry["name"] for entry in win.panelL.entries}


def test_hex_page_shortcuts_do_not_conflict_with_hidden_panel_actions(win, tmp_path):
    (tmp_path / "sample.bin").write_bytes(b"sample")
    panel = local_panel(win, tmp_path)
    panel.table.setCurrentCell(panel._row_of("sample.bin"), 0)
    win.f3_hex_view()
    page = win.workspace_stack.currentWidget()
    assert not win.act_filter.isEnabled()
    assert all(not action.isEnabled() for action in win.facts.values())
    assert not win.cmdline.isEnabled()
    page.request_close()
    assert win.workspace_stack.currentWidget() is win.splitter
    assert win.act_filter.isEnabled()
    assert win.facts["F3"].isEnabled()
    assert win.cmdline.isEnabled()


def test_swap_preserves_complete_image_and_local_sources(win, tmp_path):
    assert win.load_image(build_disk(tmp_path))
    left, right = win.panelL, win.panelR
    provider, writer, offset = left.provider, left.writer, left.volume_offset
    local_panel(win, tmp_path)
    win.swap_panels()
    assert left.mode == "local" and left.path == str(tmp_path)
    assert right.mode == "img" and right.path == "/"
    assert right.provider is provider
    assert right.writer is writer
    assert right.volume_offset == offset
    assert right.provider.read_file("/HELLO.TXT") == b"hello world"
