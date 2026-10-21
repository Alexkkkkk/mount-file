"""Regression checks for the actual dual-pane desktop interface."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("mountfile.qt_compat", exc_type=ImportError)

from mountfile import tc_style
from mountfile.qt_compat import MB_No, QAction, QApplication, QEvent, QtCore, QToolBar
from mountfile.tc_window import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    monkeypatch.setenv("MOUNTFILE_NO_UPDATE", "1")
    monkeypatch.setattr(tc_style, "SETTINGS_PATH", str(tmp_path / "ui.json"))
    old = dict(tc_style.SETTINGS)
    window = MainWindow()
    yield window
    window.close()
    tc_style.SETTINGS.clear()
    tc_style.SETTINGS.update(old)


def local_panel(win, path, side="R"):
    panel = win.panels[side]
    win.release_volume(panel)
    panel.mode = "local"
    panel.cd(str(path))
    win.set_active(side)
    return panel


def names(panel):
    return [entry["name"] for entry in panel.visible_entries()]


def test_toolbar_has_no_blank_or_unsupported_actions(win):
    toolbar = win.findChild(QToolBar)
    actions = [action for action in toolbar.actions() if not action.isSeparator()]
    assert all(action.text() for action in actions)
    assert not {"Упаковать", "Распаковать", "FTP"}.intersection(
        a.text() for a in actions
    )
    assert actions[-1] is win.act_ai
    assert actions[-2].text() == "Справка"


def test_unopened_image_has_actionable_empty_state(win):
    panel = win.panelL
    assert panel.empty_title.text() == "Откройте образ диска"
    assert not panel.empty_open.isHidden()
    assert not panel.path_edit.isEnabled()
    assert all(not win.facts[key].isEnabled() for key in win.facts)


def test_empty_open_button_is_wired(win, monkeypatch):
    calls = []
    monkeypatch.setattr(win, "open_image", lambda: calls.append(True))
    win.panelL.empty_open.click()
    assert calls == [True]


def test_marks_do_not_follow_same_name_into_another_directory(win, tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / "same.bin").write_bytes(b"a")
    (b / "same.bin").write_bytes(b"b")
    (b / "other.bin").write_bytes(b"other")
    panel = local_panel(win, a)
    panel.toggle_mark(panel._row_of("same.bin"))
    assert panel.marked == {"same.bin"}
    panel.cd(str(b))
    panel.table.setCurrentCell(panel._row_of("other.bin"), 0)
    assert panel.marked == set()
    assert [entry["name"] for entry in panel.marked_entries()] == ["other.bin"]


def test_removed_files_are_removed_from_marks_on_refresh(win, tmp_path):
    target = tmp_path / "gone.txt"
    target.write_text("hello")
    panel = local_panel(win, tmp_path)
    panel.toggle_mark(panel._row_of(target.name))
    target.unlink()
    panel.refresh()
    assert target.name not in panel.marked


def test_filter_is_case_insensitive_and_clears_hidden_marks(win, tmp_path):
    (tmp_path / "Report.TXT").write_text("report")
    (tmp_path / "disk.bin").write_bytes(b"disk")
    panel = local_panel(win, tmp_path)
    panel.mark_all()
    panel.search_edit.setText("REPORT")
    assert names(panel) == ["Report.TXT"]
    assert panel.marked == {"Report.TXT"}
    assert panel._row_of("disk.bin") == -1
    panel.search_edit.clear()
    assert "disk.bin" in names(panel)


def test_filter_select_all_only_targets_visible_files(win, tmp_path):
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "hidden.bin").write_bytes(b"hidden")
    panel = local_panel(win, tmp_path)
    panel.search_edit.setText(".txt")
    panel.mark_all()
    assert panel.marked == {"keep.txt"}
    assert [entry["name"] for entry in panel.marked_entries()] == ["keep.txt"]


def test_filter_without_results_has_safe_empty_state(win, tmp_path):
    (tmp_path / "present.txt").write_text("present")
    panel = local_panel(win, tmp_path)
    panel.search_edit.setText("does-not-exist")
    assert panel.empty_title.text() == "Совпадений нет"
    assert panel.table.isHidden()
    assert panel.marked_entries() == []
    assert not win.facts["F8"].isEnabled()


def test_filter_and_marks_reset_on_navigation_and_tabs(win, tmp_path):
    (tmp_path / "child").mkdir()
    (tmp_path / "keep.txt").write_text("keep")
    panel = local_panel(win, tmp_path)
    panel.search_edit.setText("keep")
    panel.mark_all()
    panel.new_tab()
    panel.tab_changed(0)
    assert panel.search_edit.text() == ""
    assert panel.marked == set()
    panel.search_edit.setText("child")
    panel.cd(str(tmp_path / "child"))
    assert panel.search_edit.text() == ""


def test_mouse_multiselection_is_used_for_batch_actions(win, tmp_path):
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_text(name)
    panel = local_panel(win, tmp_path)
    panel.table.clearSelection()
    selection = QtCore.QItemSelectionModel
    flags = getattr(selection, "SelectionFlag", selection)
    for name in ("one.txt", "two.txt"):
        index = panel.table.model().index(panel._row_of(name), 0)
        panel.table.selectionModel().select(index, flags.Select | flags.Rows)
    assert {entry["name"] for entry in panel.marked_entries()} == {"one.txt", "two.txt"}


def test_edit_and_copy_actions_follow_capabilities(win, tmp_path):
    (tmp_path / "readme.txt").write_text("hello")
    panel = local_panel(win, tmp_path)
    panel.table.setCurrentCell(panel._row_of("readme.txt"), 0)
    assert win.facts["F3"].isEnabled()
    assert win.facts["F4"].isEnabled()
    assert win.facts["F7"].isEnabled()
    assert not win.facts["F5"].isEnabled()  # Other panel has no mounted image.
    for i, key in enumerate(win.facts):
        assert win.fbtns[i].isEnabled() == win.facts[key].isEnabled()
    panel.mode = "img"
    panel.provider = object()
    panel.writer = None
    panel.write_reason = "Нет writer"
    panel.update_header()
    assert panel.lbl_mode.text() == "Только чтение"
    assert not win.facts["F4"].isEnabled()
    assert not win.facts["F7"].isEnabled()
    assert not win.facts["F8"].isEnabled()
    panel.provider = None


def test_f9_action_toggles_menu_in_both_directions(win, app):
    win.show()
    app.processEvents()
    toggle = next(
        a for a in win.findChildren(QAction) if a.shortcut().toString() == "F9"
    )
    assert win.menuBar().isVisible()
    toggle.trigger()
    assert not win.menuBar().isVisible()
    toggle.trigger()
    assert win.menuBar().isVisible()


def test_fkeys_are_single_shared_shortcuts(win):
    for key in win.facts:
        actions = [
            a for a in win.findChildren(QAction) if a.shortcut().toString() == key
        ]
        assert actions == [win.facts[key]]


def test_panel_input_focus_sets_active_context(win, app):
    win.set_active("L")
    kind = getattr(QEvent, "Type", QEvent)
    QApplication.sendEvent(win.panelR.path_edit, QEvent(kind.FocusIn))
    assert win.active_side == "R"
    assert win.panelR.property("active") is True
    assert win.panelL.property("active") is False


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_theme_does_not_rescan_filesystem(win, monkeypatch, theme):
    def unexpected_refresh():
        raise AssertionError("Theme changes must not re-read a disk")

    monkeypatch.setattr(win.panelR, "refresh", unexpected_refresh)
    win._apply_theme(theme)
    assert tc_style.theme() == theme
    assert "QPushButton#dangerButton:disabled" in tc_style.stylesheet(theme)


def test_compact_layout_preserves_file_name_space(win, app, tmp_path):
    (tmp_path / "a-long-firmware-filename.bin").write_bytes(b"data")
    panel = local_panel(win, tmp_path)
    win.resize(800, 600)
    win.show()
    app.processEvents()
    panel.table._relayout_columns()
    assert panel.table.columnWidth(0) >= 150
    assert panel.table.isColumnHidden(3)
    assert panel.table.isColumnHidden(4)
    assert panel.empty_title.text() != "Совпадений нет"
    assert panel.table.viewport().height() >= 100
    assert not panel.btn_up.icon().isNull()
    assert panel.btn_up.accessibleName()


def test_conflict_cancel_leaves_source_and_destination_unchanged(
    win, tmp_path, monkeypatch
):
    a, b = tmp_path / "source", tmp_path / "destination"
    a.mkdir()
    b.mkdir()
    (a / "same.txt").write_text("new")
    (b / "same.txt").write_text("old")
    src = local_panel(win, a)
    dst = local_panel(win, b, "L")
    calls = []

    def cancel(*args):
        calls.append(args)
        return MB_No

    monkeypatch.setattr("mountfile.tc_window.QMessageBox.warning", cancel)
    win._copy_local_to_local(src, dst, src.entries, delete_src=True)
    assert (a / "same.txt").read_text() == "new"
    assert (b / "same.txt").read_text() == "old"
    assert calls[0][-1] == MB_No


def test_same_directory_copy_cannot_delete_source(win, tmp_path, monkeypatch):
    folder = tmp_path / "evidence"
    folder.mkdir()
    (folder / "important.txt").write_text("keep")
    src = local_panel(win, tmp_path)
    dst = local_panel(win, tmp_path, "L")
    monkeypatch.setattr("mountfile.tc_window.QMessageBox.warning", lambda *args: MB_No)
    entries = [e for e in src.entries if e["name"] == "evidence"]
    win._copy_local_to_local(src, dst, entries, delete_src=False)
    assert (folder / "important.txt").read_text() == "keep"


def test_nested_directory_copy_is_rejected_before_changes(win, tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    child = root / "nested"
    child.mkdir(parents=True)
    (root / "keep.txt").write_text("keep")
    src = local_panel(win, tmp_path)
    dst = local_panel(win, child, "L")
    monkeypatch.setattr("mountfile.tc_window.QMessageBox.warning", lambda *args: MB_No)
    win._copy_local_to_local(src, dst, src.entries, delete_src=False)
    assert (root / "keep.txt").read_text() == "keep"
    assert not (child / "evidence").exists()


def test_sort_header_switches_active_panel_before_operations(win, tmp_path):
    (tmp_path / "right.txt").write_text("right")
    panel = local_panel(win, tmp_path)
    win.set_active("L")
    assert win.active_side == "L"
    panel.on_header_clicked(2)
    assert win.active_side == "R"
    assert win.facts["F7"].isEnabled()
