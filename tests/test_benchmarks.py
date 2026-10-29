# SPDX-License-Identifier: MIT
"""Tests for the read-only, repeatable VFS benchmark harness."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from benchmarks import bench_vfs
from mountfile.ext2_native import NativeExt2ImageWriter
from mountfile.ext2_reader import NativeExt2Image
from mountfile.vfs_api import ReadOnlyVFSStatMixin
from scripts import make_bench_ext2_image


class _FakeProvider(ReadOnlyVFSStatMixin):
    backend_name = "native-test-ro"

    def __init__(self):
        self.profile = type("Profile", (), {"fs_type": "fixture", "block_size": 4096})()
        self.closed = False

    def list_dir(self, path):
        if path == "/":
            return [{"name": "docs", "is_dir": True, "size": 0, "date": ""}]
        assert path == "/docs"
        return [{"name": "readme.txt", "is_dir": False, "size": 5}]

    def file_size(self, path):
        assert path == "/docs/readme.txt"
        return 5

    def read_file_range(self, path, offset=0, length=1 << 20):
        assert path == "/docs/readme.txt"
        return b"hello"[offset : offset + length]

    def close(self):
        self.closed = True


def test_validate_paths_rejects_relative_and_traversal_paths():
    with pytest.raises(ValueError, match="absolute"):
        bench_vfs._validate_paths(["docs/readme.txt"])
    with pytest.raises(ValueError, match="traversal"):
        bench_vfs._validate_paths(["/docs/../secret"])
    with pytest.raises(ValueError, match="unique"):
        bench_vfs._validate_paths(["/docs/readme.txt", "/docs/readme.txt"])


def test_build_report_records_repeated_metrics_and_does_not_modify_image(
    tmp_path, monkeypatch
):
    image = tmp_path / "sample.img"
    original = b"immutable image bytes"
    image.write_bytes(original)
    providers = []

    def open_fake(path, offset=0, volume_size=None):
        assert Path(path) == image.resolve()
        assert offset == 0
        assert volume_size == len(original)
        provider = _FakeProvider()
        providers.append(provider)
        return provider

    monkeypatch.setattr(bench_vfs, "open_image_provider", open_fake)
    report = bench_vfs.build_report(
        image,
        0,
        len(original),
        ["/docs/readme.txt"],
        baseline="native",
        repeats=3,
        warmups=1,
        cache_state="warm",
        hash_image=True,
    )

    assert report["schema_version"] == 2
    assert report["image"]["size_bytes"] == len(original)
    assert report["image"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert report["cache_state"]["managed_by_harness"] is False
    result = report["backends"]["native"]
    assert result["backend"] == "native-test-ro"
    assert result["fs_type"] == "fixture"
    assert result["block_size"] == 4096
    assert len(result["iterations"]) == 3
    assert result["operations"]["stat"]["calls_per_iteration"] == 1
    assert result["operations"]["read_range"]["samples"] == 3
    assert result["iterations"][0]["range_read_bytes"] == 5
    assert result["iterations"][0]["extracted_bytes"] == 5
    assert all(provider.closed for provider in providers)
    assert image.read_bytes() == original
    json.dumps(report)


def test_build_report_rejects_extent_outside_image(tmp_path):
    image = tmp_path / "sample.img"
    image.write_bytes(b"1234")
    with pytest.raises(ValueError, match="exceeds image bounds"):
        bench_vfs.build_report(image, 1, 4, ["/file"], baseline="native")


def test_build_report_compares_backends_only_when_both_succeed(tmp_path, monkeypatch):
    image = tmp_path / "sample.img"
    image.write_bytes(b"1234")

    def open_native(path, offset=0, volume_size=None):
        return _FakeProvider()

    def open_pytsk(path, offset=0):
        provider = _FakeProvider()
        provider.backend_name = "pytsk3-test"
        return provider

    monkeypatch.setattr(bench_vfs, "open_image_provider", open_native)
    monkeypatch.setattr(bench_vfs, "TskImage", open_pytsk)
    report = bench_vfs.build_report(
        image, 0, 4, ["/docs/readme.txt"], baseline="both", repeats=1, warmups=0
    )

    assert report["comparison"]["baseline"] == "pytsk3"
    assert report["comparison"]["content_equivalent"] is True
    assert report["comparison"]["target_max_regression_percent"] == 20
    assert isinstance(report["comparison"]["relative_change_percent"], float)


def test_both_backends_are_interleaved_and_execution_order_is_recorded(
    tmp_path, monkeypatch
):
    image = tmp_path / "sample.img"
    image.write_bytes(b"1234")
    execution = []
    real_iteration = bench_vfs._iteration

    def open_native(path, offset=0, volume_size=None):
        provider = _FakeProvider()
        provider.backend_name = "native-test"
        return provider

    def open_pytsk(path, offset=0):
        provider = _FakeProvider()
        provider.backend_name = "pytsk3-test"
        return provider

    def record_iteration(provider, paths):
        execution.append(provider.backend_name)
        return real_iteration(provider, paths)

    monkeypatch.setattr(bench_vfs, "open_image_provider", open_native)
    monkeypatch.setattr(bench_vfs, "TskImage", open_pytsk)
    monkeypatch.setattr(bench_vfs, "_iteration", record_iteration)
    report = bench_vfs.build_report(
        image,
        0,
        4,
        ["/docs/readme.txt"],
        baseline="both",
        repeats=3,
        warmups=1,
    )

    assert execution == [
        "native-test",
        "pytsk3-test",
        "pytsk3-test",
        "native-test",
        "native-test",
        "pytsk3-test",
        "pytsk3-test",
        "native-test",
    ]
    order = report["backend_execution_order"]
    assert [entry["backend"] for entry in order] == [
        "native",
        "pytsk3",
        "pytsk3",
        "native",
        "native",
        "pytsk3",
        "pytsk3",
        "native",
    ]
    assert [entry["discarded_warmup"] for entry in order] == [True, True] + [False] * 6
    assert all(entry["status"] == "ok" for entry in order)
    assert report["cache_state"]["managed_by_harness"] is False


def test_interleaved_benchmark_keeps_working_backend_when_other_fails(
    tmp_path, monkeypatch
):
    image = tmp_path / "sample.img"
    image.write_bytes(b"1234")

    def fail_native(*_args, **_kwargs):
        raise bench_vfs.TskError("native fixture unavailable")

    def open_pytsk(path, offset=0):
        provider = _FakeProvider()
        provider.backend_name = "pytsk3-test"
        return provider

    monkeypatch.setattr(bench_vfs, "open_image_provider", fail_native)
    monkeypatch.setattr(bench_vfs, "TskImage", open_pytsk)
    report = bench_vfs.build_report(
        image,
        0,
        4,
        ["/docs/readme.txt"],
        baseline="both",
        repeats=2,
        warmups=0,
    )

    assert report["backends"]["native_error"] == "native fixture unavailable"
    assert "native" not in report["backends"]
    assert len(report["backends"]["pytsk3"]["iterations"]) == 2
    assert report["backend_execution_order"][0]["status"] == "error"
    assert all(
        item["backend"] == "pytsk3" and item["status"] == "ok"
        for item in report["backend_execution_order"][1:]
    )
    assert "comparison" not in report


def test_build_report_withholds_performance_comparison_on_content_mismatch(
    tmp_path, monkeypatch
):
    image = tmp_path / "sample.img"
    image.write_bytes(b"1234")

    def open_native(path, offset=0, volume_size=None):
        return _FakeProvider()

    class DifferentProvider(_FakeProvider):
        backend_name = "pytsk3-test"

        def read_file_range(self, path, offset=0, length=1 << 20):
            return b"jello"[offset : offset + length]

    monkeypatch.setattr(bench_vfs, "open_image_provider", open_native)
    monkeypatch.setattr(
        bench_vfs, "TskImage", lambda *_args, **_kwargs: DifferentProvider()
    )
    report = bench_vfs.build_report(
        image, 0, 4, ["/docs/readme.txt"], baseline="both", repeats=1, warmups=0
    )

    assert report["comparison"]["content_equivalent"] is False
    assert report["comparison"]["content_mismatches"] == ["/docs/readme.txt"]
    assert "native_to_baseline_ratio" not in report["comparison"]


@pytest.mark.skipif(not shutil.which("mke2fs"), reason="mke2fs is required")
def test_real_ext2_benchmark_requires_identical_pytsk3_file_content(tmp_path):
    pytest.importorskip("pytsk3")
    image = tmp_path / "benchmark.ext2"
    image_size = 16 * 1024 * 1024
    image.write_bytes(bytes(image_size))
    subprocess.run(
        [
            shutil.which("mke2fs"),
            "-q",
            "-F",
            "-t",
            "ext2",
            "-b",
            "4096",
            "-O",
            "none,filetype,sparse_super,large_file",
            str(image),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    expected = b"benchmark differential payload\x00" * 300
    NativeExt2ImageWriter(image).write_file("/benchmark.bin", expected)

    report = bench_vfs.build_report(
        image,
        0,
        image_size,
        ["/benchmark.bin"],
        baseline="both",
        repeats=1,
        warmups=0,
    )

    assert "native_error" not in report["backends"]
    assert "pytsk3_error" not in report["backends"]
    assert report["comparison"]["content_equivalent"] is True
    assert report["backends"]["native"]["iterations"][0]["file_sha256"] == {
        "/benchmark.bin": hashlib.sha256(expected).hexdigest()
    }


def test_native_benchmark_does_not_count_pytsk_fallback_as_native(
    tmp_path, monkeypatch
):
    image = tmp_path / "sample.img"
    image.write_bytes(b"1234")
    fallback = _FakeProvider()
    fallback.backend_name = "pytsk3"
    monkeypatch.setattr(
        bench_vfs, "open_image_provider", lambda *_args, **_kwargs: fallback
    )

    report = bench_vfs.build_report(
        image, 0, 4, ["/docs/readme.txt"], baseline="native", repeats=1, warmups=0
    )

    assert "native" not in report["backends"]
    assert "selected the pytsk3 fallback" in report["backends"]["native_error"]
    assert fallback.closed


@pytest.mark.skipif(not shutil.which("mke2fs"), reason="mke2fs is required")
def test_benchmark_fixture_generator_is_byte_reproducible(tmp_path):
    first = make_bench_ext2_image.make_image(tmp_path / "first.ext2", 16, 4096)
    second = make_bench_ext2_image.make_image(tmp_path / "second.ext2", 16, 4096)

    assert (
        hashlib.sha256(first.read_bytes()).digest()
        == hashlib.sha256(second.read_bytes()).digest()
    )


@pytest.mark.skipif(
    not shutil.which("mke2fs") or not shutil.which("debugfs"),
    reason="mke2fs and debugfs are required for the optional 10 MiB fixture",
)
def test_10m_benchmark_fixture_is_reproducible_and_native_readable(tmp_path):
    first = make_bench_ext2_image.make_image(
        tmp_path / "first-10m.ext2", 32, 4096, include_10m=True
    )
    second = make_bench_ext2_image.make_image(
        tmp_path / "second-10m.ext2", 32, 4096, include_10m=True
    )
    first_hash = hashlib.sha256(first.read_bytes()).digest()
    second_hash = hashlib.sha256(second.read_bytes()).digest()
    assert first_hash == second_hash
    assert "/bench/large_10m.dat" in make_bench_ext2_image.bench_paths(include_10m=True)

    expected = make_bench_ext2_image.deterministic_payload(
        make_bench_ext2_image.LARGE_10M_NAME,
        make_bench_ext2_image.LARGE_10M_SIZE,
    )
    reader = NativeExt2Image(first, volume_size=first.stat().st_size)
    try:
        assert reader.file_size("/bench/large_10m.dat") == len(expected)
        actual = reader.read_file_range("/bench/large_10m.dat", 0, len(expected))
        assert actual == expected
    finally:
        reader.close()
