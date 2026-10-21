# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Универсальный слой чтения/записи образов (RW).

Модули:

- :mod:`mountfile.rw.universal` — базовый слой: определение ФС, стратегии,
  журнал, ``detect_fs`` / ``create_rw``;
- :mod:`mountfile.rw.complete` — нативные адаптеры, движок верификации,
  crash-recovery, CLI (``mountfile-rw``);
- :mod:`mountfile.rw.v2` — Journal v2 с побайтовым rollback (``mountfile-rw-v2``).

Пакет намеренно не импортирует подмодули в ``__init__``: ``v2`` тянет
``sqlite3`` и файловую систему, а базовый слой нужен GUI и тестам без этого.

Исторические точки входа ``mountfile_universal_rw`` / ``mountfile_rw_complete``
/ ``mountfile_rw_v2`` в корне репозитория остались тонкими шимами и
реэкспортируют эти модули.
"""
