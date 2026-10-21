# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Тонкий шим совместимости: реализация переехала в :mod:`mountfile.rw.universal`.

Имя файла сохранено, потому что его импортируют внешние скрипты, тесты и
консольные энтрипойнты из ``[project.scripts]``. Новый код должен
импортировать ``mountfile.rw.universal`` напрямую.
"""

from mountfile.rw.universal import *  # noqa: F401,F403
