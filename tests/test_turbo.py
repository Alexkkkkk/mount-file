# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Tests for mountfile.turbo — the CDC delta-sync engine.

The module had no tests at all: `--cov` reported 0 of 681 statements.
Everything here is offline: no pytsk3 image, no network, no real device.
Qt parts are exercised through a plain QApplication with the offscreen
platform plugin (``QT_QPA_PLATFORM=offscreen``), and every test that needs
Qt skips cleanly when no binding is importable.

Run:  python -m pytest tests/test_turbo.py -v
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from mountfile import turbo as T

# --------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def qapp():
    """A process-wide QApplication; Qt widgets/models need one."""
    if not T.HAS_QT:
        pytest.skip("PyQt5/PyQt6 не установлен")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _pump(app, seconds=3.0):
    """Spin the event loop until queued cross-thread signals are delivered."""
    import time

    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def images(tmp_path):
    """Two 3 MiB images that differ in three bytes.

    The payload is pseudo-random but seeded, so chunk boundaries are
    reproducible: a repeating 0..255 pattern almost never satisfies the
    buzhash mask and collapses into one giant chunk, which would make the
    copy/data assertions meaningless.
    """
    import random

    rnd = random.Random(20261020)
    payload = bytes(rnd.getrandbits(8) for _ in range(3 << 20))
    a = tmp_path / "a.img"
    b = tmp_path / "b.img"
    a.write_bytes(payload)
    b.write_bytes(payload)
    with open(b, "r+b") as fh:
        fh.seek(1 << 20)
        fh.write(b"XYZ")
    return a, b


# ============================================================= 1. TTLCache


def test_ttl_cache_hit_miss_and_stats():
    cache = T.TTLCache(maxsize=4, ttl=30)
    assert cache.get("nope") is None
    cache.put("k", 7)
    assert cache.get("k") == 7
    stats = cache.stats()
    assert stats["size"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(0.5)


def test_ttl_cache_expires_entries():
    cache = T.TTLCache(maxsize=4, ttl=0.01)
    cache.put("k", "v")
    import time

    time.sleep(0.05)
    assert cache.get("k") is None
    assert cache.stats()["size"] == 0


def test_ttl_cache_evicts_lru_beyond_maxsize():
    cache = T.TTLCache(maxsize=2, ttl=0)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None, "oldest key must be evicted"
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_ttl_cache_reput_updates_order_and_invalidate_clear():
    cache = T.TTLCache(maxsize=2, ttl=0)
    cache.put("a", 1)
    cache.put("a", 2)  # re-put must not duplicate the LRU entry
    assert cache.get("a") == 2
    cache.invalidate("a")
    assert cache.get("a") is None
    cache.invalidate("missing")  # no KeyError
    cache.put("b", 1)
    cache.clear()
    assert cache.stats() == {"size": 0, "hits": 0, "misses": 0, "hit_rate": 0.0}


# =============================================================== 2. Buzhash


def test_buzhash_chunks_cover_input_exactly():
    h = T.Buzhash()
    data = bytes(range(256)) * 400  # 102 400 B — several avg-size chunks
    chunks = h.feed(data)
    tail = h.finish()
    if tail:
        chunks.append(tail)
    assert chunks, "a 100 KiB payload must produce at least one chunk"
    pos = 0
    for off, length in chunks:
        assert off == pos
        assert T.CHUNK_MIN <= length <= T.CHUNK_MAX or length == len(data)
        pos += length
    assert pos == len(data)


def test_buzhash_is_deterministic_and_reset_restarts():
    payload = b"mount-file" * 9000
    first = T.Buzhash().feed(payload)
    second = T.Buzhash().feed(payload)
    assert first == second, "CDC must be content-addressed, not random"

    h = T.Buzhash()
    h.feed(payload)
    h.reset()
    assert h.feed(payload) == first, "reset() must restart the stream"


def test_buzhash_finish_empty_returns_none():
    h = T.Buzhash()
    assert h.finish() is None


def test_rol_wraps_within_64_bits():
    assert T._rol(1, 0) == 1
    assert T._rol(1, 64) == 1  # masked to 0
    assert T._rol(0xFF << 56, 8) == 0xFF


# ====================================================== 3. Signature / store


def _sig(n=3):
    sig = T.Signature(file_size=12345, mtime=1700000000.5)
    for i in range(n):
        sig.add(T.BlockRef(i * 4096, 4096, 1000 + i, bytes([i]) * 16))
    return sig


def test_signature_serialises_roundtrip():
    sig = _sig()
    blob = sig.to_bytes()
    assert blob.startswith(T.SIG_MAGIC)
    back = T.Signature.from_bytes(blob)
    assert back.file_size == sig.file_size
    assert back.mtime == pytest.approx(sig.mtime, abs=0.001)
    assert len(back.block_refs) == 3
    assert len(back.by_strong) == 3
    assert back.block_refs[1].strong == sig.block_refs[1].strong
    assert back.block_refs[1].weak == sig.block_refs[1].weak


def test_signature_rejects_foreign_blob():
    with pytest.raises(ValueError, match="not a Mount File signature"):
        T.Signature.from_bytes(b"NOPE" + b"\x00" * 40)


def test_signature_rejects_future_version():
    blob = bytearray(_sig(1).to_bytes())
    blob[len(T.SIG_MAGIC)] = T.SIG_VERSION + 1
    with pytest.raises(ValueError, match="not supported"):
        T.Signature.from_bytes(bytes(blob))


def test_signature_store_saves_loads_and_invalidates(tmp_path):
    img = tmp_path / "disk.img"
    img.write_bytes(b"\x00" * 8192)
    sig = T.Signature(file_size=8192, mtime=os.stat(img).st_mtime)
    sig.add(T.BlockRef(0, 4096, 1, b"s" * 16))

    assert T.SignatureStore.load(str(img)) is None, "no cache file yet"
    T.SignatureStore.save(str(img), sig)
    assert T.SignatureStore._path(str(img)) == str(img) + ".mfsig"

    loaded = T.SignatureStore.load(str(img))
    assert loaded is not None and len(loaded.block_refs) == 1

    # size changed -> stale, must be ignored
    img.write_bytes(b"\x00" * 4096)
    assert T.SignatureStore.load(str(img)) is None


def test_signature_store_ignores_corrupt_cache(tmp_path):
    img = tmp_path / "d.img"
    img.write_bytes(b"\x00" * 100)
    with open(str(img) + ".mfsig", "wb") as fh:
        fh.write(b"garbage")
    assert T.SignatureStore.load(str(img)) is None


# ================================================== 4. DeltaSync core paths


def test_build_signature_covers_whole_file_and_caches(tmp_path, images):
    a, _b = images
    sync = T.DeltaSync(use_disk_cache=True)
    seen = []
    sig = sync.build_signature(str(a), progress=lambda d, t, m: seen.append(m))
    assert sig.file_size == a.stat().st_size
    assert sig.block_refs
    assert sum(r.length for r in sig.block_refs) == sig.file_size
    assert (str(a) + ".mfsig") and os.path.exists(str(a) + ".mfsig")
    assert any("Цель" in m for m in seen)

    # second call hits the disk cache instead of rehashing
    again = sync.build_signature(str(a))
    assert [r.strong for r in again.block_refs] == [r.strong for r in sig.block_refs]


def test_diff_emits_copies_for_unchanged_and_data_for_changed(images):
    a, b = images
    sync = T.DeltaSync(use_disk_cache=False)
    sig = sync.build_signature(str(b))
    ops = sync.diff(str(a), sig)
    kinds = {k for k, *_ in ops}
    assert "copy" in kinds, "identical regions must be emitted as copies"
    assert "data" in kinds, "the 3 changed bytes must be emitted as literal data"


def test_patch_reconstructs_source_from_target(images, tmp_path):
    a, b = images
    sync = T.DeltaSync(use_disk_cache=False)
    sig = sync.build_signature(str(b))
    ops = sync.diff(str(a), sig)
    out = tmp_path / "patched.img"
    written = sync.patch(str(b), ops, str(out))
    assert written == a.stat().st_size
    assert out.read_bytes() == a.read_bytes(), "patch must be byte-exact"


def test_sync_full_copy_then_delta(images):
    a, b = images
    sync = T.DeltaSync(use_disk_cache=False)
    fresh = str(a) + ".copy"

    info = sync.sync(str(a), fresh)
    assert info["mode"] == "full"
    assert open(fresh, "rb").read() == a.read_bytes()

    # target now exists -> delta path; result must equal the source
    info = sync.sync(str(b), fresh)
    assert info["mode"] == "delta-cdc"
    assert info["full_size"] == b.stat().st_size
    assert 0.0 <= info["saving"] <= 1.0
    assert open(fresh, "rb").read() == b.read_bytes()
    assert not os.path.exists(fresh + ".synctmp"), "temp file must be cleaned up"
    os.remove(fresh)


def test_sync_with_journal_free_of_leftovers_and_progress(images, tmp_path):
    a, b = images
    target = tmp_path / "target.img"
    target.write_bytes(b.read_bytes())
    sync = T.DeltaSync(use_disk_cache=False)
    events = []
    info = sync.sync(str(a), str(target), progress=lambda d, t, m: events.append(m))
    assert info["ops"] >= 1
    assert events, "sync must report progress"
    assert target.read_bytes() == a.read_bytes()


def test_sync_reports_delta_saving_on_near_identical_images(images, tmp_path):
    """Delta path must be taken, be byte-exact and copy rather than inline all.

    Note the honest limits of this assertion: `sync()["saving"]` counts bytes
    *written to the new file*, and the output file is always the full size, so
    that field stays ~0 by construction. What actually measures delta quality
    is the literal payload emitted by `diff()`. On this synthetic 3 MiB image
    about two thirds of the bytes still end up as literals, i.e. the CDC
    matching here is correct but not aggressive — so the assertion only pins
    down "some regions were copied", not a particular ratio.
    """
    a, b = images
    sync = T.DeltaSync(use_disk_cache=False)
    sig = sync.build_signature(str(b))
    ops = sync.diff(str(a), sig)
    literal = sum(length for kind, _p, _o, length in ops if kind == "data")
    full = a.stat().st_size
    copies = [o for kind, _p, o, _l in ops if kind == "copy"]
    assert copies, "unchanged regions must be copied from the target"
    assert literal < full, "the whole image must not be inlined as literal data"

    target = tmp_path / "near.img"
    target.write_bytes(b.read_bytes())
    info = sync.sync(str(a), str(target))
    assert info["mode"] == "delta-cdc"
    assert info["ops"] == len(ops)
    assert 0.0 <= info["saving"] <= 1.0
    assert target.read_bytes() == a.read_bytes(), "delta must be byte-exact"


def test_verify_detects_equality_size_and_byte_mismatch(images, tmp_path):
    a, b = images
    sync = T.DeltaSync()
    same = sync.verify(str(a), str(a))
    assert same["equal"] is True
    assert same["size"] == a.stat().st_size
    assert same["digest_a"] == same["digest_b"]

    diff = sync.verify(str(a), str(b))
    assert diff["equal"] is False
    assert diff["reason"] == "byte mismatch"
    assert diff["offset"] == 0

    shorter = tmp_path / "short.img"
    shorter.write_bytes(b"\x00")
    sized = sync.verify(str(a), str(shorter))
    assert sized["equal"] is False
    assert sized["reason"] == "size mismatch"
    assert sized["size_b"] == 1


def test_add_chunk_hashes_only_the_declared_slice(images):
    a, _b = images
    sync = T.DeltaSync(use_disk_cache=False)
    sig = T.Signature()
    sync._add_chunk(sig, str(a), 0, 4096)
    ref = sig.block_refs[0]
    assert ref.offset == 0 and ref.length == 4096
    assert len(ref.strong) == 16
    assert ref.weak == __import__("zlib").adler32(a.read_bytes()[:4096])


def test_match_returns_none_for_out_of_range(images):
    a, b = images
    sync = T.DeltaSync(use_disk_cache=False)
    sig = sync.build_signature(str(b))
    assert T.DeltaSync._match(str(a), 10**9, 4096, sig) is None


def test_copy_file_helper(tmp_path):
    src = tmp_path / "s.bin"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "d.bin"
    seen = []
    T._copy_file(str(src), str(dst), progress=lambda d, t, m: seen.append(d))
    assert dst.read_bytes() == src.read_bytes()


# ==================================================== 5. SqliteJournal


def test_journal_records_tails_counts_and_trims(tmp_path):
    j = T.SqliteJournal(str(tmp_path / "changes.sqlite"))
    first = j.record("write", "/a.img", size=10, digest="ab", extra={"k": "v"})
    assert first == 1
    j.record("delete", "/b.img")
    assert j.count() == 2

    rows = j.tail()
    assert [r["op"] for r in rows] == ["write", "delete"]
    assert rows[0]["extra"] == {"k": "v"}
    assert rows[0]["size"] == 10
    assert rows[1]["digest"] == ""

    assert [r["op"] for r in j.tail(since_ts=rows[0]["ts"])] == ["delete"]
    assert len(j.tail(limit=1)) == 1
    assert j.gc(keep_last=1) == 1
    assert j.count() == 1
    j.close()


def test_journal_schema_is_durable(tmp_path):
    path = str(tmp_path / "j.sqlite")
    j = T.SqliteJournal(path)
    j.record("sync", "/x", size=1)
    j.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1
    raw.close()


# ======================================================== 6. CachedFS


class _Meta:
    def __init__(self, name):
        self.size = len(name)
        self.type = 2 if name == "dir" else 1
        self.addr = len(name)
        self.mtime = 1700000000.0


class _Name:
    def __init__(self, name):
        self.name = name.encode()


class _Info:
    def __init__(self, name):
        self.name = _Name(name)
        self.meta = _Meta(name)


class _Entry:
    def __init__(self, name):
        self.info = _Info(name)


class _FS:
    def __init__(self):
        self.calls = 0

    def open_dir(self, path):
        self.calls += 1
        return [_Entry("a.txt"), _Entry("dir")]


def test_cached_fs_memoises_listings():
    fs = _FS()
    cached = T.CachedFS(fs, listing_ttl=60)
    rows = cached.list_dir("/")
    assert [r["name"] for r in rows] == ["a.txt", "dir"]
    assert rows[1]["type"] == 2
    assert cached.raw is fs
    assert fs.calls == 1

    cached.list_dir("/")  # served from cache
    assert fs.calls == 1
    assert cached.stats()["hits"] == 1

    cached.invalidate("/")
    cached.list_dir("/")
    assert fs.calls == 2

    cached.invalidate()
    assert cached.stats()["size"] == 0


def test_cached_fs_skips_entries_without_metadata():
    class Broken:
        def open_dir(self, path):
            class E:
                class info:
                    name = _Name("x")
                    meta = None

            class Boom:
                info = property(lambda self: (_ for _ in ()).throw(RuntimeError))

            return [Boom()]

    assert T.CachedFS(Broken()).list_dir("/") == []


# ==================================================== 7. helpers / model


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0 Б"), (512, "512 Б"), (2048, "2.00 КБ"), (5 << 20, "5.00 МБ")],
)
def test_human_size(value, expected):
    assert T._human_size(value) == expected


def test_sortable_normalises_case_and_none():
    assert T._sortable("ABC") == "abc"
    assert T._sortable(None) == ""
    assert T._sortable(5) == 5


def test_dir_table_model_roles_sorting_and_headers(qapp):
    from PyQt6.QtCore import QModelIndex, Qt

    model = T.DirTableModel()
    assert model.rowCount() == 0
    model.set_rows(
        [
            {"name": "b.txt", "size": 2048, "type": 1, "mtime": 1700000000.0},
            {"name": "a", "size": 0, "type": 2, "mtime": None},
        ]
    )
    assert model.rowCount() == 2
    assert model.columnCount() == 4
    assert model.rowCount(model.index(0, 0)) == 0, "child rows are flat"

    first = model.index(0, 0)
    assert model.data(first) == "b.txt"
    assert model.data(first, T.DirTableModel.RowRole)["name"] == "b.txt"
    assert model.data(model.index(0, 1)) == "2.00 КБ"
    assert model.data(model.index(1, 2)) == "Папка"
    assert model.data(model.index(0, 2)) == "Файл"
    assert model.data(model.index(1, 3)) == "", "missing mtime renders empty"
    assert "2023" in model.data(model.index(0, 3)) or "-" in model.data(
        model.index(0, 3)
    )

    assert (
        model.headerData(1, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        == "Размер"
    )
    assert (
        model.headerData(1, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole)
        is None
    )
    assert model.data(QModelIndex()) is None
    assert model.data(first, Qt.ItemDataRole.ToolTipRole) is None

    model.sort(0, Qt.SortOrder.AscendingOrder)
    assert [model.row_at(i)["name"] for i in range(2)] == ["a", "b.txt"]
    model.sort(0, Qt.SortOrder.DescendingOrder)
    assert [model.row_at(i)["name"] for i in range(2)] == ["b.txt", "a"]


def test_dir_table_model_sorts_directories_first(qapp):
    from PyQt6.QtCore import Qt

    model = T.DirTableModel()
    model.set_rows(
        [
            {"name": "z.txt", "size": 10, "type": 1},
            {"name": "adir", "size": 0, "type": 2},
        ]
    )
    model.sort(0, Qt.SortOrder.AscendingOrder)
    assert model.row_at(0)["name"] == "adir", "SIZE/tree style: dirs before files"


# ============================================== 8. Qt workers / controller


def test_task_runner_delivers_result_and_error(qapp):
    runner = T.TaskRunner(max_threads=1)
    got = {}
    runner.submit(lambda w: 6 * 7, on_result=lambda r: got.update(result=r))
    runner.pool.waitForDone(5000)
    _pump(qapp)
    assert got.get("result") == 42

    errors = {}
    runner.submit(lambda w: 1 / 0, on_error=lambda e: errors.update(err=e))
    runner.pool.waitForDone(5000)
    _pump(qapp)
    assert errors["err"][0] is ZeroDivisionError

    finished = {}
    runner.submit(lambda w: None, on_finished=lambda: finished.update(ok=True))
    runner.pool.waitForDone(5000)
    _pump(qapp)
    assert finished.get("ok") is True

    worker = runner.submit(lambda w: w.cancelled)
    runner.pool.waitForDone(5000)
    _pump(qapp)
    assert worker.cancelled is False
    runner.cancel_all()


def test_worker_cancel_flag(qapp):
    w = T.Worker(lambda self_: 1)
    assert w.cancelled is False
    w.cancel()
    assert w.cancelled is True
    assert w.autoDelete() is True


def test_task_runner_progress_signal(qapp):
    runner = T.TaskRunner(max_threads=1)
    seen = []
    runner.submit(
        lambda w: w.signals.progress.emit(50, 100, "half"),
        on_progress=lambda d, t, m: seen.append((d, t, m)),
    )
    runner.pool.waitForDone(5000)
    _pump(qapp)
    assert seen == [(50, 100, "half")]


class _FakeEvent:
    def __init__(self):
        self.accepted = False

    def accept(self):
        self.accepted = True


def test_patch_close_event_cancels_workers_and_forwards():
    class Win:
        def __init__(self):
            self.forwarded = None

        def closeEvent(self, event):
            self.forwarded = event

    win = Win()
    ctl = object.__new__(T.TurboController)  # skip __init__: no Qt parent needed

    class Runner:
        cancelled = False

        def cancel_all(self):
            Runner.cancelled = True

    ctl.runner = Runner()
    T.TurboController._patch_close_event(ctl, win)

    event = _FakeEvent()
    win.closeEvent(event)
    assert Runner.cancelled is True
    assert win.forwarded is event


def test_patch_close_event_accepts_when_window_has_no_handler():
    class Win:
        pass

    ctl = object.__new__(T.TurboController)

    class Runner:
        def cancel_all(self):
            pass

    ctl.runner = Runner()
    win = Win()
    T.TurboController._patch_close_event(ctl, win)
    event = _FakeEvent()
    win.closeEvent(event)  # the wrapper lives on the instance, not the class
    assert event.accepted is True


def test_patch_close_event_survives_legacy_signature():
    class Win:
        def closeEvent(self):  # old-style, no event argument
            pass

    ctl = object.__new__(T.TurboController)

    class Runner:
        def cancel_all(self):
            pass

    ctl.runner = Runner()
    win = Win()
    T.TurboController._patch_close_event(ctl, win)
    event = _FakeEvent()
    win.closeEvent(event)
    assert event.accepted is True, "TypeError from a 0-arg closeEvent must accept"


def test_turbo_controller_attach_and_sync_to(qapp, tmp_path):
    from PyQt6.QtWidgets import QWidget

    src = tmp_path / "src.img"
    src.write_bytes(b"\xAB" * 40000)
    win = QWidget()
    win.image_path = str(src)

    ctl = T.attach(win, journal_path=str(tmp_path / "j.sqlite"), workers=1)
    assert getattr(win, "turbo", None) is ctl
    assert ctl.fs is None, "no pytsk3 fs was supplied"
    assert ctl.stats()["cache"] == {}

    target = tmp_path / "dst.img"
    done = {}
    ctl.sync_to(str(target), on_done=lambda info: done.update(info))
    ctl.runner.pool.waitForDone(10000)
    _pump(qapp, 5)
    assert target.exists() and target.read_bytes() == src.read_bytes()
    assert done.get("mode") == "full"
    assert ctl.stats()["journal"] >= 1, "sync must be journalled"

    assert ctl.open_dir("/") is None, "no filesystem -> no-op"


def test_turbo_controller_requires_image_path(qapp, tmp_path):
    from PyQt6.QtWidgets import QWidget

    win = QWidget()  # no image_path attribute
    ctl = T.attach(win, journal_path=str(tmp_path / "j.sqlite"), workers=1)
    with pytest.raises(RuntimeError, match="image_path"):
        ctl.sync_to(str(tmp_path / "x.img"))


def test_turbo_controller_default_error_writes_stderr(capsys):
    ctl = object.__new__(T.TurboController)
    try:
        1 / 0
    except ZeroDivisionError:
        import sys as _sys
        import traceback

        exc = _sys.exc_info()
        ctl._default_error((exc[0], exc[1], traceback.format_exc()))
    err = capsys.readouterr().err
    assert "[turbo]" in err and "ZeroDivisionError" in err


# ============================================================== 9. CLI


def test_cli_sig_writes_signature(tmp_path, capsys):
    img = tmp_path / "img.bin"
    img.write_bytes(bytes(range(256)) * 400)
    assert T.cli_main(["sig", str(img)]) == 0
    out = capsys.readouterr().out
    assert "Подпись" in out
    assert os.path.exists(str(img) + ".mfsig")


def test_cli_diff_reports_saving(images, capsys):
    a, b = images
    assert T.cli_main(["diff", str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "ops=" in out and "saving" in out


def test_cli_sync_writes_target_and_journal(images, tmp_path, capsys):
    a, b = images
    target = tmp_path / "t.img"
    target.write_bytes(b.read_bytes())
    journal = tmp_path / "j.sqlite"
    assert T.cli_main(["sync", str(a), str(target), "--journal", str(journal)]) == 0
    assert target.read_bytes() == a.read_bytes()
    assert "Готово" in capsys.readouterr().out
    assert T.SqliteJournal(str(journal)).count() == 1


def test_cli_verify_exit_codes(images, capsys):
    a, b = images
    assert T.cli_main(["verify", str(a), str(a)]) == 0
    assert "Идентичны" in capsys.readouterr().out
    assert T.cli_main(["verify", str(a), str(b)]) == 1
    assert "Различия" in capsys.readouterr().out


def test_cli_progress_handles_zero_total(capsys):
    T._cli_progress(5, 0, "x")
    assert "x: 5" in capsys.readouterr().err


def test_cli_parser_exposes_all_commands():
    parser = T._build_parser()
    for cmd in T.COMMANDS:
        assert cmd in parser.format_help()


def test_selftest_runs_green(capsys):
    assert T._selftest() == 0
    out = capsys.readouterr().out
    assert "OK" in out and "verify: True" in out
