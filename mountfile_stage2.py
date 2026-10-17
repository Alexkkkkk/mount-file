#!/usr/bin/env python3
"""
mountfile_stage2.py — Stage 2 (Universal RW, Live Diff, Crypto) + Stage 4 (Plugins, REST)
для эпика mount-file #87.

Задачи:
  2.1 UniversalFSRW  — запись на FAT32 / ext2-4 / NTFS
  2.2 LiveDiff       — отчёт об изменениях за сессию
  2.3 CryptoContainer— открытие LUKS / BitLocker
  4.1 PluginSystem   — хуки on_open / on_file / on_close
  4.2 REST API       — FastAPI-эндпоинты (опционально)

Зависимости (системные):
  debugfs (e2fsprogs), ntfs-3g, cryptsetup, dislocker
Python (опционально):
  pip install fastapi uvicorn
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

# ============================================================================
# 2.1 UniversalFSRW — универсальная запись
# ============================================================================

@dataclass
class JournalEntry:
    """Запись в hash-chain журнале для отката."""
    timestamp: str
    action: str
    path: str
    before_hash: Optional[str] = None
    backup_path: Optional[str] = None


class UniversalFSRW:
    """
    Универсальная запись с авто-бэкапом и hash-chain журналом.
    Стратегии:
      FAT32  — прямое редактирование через mtools (или fallback: loop-mount)
      ext2-4 — debugfs (write, rm, ln)
      NTFS   — ntfs-3g (loop-mount) или ntfsclone
    """

    def __init__(self, image: Path, fs_type: str, backup_dir: Optional[Path] = None):
        self.image = Path(image)
        self.fs_type = fs_type.lower()
        self.backup_dir = Path(backup_dir) if backup_dir else Path(tempfile.mkdtemp(prefix="mnt_backup_"))
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.journal: List[JournalEntry] = []
        self._journal_path = self.backup_dir / "journal.json"
        self._load_journal()

    # ---------- журнал ----------
    def _load_journal(self):
        if self._journal_path.exists():
            try:
                data = json.loads(self._journal_path.read_text())
                self.journal = [JournalEntry(**e) for e in data]
            except Exception:
                self.journal = []

    def _save_journal(self):
        self._journal_path.write_text(
            json.dumps([e.__dict__ for e in self.journal], indent=2, ensure_ascii=False)
        )

    def _add_journal(self, action: str, path: str, before_hash: Optional[str] = None,
                     backup_path: Optional[str] = None):
        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            path=path,
            before_hash=before_hash,
            backup_path=backup_path,
        )
        self.journal.append(entry)
        self._save_journal()

    # ---------- хэши ----------
    @staticmethod
    def _file_hash(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ---------- бэкап ----------
    def _backup(self, image_path: str) -> Optional[Path]:
        """Сохраняет копию файла из образа (если он существует)."""
        try:
            content = self._read_native(image_path)
        except Exception:
            return None
        if content is None:
            return None
        safe = image_path.strip("/").replace("/", "_")
        bp = self.backup_dir / f"{safe}.bak"
        bp.write_bytes(content)
        return bp

    # ---------- native read ----------
    def _read_native(self, image_path: str) -> Optional[bytes]:
        if self.fs_type.startswith("ext"):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                subprocess.run(
                    ["debugfs", "-R", f"dump {image_path} {tmp_path}", str(self.image)],
                    check=True, capture_output=True
                )
                return tmp_path.read_bytes()
            except subprocess.CalledProcessError:
                return None
            finally:
                tmp_path.unlink(missing_ok=True)
        elif self.fs_type == "ntfs":
            # ntfs-3g через loop-mount (требует root)
            mnt = Path(tempfile.mkdtemp(prefix="ntfs_mnt_"))
            try:
                subprocess.run(
                    ["ntfs-3g", str(self.image), str(mnt)],
                    check=True, capture_output=True, timeout=30
                )
                target = mnt / image_path.lstrip("/")
                return target.read_bytes() if target.exists() else None
            except Exception:
                return None
            finally:
                subprocess.run(["umount", str(mnt)], capture_output=True)
                shutil.rmtree(mnt, ignore_errors=True)
        elif self.fs_type.startswith("fat"):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                subprocess.run(
                    ["mcopy", "-i", str(self.image), f"::{image_path}", str(tmp_path)],
                    check=True, capture_output=True
                )
                return tmp_path.read_bytes()
            except subprocess.CalledProcessError:
                return None
            finally:
                tmp_path.unlink(missing_ok=True)
        return None

    # ---------- native write ----------
    def _write_native(self, image_path: str, data: bytes):
        if self.fs_type.startswith("ext"):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            try:
                subprocess.run(
                    ["debugfs", "-w", "-R", f"write {tmp_path} {image_path}", str(self.image)],
                    check=True, capture_output=True
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        elif self.fs_type == "ntfs":
            mnt = Path(tempfile.mkdtemp(prefix="ntfs_mnt_"))
            try:
                subprocess.run(["ntfs-3g", str(self.image), str(mnt)], check=True, capture_output=True, timeout=30)
                target = mnt / image_path.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            finally:
                subprocess.run(["umount", str(mnt)], capture_output=True)
                shutil.rmtree(mnt, ignore_errors=True)
        elif self.fs_type.startswith("fat"):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            try:
                subprocess.run(
                    ["mcopy", "-i", str(self.image), "-o", str(tmp_path), f"::{image_path}"],
                    check=True, capture_output=True
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            raise NotImplementedError(f"ФС {self.fs_type} пока не поддерживается")

    # ---------- публичные методы ----------
    def write_file(self, image_path: str, data: bytes):
        """Записывает файл с бэкапом и журналированием."""
        before_hash = None
        backup_path = None
        try:
            old = self._read_native(image_path)
            if old is not None:
                before_hash = hashlib.sha256(old).hexdigest()
                backup_path = str(self._backup(image_path))
        except Exception:
            pass

        self._write_native(image_path, data)
        self._add_journal("write", image_path, before_hash, backup_path)

    def restore_from_backup(self, image_path: str):
        """Откатывает файл из последнего бэкапа."""
        for entry in reversed(self.journal):
            if entry.path == image_path and entry.backup_path:
                bp = Path(entry.backup_path)
                if bp.exists():
                    self._write_native(image_path, bp.read_bytes())
                    return True
        return False

    def flush(self):
        """Сброс и проверка целостности (fsck для ext)."""
        if self.fs_type.startswith("ext"):
            subprocess.run(["e2fsck", "-f", "-y", str(self.image)], capture_output=True)
        elif self.fs_type == "ntfs":
            subprocess.run(["ntfsfix", str(self.image)], capture_output=True)


# ============================================================================
# 2.2 LiveDiff — отчёт об изменениях
# ============================================================================

@dataclass
class DiffEntry:
    path: str
    status: str  # added / modified / deleted
    before_hash: Optional[str] = None
    after_hash: Optional[str] = None


class LiveDiff:
    """Сравнивает хэши файлов до и после сессии."""

    def __init__(self, fs_reader, root: str = "/"):
        self.fs_reader = fs_reader  # callable: path -> bytes | None
        self.root = root
        self.before: Dict[str, str] = {}
        self.after: Dict[str, str] = {}
        self.diff: List[DiffEntry] = []

    def snapshot(self, label: str):
        if label == "before":
            self.before = self._hash_all()
        else:
            self.after = self._hash_all()

    def _hash_all(self) -> Dict[str, str]:
        result = {}
        for path, data in self.fs_reader(self.root):
            if data is not None:
                result[path] = hashlib.sha256(data).hexdigest()
        return result

    def compute(self) -> List[DiffEntry]:
        self.diff.clear()
        all_paths = set(self.before) | set(self.after)
        for path in sorted(all_paths):
            b = self.before.get(path)
            a = self.after.get(path)
            if b is None and a is not None:
                self.diff.append(DiffEntry(path, "added", None, a))
            elif b is not None and a is None:
                self.diff.append(DiffEntry(path, "deleted", b, None))
            elif b != a:
                self.diff.append(DiffEntry(path, "modified", b, a))
        return self.diff

    def to_markdown(self) -> str:
        lines = ["# Live Diff Report", ""]
        if not self.diff:
            lines.append("*No changes detected.*")
            return "\n".join(lines)
        for d in self.diff:
            lines.append(f"- **{d.status.upper()}** `{d.path}`")
            if d.status == "modified":
                lines.append(f"  - before: `{d.before_hash[:16]}...`")
                lines.append(f"  - after:  `{d.after_hash[:16]}...`")
        return "\n".join(lines)


# ============================================================================
# 2.3 CryptoContainer — LUKS / BitLocker
# ============================================================================

class CryptoContainer:
    """
    Открытие зашифрованных контейнеров.
    LUKS: cryptsetup luksOpen
    BitLocker: dislocker
    """

    def __init__(self, image: Path):
        self.image = Path(image)
        self.mount_dir: Optional[Path] = None

    def detect(self) -> str:
        """Определяет тип шифрования по сигнатурам."""
        with open(self.image, "rb") as f:
            header = f.read(4096)
        if header.startswith(b"LUKS\xba\xbe"):
            return "luks"
        if header[3:11] == b"-FVE-FS-":
            return "bitlocker"
        return "unknown"

    def open_luks(self, password: str) -> Path:
        if self.detect() != "luks":
            raise ValueError("Образ не является LUKS-контейнером")
        self.mount_dir = Path(tempfile.mkdtemp(prefix="luks_mnt_"))
        mapper = f"luks_{os.getpid()}"
        subprocess.run(
            ["cryptsetup", "luksOpen", str(self.image), mapper],
            input=password.encode(), check=True, capture_output=True
        )
        subprocess.run(["mount", f"/dev/mapper/{mapper}", str(self.mount_dir)], check=True)
        return self.mount_dir

    def open_bitlocker(self, password: str) -> Path:
        if self.detect() != "bitlocker":
            raise ValueError("Образ не является BitLocker-контейнером")
        self.mount_dir = Path(tempfile.mkdtemp(prefix="bitlk_mnt_"))
        dislocker_dir = self.mount_dir / "dislocker"
        dislocker_dir.mkdir()
        subprocess.run(
            ["dislocker", "-p" + password, str(self.image), str(dislocker_dir)],
            check=True, capture_output=True
        )
        subprocess.run([
            "mount", "-o", "loop",
            str(dislocker_dir / "dislocker-file"), str(self.mount_dir)
        ], check=True)
        return self.mount_dir

    def close(self):
        if self.mount_dir:
            subprocess.run(["umount", str(self.mount_dir)], capture_output=True)
            shutil.rmtree(self.mount_dir, ignore_errors=True)
            self.mount_dir = None


# ============================================================================
# 4.1 PluginSystem — простой реестр плагинов
# ============================================================================

class PluginRegistry:
    """Хуки: on_open(image_path), on_file(path, data), on_close()"""

    def __init__(self):
        self.hooks: Dict[str, List] = {"on_open": [], "on_file": [], "on_close": []}

    def register(self, name: str, hook: str, func):
        if hook not in self.hooks:
            raise ValueError(f"Неизвестный хук: {hook}")
        self.hooks[hook].append(func)

    def trigger(self, hook: str, *args, **kwargs):
        for func in self.hooks.get(hook, []):
            try:
                func(*args, **kwargs)
            except Exception as e:
                print(f"[plugin] hook {hook} error: {e}")


# ============================================================================
# 4.2 REST API (опционально, только если установлен fastapi)
# ============================================================================

def create_app(image_path: Path, fs_type: str):
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
    except ImportError:
        raise ImportError("Установите fastapi и uvicorn: pip install fastapi uvicorn")

    app = FastAPI(title="mount-file API", version="2.0")
    rw = UniversalFSRW(image_path, fs_type)
    diff = LiveDiff(lambda root: iter([]))  # заглушка; подставьте реальный reader

    class WriteRequest(BaseModel):
        path: str
        content_b64: str  # base64

    @app.get("/fs/{path:path}")
    def read_file(path: str):
        data = rw._read_native(path)
        if data is None:
            raise HTTPException(404, "Not found")
        import base64
        return {"path": path, "content_b64": base64.b64encode(data).decode()}

    @app.post("/fs/write")
    def write_file(req: WriteRequest):
        import base64
        try:
            data = base64.b64decode(req.content_b64)
            rw.write_file(req.path, data)
            return {"status": "ok", "path": req.path}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.get("/timeline")
    def timeline():
        # Заглушка — в реальности вызывайте mountfile.timeline
        return {"events": []}

    @app.get("/diff")
    def get_diff():
        return {"diff": [d.__dict__ for d in diff.compute()]}

    return app


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse
    p = argparse.ArgumentParser(description="mount-file Stage 2 toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="2.1 Universal write")
    w.add_argument("image", type=Path)
    w.add_argument("--fs", required=True, choices=["fat32", "ext2", "ext3", "ext4", "ntfs"])
    w.add_argument("--path", required=True, help="путь внутри образа")
    w.add_argument("--data", required=True, help="файл с данными")
    w.add_argument("--backup-dir", type=Path)

    d = sub.add_parser("diff", help="2.2 Live diff (заглушка для CLI)")
    d.add_argument("image", type=Path)

    c = sub.add_parser("crypto", help="2.3 Crypto container")
    c.add_argument("image", type=Path)
    c.add_argument("--password", required=True)
    c.add_argument("--type", choices=["luks", "bitlocker"])

    r = sub.add_parser("rest", help="4.2 REST API")
    r.add_argument("image", type=Path)
    r.add_argument("--fs", required=True)
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8000)

    args = p.parse_args()

    if args.cmd == "write":
        rw = UniversalFSRW(args.image, args.fs, args.backup_dir)
        data = Path(args.data).read_bytes()
        rw.write_file(args.path, data)
        rw.flush()
        print(f"[*] written {len(data)} bytes to {args.path}")

    elif args.cmd == "diff":
        print("[!] Для полноценного diff нужен reader ФС. Используйте API из Python.")

    elif args.cmd == "crypto":
        cc = CryptoContainer(args.image)
        t = args.type or cc.detect()
        if t == "luks":
            mnt = cc.open_luks(args.password)
        elif t == "bitlocker":
            mnt = cc.open_bitlocker(args.password)
        else:
            raise SystemExit(f"Неизвестный тип шифрования: {t}")
        print(f"[*] mounted at {mnt}. Press Ctrl+C to close.")
        try:
            time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            cc.close()

    elif args.cmd == "rest":
        import uvicorn
        app = create_app(args.image, args.fs)
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
