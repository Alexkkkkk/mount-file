# SPDX-License-Identifier: MIT
"""Property and harness tests for exFAT boot-region parsing."""

from mountfile.exfat_probe import ExfatProbeError, parse_exfat_boot_region
from tools.fuzz_exfat_probe import _build_target, _seed_corpus, _valid_seed_input


def test_exfat_fuzz_target_accepts_valid_seed_and_handles_malformed_seeds(tmp_path):
    corpus = tmp_path / "exfat-corpus"
    _seed_corpus(corpus)
    target = _build_target(parse_exfat_boot_region, ExfatProbeError)

    seed = _valid_seed_input()
    volume_size = int.from_bytes(seed[:8], "little")
    profile = parse_exfat_boot_region(seed[8:], volume_size=volume_size)
    assert profile.partition_size == volume_size

    seed_paths = sorted(corpus.iterdir())
    assert len(seed_paths) == 4
    for path in seed_paths:
        target(path.read_bytes())


def test_exfat_fuzz_seed_corpus_does_not_overwrite_existing_inputs(tmp_path):
    corpus = tmp_path / "existing-corpus"
    corpus.mkdir()
    sentinel = corpus / "boot-region-valid.bin"
    sentinel.write_bytes(b"preserve-user-seed")

    _seed_corpus(corpus)

    assert sentinel.read_bytes() == b"preserve-user-seed"
    assert (corpus / "boot-region-main-checksum.bin").is_file()
    assert (corpus / "boot-region-backup-geometry.bin").is_file()
    assert (corpus / "boot-region-selected-volume-bound.bin").is_file()
