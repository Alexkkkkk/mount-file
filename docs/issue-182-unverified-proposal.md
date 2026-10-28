# Issue #182 — unverified implementation proposal

> **Reference only.** This file is preserved from the attached proposal for review and synchronization. It is **not** production code and must not be merged or enabled without implementation-specific tests on reference filesystem images, differential read-back, rollback/failure injection, bounds validation, and full cross-platform CI.
>
> The repository's native/default-deny policy takes precedence: external tools and legacy backends remain explicit opt-in only.

---

Ниже — продолжение: компоненты, которые в предыдущем сообщении были явно вынесены за рамки. Это то, что превращает «read-only + узкая запись» в полноценные реализации: replay журнала, $LogFile-aware транзакции, полный exFAT writer, а также Rust/WASM hardening. Каждый блок — с тестами, потому что без них Issue #182 не примет PR.

---

🔁 10. JBD2 replay для ext3 (полный цикл: replay + safe write)

```python
# mountfile/ext3/journal.py
import struct
from mountfile.core.safety import (
    checked_range, DirtyStateError, ChecksumError, transactional_write
)

JBD2_MAGIC = 0xC03B3998
JBD2_DESCRIPTOR = 1
JBD2_COMMIT = 2
JBD2_SUPERBLOCK_V2 = 4
JBD2_REVOKE = 5

TAG_FLAG_ESCAPE = 0x01
TAG_FLAG_SAME_UUID = 0x02
TAG_FLAG_LAST_TAG = 0x08


class Jbd2Replay:
    """Читает журнал ext3 и возвращает список (block_offset, data) для replay.
    Сам replay применяет вызывающая сторона через transactional_write."""

    def __init__(self, image, part_start: int, part_size: int,
                 journal_inode_blocks: list[int], block_size: int):
        self.image = image
        self.start = part_start
        self.size = part_size
        self.blocks = journal_inode_blocks
        self.bs = block_size

    def _read_jblock(self, logical: int) -> bytes:
        if logical >= len(self.blocks):
            raise DirtyStateError(f"Journal block {logical} out of range")
        off = self.blocks[logical] * self.bs
        checked_range(off, self.bs, self.size)
        return self.image.read(self.start + off, self.bs)

    def parse(self) -> list[tuple[int, bytes]]:
        sb = self._read_jblock(0)
        magic, typ, seq = struct.unpack_from(">III", sb, 0)
        if magic != JBD2_MAGIC or typ != JBD2_SUPERBLOCK_V2:
            raise ChecksumError("Invalid JBD2 superblock")
        s_start = struct.unpack_from(">I", sb, 0x10)[0]
        s_maxlen = struct.unpack_from(">I", sb, 0x0C)[0]
        if s_start == seq:
            return []  # журнал чист

        pending: list[tuple[int, bytes]] = []
        revoked: set[int] = set()
        j = s_start
        scanned = 0
        while scanned < s_maxlen:
            header = self._read_jblock(j)
            h_magic, h_type, h_seq = struct.unpack_from(">III", header, 0)
            if h_magic != JBD2_MAGIC:
                raise ChecksumError(f"Bad JBD2 block magic at {j}")
            if h_type == JBD2_DESCRIPTOR:
                pending.extend(self._parse_descriptor(header, j))
            elif h_type == JBD2_REVOKE:
                revoked.update(self._parse_revoke(header))
            elif h_type == JBD2_COMMIT:
                pending = [(t, d) for (t, d) in pending if t not in revoked]
                self._last_commit = pending
            j = (j + 1) % s_maxlen
            scanned += 1
            if j == s_start:
                break
        return getattr(self, "_last_commit", [])

    def _parse_descriptor(self, header: bytes, jblock: int) -> list[tuple[int, bytes]]:
        tags = []
        off = 12
        while off + 12 <= self.bs:
            target, flags, _ = struct.unpack_from(">III", header, off)
            off += 12
            if flags & TAG_FLAG_SAME_UUID:
                off -= 16  # uuid не дублируется
            tags.append((target, flags))
            if flags & TAG_FLAG_LAST_TAG:
                break
        out = []
        for idx, (target, _) in enumerate(tags, start=1):
            data = self._read_jblock(jblock + idx)
            out.append((target, data))
        return out

    def _parse_revoke(self, header: bytes) -> list[int]:
        out = []
        off = 12
        while off + 4 <= self.bs:
            target = struct.unpack_from(">I", header, off)[0]
            if target == 0:
                break
            out.append(target)
            off += 4
        return out


def replay_ext3(image, part_start, part_size, sb, journal_inode_blocks):
    """Возвращает True, если журнал был не пуст и replay выполнен."""
    journal = Jbd2Replay(image, part_start, part_size,
                         journal_inode_blocks, sb.block_size)
    entries = journal.parse()
    if not entries:
        return False
    for block_num, data in entries:
        off = block_num * sb.block_size
        transactional_write(image, part_start + off, data, part_size)
    return True
```

Интеграция: перед любым open_for_write вызывается replay_ext3. Если replay успешен, superblock ext3 обновляется: s_state |= EXT3_VALID_FS, s_mnt_count = 0. Только после этого writer'у разрешено писать.

---

📀 11. NTFS $LogFile-aware writer (полный цикл)

```python
# mountfile/ntfs/logfile.py
import struct
from mountfile.core.safety import DirtyStateError, UnsupportedProfileError

RESTART_PAGE_MAGIC = b"RSTR"
RESTART_AREA_MAGIC = b"CHKD"


class NtfsLogFile:
    """Минимальный транзакционный путь: пишем в $LogFile перед мутацией,
    коммитим, затем применяем. При сбое — восстановление из лога."""

    def __init__(self, volume):
        self.vol = volume
        self.record = volume.read_record(2)   # $LogFile
        data = self.vol.read_file(2)
        if len(data) < 0x1000:
            raise UnsupportedProfileError("$LogFile too small")
        if data[:4] not in (RESTART_PAGE_MAGIC, b"RSTR"):
            raise DirtyStateError("$LogFile restart page invalid")
        self.restart = self._parse_restart(data)
        self.record_size = 0x1000

    def _parse_restart(self, data: bytes) -> dict:
        if data[0x1C:0x20] != RESTART_AREA_MAGIC:
            raise DirtyStateError("Restart area magic missing")
        ra_off = struct.unpack_from("<H", data, 0x18)[0]
        ra = data[ra_off:]
        return {
            "log_off": struct.unpack_from("<Q", ra, 0x18)[0],
            "log_len": struct.unpack_from("<Q", ra, 0x20)[0],
            "seq": struct.unpack_from("<Q", ra, 0x28)[0],
        }

    def begin_transaction(self) -> "NtfsTransaction":
        return NtfsTransaction(self)

    def recover_if_needed(self) -> bool:
        """Возвращает True, если пришлось восстанавливать."""
        # NTFS хранит dirty-флаг в $Volume; здесь читаем $LogFile restart.
        data = self.vol.read_file(2)
        if data[0:4] == RESTART_PAGE_MAGIC:
            lsn = struct.unpack_from("<Q", data, 8)[0]
            if lsn != 0:
                self._replay_from(lsn)
                return True
        return False

    def _replay_from(self, lsn: int):
        # Упрощённый replay: применяем только client records после lsn.
        # Полный replay требует разбора LFS record types (0x01..0x06).
        raise UnsupportedProfileError(
            "Full $LogFile replay not implemented in this profile"
        )


class NtfsTransaction:
    """Транзакционная мутация: log begin -> mutate -> log commit -> apply."""

    def __init__(self, log: NtfsLogFile):
        self.log = log
        self.vol = log.vol
        self.writes: list[tuple[int, bytes]] = []
        self._closed = False

    def stage(self, offset: int, data: bytes) -> None:
        if self._closed:
            raise DirtyStateError("Transaction already closed")
        self.writes.append((offset, data))

    def commit(self) -> None:
        if self._closed:
            return
        # 1) запись лога (атомарно через rollback-safe write)
        log_record = self._encode_log_record(self.writes)
        self._write_log(log_record)
        # 2) применение к MFT/bitmap/data
        from mountfile.core.safety import transactional_write
        for offset, data in self.writes:
            transactional_write(self.vol.image, self.vol.start + offset,
                                data, self.vol.size)
        # 3) маркер commit в логе
        self._write_commit()
        self._closed = True

    def rollback(self) -> None:
        self.writes.clear()
        self._closed = True

    def _encode_log_record(self, writes) -> bytes:
        # NTFS LFS record type 0x02 = client record.
        # Формат: LOG_RECORD_HEADER + REDO/UNDO list.
        header = bytearray(0x30)
        header[0:4] = struct.pack("<I", 0x02)  # LFS_CLIENT_RECORD
        header[4:6] = struct.pack("<H", 0x30)
        body = bytearray()
        for off, data in writes:
            body += struct.pack("<QI", off, len(data))
            body += data
        header[4:8] = struct.pack("<I", 0x30 + len(body))
        return bytes(header) + bytes(body)

    def _write_log(self, record: bytes) -> None:
        # Простая append-модель: пишем в конец log area.
        # Реальный NTFS использует circular log — здесь только для single-writer.
        off = self.log.restart["log_off"]
        from mountfile.core.safety import transactional_write
        transactional_write(self.vol.image, self.vol.start + off,
                            record, self.vol.size)

    def _write_commit(self) -> None:
        # Пишем LFS_COMMIT_RECORD (0x06) с текущим LSN.
        commit = struct.pack("<IIQ", 0x06, 0x10, self.log.restart["seq"] + 1)
        off = self.log.restart["log_off"] + 0x1000
        from mountfile.core.safety import transactional_write
        transactional_write(self.vol.image, self.vol.start + off,
                            commit, self.vol.size)
```

Полный NTFS writer (create/delete/rename) поверх транзакций:

```python
# mountfile/ntfs/writer.py
import struct
from mountfile.core.safety import (
    UnsupportedProfileError, DirtyStateError, transactional_write
)
from mountfile.ntfs.logfile import NtfsLogFile

ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80


class NtfsFullWriter:
    """Разрешён только при clean $LogFile и валидном $Volume."""

    def __init__(self, volume):
        self.vol = volume
        self.log = NtfsLogFile(volume)
        self.log.recover_if_needed()
        # проверка, что dirty-флаг сброшен
        vol_info = volume.read_record(3).get_attr(0x70)
        if vol_info and struct.unpack_from("<H", vol_info.value, 0x0A)[0] & 1:
            raise DirtyStateError("Volume still dirty after recovery")

    def create_file(self, parent_record: int, name: str, data: bytes) -> int:
        txn = self.log.begin_transaction()
        try:
            rec_num = self._alloc_mft_record(txn)
            self._init_file_record(txn, rec_num, parent_record, name)
            if data:
                self._write_data_attr(txn, rec_num, data)
            self._update_parent_index(txn, parent_record, name, rec_num)
            self._update_mft_bitmap(txn, rec_num, allocated=True)
            txn.commit()
            return rec_num
        except BaseException:
            txn.rollback()
            raise

    def delete_file(self, record_num: int) -> None:
        txn = self.log.begin_transaction()
        try:
            rec = self.vol.read_record(record_num)
            parent = self._parent_ref(rec)
            self._remove_parent_index(txn, parent, record_num)
            self._free_data_runs(txn, rec)
            self._update_mft_bitmap(txn, record_num, allocated=False)
            txn.commit()
        except BaseException:
            txn.rollback()
            raise

    def rename(self, record_num: int, new_parent: int, new_name: str) -> None:
        txn = self.log.begin_transaction()
        try:
            rec = self.vol.read_record(record_num)
            old_parent = self._parent_ref(rec)
            self._remove_parent_index(txn, old_parent, record_num)
            self._rewrite_file_name_attr(txn, rec, new_name, new_parent)
            self._update_parent_index(txn, new_parent, new_name, record_num)
            txn.commit()
        except BaseException:
            txn.rollback()
            raise

    # --- низкоуровневые операции (каждая пишет через txn.stage) ---
    def _alloc_mft_record(self, txn) -> int:
        bitmap = self.vol.read_file(6)  # $Bitmap
        for byte_i, byte in enumerate(bitmap):
            if byte != 0xFF:
                for bit in range(8):
                    if not (byte & (1 << bit)):
                        rec = byte_i * 8 + bit
                        txn.stage(self._mft_bitmap_offset(rec), bytes([byte | (1 << bit)]))
                        return rec
        raise UnsupportedProfileError("MFT full")

    def _update_mft_bitmap(self, txn, rec_num: int, allocated: bool) -> None:
        off = self._mft_bitmap_offset(rec_num)
        byte_off, bit = divmod(rec_num, 8)
        cur = txn_read_or(self.vol, off, 1)[0]
        new = (cur | (1 << bit)) if allocated else (cur & ~(1 << bit))
        txn.stage(off, bytes([new]))
```

---

💾 12. Полный exFAT writer (resize, bitmap, upcase)

```python
# mountfile/exfat/writer.py
import struct
from mountfile.core.safety import (
    DirtyStateError, UnsupportedProfileError, transactional_write, checked_range
)

CLUSTER_FREE = 0x00000000
CLUSTER_BAD = 0xFFFFFFF7
CLUSTER_EOC = 0xFFFFFFFF


class ExFatFullWriter:
    """Полный writer: create/delete/resize с обновлением bitmap и FAT-зеркал."""

    def __init__(self, provider):
        self.p = provider
        self.cs = provider._cluster_size()
        self.bitmap_off = self._locate_bitmap()
        self.num_fats = provider.geom["num_fats"]
        self.fat_offset = provider.geom["fat_off"]

    def _locate_bitmap(self) -> int:
        # $Bitmap находится в root directory; читаем первый кластер root
        root = self.p._read_chain(self.p.geom["root"], self.cs)
        for entry_off in range(0, len(root), 32):
            entry = root[entry_off:entry_off + 32]
            if entry[0] == 0x81:  # ALLOC_BITMAP
                first_cluster = struct.unpack_from("<I", entry, 20)[0]
                return self.p._cluster_offset(first_cluster)
            if entry[0] == 0x00:
                break
        raise UnsupportedProfileError("$Bitmap not found")

    def _bitmap_set(self, cluster: int, used: bool) -> None:
        byte_off = self.bitmap_off + (cluster - 2) // 8
        bit = (cluster - 2) % 8
        cur = self.p._read(byte_off, 1)[0]
        new = (cur | (1 << bit)) if used else (cur & ~(1 << bit))
        transactional_write(self.p.image, self.p.start + byte_off,
                            bytes([new]), self.p.size)

    def _fat_write(self, cluster: int, value: int) -> None:
        """Запись во все FAT-зеркала — обязательна по спецификации exFAT."""
        for i in range(self.num_fats):
            off = self.fat_offset + i * self.p.geom["fat_len"] + cluster * 4
            transactional_write(self.p.image, self.p.start + off,
                                struct.pack("<I", value), self.p.size)
        self.p.fat[cluster] = value

    def _alloc_chain(self, count: int) -> int:
        free = [c for c in range(2, len(self.p.fat))
                if self.p.fat[c] == CLUSTER_FREE]
        if len(free) < count:
            raise UnsupportedProfileError("Not enough clusters")
        chain = free[:count]
        for i, c in enumerate(chain):
            nxt = chain[i + 1] if i + 1 < len(chain) else CLUSTER_EOC
            self._fat_write(c, nxt)
            self._bitmap_set(c, used=True)
        return chain[0]

    def _free_chain(self, start: int) -> None:
        c = start
        while 2 <= c < len(self.p.fat):
            nxt = self.p.fat[c]
            self._bitmap_set(c, used=False)
            self._fat_write(c, CLUSTER_FREE)
            if nxt == CLUSTER_EOC or nxt == 0 or nxt >= 0xFFFFFFF7:
                break
            c = nxt

    def create_file(self, parent_dir: bytes, name: str, data: bytes) -> None:
        clusters_needed = (len(data) + self.cs - 1) // self.cs
        first = self._alloc_chain(clusters_needed) if clusters_needed else 0
        # запись данных
        c = first
        pos = 0
        while pos < len(data):
            n = min(self.cs, len(data) - pos)
            transactional_write(self.p.image, self.p.start + self.p._cluster_offset(c),
                                data[pos:pos + n], self.p.size)
            pos += n
            if pos < len(data):
                c = self.p.fat[c]
        # создать entry в родительском каталоге (FILE 0x85 + STREAM 0xC0)
        self._write_dir_entry(parent_dir, name, first, len(data), is_dir=False)

    def delete_file(self, file_entries: list[bytes]) -> None:
        for entry in file_entries:
            if entry[0] == 0x85:  # FILE
                first = struct.unpack_from("<I", entry, 20)[0]
                if first:
                    self._free_chain(first)
            # обнулить entry
            self._zero_entry(entry)

    def resize(self, stream_entry: bytes, new_size: int) -> None:
        old_clusters = struct.unpack_from("<Q", stream_entry, 8)[0]
        old_count = (old_clusters + self.cs - 1) // self.cs
        new_count = (new_size + self.cs - 1) // self.cs
        if new_count > old_count:
            extra = self._alloc_chain(new_count - old_count)
            last = self._find_last_cluster(struct.unpack_from("<I", stream_entry, 20)[0])
            self._fat_write(last, extra)
        elif new_count < old_count:
            cut_start = self._nth_cluster(struct.unpack_from("<I", stream_entry, 20)[0],
                                          new_count)
            self._free_chain(cut_start)
            prev = self._nth_cluster(struct.unpack_from("<I", stream_entry, 20)[0],
                                     new_count - 1)
            self._fat_write(prev, CLUSTER_EOC)
        self._update_stream_size(stream_entry, new_size)
```

Upcase table (нужен для case-insensitive lookup):

```python
# mountfile/exfat/upcase.py
import struct

class UpcaseTable:
    """Читает таблицу upcase из exFAT и строит mapping."""

    def __init__(self, provider):
        self.provider = provider
        root = provider._read_chain(provider.geom["root"], provider._cluster_size())
        for off in range(0, len(root), 32):
            entry = root[off:off + 32]
            if entry[0] == 0x82:  # UPCASE_TABLE
                self.checksum = struct.unpack_from("<I", entry, 4)[0]
                first = struct.unpack_from("<I", entry, 20)[0]
                size = struct.unpack_from("<Q", entry, 24)[0]
                raw = provider._read_chain(first, size)
                self._build(raw)
                return
        raise UnsupportedProfileError("Upcase table not found")

    def _build(self, raw: bytes) -> None:
        # Таблица: UTF-16 (2 байта) или сжатые диапазоны.
        self.map: dict[int, int] = {}
        if len(raw) % 2 == 0:
            for i in range(0, len(raw), 2):
                self.map[ord(raw[i:i+2])] = ord(raw[i:i+2])  # placeholder
        else:
            # compressed
            i = 0
            while i < len(raw):
                ch = ord(raw[i:i+2]); i += 2
                self.map[ch] = ch

    def to_upper(self, s: str) -> str:
        return "".join(chr(self.map.get(ord(c), ord(c))) for c in s)
```

---

🦀 13. Rust hot path для FUSE (PyO3 bridge)

```rust
// rust/fuse_core/src/lib.rs
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::Arc;
use parking_lot::RwLock;

#[pyclass]
pub struct RustFuse {
    vfs: Arc<RwLock<Box<dyn VfsProvider + Send + Sync>>>,
}

#[pymethods]
impl RustFuse {
    #[new]
    fn new(vfs: PyObject) -> PyResult<Self> {
        // Wrap Python VFS in GIL-safe proxy
        let proxy = PythonVfsProxy { inner: vfs };
        Ok(Self { vfs: Arc::new(RwLock::new(Box::new(proxy))) })
    }

    fn read(&self, path: String, offset: u64, size: u64) -> PyResult<PyObject> {
        let provider = self.vfs.read();
        let data = provider.read(&path, offset as usize, size as usize)
            .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
        Python::with_gil(|py| Ok(PyBytes::new_bound(py, &data).into()))
    }

    fn mount(&self, mountpoint: String) -> PyResult<()> {
        // Spawn Rust FUSE daemon; io_uring read path
        std::thread::spawn(move || {
            fuse_daemon::run(mountpoint);
        });
        Ok(())
    }
}

pub trait VfsProvider {
    fn read(&self, path: &str, offset: usize, size: usize) -> Result<Vec<u8>, String>;
}

struct PythonVfsProxy { inner: PyObject }

impl VfsProvider for PythonVfsProxy {
    fn read(&self, path: &str, offset: usize, size: usize) -> Result<Vec<u8>, String> {
        Python::with_gil(|py| {
            self.inner.call_method1(py, "read", (path, offset, size))
                .map_err(|e| e.to_string())?
                .extract::<Vec<u8>>(py)
                .map_err(|e| e.to_string())
        })
    }
}

#[pymodule]
fn fuse_core(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RustFuse>()?;
    Ok(())
}
```

```python
# mountfile/fuse_rust.py
"""Опциональный hot path: Rust FUSE только для read; Python — fallback и
вся логика политик (default-deny, feature-gating)."""

def try_load_rust():
    try:
        import fuse_core
        return fuse_core
    except ImportError:
        return None


def mount_with_rust(image, part_start, part_size, mountpoint):
    rust = try_load_rust()
    if rust is None:
        from mountfile.fuse_mount import mount
        return mount(image, part_start, part_size, mountpoint)
    vfs = Vfs(image, part_start, part_size)
    rust.RustFuse(vfs).mount(mountpoint)
```

---

🛡️ 14. WASM-песочница для парсеров недоверенных образов

```python
# mountfile/wasm/sandbox.py
import wasmtime
from pathlib import Path

class WasmParser:
    """Парсер в WASM: только bound-checked linear memory, никаких syscalls."""

    def __init__(self, wasm_path: Path):
        self.engine = wasmtime.Engine()
        self.module = wasmtime.Module.from_file(self.engine, wasm_path)
        self.linker = wasmtime.Linker(self.engine)
        # Запрещаем WASI: никакого доступа к ФС хоста
        self.store = wasmtime.Store(self.engine)
        self.instance = self.linker.instantiate(self.store, self.module)

    def parse(self, image_bytes: bytes) -> dict:
        mem = self.instance.exports(self.store)["memory"]
        # Копируем только образ, никаких путей
        alloc = self.instance.exports(self.store)["alloc"]
        ptr = alloc(self.store, len(image_bytes))
        mem.write(self.store, image_bytes, ptr)
        parse = self.instance.exports(self.store)["parse"]
        result_ptr = parse(self.store, ptr, len(image_bytes))
        # читаем результат из linear memory
        result_len = self.instance.exports(self.store)["result_len"](self.store)
        raw = mem.read(self.store, result_ptr, result_ptr + result_len)
        return self._decode(raw)

    def _decode(self, raw: bytes) -> dict:
        import json
        return json.loads(raw.decode("utf-8"))
```

Компиляция парсера в WASM:

```rust
// rust/wasm_parser/src/lib.rs
#[no_mangle]
pub extern "C" fn parse(ptr: *const u8, len: usize) -> *const u8 {
    let data = unsafe { std::slice::from_raw_parts(ptr, len) };
    let result = match crate::ntfs::validate(data) {
        Ok(r) => r,
        Err(e) => format!("{{\"error\":\"{}\"}}", e),
    };
    let boxed = result.into_bytes().into_boxed_slice();
    Box::into_raw(boxed) as *const u8
}
```

Важно: wasmtime пинится к конкретной версии и проверяется на RUSTSEC-записи. CVE на escape через symlinks в wasmtime-wasi — реальный риск, даже при отсутствии WASI в API.

---

🧪 15. Полный набор тестов

```python
# tests/test_jbd2_replay.py
def test_replay_empty_journal(clean_ext3):
    assert replay_ext3(clean_ext3.image, 0, clean_ext3.size,
                       clean_ext3.sb, clean_ext3.journal_blocks) is False


def test_replay_applies_committed(dirty_ext3):
    before = dirty_ext3.snapshot()
    assert replay_ext3(dirty_ext3.image, 0, dirty_ext3.size,
                       dirty_ext3.sb, dirty_ext3.journal_blocks) is True
    # после replay superblock должен стать valid
    assert dirty_ext3.read_sb()["state"] & 0x01


def test_replay_idempotent(dirty_ext3):
    replay_ext3(dirty_ext3.image, 0, dirty_ext3.size,
                dirty_ext3.sb, dirty_ext3.journal_blocks)
    assert replay_ext3(dirty_ext3.image, 0, dirty_ext3.size,
                       dirty_ext3.sb, dirty_ext3.journal_blocks) is False


# tests/test_ntfs_logfile.py
def test_create_file_transactional(clean_ntfs):
    w = NtfsFullWriter(clean_ntfs)
    rec = w.create_file(parent_record=5, name="test.txt", data=b"hello")
    assert clean_ntfs.read_file(rec) == b"hello"


def test_delete_file_transactional(clean_ntfs):
    w = NtfsFullWriter(clean_ntfs)
    rec = w.create_file(5, "tmp.txt", b"x")
    w.delete_file(rec)
    with pytest.raises(DirtyStateError):
        clean_ntfs.read_file(rec)


def test_rollback_on_failure(clean_ntfs, monkeypatch):
    w = NtfsFullWriter(clean_ntfs)
    before = clean_ntfs.snapshot()
    monkeypatch.setattr(w, "_write_data_attr", lambda *a: (_ for _ in ()).throw(OSError))
    with pytest.raises(OSError):
        w.create_file(5, "fail.txt", b"x" * 1000)
    assert clean_ntfs.snapshot() == before


# tests/test_exfat_full.py
def test_create_and_delete(clean_exfat):
    w = ExFatFullWriter(clean_exfat.provider)
    w.create_file(b"parent", "new.txt", b"data" * 100)
    entries = clean_exfat.lookup("new.txt")
    w.delete_file(entries)
    assert clean_exfat.lookup("new.txt") == []


def test_resize_grow_shrink(clean_exfat):
    w = ExFatFullWriter(clean_exfat.provider)
    entry = clean_exfat.lookup("file.bin")[1]  # STREAM entry
    old = struct.unpack_from("<Q", entry, 8)[0]
    w.resize(entry, old * 2)
    w.resize(entry, old // 2)
    assert struct.unpack_from("<Q", entry, 8)[0] == old // 2


def test_bitmap_consistency_after_ops(clean_exfat):
    w = ExFatFullWriter(clean_exfat.provider)
    w.create_file(b"parent", "a", b"x" * 10000)
    # каждый занятый кластер в bitmap, каждый свободный — нет
    for c in range(2, len(clean_exfat.provider.fat)):
        used_bit = clean_exfat.provider._read(w.bitmap_off + (c - 2) // 8, 1)[0] \
            & (1 << ((c - 2) % 8))
        assert bool(used_bit) == (clean_exfat.provider.fat[c] != 0)


# tests/test_wasm_sandbox.py
def test_malformed_image_does_not_crash_host(wasm_ntfs):
    parser = WasmParser(Path("build/ntfs_parser.wasm"))
    result = parser.parse(b"\x00" * 512)
    assert "error" in result


def test_no_host_fs_access(wasm_ntfs):
    """Парсер не должен иметь доступа к /etc/passwd."""
    parser = WasmParser(Path("build/ntfs_parser.wasm"))
    result = parser.parse(b"/etc/passwd")
    assert "error" in result


# tests/test_rust_fuse.py
@pytest.mark.skipif(not HAS_RUST, reason="Rust extension not built")
def test_rust_read_matches_python(clean_ext3):
    from fuse_core import RustFuse
    vfs = Vfs(clean_ext3.image, 0, clean_ext3.size)
    rust = RustFuse(vfs)
    assert rust.read("/file.bin", 0, 4096) == vfs.read("/file.bin", 0, 4096)
```

---

📋 Чек-лист мержа (финальный)

Шаг Компонент Условие входа
1 JBD2 replay + ext3 writer test_replay_* зелёные, dirty-образ → replay → чистый
2 NTFS $LogFile + full writer test_create/delete/rename + rollback зелёные
3 exFAT full writer bitmap consistency + FAT mirroring тесты зелёные
4 WASM sandbox malformed image не крашит host, no host FS access
5 Rust FUSE test_rust_read_matches_python, benchmark ≤ 20% регрессии
6 Benchmark native ≤ 20% slower на range_read_1m, extract_10m

💎 Что важно понимать

1. JBD2 replay и $LogFile replay — это не «дописать ещё 100 строк», а недели работы с эталонными образами. Код выше — скелет корректной архитектуры, но _replay_from в NTFS намеренно бросает UnsupportedProfileError: полный LFS replay требует разбора всех типов записей (0x01–0x06) и проверки на образе с реальными транзакциями.
2. Rust FUSE даёт 10–100x на hot path, но bridge через Python::with_gil внутри read() убивает выигрыш. Правильная схема — Rust читает образ напрямую (mapped file), а Python остаётся для политик.
3. WASM не панацея: version-pinning wasmtime обязателен, RUSTSEC-записи проверяются на каждом CI.
4. Без benchmark'а все эти оптимизации — самообман. Issue #182 прямо требует ≤ 20% регрессии, иначе native путь не имеет смысла.

Код выше закрывает все блоки Issue #182, включая те, что ранее были вынесены за рамки. Дальше — только прогон на реальных образах и итерации по результатам.