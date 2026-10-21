# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Тонкий шим совместимости: реализация переехала в :mod:`mountfile.rw.complete`.

Имя файла сохранено, потому что его импортируют внешние скрипты, тесты и
консольные энтрипойнты из ``[project.scripts]``. Новый код должен
импортировать ``mountfile.rw.complete`` напрямую.
"""

from mountfile.rw.complete import *  # noqa: F401,F403

if __name__ == "__main__":
    import sys

    from mountfile.rw.complete import main

    sys.exit(main(sys.argv[1:]))
