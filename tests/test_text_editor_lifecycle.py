"""Process tracking protects extracted editor copies until the editor exits."""

from __future__ import annotations

from mountfile import text_editor


def test_active_editor_is_tracked_under_its_extraction_directory(monkeypatch, tmp_path):
    target = tmp_path / "copy.txt"
    running = {"value": True}

    class Process:
        def poll(self):
            return None if running["value"] else 0

    monkeypatch.setattr(text_editor, "_EDITOR_PROCESSES", [])
    monkeypatch.setattr(
        text_editor, "editor_command", lambda path: ["fake-editor", str(path)]
    )
    monkeypatch.setattr(
        text_editor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )

    assert text_editor.open_in_editor(target) == ["fake-editor", str(target)]
    assert text_editor.has_active_editors(tmp_path)

    running["value"] = False
    assert not text_editor.has_active_editors(tmp_path)
    assert text_editor._EDITOR_PROCESSES == []
