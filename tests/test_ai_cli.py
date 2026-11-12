# SPDX-License-Identifier: MIT
"""Tests for the package MCP command-line entry point."""

import importlib.util
import json
from pathlib import Path

import pytest

from mountfile.ai import cli, mcp_server

ROOT_CLI_PATH = Path(__file__).resolve().parents[1] / "mountfile_ai.py"
ROOT_CLI_SPEC = importlib.util.spec_from_file_location(
    "mountfile_ai_root", ROOT_CLI_PATH
)
root_cli = importlib.util.module_from_spec(ROOT_CLI_SPEC)
ROOT_CLI_SPEC.loader.exec_module(root_cli)


class FakeServer:
    def __init__(self):
        self.ran = False

    def run(self):
        self.ran = True


def test_mcp_command_builds_and_runs_stdio_server(monkeypatch, tmp_path):
    server = FakeServer()
    calls = []

    def fake_create_server(**kwargs):
        calls.append(kwargs)
        return server

    monkeypatch.setattr(mcp_server, "create_server", fake_create_server)
    image = tmp_path / "case.img"
    workspace = tmp_path / "outputs"

    assert (
        cli.main(
            [
                "mcp",
                "--image",
                str(image),
                "--workspace",
                str(workspace),
                "--name",
                "test",
            ]
        )
        == 0
    )
    assert calls == [
        {"image_root": str(image), "workspace": str(workspace), "name": "test"}
    ]
    assert server.ran is True


def test_mcp_command_reports_missing_optional_dependency(monkeypatch, capsys):
    def unavailable(**kwargs):
        raise mcp_server.McpUnavailableError("install the optional mcp package")

    monkeypatch.setattr(mcp_server, "create_server", unavailable)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["mcp", "--image", "case.img"])

    assert exc_info.value.code == 2
    assert "install the optional mcp package" in capsys.readouterr().err


def test_root_inspect_returns_zero_when_partitions_are_read(
    monkeypatch, tmp_path, capsys
):
    image = tmp_path / "case.img"
    image.write_bytes(b"test image")
    monkeypatch.setattr(root_cli, "list_partitions", lambda image: [])

    exit_code = root_cli.main(["inspect", "--image", str(image)])

    assert exit_code == 0
    assert "partitions:\n  (empty)" in capsys.readouterr().out


def test_root_inspect_returns_nonzero_when_partitions_are_unreadable(
    monkeypatch, tmp_path, capsys
):
    image = tmp_path / "case.img"
    image.write_bytes(b"test image")

    def unreadable(image):
        raise RuntimeError("no volume table")

    monkeypatch.setattr(root_cli, "list_partitions", unreadable)

    exit_code = root_cli.main(["inspect", "--image", str(image)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "(unreadable: no volume table)" in output
    assert "try: python mountfile_ai.py agent --image" in output


def _make_encrypted_mtk_dump(path):
    from mountfile import rt809

    data = bytearray(rt809.BLOCK)
    data[0x100:0x114] = rt809.MARKER_MTK
    data[rt809.MTK_FLAG_ADDR : rt809.MTK_FLAG_ADDR + 2] = rt809.FLAG_ENCODE
    data[rt809.MTK_PLAIN_CHECK[0] : rt809.MTK_PLAIN_CHECK[0] + 5] = b"Start"
    path.write_bytes(data)


def test_legacy_try_xor_decrypt_uses_rt809_and_refuses_fake_keys(monkeypatch, tmp_path):
    from mountfile import rt809

    image = tmp_path / "mtk.bin"
    output = tmp_path / "decoded.bin"
    _make_encrypted_mtk_dump(image)
    monkeypatch.setattr(root_cli.CONFIG, "read_only", False)
    monkeypatch.setattr(root_cli.AUDIT, "write", lambda *args, **kwargs: None)
    monkeypatch.setattr(rt809, "load_table", lambda kind, table_dir=None: b"\x00")

    result = root_cli.try_xor_decrypt(image, "mtk", output)

    assert result["key"] == "MTK"
    assert result["output"] == str(output)
    decoded = output.read_bytes()
    assert decoded[rt809.MTK_FLAG_ADDR : rt809.MTK_FLAG_ADDR + 4] == b"\x00" * 4
    assert decoded[rt809.MTK_PLAIN_CHECK[0] : rt809.MTK_PLAIN_CHECK[0] + 5] == b"Start"
    assert not hasattr(root_cli, "XOR_KEYS")
    with pytest.raises(ValueError, match="supported RT809 dump kinds are MTK and MST"):
        root_cli.try_xor_decrypt(image, "mstar_a", tmp_path / "fake-key.bin")
    assert not (tmp_path / "fake-key.bin").exists()


def test_legacy_auto_decrypt_uses_rt809_detection(monkeypatch, tmp_path):
    from mountfile import rt809

    image = tmp_path / "mtk-auto.bin"
    _make_encrypted_mtk_dump(image)
    monkeypatch.setattr(root_cli.CONFIG, "read_only", False)
    monkeypatch.setattr(root_cli.CONFIG, "workspace", tmp_path)
    monkeypatch.setattr(root_cli.AUDIT, "write", lambda *args, **kwargs: None)
    monkeypatch.setattr(rt809, "load_table", lambda kind, table_dir=None: b"\x00")

    result = root_cli.auto_decrypt(image)

    assert result["key"] == "MTK"
    assert Path(result["output"]) == tmp_path / "mtk-auto.decoded.bin"
    assert Path(result["output"]).is_file()


def test_audit_verifies_existing_chain_and_appends_after_reopen(tmp_path):
    path = tmp_path / "audit" / "events.jsonl"
    audit = root_cli.Audit(path)

    first = audit.write("created", details={"count": 2})
    second = audit.write("updated", details=["a", "b"])
    assert audit.verify() == (True, None)

    reopened = root_cli.Audit(path)
    third = reopened.write("continued")

    assert first["prev"] == "0" * 64
    assert second["prev"] == first["hash"]
    assert third["prev"] == second["hash"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_audit_rejects_record_content_change_on_open(tmp_path):
    path = tmp_path / "audit.jsonl"
    root_cli.Audit(path).write("created", detail="original")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["detail"] = "modified"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(root_cli.AuditIntegrityError, match="hash does not match"):
        root_cli.Audit(path)


def test_audit_rejects_broken_link_even_when_record_hash_is_recomputed(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = root_cli.Audit(path)
    audit.write("first")
    audit.write("second")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1]["prev"] = "0" * 64
    unsigned = {key: value for key, value in records[1].items() if key != "hash"}
    records[1]["hash"] = root_cli.Audit._record_hash(unsigned)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    with pytest.raises(
        root_cli.AuditIntegrityError, match="breaks the previous-hash link"
    ):
        root_cli.Audit(path)


def test_audit_rejects_incomplete_final_record(tmp_path):
    path = tmp_path / "audit.jsonl"
    root_cli.Audit(path).write("created")
    path.write_bytes(path.read_bytes()[:-1])

    with pytest.raises(root_cli.AuditIntegrityError, match="is incomplete"):
        root_cli.Audit(path)


def test_audit_does_not_append_after_log_changes_during_session(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = root_cli.Audit(path)
    audit.write("created")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["event"] = "rewritten"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    modified = path.read_bytes()

    with pytest.raises(root_cli.AuditIntegrityError, match="hash does not match"):
        audit.write("must-not-append")
    assert path.read_bytes() == modified


def test_audit_rejects_reserved_fields_and_empty_event(tmp_path):
    audit = root_cli.Audit(tmp_path / "audit.jsonl")

    with pytest.raises(ValueError, match="non-empty"):
        audit.write(" ")
    with pytest.raises(ValueError, match="reserved fields"):
        audit.write("event", hash="caller-controlled")
    assert not audit.path.exists()


def test_audit_refuses_append_after_in_session_truncation(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = root_cli.Audit(path)
    audit.write("created")
    path.write_bytes(b"")

    with pytest.raises(
        root_cli.AuditIntegrityError, match="changed since this instance"
    ):
        audit.write("must-not-replace-history")
    assert path.read_bytes() == b""
