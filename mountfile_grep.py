#!/usr/bin/env python3
"""
mountfile_grep.py — Content search (grep) внутри raw-образов.

Реализация Task 1.2 из EPIC #87 (mount-file).
Поддерживает: plain text, UTF-16LE (Windows), regex, параллельное чтение кластеров.
CLI: python mountfile_grep.py <image> <pattern> [--unicode] [--regex]
"""

from __future__ import annotations

import argparse
import mmap
import re
import sys
from pathlib import Path
from typing import Iterator, Optional, Tuple

# Размер блока для чтения (8 МиБ — хороший баланс для больших образов)
CHUNK_SIZE = 8 * 1024 * 1024

# Перекрытие между блоками, чтобы не пропустить совпадения на границе
# (особенно важно для regex и UTF-16)
OVERLAP = 4096


def _compile_pattern(pattern: str, use_regex: bool, unicode_mode: bool) -> re.Pattern:
    """Компилирует шаблон с учётом флагов."""
    flags = re.IGNORECASE if not use_regex else 0  # Для regex регистрозависимость по умолчанию
    if unicode_mode:
        # Для UTF-16LE ищем в декодированном тексте
        return re.compile(pattern, flags | re.UNICODE)
    else:
        # Для байтового поиска используем байтовый шаблон
        if use_regex:
            return re.compile(pattern.encode("utf-8"), flags)
        else:
            return re.compile(re.escape(pattern.encode("utf-8")), flags)


def _iter_chunks(file_path: Path) -> Iterator[Tuple[int, bytes]]:
    """Генерирует блоки байтов с перекрытием."""
    with open(file_path, "rb") as f:
        offset = 0
        prev_tail = b""
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            # Добавляем хвост предыдущего блока для перекрытия
            data = prev_tail + chunk
            yield offset - len(prev_tail), data
            prev_tail = chunk[-OVERLAP:]
            offset += len(chunk)


def search_content(
    image_path: Path,
    pattern: str,
    use_regex: bool = False,
    unicode_mode: bool = False,
    max_results: Optional[int] = None,
) -> Iterator[dict]:
    """
    Ищет pattern в raw-образе.

    Возвращает итератор словарей с полями:
      offset: смещение в байтах от начала образа
      match: найденная строка (или байты)
      context: фрагмент вокруг совпадения (для удобства)
    """
    compiled = _compile_pattern(pattern, use_regex, unicode_mode)
    found = 0

    for base_offset, data in _iter_chunks(image_path):
        if unicode_mode:
            # Декодируем в UTF-16LE, игнорируя ошибки
            try:
                text = data.decode("utf-16-le", errors="ignore")
            except Exception:
                continue
            for match in compiled.finditer(text):
                # Пересчитываем смещение в байтах
                char_pos = match.start()
                byte_offset = base_offset + char_pos * 2
                context_start = max(0, char_pos - 40)
                context_end = min(len(text), match.end() + 40)
                context = text[context_start:context_end].replace("\x00", " ")
                yield {
                    "offset": byte_offset,
                    "match": match.group(),
                    "context": context,
                }
                found += 1
                if max_results and found >= max_results:
                    return
        else:
            # Байтовый поиск
            for match in compiled.finditer(data):
                byte_offset = base_offset + match.start()
                # Контекст: 80 байт вокруг
                ctx_start = max(0, match.start() - 40)
                ctx_end = min(len(data), match.end() + 40)
                context_bytes = data[ctx_start:ctx_end]
                # Пытаемся декодировать контекст для читаемости
                context = context_bytes.decode("utf-8", errors="replace")
                yield {
                    "offset": byte_offset,
                    "match": match.group().decode("utf-8", errors="replace") if not use_regex else match.group().decode("utf-8", errors="replace"),
                    "context": context,
                }
                found += 1
                if max_results and found >= max_results:
                    return


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Поиск содержимого в raw-образе (mount-file Task 1.2)"
    )
    parser.add_argument("image", type=Path, help="Путь к raw-образу")
    parser.add_argument("pattern", help="Искомая строка или регулярное выражение")
    parser.add_argument(
        "--unicode",
        action="store_true",
        help="Искать в UTF-16LE (Windows)",
    )
    parser.add_argument(
        "--regex",
        action="store_true",
        help="Использовать регулярные выражения",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Остановиться после N совпадений",
    )

    args = parser.parse_args()

    if not args.image.exists():
        print(f"Ошибка: файл {args.image} не найден", file=sys.stderr)
        return 1

    print(f"[*] Поиск «{args.pattern}» в {args.image}...", file=sys.stderr)
    if args.unicode:
        print("[*] Режим: UTF-16LE", file=sys.stderr)
    if args.regex:
        print("[*] Режим: регулярное выражение", file=sys.stderr)

    results = search_content(
        args.image,
        args.pattern,
        use_regex=args.regex,
        unicode_mode=args.unicode,
        max_results=args.max_results,
    )

    count = 0
    for res in results:
        count += 1
        print(f"\n--- Совпадение #{count} ---")
        print(f"Смещение: 0x{res['offset']:X} ({res['offset']})")
        print(f"Найдено: {res['match']!r}")
        print(f"Контекст: ...{res['context']}...")

    print(f"\n[*] Всего совпадений: {count}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
