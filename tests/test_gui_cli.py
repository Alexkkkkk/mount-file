"""The GUI command center is the discoverable front end for package CLI."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# The portable test matrix deliberately runs without Qt. Importing the
# compatibility layer can fail with ImportError either because a binding is
# absent or because its system libraries (for example libEGL) are absent.
try:
    from mountfile.qt_compat import QApplication, QMessageBox
except ImportError as exc:
    pytest.skip("Qt bindings are not usable: %s" % exc, allow_module_level=True)

from mountfile import cli
from mountfile.gui_cli import (
    CLI_COMMANDS,
    CLI_HELP,
    IMAGE_WRITE_COMMANDS,
    CLIWorker,
    CommandCenterDialog,
    _BoundedOutput,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_ui_command_inventory_matches_every_package_cli_command():
    assert CLI_COMMANDS == tuple(cli.COMMANDS)
    assert set(CLI_HELP) == set(cli.COMMANDS)
    assert all(CLI_HELP[command] for command in CLI_COMMANDS)
    assert set(IMAGE_WRITE_COMMANDS) == {"put", "mkdir", "mv", "rm"}


def test_command_center_exposes_help_for_every_registered_command(app):
    dialog = CommandCenterDialog()
    assert [dialog.command.itemText(i) for i in range(dialog.command.count())] == list(
        cli.COMMANDS
    )
    for command in CLI_COMMANDS:
        dialog.command.setCurrentText(command)
        assert dialog.help.toPlainText() == CLI_HELP[command]
        assert dialog.help.toPlainText().startswith("usage:")
    dialog.close()


def test_arguments_are_individual_argv_tokens_not_shell_syntax(app):
    dialog = CommandCenterDialog()
    dialog.command.setCurrentText("info")
    dialog.arguments.setPlainText(
        "evidence image.img\n--offset\n0x1000\n$(touch /tmp/should-never-exist)\n"
    )
    assert dialog.parsed_arguments() == [
        "evidence image.img",
        "--offset",
        "0x1000",
        "$(touch /tmp/should-never-exist)",
    ]
    dialog.close()


def test_image_write_requires_yes_confirmation_and_appends_cli_guard(
    app, monkeypatch, tmp_path
):
    invoked = []
    monkeypatch.setattr(CLIWorker, "start", lambda self: invoked.append(self.arguments))
    monkeypatch.setattr(
        "mountfile.gui_cli.QMessageBox.warning",
        lambda *_args: (
            QMessageBox.StandardButton.Yes
            if hasattr(QMessageBox, "StandardButton")
            else QMessageBox.Yes
        ),
    )
    dialog = CommandCenterDialog()
    dialog.command.setCurrentText("rm")
    dialog.arguments.setPlainText(str(tmp_path / "evidence.img") + "\n/secret.txt")
    dialog.run_command()
    assert invoked == [["rm", str(tmp_path / "evidence.img"), "/secret.txt", "--yes"]]
    assert not dialog.run_button.isEnabled()
    dialog.close()


def test_image_write_cancel_never_starts_the_backend(app, monkeypatch, tmp_path):
    invoked = []
    monkeypatch.setattr(CLIWorker, "start", lambda self: invoked.append(self.arguments))
    monkeypatch.setattr(
        "mountfile.gui_cli.QMessageBox.warning",
        lambda *_args: (
            QMessageBox.StandardButton.No
            if hasattr(QMessageBox, "StandardButton")
            else QMessageBox.No
        ),
    )
    dialog = CommandCenterDialog()
    dialog.command.setCurrentText("mkdir")
    dialog.arguments.setPlainText(str(tmp_path / "evidence.img") + "\n/recovery")
    dialog.run_command()
    assert invoked == []
    assert dialog.run_button.isEnabled()
    dialog.close()


def test_worker_runs_cli_without_shell_and_returns_captured_help(app):
    worker = CLIWorker(["info", "--help"])
    results = []
    worker.completed.connect(lambda code, text: results.append((code, text)))
    worker.start()
    assert worker.wait(5000)
    app.processEvents()
    assert results
    code, text = results[0]
    assert code == 0
    assert "usage:" in text.lower()
    assert "image" in text.lower()


def test_output_capture_has_a_hard_memory_bound():
    output = _BoundedOutput(limit=5)
    output.write("hello world")
    assert output.getvalue().startswith("hello")
    assert "ограничен 2 MiB" in output.getvalue()


def test_command_center_can_be_reopened_after_a_completed_command(app):
    dialog = CommandCenterDialog()
    dialog.command.setCurrentText("hash")
    assert "image" in dialog.help.toPlainText().lower()
    assert dialog.run_button.isEnabled()
    dialog.close()
