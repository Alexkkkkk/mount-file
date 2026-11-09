"""Regression tests for actual entry points and standalone helpers."""

from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import sys

import pytest

import mountfile_all
import mountfile_dfir
import mountfile_grep
import mountfile_rw_complete
import mountfile_rw_v2
from mountfile.rw import complete, v2


@pytest.mark.parametrize("module", [complete, v2])
def test_console_entrypoint_uses_sys_argv_when_argv_is_omitted(module, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["installed-command", "--help"])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 0


@pytest.mark.parametrize("module", [mountfile_rw_complete, mountfile_rw_v2])
def test_top_level_console_wrapper_uses_sys_argv_when_argv_is_omitted(
    module, monkeypatch
):
    monkeypatch.setattr(sys, "argv", ["installed-command", "--help"])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 0


@pytest.mark.parametrize("code", [1, 2, 127, 130])
def test_all_launcher_propagates_failed_child_exit(code, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mountfile_all", "--diag"])
    monkeypatch.setattr(
        mountfile_all.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], code),
    )
    assert mountfile_all.main() == (130 if code == 130 else 1)


def test_launcher_resets_prior_failures_between_runs(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mountfile_all", "--diag"])
    outcomes = iter([1, 0])
    monkeypatch.setattr(
        mountfile_all.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], next(outcomes)),
    )
    assert mountfile_all.main() == 1
    assert mountfile_all.main() == 0


def test_launcher_records_checked_subprocess_failures(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(7, args[0])

    monkeypatch.setattr(mountfile_all.subprocess, "run", fail)
    mountfile_all._RUN_FAILURES.clear()
    assert mountfile_all.run_cli(["missing"], check=True) == 7
    assert mountfile_all._exit_status() == 1
    mountfile_all._RUN_FAILURES.clear()


def test_standalone_grep_utf16_offsets_include_surrogate_pairs(tmp_path):
    image = tmp_path / "unicode.img"
    image.write_bytes("\U0001f600needle\U0001f600needle".encode("utf-16-le"))
    hits = list(mountfile_grep.search_content(image, "needle", unicode_mode=True))
    assert [hit["offset"] for hit in hits] == [4, 20]


def test_standalone_grep_deduplicates_chunk_overlap(tmp_path, monkeypatch):
    image = tmp_path / "overlap.img"
    image.write_bytes(b"needle___needle___needle")
    monkeypatch.setattr(mountfile_grep, "CHUNK_SIZE", 7)
    hits = list(mountfile_grep.search_content(image, "needle"))
    assert [hit["offset"] for hit in hits] == [0, 9, 18]


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_standalone_grep_rejects_invalid_limits(tmp_path, limit):
    with pytest.raises(ValueError):
        list(
            mountfile_grep.search_content(tmp_path / "missing", "x", max_results=limit)
        )


def test_standalone_grep_zero_limit_does_not_open_image(tmp_path):
    assert (
        list(mountfile_grep.search_content(tmp_path / "missing", "x", max_results=0))
        == []
    )


def test_standalone_timeline_db_accepts_threaded_api_reads(tmp_path):
    with mountfile_dfir.TimelineDB(tmp_path / "timeline.db") as database:
        assert database.add([mountfile_dfir.TimelineRecord("a.txt", mtime=42)]) == 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: database.query(limit=1), range(12)))
        assert all(rows[0]["path"] == "a.txt" for rows in results)
        for limit in (0, 10001, True):
            with pytest.raises(ValueError):
                database.query(limit=limit)


def test_standalone_timeline_rest_uses_thread_safe_connection_and_closes_it(
    tmp_path, monkeypatch
):
    fastapi = pytest.importorskip("fastapi.testclient")
    uvicorn = pytest.importorskip("uvicorn")
    tracked = []
    database_class = mountfile_dfir.TimelineDB

    class Database(database_class):
        def __init__(self, path):
            super().__init__(path)
            self.add([mountfile_dfir.TimelineRecord("note.txt", size=3)])
            tracked.append(self)

    def run(app, **kwargs):
        with fastapi.TestClient(app) as client:
            response = client.get("/timeline")
            assert response.status_code == 200
            assert response.json()[0]["path"] == "note.txt"
            assert client.get("/timeline?limit=0").status_code == 422

    monkeypatch.setattr(mountfile_dfir, "TimelineDB", Database)
    monkeypatch.setattr(uvicorn, "run", run)
    mountfile_dfir.cmd_serve(
        argparse.Namespace(host="127.0.0.1", port=8000, db=tmp_path / "api.db")
    )
    with pytest.raises(Exception, match="closed database"):
        tracked[0].query()


def test_standalone_timeline_rest_refuses_nonloopback_host(tmp_path):
    with pytest.raises(SystemExit, match="non-loopback"):
        mountfile_dfir.cmd_serve(
            argparse.Namespace(host="0.0.0.0", port=8000, db=tmp_path / "api.db")
        )
    assert not (tmp_path / "api.db").exists()
