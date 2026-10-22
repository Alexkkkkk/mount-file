# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Tests for read-only single-file evidence manifests."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from mountfile.evidence import EvidenceManifestError, create_evidence_manifest


def test_manifest_hashes_source_bytes_in_bounded_chunks_without_modifying_input(
    tmp_path,
):
    image = tmp_path / "sample.img"
    source_bytes = bytes(range(256)) * 10_000
    image.write_bytes(source_bytes)
    manifest_path = tmp_path / "sample.manifest.json"

    manifest = create_evidence_manifest(image, manifest_path, chunk_size=64 * 1024)

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == written
    assert written["schema"] == "mountfile.evidence-manifest/v1"
    assert written["source"]["size_bytes"] == len(source_bytes)
    assert written["source"]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert written["hashing"]["chunk_size_bytes"] == 64 * 1024
    assert written["hashing"]["opened_read_only"] is True
    assert written["hashing"]["noatime_requested"] == bool(getattr(os, "O_NOATIME", 0))
    assert isinstance(written["hashing"]["noatime_applied"], bool)
    assert written["hashing"]["format_detection"] == "not performed"
    assert written["hashing"]["logical_media_stream_hash"] is None
    assert image.read_bytes() == source_bytes
    if os.name != "nt":
        assert manifest_path.stat().st_mode & 0o777 == 0o600


def test_manifest_refuses_to_overwrite_existing_output(tmp_path):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"evidence")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("keep me", encoding="utf-8")

    with pytest.raises(EvidenceManifestError, match="already exists"):
        create_evidence_manifest(image, manifest)

    assert manifest.read_text(encoding="utf-8") == "keep me"
    assert image.read_bytes() == b"evidence"


def test_manifest_refuses_source_changed_during_read(tmp_path, monkeypatch):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"A" * (2 * 1024 * 1024))
    manifest = tmp_path / "manifest.json"
    original_read = os.read
    changed = False

    def mutate_after_first_chunk(fd, size):
        nonlocal changed
        chunk = original_read(fd, size)
        if chunk and not changed:
            changed = True
            image.write_bytes(b"changed")
        return chunk

    monkeypatch.setattr("mountfile.evidence.os.read", mutate_after_first_chunk)

    with pytest.raises(EvidenceManifestError, match="source changed"):
        create_evidence_manifest(image, manifest, chunk_size=1024)

    assert not manifest.exists()


def test_cli_reports_existing_manifest_without_traceback_or_overwrite(tmp_path, capsys):
    from mountfile.cli import ERROR, main

    image = tmp_path / "evidence.img"
    image.write_bytes(b"evidence")
    output = tmp_path / "manifest.json"
    output.write_text("preserve", encoding="utf-8")

    result = main(["evidence-manifest", str(image), "--output", str(output)])

    assert result == ERROR
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "already exists" in captured.err
    assert output.read_text(encoding="utf-8") == "preserve"


def test_manifest_refuses_source_as_output(tmp_path):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"evidence")

    with pytest.raises(EvidenceManifestError, match="separate from the source"):
        create_evidence_manifest(image, image)

    assert image.read_bytes() == b"evidence"


def test_manifest_refuses_symlink_source(tmp_path):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"evidence")
    link = tmp_path / "alias.img"
    try:
        link.symlink_to(image)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(EvidenceManifestError, match="symbolic-link"):
        create_evidence_manifest(link, tmp_path / "manifest.json")


def test_manifest_rejects_invalid_chunk_sizes(tmp_path):
    image = tmp_path / "evidence.img"
    image.write_bytes(b"evidence")

    for chunk_size in (0, -1, 64 * 1024 * 1024 + 1, True, 1.5):
        with pytest.raises(EvidenceManifestError, match="chunk_size"):
            create_evidence_manifest(
                image, tmp_path / "manifest.json", chunk_size=chunk_size
            )


def test_cli_creates_read_only_manifest_for_explicit_output(tmp_path, capsys):
    from mountfile.cli import main

    image = tmp_path / "evidence.img"
    image.write_bytes(b"MountFile test evidence\x00\x01")
    output = tmp_path / "evidence.manifest.json"

    result = main(["evidence-manifest", str(image), "--output", str(output)])

    assert result == 0
    captured = capsys.readouterr()
    assert "SHA-256:" in captured.out
    assert "Wrote manifest:" in captured.out
    assert (
        json.loads(output.read_text(encoding="utf-8"))["source"]["sha256"]
        == hashlib.sha256(image.read_bytes()).hexdigest()
    )


def test_cli_command_is_registered_for_gui_command_center():
    from mountfile.cli import COMMANDS, build_parser

    assert "evidence-manifest" in COMMANDS
    args = build_parser().parse_args(
        ["evidence-manifest", "sample.img", "--output", "manifest.json"]
    )
    assert args.command == "evidence-manifest"
