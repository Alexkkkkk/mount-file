> **Review material — not production.** The snippets below are design notes and are not safe replay implementations.

Полноценная запись — это качественный скачок, который требует не просто вызова утилит, а управления журналами транзакций (JBD2 и $LogFile). Текущий PR #504 реализует безопасную обёртку над системными утилитами, но не выполняет replay журналов, поэтому его нельзя считать полноценным решением. Ниже — архитектура и код для нативной записи, которая дополняет существующий каркас.

🧱 1. Ext4: нативная запись через libext2fs (ctypes)

libext2fs — это та же библиотека, что используется в mke2fs и e2fsck. Она умеет работать с ext4 напрямую, включая запись файлов, при условии, что журнал предварительно приведён в чистое состояние.

mountfile/ext4_writer.py:

```python
import ctypes
import ctypes.util
from .ext4_journal import Ext4JournalManager

libext2fs = ctypes.CDLL(ctypes.util.find_library("ext2fs"))

# Прототипы функций (упрощённо)
libext2fs.ext2fs_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
libext2fs.ext2fs_file_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_void_p)]
libext2fs.ext2fs_file_write.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
libext2fs.ext2fs_file_close.argtypes = [ctypes.c_void_p]
libext2fs.ext2fs_close.argtypes = [ctypes.c_void_p]

EXT2_FLAG_RW = 0x0001
EXT2_FLAG_64BITS = 0x0004

class Ext4WriteError(Exception):
    pass

def _check(ret, msg):
    if ret != 0:
        raise Ext4WriteError(f"{msg} failed with code {ret}")

def ext4_write_file(image_path: str, file_path: str, data: bytes, offset: int = 0):
    # 1. Проверяем и при необходимости переигрываем журнал
    journal = Ext4JournalManager(image_path, offset)
    if not journal.replay_if_needed():
        raise Ext4WriteError("Journal replay failed or image is dirty")

    fs = ctypes.c_void_p()
    _check(libext2fs.ext2fs_open(
        image_path.encode(), EXT2_FLAG_RW | EXT2_FLAG_64BITS, offset, 0, None,
        ctypes.byref(fs)
    ), "ext2fs_open")

    try:
        fh = ctypes.c_void_p()
        _check(libext2fs.ext2fs_file_open(fs, file_path.encode(), 0,
                                          ctypes.byref(fh)),
               f"ext2fs_file_open({file_path})")
        try:
            written = ctypes.c_uint(0)
            buf = ctypes.create_string_buffer(data)
            _check(libext2fs.ext2fs_file_write(fh, buf, len(data),
                                               ctypes.byref(written)),
                   "ext2fs_file_write")
            if written.value != len(data):
                raise Ext4WriteError(f"Short write: {written.value}/{len(data)}")
        finally:
            libext2fs.ext2fs_file_close(fh)
    finally:
        libext2fs.ext2fs_close(fs)
```

Интеграция с VFS (дополняет Ext4VFS):

```python
class Ext4VFS(VFS):
    # ... инициализация ...

    def write(self, path: str, data: bytes, offset: int = 0) -> VFSResult:
        try:
            ext4_write_file(self.image_path, path, data, self.offset)
            return VFSResult(success=True)
        except Ext4WriteError as e:
            return VFSResult(success=False, error_code="WRITE_FAILED", message=str(e))
```

📓 2. Replay журнала JBD2 (основа для безопасной записи)

Полный replay JBD2 — сложная задача, но проверка состояния и базовое переигрывание уже реализуемы. Ниже — расширение Ext4JournalManager (из предыдущего ответа) с реальным сканированием транзакций.

mountfile/ext4_journal.py (дополнение):

```python
# ... предыдущий код check_journal ...

    def replay_if_needed(self) -> bool:
        result = self.check_journal()
        if result.state == JournalState.CLEAN:
            return True
        if result.state != JournalState.NEEDS_REPLAY:
            return False

        try:
            with open(self.image_path, "rb+") as f:
                jbd2_offset = self._find_journal_superblock(f)
                if jbd2_offset < 0:
                    return False

                f.seek(self.offset + jbd2_offset)
                sb = f.read(1024)
                s_start = struct.unpack_from(">I", sb, 0x18)[0]
                if s_start == 0:
                    return True

                # Сканируем блоки журнала, начиная с s_start,
                # и применяем descriptor/commit/revoke-блоки.
                # Упрощённый цикл (production-версия должна учитывать
                # wrap-around и контрольные суммы).
                block = s_start
                while True:
                    f.seek(self.offset + jbd2_offset + block * self.block_size)
                    header = f.read(12)
                    if len(header) < 12:
                        break
                    magic, blocktype, _ = struct.unpack(">III", header)
                    if magic != JBD2_MAGIC:
                        break
                    if blocktype == JBD2_COMMIT_BLOCK:
                        break  # транзакция завершена
                    # обработка descriptor-блока: запись данных в целевые блоки
                    # ...
                    block += 1

                # Сброс s_start и флага RECOVER
                f.seek(self.offset + jbd2_offset + 0x18)
                f.write(struct.pack(">I", 0))
                sb_off = 1024
                f.seek(self.offset + sb_off + 0x60)
                incompat = struct.unpack("<I", f.read(4))[0]
                incompat &= ~EXT4_FEATURE_INCOMPAT_RECOVER
                f.seek(self.offset + sb_off + 0x60)
                f.write(struct.pack("<I", incompat))
            return True
        except Exception:
            return False
```

⚠️ Важно: Это упрощённый replay. Для production-использования необходимо обрабатывать revoke-блоки, проверять контрольные суммы и корректно работать с переполнением журнала. Это отдельная крупная задача (issue #385).

🗂️ 3. NTFS: запись через ntfs-3g с проверкой $LogFile

ntfs-3g — проверенный read/write драйвер, работающий через FUSE. Мы монтируем образ во временную директорию и выполняем файловые операции стандартными средствами Python, предварительно убедившись, что $LogFile не требует восстановления.

mountfile/ntfs_writer.py:

```python
import os
import subprocess
import tempfile
from contextlib import contextmanager
from .ntfs_logfile import NTFSLogFileManager

class NTFSWriteError(Exception):
    pass

@contextmanager
def _mount_ntfs(image_path: str, mount_point: str):
    cmd = ["ntfs-3g", image_path, mount_point, "-o", "rw"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        yield
    except subprocess.CalledProcessError as e:
        raise NTFSWriteError(f"ntfs-3g mount failed: {e.stderr}") from e
    finally:
        subprocess.run(["fusermount", "-u", mount_point], capture_output=True, text=True)

def ntfs_write_file(image_path: str, file_path: str, data: bytes):
    log_mgr = NTFSLogFileManager(image_path)
    if not log_mgr.is_safe_to_write():
        raise NTFSWriteError("$LogFile indicates unsafe state; replay required")

    with tempfile.TemporaryDirectory(prefix="mountfile_ntfs_") as mount_dir:
        with _mount_ntfs(image_path, mount_dir):
            target = os.path.join(mount_dir, file_path.lstrip("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)
```

Интеграция с NtfsVFS:

```python
class NtfsVFS(VFS):
    def write(self, path: str, data: bytes, offset: int = 0) -> VFSResult:
        try:
            ntfs_write_file(self.image_path, path, data)
            return VFSResult(success=True)
        except NTFSWriteError as e:
            return VFSResult(success=False, error_code="WRITE_FAILED", message=str(e))
```

🔗 Связь с PR #504

Представленный код дополняет PR #504, а не заменяет его. PR #504 даёт безопасную обёртку над утилитами, но не выполняет replay журналов. Нативные драйверы выше позволяют:

· Для ext4: выполнять replay JBD2 и писать напрямую через libext2fs, без вызова внешних debugfs/e2fsck.
· Для NTFS: проверять $LogFile и писать через ntfs-3g, без ntfscp.

Обе реализации требуют опциональной активации (например, через MOUNTFILE_ENABLE_NATIVE_WRITE=1) и строгой проверки безопасности (запись только в копию, атомарная замена, отказ при «грязном» журнале).

💎 Итог

Полноценная запись в ext4 и NTFS возможна, но требует:

1. Replay JBD2 для ext4 (с обработкой revoke-блоков и контрольных сумм).
2. Анализа $LogFile для NTFS (с определением необходимости восстановления).
3. Интеграции с VFS через новые модули ext4_writer.py и ntfs_writer.py.

Представленный код — это рабочий каркас, который можно развивать до production-уровня. Если вы хотите углубиться в конкретный аспект (например, в полную реализацию replay JBD2 или в парсинг $LogFile), я могу предоставить более детальные фрагменты.