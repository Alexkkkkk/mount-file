# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Qt command center that exposes the complete package CLI without a shell."""

from __future__ import annotations

import contextlib
import io
import threading
import traceback
from typing import Optional

from . import cli
from .qt_compat import (
    AlignRight,
    DialogAccepted,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QThread,
    QVBoxLayout,
    QWidget,
    exec_dialog,
    pyqtSignal,
)

# These four package CLI commands mutate an existing evidence image. Keep the
# set explicit: future write commands require an explicit safety review.
IMAGE_WRITE_COMMANDS = frozenset({"put", "mkdir", "mv", "rm"})
_IMAGE_WRITE_NOTICE = (
    "Команда изменяет исходный образ напрямую. Это нельзя отменить. Работайте "
    "с проверенной копией образа. При необходимости добавьте --backup: он "
    "создаёт или перезаписывает соседний файл IMAGE.bak. Ниже точно показаны "
    "передаваемые команде аргументы. Продолжить?"
)
_OUTPUT_COMMANDS = frozenset(
    {
        "decode",
        "split",
        "timeline-export",
        "timeline-build",
        "slack-extract",
        "live-diff",
    }
)
_CLI_LOCK = threading.Lock()
_MAX_OUTPUT_CHARS = 2 * 1024 * 1024


def _subparsers():
    parser = cli.build_parser()
    for action in parser._actions:
        if getattr(action, "choices", None) is not None and "volumes" in action.choices:
            return action.choices
    raise RuntimeError("Mount File CLI subcommand registry was not found")


CLI_COMMANDS = tuple(cli.COMMANDS)
CLI_SUBPARSERS = _subparsers()
CLI_HELP = {name: CLI_SUBPARSERS[name].format_help().strip() for name in CLI_COMMANDS}
CLI_DESCRIPTIONS = {
    name: CLI_SUBPARSERS[name].description
    or CLI_SUBPARSERS[name].format_help().splitlines()[0]
    for name in CLI_COMMANDS
}


class _BoundedOutput(io.TextIOBase):
    """Keep large scans from growing a GUI process's memory without a bound."""

    def __init__(self, limit: int = _MAX_OUTPUT_CHARS):
        self.limit = limit
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    @property
    def encoding(self):  # pragma: no cover - argparse may inspect it
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        text = str(text)
        remaining = self.limit - self.length
        if remaining > 0:
            piece = text[:remaining]
            self.parts.append(piece)
            self.length += len(piece)
        if len(text) > remaining:
            self.truncated = True
        return len(text)

    def getvalue(self):
        value = "".join(self.parts)
        if self.truncated:
            value += "\n\n[Вывод ограничен 2 MiB. Ограничьте число результатов через параметры CLI.]\n"
        return value


class CLIWorker(QThread):
    """Run one package CLI command away from the GUI event loop."""

    completed = pyqtSignal(int, str)

    def __init__(self, arguments, parent=None):
        super().__init__(parent)
        self.arguments = list(arguments)

    def run(self):
        output = _BoundedOutput()
        code = 1
        try:
            with (
                _CLI_LOCK,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                try:
                    code = int(cli.main(self.arguments))
                except SystemExit as exc:
                    code = (
                        int(exc.code)
                        if isinstance(exc.code, int)
                        else (0 if exc.code is None else 1)
                    )
                except Exception:
                    traceback.print_exc(file=output)
                    code = 1
        except Exception:
            traceback.print_exc(file=output)
            code = 1
        self.completed.emit(code, output.getvalue())


class CommandCenterDialog(QDialog):
    """Discover and run every ``mountfile.cli`` subcommand from the GUI.

    Arguments are one token per line. This makes paths with spaces unambiguous
    and all invocation remains an argv list (never a shell command).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Центр команд Mount File")
        self.resize(900, 700)
        self.worker: Optional[CLIWorker] = None
        root = QVBoxLayout(self)
        root.addWidget(
            QLabel("Все команды пакетного CLI — без запуска командной оболочки.")
        )

        self.command = QComboBox()
        self.command.setObjectName("cliCommand")
        self.command.addItems(CLI_COMMANDS)
        self.command.currentTextChanged.connect(self._show_help)
        root.addWidget(self.command)
        self.description = QLabel()
        self.description.setObjectName("panelSpace")
        self.description.setWordWrap(True)
        root.addWidget(self.description)
        self.help = QPlainTextEdit()
        self.help.setObjectName("cliHelp")
        self.help.setReadOnly(True)
        self.help.setMaximumHeight(180)
        root.addWidget(self.help)
        root.addWidget(
            QLabel("Аргументы — по одному на строку; пробелы в пути допустимы.")
        )
        self.arguments = QPlainTextEdit()
        self.arguments.setObjectName("cliArguments")
        self.arguments.setPlaceholderText(
            "Например, для info:\n/path/to/evidence.img\n\n"
            "Для опций: сначала positional-аргументы, затем флаги, каждый отдельной строкой."
        )
        root.addWidget(self.arguments, 1)

        pick_row = QHBoxLayout()
        self.pick_file = QPushButton("Вставить путь к файлу…")
        self.pick_file.clicked.connect(self._insert_file)
        pick_row.addWidget(self.pick_file)
        pick_row.addStretch(1)
        root.addLayout(pick_row)

        self.status = QLabel("Выберите команду. Аргументы сверяйте со справкой выше.")
        self.status.setObjectName("panelStatus")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(8)
        self.progress.hide()
        root.addWidget(self.progress)
        self.output = QPlainTextEdit()
        self.output.setObjectName("cliOutput")
        self.output.setReadOnly(True)
        root.addWidget(self.output, 2)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.run_button = QPushButton("Выполнить")
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self.run_command)
        controls.addWidget(self.run_button)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.close)
        controls.addWidget(self.close_button)
        root.addLayout(controls)
        self._show_help(self.command.currentText())

    def _show_help(self, name):
        self.help.setPlainText(CLI_HELP.get(name, ""))
        if name in IMAGE_WRITE_COMMANDS:
            self.description.setText(
                "ОСТОРОЖНО: эта команда изменяет файловую систему внутри образа. "
                "После запуска появится отдельный диалог точного подтверждения."
            )
        elif name in _OUTPUT_COMMANDS:
            self.description.setText(
                "Создаёт внешние файлы/отчёты по явно заданным путям. Исходный образ "
                "не меняется; проверьте, не существует ли выходной файл."
            )
        else:
            self.description.setText(
                "Read-only или исследовательская команда. Возможные optional "
                "зависимости, границы поиска и ограничения указаны в справке CLI."
            )

    def _insert_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл или образ", "")
        if not path:
            return
        cursor = self.arguments.textCursor()
        if cursor.position() > 0:
            before = self.arguments.toPlainText()[: cursor.position()]
            if not before.endswith("\n"):
                cursor.insertText("\n")
        cursor.insertText(path)
        cursor.insertText("\n")
        self.arguments.setTextCursor(cursor)
        self.arguments.setFocus()

    def parsed_arguments(self):
        """Treat each non-empty line as one exact argv token; no shell parsing."""
        return [
            line for line in self.arguments.toPlainText().splitlines() if line.strip()
        ]

    def run_command(self):
        if self.worker is not None and self.worker.isRunning():
            return
        command = self.command.currentText()
        arguments = self.parsed_arguments()
        if command in IMAGE_WRITE_COMMANDS:
            displayed = "mountfile %s\n%s" % (
                command,
                "\n".join(arguments) or "<аргументы не заданы>",
            )
            if "--yes" not in arguments:
                displayed += "\n--yes  (добавляется только после подтверждения)"
            answer = QMessageBox.warning(
                self,
                "Подтвердите запись в образ",
                "%s\n\n%s" % (_IMAGE_WRITE_NOTICE, displayed),
                (
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    if hasattr(QMessageBox, "StandardButton")
                    else QMessageBox.Yes | QMessageBox.No
                ),
                (
                    QMessageBox.StandardButton.No
                    if hasattr(QMessageBox, "StandardButton")
                    else QMessageBox.No
                ),
            )
            yes = (
                QMessageBox.StandardButton.Yes
                if hasattr(QMessageBox, "StandardButton")
                else QMessageBox.Yes
            )
            if answer != yes:
                return
            if "--yes" not in arguments:
                arguments.append("--yes")

        argv = [command, *arguments]
        self.output.clear()
        self.run_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.command.setEnabled(False)
        self.arguments.setEnabled(False)
        self.pick_file.setEnabled(False)
        self.progress.show()
        self.status.setText(
            "Выполняется… Дождитесь завершения команды; окно остаётся отзывчивым."
        )
        self.worker = CLIWorker(argv, self)
        self.worker.completed.connect(self._command_finished)
        self.worker.start()

    def _command_finished(self, code, output):
        self.output.setPlainText(output or "Команда завершена без текстового вывода.")
        self.status.setText(
            "Завершено, код %d%s"
            % (code, " (успех)" if code == 0 else " (ошибка или нет совпадений)")
        )
        self.run_button.setEnabled(True)
        self.close_button.setEnabled(True)
        self.command.setEnabled(True)
        self.arguments.setEnabled(True)
        self.pick_file.setEnabled(True)
        self.progress.hide()
        self.worker = None

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
            self.status.setText(
                "Команда выполняется; дождитесь результата перед закрытием."
            )
            return
        super().closeEvent(event)


def open_command_center(parent=None):
    return exec_dialog(CommandCenterDialog(parent)) == DialogAccepted


def cli_command_inventory():
    """Public, test-friendly inventory for UI/API parity checks."""
    return tuple(CLI_COMMANDS)
