# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Авто-подбор рабочего тома (offset + файловая система) в raw-образе.

Зачем модуль нужен
------------------
В дампах NAND/eMMC таблица разделов часто отсутствует или врёт, а смещения из
файла ``udev`` указывают на контейнер, а не на файловую систему. Попытка
смонтировать такое смещение «как есть» даёт ошибки вида::

    The Sleuth Kit: FS_Info_Con: (tsk3.cpp:328) Unable to open the image as a
    filesystem at offset: 0x00a5a000 with error: Cannot determine file system
    type FAT32: invalid bytes-per-sector (not a FAT BPB?)

    FAT32: image too small

Модуль решает это автоматически:

1. собирает кандидатов из таблицы разделов (MBR/GPT), из файла ``udev`` и из
   сигнатур файловых систем;
2. строго валидирует каждого кандидата (BPB FAT, суперблок ext, NTFS, exFAT,
   ISO9660, HFS+, squashfs, UBIFS, JFFS2) — «случайный» байт 0x55AA больше не
   считается томом;
3. если смещение раздела не содержит ФС, ищет ФС *внутри* раздела (окно
   ``inner_window``) — типичный случай, когда udev указывает на контейнер;
4. подтверждает лучшего кандидата реальным монтированием (pytsk3 или встроенный
   FAT32) и возвращает готовое смещение для :mod:`mountfile.vfs` /
   :mod:`mountfile.fat32`.

Модуль только читает образ, ничего в него не пишет.
"""

from __future__ import annotations

import os
import struct

from mountfile.ntfs_probe import parse_ntfs_boot_sector

__all__ = [
    "Candidate",
    "auto_select",
    "find_udev_files",
    "format_candidates",
    "scan_candidates",
    "verify_mount",
]

SECTOR_SIZE = 512

#: По умолчанию сигнатуры ищутся в первых 512 МБ образа.
DEFAULT_SCAN_LIMIT = 0

#: Окно поиска ФС внутри раздела, если по его началу ФС не найдена.
DEFAULT_INNER_WINDOW = 64 * 1024 * 1024

#: Предохранители, чтобы «мусорный» образ не превратился в бесконечный скан.
MAX_VALIDATE = 20000
MAX_CANDIDATES = 512

_CHUNK = 1 << 20
#: Самый большой look-behind (ISO9660: "CD001" на 0x8001) плюс запас.
_CARRY = 0x8006

#: База: magic, сколько байт до начала ФС, выравнивание.
_MAGICS = (
    (b"\x55\xaa", 510, 512),  # FAT / NTFS / exFAT / HFS+ boot sector
    (b"NTFS    ", 3, 512),
    (b"EXFAT   ", 3, 512),
    (b"FAT32   ", 82, 512),  # метка тома FAT32 в расширенном BPB
    (b"\x53\xef", 0x438, 512),  # ext2/3/4, магия 0xEF53
    (b"CD001", 0x8001, 2048),  # ISO9660, primary volume descriptor
    (b"H+", 1024, 512),  # HFS+ volume header
    (b"hsqs", 0, 512),  # squashfs
    (b"\x31\x18\x10\x06", 0, 512),  # UBIFS
    (b"\x19\x85", 0, 512),  # JFFS2 (big endian)
    (b"\x85\x19", 0, 512),  # JFFS2 (little endian)
)

#: Базовый вес файловой системы: что вероятнее является рабочим томом.
FS_WEIGHT = {
    "FAT32": 60,
    "exFAT": 60,
    "NTFS": 60,
    "ext2/3/4": 58,
    "FAT16": 55,
    "FAT12": 50,
    "ISO9660": 54,
    "HFS+": 52,
    "squashfs": 40,
    "UBIFS": 34,
    "JFFS2": 34,
}


class Candidate:
    """Один возможный том: смещение, тип ФС, вес и объяснение веса."""

    __slots__ = (
        "backend",
        "error",
        "fstype",
        "label",
        "name",
        "offset",
        "reasons",
        "score",
        "size",
        "source",
    )

    def __init__(self, offset, fstype, source="scan", size=None, label="", name=""):
        self.offset = int(offset)
        self.fstype = fstype
        self.source = source
        self.size = size
        self.label = label or ""
        self.name = name or ""
        self.score = FS_WEIGHT.get(fstype, 20)
        self.reasons = ["%s (%s): %d" % (fstype, source, self.score)]
        self.backend = None
        self.error = None

    def add(self, points, why):
        self.score += int(points)
        self.reasons.append("%+d %s" % (points, why))

    @property
    def desc(self):
        text = "%s @ 0x%X [%s]" % (self.fstype, self.offset, self.source)
        if self.label:
            text += " метка=%r" % self.label
        if self.size:
            text += "  %.1f MB" % (self.size / 1048576.0)
        return text

    def __repr__(self):
        return "<Candidate %s score=%d>" % (self.desc, self.score)


# --------------------------------------------------------------- валидация
def _fat_geometry(buf):
    """Разобрать BPB FAT. Возвращает ``(kind, label, size_bytes)`` или None.

    Проверяются все поля, из-за которых The Sleuth Kit и встроенный FAT32
    отказываются открывать том: bytes-per-sector, sectors-per-cluster,
    reserved, число FAT, размер FAT и общее число секторов. Именно эти
    проверки превращают ошибку ``invalid bytes-per-sector (not a FAT BPB?)``
    в «этот offset не является началом файловой системы».
    """
    if len(buf) < 512 or buf[510:512] != b"\x55\xaa":
        return None
    if buf[0] not in (0xE9, 0xEB):
        return None
    bps = struct.unpack_from("<H", buf, 11)[0]
    if bps not in (512, 1024, 2048, 4096):
        return None
    spc = buf[13]
    if not spc or spc & (spc - 1) or spc > 128:
        return None
    reserved = struct.unpack_from("<H", buf, 14)[0]
    if not 1 <= reserved <= 64:
        return None
    nfats = buf[16]
    if not 1 <= nfats <= 2:
        return None
    root_entries = struct.unpack_from("<H", buf, 17)[0]
    total16 = struct.unpack_from("<H", buf, 19)[0]
    fatsz16 = struct.unpack_from("<H", buf, 22)[0]
    total32 = struct.unpack_from("<I", buf, 32)[0]
    fatsz32 = struct.unpack_from("<I", buf, 36)[0]
    total = total32 or total16
    if not total:
        return None
    if fatsz32 and not fatsz16:
        if struct.unpack_from("<I", buf, 44)[0] < 2:  # root cluster
            return None
        kind, label_at = "FAT32", 71
    elif fatsz16 and not fatsz32:
        if root_entries == 0 or root_entries % 16:
            return None
        kind, label_at = ("FAT16" if root_entries > 512 else "FAT12"), 43
    else:
        return None
    label = buf[label_at : label_at + 11].decode("latin-1").strip()
    return kind, label, total * bps


def _validate_at(f, offset):
    """Проверить смещение. Возвращает ``(fstype, label, size_bytes)`` или None."""
    if offset < 0:
        return None
    try:
        f.seek(offset)
        head = f.read(0x8006)
    except OSError:
        return None
    if len(head) < 512:
        return None

    # --- FAT / NTFS / exFAT: загрузочный сектор -------------------------
    if head[510:512] == b"\x55\xaa" and head[0] in (0xE9, 0xEB):
        oem = head[3:11]
        if oem == b"NTFS    ":
            try:
                f.seek(0, os.SEEK_END)
                available_size = f.tell() - offset
                profile = parse_ntfs_boot_sector(
                    head[:SECTOR_SIZE], volume_size=available_size
                )
            except (OSError, ValueError):
                return None
            return "NTFS", "", profile.filesystem_size
        elif oem == b"EXFAT   ":
            shift, spc_shift = head[108], head[109]
            vol_len = struct.unpack_from("<Q", head, 72)[0]
            if 9 <= shift <= 12 and spc_shift <= 25 and vol_len:
                return "exFAT", "", vol_len << shift
        else:
            fat = _fat_geometry(head)
            if fat:
                kind, label, size = fat
                # Первый байт FAT — дескриптор среды 0xF0..0xFF. Случайные
                # данные его почти никогда не дают, а битый BPB — не даёт.
                bps = struct.unpack_from("<H", head, 11)[0]
                reserved = struct.unpack_from("<H", head, 14)[0]
                try:
                    f.seek(offset + reserved * bps)
                    media = f.read(1)
                except OSError:
                    media = b""
                if media and 0xF0 <= media[0] <= 0xFF:
                    return kind, label, size

    # --- ext2/3/4: суперблок на 1024, магия 0xEF53 на +0x38 ------------
    if len(head) >= 0x440 and head[0x438:0x43A] == b"\x53\xef":
        log_bs = struct.unpack_from("<I", head, 0x418)[0]
        blocks = struct.unpack_from("<I", head, 0x404)[0]
        inodes = struct.unpack_from("<I", head, 0x400)[0]
        if log_bs <= 6 and blocks and inodes:
            return "ext2/3/4", "", blocks << (log_bs + 10)

    # --- HFS+ ----------------------------------------------------------
    if len(head) >= 1048 and head[1024:1026] == b"H+":
        block_size = struct.unpack_from("<I", head, 1040)[0]
        total_blocks = struct.unpack_from("<I", head, 1044)[0]
        if block_size and not block_size & (block_size - 1) and total_blocks:
            return "HFS+", "", block_size * total_blocks

    # --- ISO9660 -------------------------------------------------------
    if len(head) >= 0x8006 and head[0x8000] == 1 and head[0x8001:0x8006] == b"CD001":
        size = struct.unpack_from("<I", head, 0x8050)[0]
        return "ISO9660", "", size * 2048 if size else None

    # --- raw flash файловые системы ------------------------------------
    if head[0:4] == b"hsqs":
        block_size = struct.unpack_from("<I", head, 12)[0]
        used = struct.unpack_from("<Q", head, 40)[0]
        if block_size >= 4096 and not block_size & (block_size - 1) and used:
            return "squashfs", "", used
    if head[0:4] == b"\x31\x18\x10\x06":
        node_type = struct.unpack_from("<I", head, 20)[0]
        if 1 <= node_type <= 12:
            return "UBIFS", "", None
    if head[0:2] in (b"\x19\x85", b"\x85\x19"):
        node_type = struct.unpack_from(">H", head, 2)[0]
        if node_type in (0xE001, 0xE002, 0x2003, 0x2004, 0x2006):
            return "JFFS2", "", None

    return None


# ------------------------------------------------------------------ скан
def _scan_range(f, start, end):
    """Найти смещения-кандидаты в диапазоне ``[start, end)``."""
    try:
        f.seek(0, 2)
        total = f.tell()
    except OSError:
        return []
    start = max(0, int(start))
    # ``end`` == 0 (DEFAULT_SCAN_LIMIT) means "до конца файла": прежний
    # min(0, total) давал пустой диапазон и сканер не находил ни одной ФС.
    end = int(end or 0)
    end = min(end, total) if end > 0 else total
    if end <= start:
        return []

    offsets = []
    seen = set()
    base, carry = start, b""
    while base < end:
        try:
            f.seek(base)
            chunk = f.read(min(_CHUNK, end - base))
        except OSError:
            break
        if not chunk:
            break
        buf = carry + chunk
        buf_base = base - len(carry)
        for magic, back, align in _MAGICS:
            pos = buf.find(magic)
            while pos >= 0:
                cand = buf_base + pos - back
                if start <= cand < end and cand >= 0 and cand % align == 0:
                    if cand not in seen:
                        seen.add(cand)
                        offsets.append(cand)
                pos = buf.find(magic, pos + 1)
        base += len(chunk)
        carry = buf[-_CARRY:]

    found = []
    for offset in offsets[:MAX_VALIDATE]:
        info = _validate_at(f, offset)
        if info:
            found.append((offset, info[0], info[1], info[2]))
    return found


def _volume_candidates(path, udev=None, scan_limit=DEFAULT_SCAN_LIMIT):
    """Кандидаты из таблицы разделов и файла udev (даже без валидной ФС)."""
    out = []

    try:
        from .partition import list_partitions

        for p in list_partitions(path):
            out.append(Candidate(p.offset, "raw", source="partition", name=p.name))
    except Exception:
        pass

    if udev:
        try:
            from .rt809 import parse_udev

            for part in parse_udev(udev):
                out.append(Candidate(part.offset, "raw", source="udev", name=part.name))
        except Exception:
            pass
    return out


def scan_candidates(
    path,
    udev=None,
    scan_limit=DEFAULT_SCAN_LIMIT,
    inner_window=DEFAULT_INNER_WINDOW,
    deep=True,
):
    """Собрать, проверить и отсортировать всех кандидатов образа.

    ``deep=True`` дополнительно ищет файловую систему внутри каждого раздела
    (таблица разделов / udev), по началу которого валидной ФС нет.
    """
    candidates = []
    by_offset = {}

    def add(cand):
        old = by_offset.get(cand.offset)
        if old is None:
            by_offset[cand.offset] = cand
            candidates.append(cand)
        else:
            # Один и тот же offset из двух источников: объединяем вес и
            # переносим имя/метку/размер, чтобы подсказка (--hint) и вывод
            # работали, даже если первым кандидата нашёл сканер сигнатур.
            if cand.fstype != "raw" and old.fstype == "raw":
                old.fstype = cand.fstype
            if not old.name and cand.name:
                old.name = cand.name
            if not old.label and cand.label:
                old.label = cand.label
            if not old.size and cand.size:
                old.size = cand.size
            old.add(6, "подтверждён источником %s" % cand.source)
        return by_offset[cand.offset]

    with open(path, "rb") as f:
        # 1. сигнатуры в начале образа
        for offset, fstype, label, size in _scan_range(f, 0, scan_limit):
            add(Candidate(offset, fstype, source="signature", size=size, label=label))

        # 2. таблица разделов и udev
        for cand in _volume_candidates(path, udev=udev, scan_limit=scan_limit):
            info = _validate_at(f, cand.offset)
            if info:
                cand.fstype, cand.label, cand.size = info[0], info[1], info[2]
                cand.add(12, "ФС подтверждена на смещении раздела")
            else:
                cand.add(-6, "по началу раздела ФС не найдена")
            cand.add(
                20 if cand.source == "partition" else 16, "смещение из %s" % cand.source
            )
            add(cand)

        # 3. глубокий поиск внутри разделов без валидной ФС
        if deep and inner_window:
            starts = [
                (c.offset, c.name or c.source)
                for c in list(candidates)
                if c.fstype == "raw" and c.source in ("partition", "udev")
            ]
            for offset, name in starts:
                for hit, fstype, label, size in _scan_range(
                    f, offset + SECTOR_SIZE, offset + inner_window
                ):
                    inner = add(
                        Candidate(
                            hit,
                            fstype,
                            source="inner",
                            size=size,
                            label=label,
                            name=name,
                        )
                    )
                    inner.add(4, "ФС найдена внутри раздела %r" % name)

    for cand in candidates:
        _score_alignment(cand, path)

    candidates.sort(key=lambda c: (-c.score, c.offset))
    return candidates[:MAX_CANDIDATES]


def _score_alignment(cand, path):
    """Добавить вес за правдоподобность смещения и размера."""
    if cand.offset % 4096 == 0:
        cand.add(4, "выровнено на 4096")
    elif cand.offset % 1024 == 0:
        cand.add(2, "выровнено на 1024")
    try:
        total = os.path.getsize(path)
    except OSError:
        return
    if cand.size:
        if cand.offset + cand.size > total:
            cand.add(-20, "размер тома больше образа (BPB повреждён)")
        elif cand.size > 64 * 1024 * 1024:
            cand.add(3, "том крупнее 64 МБ")
    if cand.fstype != "raw" and cand.offset == 0:
        cand.add(3, "том начинается с нуля")


def verify_mount(path, offset, fstype=None):
    """Реально открыть том. Возвращает ``(backend, entries, error)``.

    ``backend`` — ``"tsk"``, ``"fat32"`` или None. Это единственная проверка,
    которая ловит ошибки ``FS_Info_Con`` до показа их пользователю.
    """
    errors = []
    try:
        from .vfs import TskImage

        img = TskImage(path, offset=offset)
        try:
            entries = len(img.list_dir("/"))
        finally:
            if hasattr(img, "close"):
                img.close()
        return "tsk", entries, None
    except Exception as e:  # TskError, отсутствие pytsk3 и т.п.
        errors.append("The Sleuth Kit: %s" % e)

    try:
        from .fat32 import Fat32Provider

        provider = Fat32Provider(path, offset=offset)
        try:
            entries = len(provider.list_dir("/"))
        finally:
            if hasattr(provider, "close"):
                provider.close()
        return "fat32", entries, None
    except Exception as e:
        errors.append("FAT32: %s" % e)

    return None, 0, " | ".join(errors)


def _match_hint(cand, hint):
    """Совпадение подсказки (имя раздела, метка) с кандидатом."""
    if not hint:
        return False
    h = str(hint).strip().lower()
    if not h:
        return False
    for value in (cand.label, cand.name):
        if value and (h in value.lower() or value.lower() in h):
            return True
    return False


def auto_select(
    path,
    udev=None,
    hint=None,
    scan_limit=DEFAULT_SCAN_LIMIT,
    inner_window=DEFAULT_INNER_WINDOW,
    deep=True,
    verify=True,
):
    """Выбрать рабочий том автоматически.

    Возвращает ``(best, candidates)``: лучший кандидат (или None) и полный
    список с весами. При ``verify=True`` каждый кандидат проверяется реальным
    монтированием, поэтому выбранный offset гарантированно открывается.
    """
    candidates = scan_candidates(
        path, udev=udev, scan_limit=scan_limit, inner_window=inner_window, deep=deep
    )

    if verify:
        for cand in candidates:
            backend, entries, error = verify_mount(path, cand.offset, cand.fstype)
            cand.backend = backend
            cand.error = error
            if backend:
                cand.add(30, "монтирование OK (%s, записей: %d)" % (backend, entries))
                if entries:
                    cand.add(5, "корень читается")
            else:
                cand.add(-25, "монтирование не удалось")

    if hint:
        for cand in candidates:
            if _match_hint(cand, hint):
                cand.add(35, "совпало с подсказкой %r" % hint)

    candidates.sort(key=lambda c: (-c.score, c.offset))
    best = None
    for cand in candidates:
        if cand.fstype == "raw":
            continue
        if verify and not cand.backend:
            continue
        best = cand
        break
    if best is None and candidates:
        best = candidates[0]
    return best, candidates


# --------------------------------------------------------------- вывод
def format_candidates(candidates, limit=20):
    """Таблица кандидатов для журнала GUI и вывода CLI."""
    if not candidates:
        return "Кандидаты не найдены: в образе нет известных сигнатур файловых систем."
    lines = ["  № Offset      Тип        Вес  Источник   Описание"]
    for i, c in enumerate(candidates[:limit], 1):
        lines.append(
            "%-3d 0x%08X  %-9s  %4d  %-9s  %s"
            % (i, c.offset, c.fstype, c.score, c.source, c.name or c.label or "")
        )
    return "\n".join(lines)


def format_choice(best):
    """Человекочитаемое объяснение выбора."""
    if best is None:
        return "Рабочий том не найден."
    lines = ["Выбран том: %s (вес %d)" % (best.desc, best.score)]
    lines.extend("  " + r for r in best.reasons)
    if best.backend:
        lines.append("  проверка монтирования: %s" % best.backend)
    return "\n".join(lines)


# ------------------------------------------------------- авто-подбор udev
def looks_like_udev(path, probe=1 << 20):
    """Похож ли файл на текстовый udev (секция ``[PARTITIONS]``).

    Ловит типичную ошибку выбора: вместо ``udev.txt`` выбран сам дамп или
    другой большой бинарный файл.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(probe)
    except OSError:
        return False
    if not raw:
        return False
    if b"\x00" in raw[:4096] and raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        return False  # бинарный файл, не текст
    text = _decode_udev_text(raw).upper()
    if "[PARTITIONS]" in text.replace(" ", ""):
        return True
    for line in text.splitlines():
        pieces = [p.strip() for p in line.replace(";", ",").split(",")]
        if len(pieces) >= 3 and _is_number(pieces[0]) and _is_number(pieces[1]):
            return True
    return False


def _decode_udev_text(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    return raw.decode("latin-1", "replace")


def _is_number(text):
    text = text.strip()
    if not text:
        return False
    try:
        int(text, 16) if text[:2].lower() == "0x" else int(text, 10)
    except ValueError:
        return False
    return True


def find_udev_files(dump_path, max_files=200):
    """Найти подходящие файлы udev рядом с дампом, лучшие — первыми.

    Сначала файлы с «udev» в имени, затем любые текстовые файлы с секцией
    ``[PARTITIONS]``. Именно этот шаг убирает ручной выбор файла.
    """
    folder = os.path.dirname(os.path.abspath(dump_path)) or "."
    stem = os.path.splitext(os.path.basename(dump_path))[0].lower()
    named, sniffed = [], []
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return []
    for name in entries[:max_files]:
        full = os.path.join(folder, name)
        if not os.path.isfile(full) or os.path.abspath(full) == os.path.abspath(
            dump_path
        ):
            continue
        low = name.lower()
        if "udev" in low:
            named.append(full)
            continue
        if low.startswith(stem) and not low.endswith((".bin", ".img", ".raw", ".dump")):
            sniffed.append(full)
            continue
        try:
            if os.path.getsize(full) <= 8 << 20 and looks_like_udev(full):
                sniffed.append(full)
        except OSError:
            continue
    return named + sniffed
