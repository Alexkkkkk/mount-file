# SPDX-License-Identifier: MIT
"""DFIR follow-up regressions: only synthetic images, no mounts or services."""

from __future__ import annotations

import builtins
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

import mountfile_dfir_tools as tools
from mountfile import slack


def image(tmp_path, data=b"synthetic evidence"):
    path = tmp_path / "image.img"
    path.write_bytes(data)
    return path


def rule_file(tmp_path, source):
    path = tmp_path / "rules.yar"
    path.write_text(source, encoding="utf-8")
    return path


def attribute(runs, *, kind=128, name=None, flags=2, size=1000):
    class Attribute:
        info = NS(type=kind, name=name, flags=flags, size=size)

        def __iter__(self):
            return iter(runs)

    return Attribute()


def run(addr, count, offset=0, flags=0):
    return NS(addr=addr, len=count, offset=offset, flags=flags)


def filesystem(attributes, *, size=1000, block_count=100):
    entry = NS(info=NS(name=NS(name=b"file.bin"), meta=NS(type=1, size=size, addr=7)))

    class FS:
        info = NS(block_size=512, block_count=block_count, root_inum=2)

        def open_dir(self, path):
            return [entry]

        def open_meta(self, inode):
            return attributes

    return FS()


@pytest.mark.parametrize("have_tsk", [False, True])
def test_incomplete_allocation_map_is_refused_not_inferred_from_files(
    tmp_path, monkeypatch, have_tsk
):
    reader = tools.ImageReader(image(tmp_path))
    monkeypatch.setattr(reader, "has_pytsk3", lambda: have_tsk)
    monkeypatch.setattr(
        reader, "iter_files", lambda: iter([("/allocated", b"evidence")])
    )
    with pytest.raises(tools.DFIRError, match="complete filesystem allocation map"):
        list(reader.iter_allocated_regions())
    with pytest.raises(tools.DFIRError, match="complete filesystem allocation map"):
        list(reader._iter_ntfs_allocated())


def test_unallocated_refusal_does_not_create_artifacts(tmp_path):
    source = image(tmp_path)
    output = tmp_path / "output"
    with pytest.raises(tools.DFIRError, match="complete filesystem allocation map"):
        tools.UnallocatedExtractor(source).extract(output, min_region=1)
    assert not output.exists()
    assert source.read_bytes() == b"synthetic evidence"


def test_unallocated_yara_refuses_automatic_discovery(tmp_path):
    pytest.importorskip("yara")
    scanner = tools.YaraScanner(image(tmp_path))
    rules = rule_file(tmp_path, "rule t { condition: true }")
    with pytest.raises(tools.DFIRError, match="complete filesystem allocation map"):
        scanner.scan_unallocated(rules)


def test_complete_free_regions_are_scanned_without_allocated_bytes_or_padding(
    tmp_path, monkeypatch
):
    pytest.importorskip("yara")
    source = image(tmp_path, b"FREEALLOCATEDFREE")
    scanner = tools.YaraScanner(source)
    # Independently supplied synthetic allocation map isolates the scanner;
    # production discovery remains refused (covered by the preceding tests).
    monkeypatch.setattr(
        scanner.reader,
        "iter_allocated_regions",
        lambda: iter([tools.RegionInfo(4, 9, "file", "/allocated")]),
    )
    rules = rule_file(
        tmp_path,
        """
rule free { strings: $a = "FREE" condition: $a and filesize == 4 }
rule allocated { strings: $a = "ALLOCATED" condition: $a }
rule zero_padding { strings: $a = {00 00} condition: $a }
""",
    )
    matches = scanner.scan_unallocated(rules)
    assert [(match.rule, match.offset) for match in matches] == [
        ("free", 0),
        ("free", 13),
    ]
    assert source.read_bytes() == b"FREEALLOCATEDFREE"


def test_yara_conjunction_and_false_condition_are_respected(tmp_path):
    pytest.importorskip("yara")
    scanner = tools.YaraScanner(image(tmp_path))
    rules = scanner._load_rules(
        rule_file(
            tmp_path,
            """
rule conjunction { strings: $a = "ONE" $b = "TWO" condition: all of them }
rule disabled { strings: $a = "ONE" condition: $a and false }
""",
        )
    )
    assert scanner._scan_blob(b"ONE", rules, "/file") == []
    matches = scanner._scan_blob(b"ONE TWO", rules, "/file")
    assert [match.rule for match in matches] == ["conjunction"]


def test_no_yara_dependency_is_not_a_regex_scan(tmp_path, monkeypatch):
    scanner = tools.YaraScanner(image(tmp_path))
    monkeypatch.setitem(sys.modules, "yara", None)
    with pytest.raises(tools.DependencyMissing, match="no regex fallback"):
        scanner._load_rules(rule_file(tmp_path, "rule t { condition: true }"))
    with pytest.raises(tools.DependencyMissing, match="conditions"):
        scanner._parse_simple_rules('rule t { strings: $a = "X" condition: false }')


def test_engine_errors_are_not_reported_as_clean_results(tmp_path):
    class BrokenRules:
        def match(self, **kwargs):
            assert kwargs["timeout"] == tools.SCAN_TIMEOUT
            raise TimeoutError("synthetic timeout")

    scanner = tools.YaraScanner(image(tmp_path))
    with pytest.raises(tools.DFIRError, match="YARA scan failed.*synthetic timeout"):
        scanner._scan_blob(b"X", {"type": "yara", "compiled": BrokenRules()}, "/file")
    with pytest.raises(tools.DFIRError, match="real YARA"):
        scanner._scan_blob(b"X", {"type": "regex", "rules": []}, "/file")


def test_compile_errors_are_explicit(tmp_path):
    pytest.importorskip("yara")
    scanner = tools.YaraScanner(image(tmp_path))
    with pytest.raises(tools.DFIRError, match="compile YARA"):
        scanner._load_rules(rule_file(tmp_path, "not a YARA rule"))


def test_scan_size_limits_refuse_semantically_incorrect_chunking(tmp_path, monkeypatch):
    pytest.importorskip("yara")
    scanner = tools.YaraScanner(image(tmp_path, b"ONE TWO"))
    rules = rule_file(tmp_path, "rule t { condition: true }")
    monkeypatch.setattr(tools, "MAX_SCAN_SIZE", 4)
    monkeypatch.setattr(scanner.reader, "iter_allocated_regions", lambda: iter([]))
    with pytest.raises(tools.DFIRError, match="bounded scan limit"):
        scanner.scan_unallocated(rules)
    with pytest.raises(tools.DFIRError, match="bounded scan limit"):
        scanner._scan_blob(b"ONE TWO", scanner._load_rules(rules), "/file")


def test_file_enumeration_never_fabricates_raw_files(tmp_path, monkeypatch):
    reader = tools.ImageReader(image(tmp_path))
    monkeypatch.setitem(sys.modules, "pytsk3", None)
    with pytest.raises(tools.DependencyMissing):
        list(reader.iter_files())


def test_file_short_reads_are_explicit(tmp_path, monkeypatch):
    reader = tools.ImageReader(image(tmp_path))
    fs = filesystem([attribute([run(4, 2)])])
    entry = fs.open_dir("/")[0]
    entry.read_random = lambda offset, length: b"short"
    monkeypatch.setattr(fs, "open_dir", lambda path: [entry])
    monkeypatch.setattr(reader, "_tsk", lambda: fs)
    with pytest.raises(tools.DFIRError, match="short file read"):
        list(reader.iter_files())


def test_subtract_merges_overlaps_in_logical_order():
    free = tools.UnallocatedExtractor._subtract(0, 40, [(20, 30), (4, 10), (8, 25)])
    assert [(region.offset, region.length) for region in free] == [(0, 4), (30, 10)]


@pytest.mark.parametrize(
    "region", [tools.RegionInfo(-1, 2, "file"), tools.RegionInfo(0, 100, "file")]
)
def test_invalid_allocation_regions_are_refused(tmp_path, monkeypatch, region):
    reader = tools.ImageReader(image(tmp_path))
    monkeypatch.setattr(reader, "iter_allocated_regions", lambda: iter([region]))
    with pytest.raises(tools.DFIRError):
        tools._allocated_ranges(reader)


def test_image_exact_read_detects_truncation(tmp_path):
    path = image(tmp_path)
    reader = tools.ImageReader(path)
    path.write_bytes(b"short")
    with pytest.raises(tools.DFIRError, match="short image read"):
        reader.read_exact(0, reader.size)


@pytest.mark.parametrize(
    "size,runs,expected",
    [
        (1000, [(10, 2), (18, 2)], (10 * 512 + 1000, 24)),
        (1100, [(10, 2), (18, 2)], (18 * 512 + 76, 436)),
        (600, [(50, 1), (3, 3)], (3 * 512 + 88, 424)),
        (1, [(9, 10)], (9 * 512 + 1, 511)),
    ],
)
def test_slack_maps_logical_eof_not_end_of_preallocation(size, runs, expected):
    region = slack.slack_region_for("/file", size, runs, 512)
    assert (region.offset, region.length) == expected
    assert region.allocated_size == sum(count for start, count in runs) * 512


def test_preallocated_cluster_aligned_file_has_no_slack():
    assert slack.slack_region_for("/file", 512, [(10, 3)], 512) is None


@pytest.mark.parametrize(
    "size,runs,block",
    [
        (-1, [(1, 2)], 512),
        (100, [(-1, 2)], 512),
        (100, [(1, 0)], 512),
        (2000, [(1, 2)], 512),
        (1.5, [(1, 2)], 512),
        (True, [(1, 2)], 512),
        (100, [(1, 2)], 0),
    ],
)
def test_invalid_slack_geometry_is_refused(size, runs, block):
    with pytest.raises(slack.SlackError):
        slack.slack_region_for("/file", size, runs, block)


def test_only_unnamed_default_stream_contributes_to_slack():
    fs = filesystem(
        [
            attribute([run(70, 2)], kind=16),
            attribute([run(90, 2)], name=b"alternate"),
            attribute([run(10, 1), run(3, 1, offset=1)]),
        ]
    )
    region = slack.find_slack_regions_from_fs(fs, filesystem_offset=4096)[0]
    assert region.offset == 4096 + 3 * 512 + 488
    assert region.length == 24


def test_run_maps_use_explicit_logical_offsets_not_iteration_order():
    fs = filesystem([attribute([run(3, 1, offset=1), run(10, 1, offset=0)])])
    assert slack.find_slack_regions_from_fs(fs)[0].offset == 3 * 512 + 488


@pytest.mark.parametrize(
    "attrs",
    [
        [attribute([run(10, 2)], flags=32)],
        [attribute([run(10, 2, flags=2)])],
        [attribute([run(10, 1), run(20, 1, offset=2)])],
        [attribute([run(10, 2)]), attribute([run(20, 2)])],
        [attribute([])],
        [attribute([run(10, 2)], kind=16)],
        [attribute([run(10, 2)], size=999)],
    ],
)
def test_ambiguous_or_unsupported_data_maps_are_not_clean_slack_scans(attrs):
    with pytest.raises(slack.SlackError):
        slack.find_slack_regions_from_fs(filesystem(attrs))


def test_resident_ntfs_attribute_has_no_cluster_slack():
    assert slack.find_slack_regions_from_fs(filesystem([attribute([], flags=4)])) == []


def test_physical_runs_must_fit_filesystem():
    with pytest.raises(slack.SlackError, match="beyond filesystem"):
        slack.find_slack_regions_from_fs(filesystem([attribute([run(99, 2)])]))


def test_directory_read_errors_are_explicit():
    fs = filesystem([])

    def damaged():
        raise OSError("synthetic damaged directory")
        yield  # pragma: no cover

    fs.open_dir = lambda path: damaged()
    with pytest.raises(slack.SlackError, match="synthetic damaged directory"):
        slack.find_slack_regions_from_fs(fs)


@pytest.mark.parametrize(
    "offset,length", [(-1, 1), (0, -1), (50, 1), (1, 100), (False, 1), (0, 1.5)]
)
def test_reads_reject_invalid_or_outside_regions(tmp_path, offset, length):
    with pytest.raises(slack.SlackError):
        slack.read_region(image(tmp_path), offset, length)


def test_stream_validates_all_regions_before_yielding(tmp_path):
    source = image(tmp_path)
    regions = [
        slack.SlackRegion("/ok", 0, 1, 1, 0),
        slack.SlackRegion("/bad", 100, 1, 1, 0),
    ]
    stream = slack.iter_slack_bytes(source, regions)
    with pytest.raises(slack.SlackError, match="beyond image"):
        next(stream)


@pytest.mark.parametrize("chunk", [0, -1, False, slack.MAX_READ + 1])
def test_stream_invalid_chunk_is_refused(tmp_path, chunk):
    with pytest.raises(slack.SlackError):
        list(slack.iter_slack_bytes(image(tmp_path), [], chunk=chunk))


def test_stream_detects_short_read_after_source_shrinks(tmp_path):
    source = image(tmp_path, b"A" * 20)
    stream = slack.iter_slack_bytes(
        source, [slack.SlackRegion("/file", 0, 20, 20, 0)], chunk=10
    )
    assert next(stream)[1] == b"A" * 10
    source.write_bytes(b"A" * 10)
    with pytest.raises(slack.SlackError, match="short read"):
        next(stream)


def test_read_region_detects_short_read_not_empty_success(tmp_path, monkeypatch):
    source = image(tmp_path)
    actual_open = builtins.open

    class ShortStream:
        def __init__(self):
            self.real = actual_open(source, "rb")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.real.close()

        def fileno(self):
            return self.real.fileno()

        def seek(self, offset):
            return self.real.seek(offset)

        def read(self, length):
            return b"X"

    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: ShortStream())
    with pytest.raises(slack.SlackError, match="short read"):
        slack.read_region(source, 0, 10)


def test_slack_extraction_is_bounded_read_only_and_never_overwrites(tmp_path):
    source = image(tmp_path, b"DATAsecret")
    output = tmp_path / "out"
    region = slack.SlackRegion("/file", 4, 6, 10, 4)
    assert slack.extract_slack(source, output, regions=[region]) == [region]
    assert (output / "slack_0000_file.bin").read_bytes() == b"secret"
    with pytest.raises(FileExistsError):
        slack.extract_slack(source, output, regions=[region])
    assert source.read_bytes() == b"DATAsecret"


def test_invalid_slack_extraction_creates_no_output_directory(tmp_path):
    source = image(tmp_path)
    output = tmp_path / "out"
    with pytest.raises(slack.SlackError):
        slack.extract_slack(
            source, output, regions=[slack.SlackRegion("/file", 100, 1, 1, 0)]
        )
    assert not output.exists()


def test_partial_slack_artifact_is_removed_on_read_failure(tmp_path, monkeypatch):
    source = image(tmp_path)
    output = tmp_path / "out"

    def broken(*args, **kwargs):
        yield None, b"partial"
        raise slack.SlackError("synthetic short read")

    monkeypatch.setattr(slack, "iter_slack_bytes", broken)
    with pytest.raises(slack.SlackError, match="short read"):
        slack.extract_slack(
            source, output, regions=[slack.SlackRegion("/file", 0, 2, 2, 0)]
        )
    assert list(output.iterdir()) == []


def test_legacy_slack_uses_verified_implementation(tmp_path, monkeypatch):
    source = image(tmp_path, b"DATAsecret")
    region = slack.SlackRegion("/file", 4, 6, 10, 4)
    monkeypatch.setattr(slack, "find_slack_regions", lambda path: [region])
    result = tools.SlackExtractor(source).extract(tmp_path / "out")
    assert result == [tools.RegionInfo(4, 6, "slack", "/file")]
    assert (tmp_path / "out" / "slack_0000_file.bin").read_bytes() == b"secret"


def test_legacy_slack_refuses_unsupported_image_instead_of_empty_success(tmp_path):
    source = image(tmp_path, b"\0" * 4096)
    with pytest.raises(tools.DFIRError):
        tools.SlackExtractor(source).extract(tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_selftest_accepts_real_yara_installation():
    assert tools.main(["selftest"]) == 0


def test_native_ntfs_nonresident_eof_slack_without_mounts(tmp_path):
    """Real TSK run offsets on a tiny disposable NTFS image, without mounting."""
    pytsk3 = pytest.importorskip("pytsk3")
    mkfs, copy = shutil.which("mkfs.ntfs"), shutil.which("ntfscp")
    if not mkfs or not copy:
        pytest.skip("synthetic NTFS tools unavailable")
    source = tmp_path / "synthetic.ntfs"
    with source.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    subprocess.run(
        [mkfs, "-F", "-Q", str(source)], check=True, capture_output=True, timeout=30
    )
    payload = tmp_path / "payload"
    payload.write_bytes(b"A" * 5000)
    subprocess.run(
        [copy, str(source), str(payload), "/payload.bin"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    fs = pytsk3.FS_Info(pytsk3.Img_Info(str(source)))
    native = fs.open("/payload.bin")
    runs = slack._runs(native, 5000)
    expected = slack.slack_region_for("/payload.bin", 5000, runs, fs.info.block_size)
    assert expected is not None
    assert expected.length == fs.info.block_size - 5000 % fs.info.block_size
    found = slack.find_slack_regions(source)
    matching = [region for region in found if region.path == "/payload.bin"]
    assert matching == [expected]
    assert (
        len(slack.read_region(source, expected.offset, expected.length))
        == expected.length
    )
    extracted = tools.SlackExtractor(source).extract(tmp_path / "native-slack")
    assert any(
        region.owner == "/payload.bin" and region.offset == expected.offset
        for region in extracted
    )
