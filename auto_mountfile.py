#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_mountfile.py — авто-определение типа дампа по имени файла и автонастройка Mount File.

Что делает:
  1. По имени файла (regex-правила) определяет тип дампа:
       - tv_firmware  : CtvUpgrade.bin, firmware.bin, upgrade.bin, flash.bin
       - mtk_dump     : *mtk*, *mediatek*, *MTK_*, *MTxxxx*
       - mst_dump     : *mst*, *mstar*, *MST_*
       - rt809h_dump  : *rt809*, *rt809h*, *eeprom*, *nand*
       - emmc_dump    : *emmc*, *.mmc*, *.img, *user*, *boot1*, *boot2*
       - nand_dump    : *nand*, *.nand, *spi*
       - generic      : всё остальное
  2. Сканирует образ: MBR/GPT + сигнатуры ФС (ext, FAT, NTFS, exFAT, HFS+, ISO)
  3. Подбирает параметры:
       - offset раздела для открытия в Mount File
       - нужен ли XOR-decode (RT809H MST/MTK)
       - нужен ли split по udev
       - разрешена ли запись (только FAT32)
  4. Запускает Mount File (GUI или CLI) с уже готовыми параметрами.

Запуск:
  python auto_mountfile.py CtvUpgrade.bin
  python auto_mountfile.py dump_MTK.bin --gui
  python auto_mountfile.py emmc_user.img --list
  python auto_mountfile.py rt809h_mst.bin --decode
  python auto_mountfile.py nand.bin --udev udev.txt --split
  python auto_mountfile.py CtvUpgrade.bin --auto        # всё сам: decode+split+GUI
  python auto_mountfile.py CtvUpgrade.bin --extract     # вытащить ключи/сертификаты
  python auto_mountfile.py CtvUpgrade.bin --json        # только анализ, в JSON
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ============================================================
# 1. ПРАВИЛА АВТО-ОПРЕДЕЛЕНИЯ ПО ИМЕНИ
# ============================================================
# Порядок важен: первое совпадение побеждает.
NAME_RULES: list[tuple[str, str]] = [
    # (regex по имени файла (lowercase), тип)
    (r"ctv.?upgrade", "tv_firmware"),
    (r"(firmware|upgrade|flash)", "tv_firmware"),
    (r"(mediatek|_mtk_|mt\d{4}|mtk[_\-\.])", "mtk_dump"),
    (r"(mstar|_mst_|mst[_\-\.])", "mst_dump"),
    (r"rt809", "rt809h_dump"),
    (r"(eeprom|24c\d{2}|25q\d{2}|spi[_\-]?flash)", "nand_dump"),
    (r"(boot1|boot2)", "emmc_dump"),
    (r"emmc", "emmc_dump"),
    (r"\.mmc$", "emmc_dump"),
    (r"nand", "nand_dump"),
    (r"\.img$", "emmc_dump"),
    (r"\.bin$", "generic"),
]

# Что делать с каждым типом
TYPE_PROFILES: dict[str, dict] = {
    "tv_firmware": {
        "description": "Прошивка/образ Android TV (CtvUpgrade и аналоги)",
        "prefer_ext": True,
        "allow_write": False,  # запись только если найден FAT32
        "need_decode": False,
        "need_split": False,
        "cli_after_open": ["volumes", "ls"],  # что показать автоматически
    },
    "mtk_dump": {
        "description": "Дамп MediaTek (eMMC/NAND)",
        "prefer_ext": True,
        "allow_write": False,
        "need_decode": True,  # XOR-таблицы MTK встроены в Mount File
        "need_split": True,  # часто нужен udev
        "cli_after_open": ["volumes"],
    },
    "mst_dump": {
        "description": "Дамп MStar (MST)",
        "prefer_ext": True,
        "allow_write": False,
        "need_decode": True,
        "need_split": True,
        "cli_after_open": ["volumes"],
    },
    "rt809h_dump": {
        "description": "Дамп с программатора RT809H",
        "prefer_ext": False,
        "allow_write": False,
        "need_decode": True,  # decode обязателен
        "need_split": True,
        "cli_after_open": [],
    },
    "emmc_dump": {
        "description": "Пользовательский образ eMMC (USER/BOOT1/BOOT2)",
        "prefer_ext": True,
        "allow_write": True,  # записи разрешены, если том FAT32
        "need_decode": False,
        "need_split": False,
        "cli_after_open": ["volumes", "ls"],
    },
    "nand_dump": {
        "description": "Дамп NAND/SPI (может содержать OOB)",
        "prefer_ext": False,
        "allow_write": False,
        "need_decode": False,
        "need_split": False,
        "cli_after_open": ["volumes"],
    },
    "generic": {
        "description": "Универсальный raw-образ",
        "prefer_ext": True,
        "allow_write": False,
        "need_decode": False,
        "need_split": False,
        "cli_after_open": ["volumes"],
    },
}


def detect_type_by_name(path: Path) -> tuple[str, str]:
    """Возвращает (тип, regex-совпадение)."""
    name = path.name.lower()
    for pattern, dtype in NAME_RULES:
        m = re.search(pattern, name)
        if m:
            return dtype, m.group(0)
    return "generic", ""


# ============================================================
# 2. АНАЛИЗ СОДЕРЖИМОГО ОБРАЗА
# ============================================================
FS_SIGNATURES = [
    (
        "ext2/3/4",
        lambda b: len(b) > 0x43A and struct.unpack("<H", b[0x438:0x43A])[0] == 0xEF53,
    ),
    (
        "FAT12/16/32",
        lambda b: len(b) > 0x5A
        and (
            b[0:3] in (b"\xEB\x3C\x90", b"\xEB\x3E\x90", b"\xE9\x00\x00")
            or b[0x36:0x3B] in (b"FAT12", b"FAT16")
            or b[0x52:0x57] == b"FAT32"
        ),
    ),
    ("NTFS", lambda b: len(b) > 0x0B and b[0x003:0x00B] == b"NTFS    "),
    ("exFAT", lambda b: len(b) > 0x0B and b[0x003:0x00B] == b"EXFAT   "),
    ("HFS+", lambda b: len(b) > 0x402 and b[0x400:0x402] in (b"H+", b"HX")),
    ("ISO9660", lambda b: len(b) > 0x8006 and b[0x8001:0x8006] == b"CD001"),
    ("GPT", lambda b: len(b) > 0x208 and b[0x200:0x208] == b"EFI PART"),
]

MBR_TYPES = {
    0x01: "FAT12",
    0x04: "FAT16",
    0x05: "Extended",
    0x06: "FAT16",
    0x07: "NTFS/exFAT",
    0x0B: "FAT32",
    0x0C: "FAT32 LBA",
    0x0E: "FAT16 LBA",
    0x0F: "Extended LBA",
    0x82: "Linux swap",
    0x83: "Linux (ext)",
    0x8E: "Linux LVM",
    0xA5: "FreeBSD",
    0xEE: "GPT protective",
}


@dataclass
class Volume:
    offset: int
    fs: str
    size: int = 0
    source: str = "scan"  # "MBR" / "GPT" / "scan"


@dataclass
class Analysis:
    path: str
    size: int
    dtype: str
    matched: str
    profile: dict
    volumes: list[Volume] = field(default_factory=list)
    has_gpt: bool = False
    has_mbr: bool = False
    recommended_offset: int = 0
    recommended_fs: str = ""
    write_allowed: bool = False
    notes: list[str] = field(default_factory=list)


def hexoff(x: int) -> str:
    return f"0x{x:08X}"


def read_mbr(f) -> list[Volume]:
    f.seek(0)
    mbr = f.read(512)
    out: list[Volume] = []
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xAA":
        return out
    for i in range(4):
        e = mbr[446 + i * 16 : 446 + (i + 1) * 16]
        ptype = e[4]
        lba = struct.unpack("<I", e[8:12])[0]
        sec = struct.unpack("<I", e[12:16])[0]
        if ptype == 0 or sec == 0:
            continue
        out.append(
            Volume(
                offset=lba * 512,
                fs=MBR_TYPES.get(ptype, f"0x{ptype:02X}"),
                size=sec * 512,
                source="MBR",
            )
        )
    return out


def read_gpt(f) -> list[Volume]:
    f.seek(512)
    hdr = f.read(92)
    if len(hdr) < 92 or hdr[0:8] != b"EFI PART":
        return []
    try:
        part_lba = struct.unpack("<Q", hdr[72:80])[0]
        num = struct.unpack("<I", hdr[80:84])[0]
        esize = struct.unpack("<I", hdr[84:88])[0]
    except Exception:
        return []
    out: list[Volume] = []
    f.seek(part_lba * 512)
    for _ in range(num):
        raw = f.read(esize)
        if len(raw) < 48:
            break
        if raw[0:16] == b"\x00" * 16:
            continue
        first = struct.unpack("<Q", raw[32:40])[0]
        last = struct.unpack("<Q", raw[40:48])[0]
        out.append(
            Volume(
                offset=first * 512,
                fs=f"GPT:{raw[0:16].hex()[:8]}",
                size=(last - first + 1) * 512,
                source="GPT",
            )
        )
    return out


def scan_signatures(
    f, total: int, step: int = 0x4000, window: int = 0x10000, limit: int | None = None
) -> list[Volume]:
    """Грубый скан: ищет сигнатуры ФС по сетке с шагом `step`.

    Жадный режим (limit=None) читает образ целиком — для многогигабайтных
    дампов используйте --max-scan.
    """
    out: list[Volume] = []
    stop = total if limit is None else min(total, limit)
    pos = 0
    while pos < stop:
        f.seek(pos)
        buf = f.read(window)
        if not buf:
            break
        for name, check in FS_SIGNATURES:
            try:
                if check(buf):
                    out.append(Volume(offset=pos, fs=name, source="scan"))
            except Exception:
                pass
        pos += step
    # dedup
    seen = set()
    uniq = []
    for v in out:
        k = (v.offset, v.fs)
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return sorted(uniq, key=lambda v: v.offset)


def analyze(path: Path, *, max_scan: int | None = None) -> Analysis:
    dtype, matched = detect_type_by_name(path)
    profile = TYPE_PROFILES[dtype]
    size = path.stat().st_size

    a = Analysis(
        path=str(path),
        size=size,
        dtype=dtype,
        matched=matched,
        profile=profile,
    )

    with path.open("rb") as f:
        mbr = read_mbr(f)
        gpt = read_gpt(f)
        a.has_mbr = bool(mbr)
        a.has_gpt = bool(gpt)
        a.volumes = mbr + gpt

        # скан сигнатур — даже если MBR/GPT есть, скан не помешает
        fs_scan = scan_signatures(f, size, limit=max_scan)

        # объединяем: если MBR/GPT дал offset, но не дал реальную ФС — берём из скана
        existing_offsets = {v.offset for v in a.volumes}
        for v in fs_scan:
            if v.offset not in existing_offsets:
                a.volumes.append(v)
        a.volumes.sort(key=lambda v: v.offset)

    # ---- выбор рекомендуемого тома ----
    ext_vols = [v for v in a.volumes if v.fs.startswith("ext")]
    fat_vols = [v for v in a.volumes if v.fs.startswith("FAT") or v.fs == "exFAT"]
    ntfs_vols = [v for v in a.volumes if v.fs == "NTFS"]

    prefer_ext = profile["prefer_ext"]
    if prefer_ext and ext_vols:
        rec = ext_vols[0]
    elif fat_vols:
        rec = fat_vols[0]
    elif ntfs_vols:
        rec = ntfs_vols[0]
    elif a.volumes:
        rec = a.volumes[0]
    else:
        rec = None

    if rec:
        a.recommended_offset = rec.offset
        a.recommended_fs = rec.fs
        a.write_allowed = rec.fs.startswith("FAT") or rec.fs == "exFAT"
    else:
        a.notes.append(
            "Известных ФС не найдено — возможно, зашифрованный дамп или нестандартная разметка."
        )

    # ---- заметки по типу ----
    if profile["need_decode"]:
        a.notes.append(
            "Тип дампа предполагает XOR-декодирование (MST/MTK) — "
            "выполните `python main.py decode <file>`."
        )
    if profile["need_split"]:
        a.notes.append(
            "Тип дампа часто требует разбиения по `udev` — "
            "запустите `python main.py split <file> udev.txt -o parts`."
        )

    # ---- спец-предупреждения ----
    if dtype == "nand_dump":
        a.notes.append(
            "NAND/SPI: возможно наличие OOB/ECC — Mount File их не обрабатывает. "
            "Используйте образ без OOB."
        )
    if not a.write_allowed:
        a.notes.append(
            "Запись недоступна: только FAT32 поддерживает запись в Mount File. "
            "Рекомендуемый том — только для чтения."
        )
    if "boot1" in path.name.lower() or "boot2" in path.name.lower():
        a.notes.append("BOOT1/BOOT2: критичный раздел, не перезаписывайте без бэкапа.")

    return a


# ============================================================
# 3. АВТОНАСТРОЙКА И ЗАПУСК MOUNT FILE
# ============================================================
def mountfile_cmd(*args: str) -> list[str]:
    return [sys.executable, "main.py", *args]


def run_mountfile_cli(image: Path, args: list[str]) -> int:
    # The CLI grammar is ``<command> <image> [command arguments...]``.
    # Appending the image at the end mis-parses ``ls`` and ``split``.
    if not args:
        raise ValueError("a mountfile CLI subcommand is required")
    cmd = mountfile_cmd(args[0], str(image), *args[1:])
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


def auto_run(
    a: Analysis,
    *,
    gui: bool,
    decode: bool,
    split: bool,
    udev: str | None,
    list_only: bool,
    extract: bool = False,
    extract_dir: str = "extracted",
) -> int:
    img = Path(a.path)

    print("\n── Автонастройка Mount File ──")
    print(f"  Тип: {a.dtype} ({a.profile['description']})")
    print(f"  Совпадение по имени: {a.matched or '—'}")
    if a.recommended_fs:
        print(
            f"  Рекомендуемый том: {a.recommended_fs} @ {hexoff(a.recommended_offset)}"
        )
    print(
        f"  Запись разрешена: {'да (FAT32)' if a.write_allowed else 'нет (только чтение)'}"
    )

    if list_only:
        return 0

    # ---- decode (RT809H / MST / MTK) ----
    if decode or a.profile["need_decode"]:
        print("\n── Шаг 1: декодирование дампа (RT809H MST/MTK) ──")
        run_mountfile_cli(img, ["decode"])

    # ---- split по udev ----
    if split or (a.profile["need_split"] and udev):
        if not udev:
            print("\n⚠️  Split пропущен: не указан --udev <file>")
        else:
            print(f"\n── Шаг 2: split по udev={udev} ──")
            run_mountfile_cli(img, ["split", udev, "-o", "parts"])
    elif a.profile["need_split"] and not udev:
        print("\nℹ️  Для split укажите --udev <файл разметки>")

    # ---- CLI: volumes + ls ----
    for sub in a.profile.get("cli_after_open", []):
        print(f"\n── CLI: {sub} ──")
        run_mountfile_cli(img, [sub, "/"] if sub == "ls" else [sub])

    # ---- извлечение типовых файлов ----
    if extract:
        print("\n── Шаг 3: извлечение типовых файлов ──")
        auto_extract_known_files(a, extract_dir)

    # ---- GUI ----
    if gui:
        print("\n── Запуск GUI ──")
        cmd = mountfile_cmd(str(img))
        print(f"  $ {' '.join(cmd)}")
        subprocess.run(cmd, check=False)

    return 0


# ============================================================
# 4. MAIN
# ============================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Авто-определение дампа по имени файла и автонастройка Mount File.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("image", help="Путь к дампу/образу")
    p.add_argument("--gui", action="store_true", help="Открыть GUI Mount File")
    p.add_argument(
        "--decode", action="store_true", help="Форсировать decode (RT809H MST/MTK)"
    )
    p.add_argument("--split", action="store_true", help="Форсировать split по udev")
    p.add_argument("--udev", help="Файл разметки udev для split")
    p.add_argument(
        "--auto",
        action="store_true",
        help="Разрешить всё автоматически (decode, split, GUI)",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Только показать анализ, ничего не запускать",
    )
    p.add_argument("--json", action="store_true", help="Вывести анализ в JSON")
    p.add_argument(
        "--extract",
        action="store_true",
        help="Извлечь типовые файлы (hdcp_key.bin, keymaster, PLAYREADY*)",
    )
    p.add_argument(
        "--out",
        default="extracted",
        help="Каталог для --extract (по умолчанию: extracted)",
    )
    p.add_argument(
        "--max-scan",
        type=lambda s: int(s, 0),
        default=None,
        help="Ограничить скан сигнатур первыми N байтами (напр. 0x40000000)",
    )
    return p.parse_args()


def print_analysis(a: Analysis) -> None:
    line = "═" * 62
    print(f"\n{line}\n  Анализ дампа\n{line}")
    print(f"  Файл:   {a.path}")
    print(f"  Размер: {a.size} байт ({a.size / (1024**3):.3f} ГБ)")
    print(f"  Тип:    {a.dtype} — {a.profile['description']}")
    print(f"  Имя:    совпадение «{a.matched or '—'}»")

    if a.has_mbr or a.has_gpt:
        tables = []
        if a.has_mbr:
            tables.append("MBR")
        if a.has_gpt:
            tables.append("GPT")
        print(f"  Таблица разделов: {', '.join(tables)}")

    print("\n  Найденные тома:")
    if not a.volumes:
        print("    (нет)")
    for i, v in enumerate(a.volumes):
        marker = " ← рекомендуется" if v.offset == a.recommended_offset else ""
        print(
            f"    [{i}] {v.fs:<15} @ {hexoff(v.offset)}  " f"source={v.source}{marker}"
        )

    if a.notes:
        print("\n  Заметки:")
        for n in a.notes:
            print(f"    • {n}")

    print()


def main() -> int:
    args = parse_args()
    img = Path(args.image)
    if not img.exists():
        print(f"❌ Файл не найден: {img}")
        return 1

    a = analyze(img, max_scan=args.max_scan)

    if args.json:
        out = asdict(a)
        # убрать несериализуемое
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    print_analysis(a)

    # авто-всё, если указан --auto
    gui = args.gui or args.auto
    decode = args.decode or args.auto
    split = args.split or args.auto

    return auto_run(
        a,
        gui=gui,
        decode=decode,
        split=split,
        udev=args.udev,
        list_only=args.list,
        extract=args.extract,
        extract_dir=args.out,
    )


# ============================================================
# 5. ИЗВЛЕЧЕНИЕ ТИПОВЫХ ФАЙЛОВ (tv_firmware / emmc_dump)
# ============================================================
KNOWN_FILES = [
    "/hdcp_key.bin",
    "/keymaster",
    "/PLAYREADY25",
    "/PLAYREADY30",
]


def auto_extract_known_files(a: Analysis, out_dir: str = "extracted") -> None:
    """Для tv_firmware / emmc_dump — извлечь типичные интересные файлы."""
    os.makedirs(out_dir, exist_ok=True)
    if a.dtype not in ("tv_firmware", "emmc_dump"):
        print(f"  ℹ️  --extract для типа {a.dtype}: типовые пути могут отсутствовать.")
    img = Path(a.path)
    for src in KNOWN_FILES:
        dst = os.path.join(out_dir, Path(src).name)
        print(f"  get {src} → {dst}")
        subprocess.run(
            mountfile_cmd("get", str(img), src, "-o", out_dir),
            check=False,
        )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⏹  Прервано.")
        sys.exit(130)
