"""GUI entry point: ``python -m mfim.gui.app [image.img]``."""

from __future__ import annotations

import sys


def main(argv: list | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    try:  # pragma: no cover
        from PyQt6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover
        try:
            from PySide6.QtWidgets import QApplication  # type: ignore
        except ImportError:
            print(
                "Для графического интерфейса установите PyQt6:\n"
                "    pip install PyQt6\n"
                "Без графики доступен CLI:\n"
                "    python -m mfim.cli --help",
                file=sys.stderr,
            )
            return 4

    from .win_main import MainWindow

    app = QApplication(argv)
    app.setApplicationName("Mount File")
    path = argv[1] if len(argv) > 1 else None
    window = MainWindow(path)
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
