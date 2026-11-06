#!/usr/bin/env python3
"""
mountfile_dfir_tools.py — DFIR-инструменты для Mount File (issue #87).

Реализует:
  Task 1.3  YaraScanner          — YARA по файлам и unallocated
  Task 1.5  UnallocatedExtractor — извлечение нераспределённого пространства
  Task 1.6  SlackExtractor       — извлечение slack-пространства
  Task 2.2  ImageDiffer          — live diff между двумя образами

Зависимости:
  обязательные: stdlib
  опциональные: pytsk3, yara-python

Запуск:
    python mountfile_dfir_tools.py yara disk.img rules.yar
    python mountfile_dfir_tools.py unallocated disk.img -o unalloc/
    python mountfile_dfir_tools.py slack disk.img -o slack/
    python mountfile_dfir_tools.py diff before.img after.img -o diff.json
    python mountfile_dfir_tools.py selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import operator
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Optional

# ===========================================================================
# 0. Общие типы и исключения
# ===========================================================================


class DFIRError(Exception):
    pass


class DependencyMissing(DFIRError):
    pass


MAX_SCAN_SIZE = 64 * 1024 * 1024
MAX_RULE_SIZE = 4 * 1024 * 1024
MAX_REGIONS = 1_000_000
SCAN_TIMEOUT = 30


def _integer(value, name: str, *, positive: bool = False) -> int:
    try:
        if isinstance(value, bool):
            raise TypeError("bool is not an offset/size")
        result = operator.index(value)
    except TypeError as exc:
        raise DFIRError(f"{name} must be an integer") from exc
    if result < 0 or (positive and result == 0):
        raise DFIRError(f"{name} has an invalid value")
    return result


@dataclass
class YaraMatch:
    rule: str
    namespace: str
    tags: list[str]
    data_sha256: str
    path: str  # виртуальный путь внутри образа или "<unallocated>"
    offset: Optional[int] = None  # для unallocated/slack


@dataclass
class RegionInfo:
    offset: int
    length: int
    kind: str  # "unallocated" | "slack" | "file"
    owner: Optional[str] = None


# ===========================================================================
# 1. Универсальный читатель образа (RO)
# ===========================================================================


class ImageReader:
    """
    Обёртка над образом: прямой доступ + опционально pytsk3
    для понимания файловой структуры.
    """

    def __init__(self, image_path: Path):
        self.image_path = Path(image_path)
        if not self.image_path.exists():
            raise DFIRError(f"образ не найден: {image_path}")
        self.size = self.image_path.stat().st_size
        self._tsk_fs = None

    # --- прямой доступ -----------------------------------------------------

    def read(self, offset: int, length: int) -> bytes:
        offset = _integer(offset, "offset")
        length = _integer(length, "length")
        if length > MAX_SCAN_SIZE:
            raise DFIRError("read exceeds bounded memory limit")
        try:
            with open(self.image_path, "rb") as f:
                f.seek(offset)
                return f.read(length)
        except OSError as exc:
            raise DFIRError(f"cannot read image: {exc}") from exc

    def read_exact(self, offset: int, length: int) -> bytes:
        offset = _integer(offset, "offset")
        length = _integer(length, "length")
        if offset > self.size or length > self.size - offset:
            raise DFIRError("region extends beyond image")
        data = self.read(offset, length)
        if len(data) != length:
            raise DFIRError(
                f"short image read at {offset}: wanted {length}, got {len(data)}"
            )
        return data

    def stream_chunks(
        self, chunk_size: int = 8 * 1024 * 1024
    ) -> Iterator[tuple[int, bytes]]:
        chunk_size = _integer(chunk_size, "chunk_size", positive=True)
        if chunk_size > MAX_SCAN_SIZE:
            raise DFIRError("chunk exceeds bounded memory limit")
        for offset in range(0, self.size, chunk_size):
            yield offset, self.read_exact(offset, min(chunk_size, self.size - offset))

    # --- pytsk3 ------------------------------------------------------------

    def _tsk(self):
        if self._tsk_fs is not None:
            return self._tsk_fs
        try:
            import pytsk3  # type: ignore
        except ImportError as e:
            raise DependencyMissing(f"pytsk3 не установлен: {e}") from e
        try:
            img = pytsk3.Img_Info(str(self.image_path))
            self._tsk_fs = pytsk3.FS_Info(img)
        except Exception as exc:
            raise DFIRError(f"cannot open filesystem: {exc}") from exc
        return self._tsk_fs

    def has_pytsk3(self) -> bool:
        try:
            self._tsk()
            return True
        except DependencyMissing:
            return False

    def iter_files(self) -> Iterator[tuple[str, bytes]]:
        """Yield complete regular files; never pretend raw chunks are files.

        Oversized files and unreadable directories/files are explicitly refused,
        rather than scanning truncated content under different YARA semantics.
        """
        from mountfile.slack import _iter_file_entries

        fs = self._tsk()
        try:
            for path, entry in _iter_file_entries(fs):
                size = _integer(entry.info.meta.size, "file size")
                if size > MAX_SCAN_SIZE:
                    raise DFIRError(f"file exceeds bounded scan limit: {path}")
                data = entry.read_random(0, size) if size else b""
                if len(data) != size:
                    raise DFIRError(f"short file read: {path}")
                yield path, data
        except DFIRError:
            raise
        except Exception as exc:
            raise DFIRError(f"cannot enumerate/read filesystem files: {exc}") from exc

    # --- файловая структура (для unallocated/slack) -------------------------

    def iter_allocated_regions(self) -> Iterator[RegionInfo]:
        """Refuse discovery without a proven complete filesystem allocation map.

        File runs alone omit metadata, reserved blocks and unreachable allocated
        inodes. pytsk3 does not expose TSK block_walk; an MFT prefix is not an
        allocation bitmap. Consequently neither source proves the complement.
        Use mountfile.dfir_unallocated_scan with independently verified ranges.
        """
        raise DFIRError(
            "complete filesystem allocation map unavailable; automatic unallocated "
            "discovery refused (file runs/MFT heuristics are insufficient). "
            "Use scan_unallocated() with independently verified explicit ranges"
        )

    def _iter_ntfs_allocated(self) -> Iterator[RegionInfo]:
        """Compatibility entry point; the former incomplete heuristic is refused."""
        return self.iter_allocated_regions()


def _allocated_ranges(reader: ImageReader) -> list[tuple[int, int]]:
    ranges = []
    for index, region in enumerate(reader.iter_allocated_regions()):
        if index >= MAX_REGIONS:
            raise DFIRError("too many allocation regions")
        offset = _integer(region.offset, "allocated offset")
        length = _integer(region.length, "allocated length", positive=True)
        if offset > reader.size or length > reader.size - offset:
            raise DFIRError("allocation region extends beyond image")
        ranges.append((offset, offset + length))
    return sorted(ranges)


# ===========================================================================
# 2. Task 1.3 — YARA-сканер
# ===========================================================================


class YaraScanner:
    """Bounded YARA scans using the real engine, never a condition-blind regex."""

    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)

    def scan_files(self, rules_path: Path) -> list[YaraMatch]:
        rules = self._load_rules(rules_path)
        matches: list[YaraMatch] = []
        for path, data in self.reader.iter_files():
            matches.extend(self._scan_blob(data, rules, path=path))
        return matches

    def scan_unallocated(self, rules_path: Path) -> list[YaraMatch]:
        """Scan complete contiguous free regions, without zero-filling allocation.

        Rules see each region as an independent blob. Oversized regions are
        refused: splitting changes conditions (filesize, offsets, conjunctions).
        Automatic discovery currently refuses without a complete allocation map.
        """
        rules = self._load_rules(rules_path)
        matches: list[YaraMatch] = []
        alloc = self._allocated_ranges()
        free = UnallocatedExtractor._subtract(0, self.reader.size, alloc)
        if any(region.length > MAX_SCAN_SIZE for region in free):
            raise DFIRError("unallocated region exceeds bounded scan limit")
        for region in free:
            data = self.reader.read_exact(region.offset, region.length)
            matches.extend(
                self._scan_blob(
                    data, rules, path="<unallocated>", base_offset=region.offset
                )
            )
        return matches

    def _allocated_ranges(self) -> list[tuple[int, int]]:
        return _allocated_ranges(self.reader)

    def _load_rules(self, rules_path: Path):
        try:
            import yara  # type: ignore
        except ImportError as exc:
            raise DependencyMissing(
                "YARA scanning requires yara-python; no regex fallback"
            ) from exc
        try:
            path = Path(rules_path)
            if path.stat().st_size > MAX_RULE_SIZE:
                raise DFIRError("rules exceed bounded input limit")
            with path.open("rb") as stream:
                raw = stream.read(MAX_RULE_SIZE + 1)
            if len(raw) > MAX_RULE_SIZE:
                raise DFIRError("rules exceed bounded input limit")
            return {
                "type": "yara",
                "compiled": yara.compile(source=raw.decode("utf-8")),
            }
        except DFIRError:
            raise
        except Exception as exc:
            raise DFIRError(f"cannot load/compile YARA rules: {exc}") from exc

    def _parse_simple_rules(self, text: str):
        """Former private fallback entry point; incomplete YARA parsing is refused."""
        raise DependencyMissing("regex fallback cannot preserve YARA conditions")

    def _scan_blob(
        self, data: bytes, rules, path: str, base_offset: int = 0
    ) -> list[YaraMatch]:
        base_offset = _integer(base_offset, "base_offset")
        if len(data) > MAX_SCAN_SIZE:
            raise DFIRError("blob exceeds bounded scan limit")
        if rules.get("type") != "yara":
            raise DFIRError("unsupported rule engine; real YARA is required")
        try:
            result = rules["compiled"].match(data=data, timeout=SCAN_TIMEOUT)
            digest = hashlib.sha256(data).hexdigest()
            return [
                YaraMatch(
                    rule=match.rule,
                    namespace=match.namespace,
                    tags=list(match.tags),
                    data_sha256=digest,
                    path=path,
                    offset=(
                        base_offset if path == "<unallocated>" or base_offset else None
                    ),
                )
                for match in result
            ]
        except Exception as exc:
            raise DFIRError(f"YARA scan failed for {path}: {exc}") from exc


# ===========================================================================
# 3. Task 1.5 — Unallocated space
# ===========================================================================


class UnallocatedExtractor:
    """Extract verified free space; incomplete automatic discovery is refused."""

    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)
        self.image_path = Path(image_path)

    def extract(self, out_dir: Path, *, min_region: int = 4096) -> list[RegionInfo]:
        min_region = _integer(min_region, "min_region", positive=True)
        occupied = self._occupied_ranges()
        free = self._subtract(0, self.reader.size, occupied)
        free = [r for r in free if r.length >= min_region]
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for i, r in enumerate(free):
            out = out_dir / f"unalloc_{i:04d}_off{r.offset:08x}_len{r.length}.bin"
            created = False
            try:
                with open(out, "xb") as f:
                    created = True
                    remaining = r.length
                    cursor = r.offset
                    while remaining > 0:
                        chunk = self.reader.read_exact(
                            cursor, min(8 * 1024 * 1024, remaining)
                        )
                        f.write(chunk)
                        cursor += len(chunk)
                        remaining -= len(chunk)
            except Exception:
                if created:
                    out.unlink()
                raise
            written.append(r)
        return written

    def _occupied_ranges(self) -> list[tuple[int, int]]:
        return _allocated_ranges(self.reader)

    @staticmethod
    def _subtract(
        start: int, end: int, occupied: list[tuple[int, int]]
    ) -> list[RegionInfo]:
        """Вычисляет свободные интервалы как разность [start, end] и occupied."""
        start = _integer(start, "range start")
        end = _integer(end, "range end")
        if end < start:
            raise DFIRError("inverted range")
        free = []
        cursor = start
        checked = []
        for o_start, o_end in occupied:
            o_start = _integer(o_start, "occupied start")
            o_end = _integer(o_end, "occupied end")
            if o_end < o_start:
                raise DFIRError("inverted occupied range")
            checked.append((o_start, o_end))
        for o_start, o_end in sorted(checked):
            if o_end <= cursor:
                continue
            if o_start >= end:
                break
            if o_start > cursor:
                free.append(
                    RegionInfo(
                        offset=cursor, length=o_start - cursor, kind="unallocated"
                    )
                )
            cursor = max(cursor, o_end)
        if cursor < end:
            free.append(
                RegionInfo(offset=cursor, length=end - cursor, kind="unallocated")
            )
        return free


# ===========================================================================
# 4. Task 1.6 — Slack space
# ===========================================================================


class SlackExtractor:
    """Compatibility facade for verified EOF-cluster slack via pytsk3."""

    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)
        self.image_path = Path(image_path)

    def extract(self, out_dir: Path) -> list[RegionInfo]:
        from mountfile.slack import SlackError, SlackRegion, extract_slack

        regions = list(self._find_slack_regions())
        # Extraction receives already located physical regions; the original
        # public return type remains RegionInfo for the compatibility facade.
        mapped = [
            SlackRegion(r.owner or "unknown", r.offset, r.length, 0, 0) for r in regions
        ]
        try:
            extract_slack(self.image_path, out_dir, regions=mapped)
        except SlackError as exc:
            raise DFIRError(str(exc)) from exc
        return regions

    def _find_slack_regions(self) -> Iterator[RegionInfo]:
        from mountfile.slack import SlackError, find_slack_regions

        try:
            for region in find_slack_regions(self.image_path):
                yield RegionInfo(region.offset, region.length, "slack", region.path)
        except SlackError as exc:
            raise DFIRError(str(exc)) from exc

    def _ntfs_slack(self, boot: bytes) -> Iterator[RegionInfo]:
        """Compatibility entry point: use TSK data runs, never a partial MFT parse."""
        yield from self._find_slack_regions()

    def _fat32_slack(self, boot: bytes) -> Iterator[RegionInfo]:
        """Compatibility entry point: TSK follows the actual FAT cluster chain."""
        yield from self._find_slack_regions()


# ===========================================================================
# 5. Task 2.2 — Live diff
# ===========================================================================


@dataclass
class DiffEntry:
    path: str
    status: str  # "added" | "removed" | "modified" | "same"
    before_sha: Optional[str] = None
    after_sha: Optional[str] = None
    before_size: Optional[int] = None
    after_size: Optional[int] = None


class ImageDiffer:
    """
    Сравнивает два образа: файлы (через pytsk3, если есть) и регионы
    (через прямой hash по чанкам).
    """

    def __init__(self, before: Path, after: Path):
        self.before = ImageReader(before)
        self.after = ImageReader(after)

    def diff_files(self) -> list[DiffEntry]:
        if not (self.before.has_pytsk3() and self.after.has_pytsk3()):
            return self.diff_regions()
        b = {p: hashlib.sha256(d).hexdigest() for p, d in self.before.iter_files()}
        a = {p: hashlib.sha256(d).hexdigest() for p, d in self.after.iter_files()}
        out: list[DiffEntry] = []
        for p in sorted(set(b) | set(a)):
            if p not in b:
                out.append(DiffEntry(path=p, status="added", after_sha=a[p]))
            elif p not in a:
                out.append(DiffEntry(path=p, status="removed", before_sha=b[p]))
            elif b[p] != a[p]:
                out.append(
                    DiffEntry(
                        path=p, status="modified", before_sha=b[p], after_sha=a[p]
                    )
                )
        return out

    def diff_regions(self, chunk: int = 8 * 1024 * 1024) -> list[DiffEntry]:
        """Побайтовый diff без файловой структуры."""
        out: list[DiffEntry] = []
        max_size = max(self.before.size, self.after.size)
        for off in range(0, max_size, chunk):
            b = self.before.read(off, chunk)
            a = self.after.read(off, chunk)
            if b == a:
                continue
            bs = hashlib.sha256(b).hexdigest() if b else None
            as_ = hashlib.sha256(a).hexdigest() if a else None
            out.append(
                DiffEntry(
                    path=f"<region:{off:08x}>",
                    status="modified",
                    before_sha=bs,
                    after_sha=as_,
                    before_size=len(b),
                    after_size=len(a),
                )
            )
        return out

    def to_json(self, out: Path) -> int:
        entries = self.diff_files()
        with open(out, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in entries], f, indent=2, ensure_ascii=False)
        return len(entries)


# ===========================================================================
# 6. CLI
# ===========================================================================


def cmd_yara(args):
    scanner = YaraScanner(Path(args.image))
    matches = scanner.scan_files(Path(args.rules))
    if args.unallocated:
        matches.extend(scanner.scan_unallocated(Path(args.rules)))
    if args.json:
        print(json.dumps([asdict(m) for m in matches], indent=2, ensure_ascii=False))
    else:
        for m in matches:
            print(
                f"{m.rule:<30} {m.path:<40} {m.offset if m.offset is not None else '-'}"
            )
    return 0 if not matches else 1


def cmd_unallocated(args):
    ex = UnallocatedExtractor(Path(args.image))
    regions = ex.extract(Path(args.out), min_region=args.min_region)
    print(f"extracted {len(regions)} regions -> {args.out}")
    for r in regions[:10]:
        print(f"  off={r.offset:#x} len={r.length} kind={r.kind}")
    if len(regions) > 10:
        print(f"  ... and {len(regions) - 10} more")
    return 0


def cmd_slack(args):
    ex = SlackExtractor(Path(args.image))
    regions = ex.extract(Path(args.out))
    print(f"extracted {len(regions)} slack regions -> {args.out}")
    return 0


def cmd_diff(args):
    differ = ImageDiffer(Path(args.before), Path(args.after))
    n = differ.to_json(Path(args.out))
    print(f"diff: {n} entries -> {args.out}")
    return 0


def cmd_selftest(args):
    print("=== DFIR tools selftest ===")
    failures = 0

    # 1. ImageReader
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "img.bin"
        img.write_bytes(b"\0" * 1024 * 1024)
        r = ImageReader(img)
        assert r.size == 1024 * 1024
        assert len(r.read(0, 512)) == 512
        print("ok: ImageReader")

    # 2. UnallocatedExtractor.subtract
    free = UnallocatedExtractor._subtract(
        0, 1000, [(100, 200), (500, 600), (1500, 1600)]
    )
    assert [(r.offset, r.length) for r in free] == [
        (0, 100),
        (200, 300),
        (600, 400),
    ], f"bad subtract: {[(r.offset, r.length) for r in free]}"
    print("ok: UnallocatedExtractor._subtract")

    # 3. Real YARA, or an explicit missing-dependency refusal.
    with tempfile.TemporaryDirectory() as td:
        rules = Path(td) / "r.yar"
        rules.write_text(
            'rule test_rule {\n  strings: $a = "hello"\n  condition: $a\n}\n'
        )
        img = Path(td) / "img.bin"
        img.write_bytes(b"\0" * 1024 + b"hello" + b"\0" * 1024)
        scanner = YaraScanner(img)
        try:
            loaded = scanner._load_rules(rules)
        except DependencyMissing:
            print("ok: YaraScanner explicitly requires yara-python")
        else:
            assert loaded["type"] == "yara"
            matches = scanner._scan_blob(b"...hello...", loaded, path="/test")
            assert len(matches) == 1 and matches[0].rule == "test_rule"
            print("ok: YaraScanner (real engine)")

    # 4. ImageDiffer.diff_regions
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a.bin"
        a.write_bytes(b"\0" * 4096)
        b = Path(td) / "b.bin"
        b.write_bytes(b"\0" * 2048 + b"\xFF" * 2048)
        differ = ImageDiffer(a, b)
        entries = differ.diff_regions(chunk=4096)
        assert len(entries) == 1, f"ожидали 1 diff, получили {len(entries)}"
        assert entries[0].status == "modified"
        print("ok: ImageDiffer.diff_regions")

    # 5. Unsupported raw images must not claim to have clean/empty slack.
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "raw.img"
        img.write_bytes(b"\0" * 4096)
        ex = SlackExtractor(img)
        try:
            list(ex._find_slack_regions())
        except DFIRError:
            print("ok: SlackExtractor refuses unsupported images")
        else:
            raise AssertionError("unsupported image was not refused")

    print(f"=== {failures} failures ===")
    return failures


def build_parser():
    p = argparse.ArgumentParser(prog="mountfile-dfir")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("yara", help="YARA-скан образа")
    sp.add_argument("image")
    sp.add_argument("rules")
    sp.add_argument(
        "--unallocated",
        action="store_true",
        help="также сканировать нераспределённое пространство",
    )
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_yara)

    sp = sub.add_parser("unallocated", help="Извлечь unallocated space")
    sp.add_argument("image")
    sp.add_argument("-o", "--out", required=True)
    sp.add_argument("--min-region", type=int, default=4096)
    sp.set_defaults(func=cmd_unallocated)

    sp = sub.add_parser("slack", help="Извлечь slack space")
    sp.add_argument("image")
    sp.add_argument("-o", "--out", required=True)
    sp.set_defaults(func=cmd_slack)

    sp = sub.add_parser("diff", help="Live diff между образами")
    sp.add_argument("before")
    sp.add_argument("after")
    sp.add_argument("-o", "--out", required=True)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("selftest")
    sp.set_defaults(func=cmd_selftest)
    return p


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DependencyMissing as e:
        print(f"dependency: {e}", file=sys.stderr)
        return 2
    except DFIRError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
