#!/usr/bin/env python3
"""
mount-file AI: neuro-symbolic multi-agent DFIR layer (single file)
==================================================================

Integration layer for https://github.com/Alexkkkkk/mount-file that adds:
  • typed tool registry (MCP + OpenAI function calling)
  • neuro-symbolic gates that reject unverifiable LLM claims
  • 4-role orchestration (Planner → Executor → Analyst → Skeptic)
  • append-only hash-chained audit log (chain of custody)
  • XOR auto-decrypt for MStar/MediaTek dumps
  • optional MCP server for Claude Desktop / Cursor / Open WebUI

Install
-------
    pip install "mcp[cli]" pydantic pytsk3 openai

Run
---
    # Human inspection (no AI)
    python mountfile_ai.py inspect --image dump.bin

    # Autonomous multi-agent analysis (Ollama)
    export OPENAI_API_KEY=ollama
    export OPENAI_BASE_URL=http://localhost:11434/v1
    export MOUNTFILE_MODEL=qwen2.5-coder:7b
    export MOUNTFILE_READONLY=0
    python mountfile_ai.py agent --image dump.bin \
        --query "decrypt if needed, find deleted PDFs, verify completeness"

    # MCP server (Claude Desktop config):
    #   "mount-file": {
    #     "command": "python",
    #     "args": ["/abs/mountfile_ai.py", "mcp", "--image", "/abs/dump.bin"]
    #   }

Env
---
    MOUNTFILE_READONLY   default "1" (extraction disabled)
    MOUNTFILE_AUDIT      default ./mountfile_audit.jsonl
    MOUNTFILE_WORKDIR    default ./ai_workspace
    MOUNTFILE_MODEL      default gpt-4o-mini
    MOUNTFILE_MAX_ROUNDS default 12
    OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
"""

from __future__ import annotations

# =============================================================================
# STANDARD LIBRARY
# =============================================================================
import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

# =============================================================================
# THIRD-PARTY (with graceful degradation)
# =============================================================================
from pydantic import BaseModel, Field

try:
    import pytsk3
except ImportError:
    pytsk3 = None

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("mountfile.ai")


# =============================================================================
# 1. CONFIG + HASH-CHAINED AUDIT
# =============================================================================
class Config:
    def __init__(self) -> None:
        self.read_only = os.getenv("MOUNTFILE_READONLY", "1") != "0"
        self.audit_path = Path(os.getenv("MOUNTFILE_AUDIT", "./mountfile_audit.jsonl"))
        self.workspace = Path(os.getenv("MOUNTFILE_WORKDIR", "./ai_workspace"))
        self.model = os.getenv("MOUNTFILE_MODEL") or os.getenv(
            "OPENAI_MODEL", "gpt-4o-mini"
        )
        try:
            self.max_agent_rounds = max(1, int(os.getenv("MOUNTFILE_MAX_ROUNDS", "12")))
        except ValueError:
            self.max_agent_rounds = 12
        self.allowlist: set[Path] = set()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def allow(self, path: str | Path) -> Path:
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        self.allowlist.add(p)
        return p

    def check(self, path: str | Path) -> Path:
        p = Path(path).expanduser().resolve()
        if p not in self.allowlist:
            raise PermissionError(f"image {p} not in allowlist")
        return p


class Audit:
    """Append-only JSONL with hash-chained entries (chain-of-custody)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._prev_hash = "0" * 64
        if self.path.exists():
            last = self.path.read_text(encoding="utf-8").strip().splitlines()
            if last:
                self._prev_hash = json.loads(last[-1]).get("hash", self._prev_hash)

    @staticmethod
    def sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def write(self, event: str, **data: Any) -> dict:
        body = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "prev": self._prev_hash,
            "event": event,
            **data,
        }
        payload = json.dumps(body, sort_keys=True, default=str)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        body["hash"] = digest
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(body, default=str) + "\n")
        self._prev_hash = digest
        return body


CONFIG = Config()
AUDIT = Audit(CONFIG.audit_path)


# =============================================================================
# 2. PYDANTIC MODELS
# =============================================================================
class PartitionInfo(BaseModel):
    addr: int
    offset: int
    length: int
    desc: str
    fs_type: str | None = None


class FileEntry(BaseModel):
    name: str
    path: str
    inode: int
    size: int
    is_directory: bool
    deleted: bool = False
    mtime: float | None = None


class SignatureHit(BaseModel):
    offset: int
    kind: str
    hint: str = ""


class RecoveryResult(BaseModel):
    inode: int
    path: str
    recovered_size: int
    original_size: int
    complete: bool
    sha256: str | None = None


class ToolCall(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class Plan(BaseModel):
    steps: list[ToolCall]
    hypothesis: str = ""


class Claim(BaseModel):
    type: Literal[
        "deleted_claim",
        "recovered_complete",
        "xor_signature_valid",
        "fs_present",
        "anomaly",
        "summary",
    ]
    statement: str
    execution_id: str
    confidence: float = 0.5


class ExecutionRecord(BaseModel):
    execution_id: str
    tool: str
    args: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None


class InvestigationReport(BaseModel):
    query: str
    image: str
    sha256: str
    plan: Plan
    executions: list[ExecutionRecord]
    accepted_claims: list[Claim]
    rejected_claims: list[Claim]
    verdict: Literal["CONFIRMED", "PARTIAL", "REJECTED", "NEEDS_MORE_DATA"]
    summary_md: str


# =============================================================================
# 3. CORE: pytsk3 wrappers, signatures, XOR, recovery
# =============================================================================
def _require_tsk() -> None:
    if pytsk3 is None:
        raise RuntimeError("pytsk3 not installed: pip install pytsk3")


SIGNATURES: list[tuple[bytes, int, str, str]] = [
    (b"\x53\xEF", 0x438, "ext2/3/4", "superblock magic"),
    (b"NTFS    ", 3, "ntfs", "NTFS OEM"),
    (b"MSWIN4.1", 3, "fat", "FAT boot sector"),
    (b"FAT12", 54, "fat12", ""),
    (b"FAT16", 54, "fat16", ""),
    (b"FAT32", 82, "fat32", ""),
    (b"\x55\xAA", 510, "mbr", "MBR boot sig"),
]


def detect_signatures(image: Path, span: int = 1 << 20) -> list[SignatureHit]:
    with image.open("rb") as f:
        head = f.read(span)
    hits: list[SignatureHit] = []
    for magic, off, kind, hint in SIGNATURES:
        if off + len(magic) <= len(head) and head[off : off + len(magic)] == magic:
            hits.append(SignatureHit(offset=off, kind=kind, hint=hint))
    return hits


def list_partitions(image: Path) -> list[PartitionInfo]:
    _require_tsk()
    img = pytsk3.Img_Info(str(image))
    try:
        vol = pytsk3.Volume_Info(img)
    except Exception as e:
        raise RuntimeError(f"no volume table: {e}") from e
    bs = getattr(vol.info, "block_size", 512) or 512
    parts: list[PartitionInfo] = []
    for p in vol:
        if p.len == 0:
            continue
        off = p.start * bs
        fs_type = None
        try:
            fs_type = str(pytsk3.FS_Info(img, offset=off).info.ftype)
        except Exception:
            pass
        parts.append(
            PartitionInfo(
                addr=p.addr,
                offset=off,
                length=p.len * bs,
                desc=(p.desc or b"").decode("utf-8", "replace").strip() or f"p{p.addr}",
                fs_type=fs_type,
            )
        )
    return parts


def _open_fs(image: Path, offset: int) -> Any:
    _require_tsk()
    return pytsk3.FS_Info(pytsk3.Img_Info(str(image)), offset=offset)


def list_directory(image: Path, offset: int, path: str = "/") -> list[FileEntry]:
    fs = _open_fs(image, offset)
    try:
        d = fs.open_dir(path=path)
    except Exception as e:
        raise RuntimeError(f"cannot open {path}: {e}") from e
    out: list[FileEntry] = []
    for e in d:
        name = e.info.name.name
        if name in (b".", b".."):
            continue
        meta = e.info.meta
        if meta is None:
            continue
        decoded = name.decode("utf-8", "replace")
        out.append(
            FileEntry(
                name=decoded,
                path=path.rstrip("/") + "/" + decoded,
                inode=meta.addr,
                size=meta.size,
                is_directory=(meta.type == pytsk3.TSK_FS_META_TYPE_DIR),
                deleted=bool(meta.flags & pytsk3.TSK_FS_META_FLAG_UNALLOC),
                mtime=float(meta.mtime) if meta.mtime else None,
            )
        )
    return out


def search_files(
    image: Path,
    offset: int,
    pattern: str,
    start: str = "/",
    include_deleted: bool = True,
) -> list[FileEntry]:
    if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
        raise ValueError("pattern must be a non-empty regex of at most 256 characters")
    if re.search(r"(?:\\([^)]*)[+*]|\\([^)]*\\)[+*])", pattern):
        raise ValueError("pattern uses a potentially catastrophic regex form")
    rx = re.compile(pattern, re.IGNORECASE)
    matches: list[FileEntry] = []
    stack, seen = [start], set()
    visits = 0
    while stack and visits < 10000 and len(matches) < 1000:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        visits += 1
        entries = list_directory(image, offset, cur)
        for e in entries:
            if e.deleted and not include_deleted:
                continue
            if rx.search(e.name):
                matches.append(e)
            if e.is_directory and not e.deleted:
                stack.append(e.path)
    return matches


def get_file(image: Path, offset: int, path: str, output: Path | None = None) -> Path:
    if CONFIG.read_only:
        raise PermissionError("read-only; set MOUNTFILE_READONLY=0")
    fs = _open_fs(image, offset)
    f = fs.open(path=path)
    data = f.read_random(0, f.info.meta.size)
    dest = output or (CONFIG.workspace / Path(path).name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    AUDIT.write(
        "extract",
        image=str(image),
        offset=offset,
        path=path,
        dest=str(dest),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return dest


def recover_deleted_file(
    image: Path,
    offset: int,
    inode: int,
    original_size: int | None = None,
    output: Path | None = None,
) -> RecoveryResult:
    if CONFIG.read_only:
        raise PermissionError("read-only; set MOUNTFILE_READONLY=0")
    fs = _open_fs(image, offset)
    f = fs.open_meta(inode=inode)
    if f.info.meta is None:
        raise RuntimeError(f"inode {inode} not found")
    size = f.info.meta.size
    data = f.read_random(0, size)
    dest = output or (CONFIG.workspace / f"recovered_{inode}.bin")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    complete = original_size is None or len(data) == original_size
    AUDIT.write(
        "recover",
        image=str(image),
        inode=inode,
        recovered_size=len(data),
        complete=complete,
        sha256=sha,
    )
    return RecoveryResult(
        inode=inode,
        path=str(dest),
        recovered_size=len(data),
        original_size=original_size or size,
        complete=complete,
        sha256=sha,
    )


def hash_file(path: Path) -> str:
    return Audit.sha256_file(path)


# !!! REPLACE with real keys from mount-file resources/XOR_MST_*.bin !!!
XOR_KEYS: dict[str, bytes] = {
    "identity": b"\x00" * 16,
    "mstar_a": b"\xA5" * 16,
    "mtk_a": b"\x5A" * 16,
}


def _xor(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    k = (key * (len(data) // len(key) + 1))[: len(data)]
    return bytes(a ^ b for a, b in zip(data, k))


def try_xor_decrypt(image: Path, key_name: str, output: Path) -> dict:
    if CONFIG.read_only:
        raise PermissionError("read-only; set MOUNTFILE_READONLY=0")
    if key_name not in XOR_KEYS:
        raise KeyError(f"unknown key {key_name}; known={list(XOR_KEYS)}")
    key = XOR_KEYS[key_name]
    with image.open("rb") as src, output.open("wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(_xor(chunk, key))
    hits = detect_signatures(output)
    AUDIT.write(
        "xor",
        image=str(image),
        key=key_name,
        output=str(output),
        hits=[h.model_dump() for h in hits],
    )
    return {
        "output": str(output),
        "key": key_name,
        "signatures": [h.model_dump() for h in hits],
    }


def auto_decrypt(image: Path) -> dict | None:
    if CONFIG.read_only:
        raise PermissionError("read-only; set MOUNTFILE_READONLY=0")
    for name in XOR_KEYS:
        out = CONFIG.workspace / f"{image.stem}.{name}.bin"
        try:
            res = try_xor_decrypt(image, name, out)
        except Exception:
            continue
        if any(h["kind"] != "mbr" for h in res["signatures"]):
            return res
    return None


# =============================================================================
# 4. NEURO-SYMBOLIC GATES
# =============================================================================
Gate = Callable[[ExecutionRecord, Claim], tuple[bool, str]]


def gate_deleted_claim(rec: ExecutionRecord, _: Claim) -> tuple[bool, str]:
    if not rec.ok:
        return False, "tool call failed"
    res = rec.result or {}
    if isinstance(res, dict) and res.get("deleted") is True:
        return True, "UNALLOC flag present"
    if isinstance(res, list) and any(
        isinstance(r, dict) and r.get("deleted") for r in res
    ):
        return True, "at least one deleted entry confirmed"
    return False, "no deleted artifact in tool result"


def gate_recovered_complete(rec: ExecutionRecord, _: Claim) -> tuple[bool, str]:
    if not rec.ok:
        return False, "recovery failed"
    res = rec.result or {}
    if res.get("complete") is True:
        return True, "recovered_size == original_size"
    return (
        False,
        f"partial recovery: {res.get('recovered_size')}/{res.get('original_size')}",
    )


def gate_xor_valid(rec: ExecutionRecord, _: Claim) -> tuple[bool, str]:
    if not rec.ok:
        return False, "decrypt failed"
    hits = (rec.result or {}).get("signatures", [])
    strong = [h for h in hits if h.get("kind") in {"ext2/3/4", "ntfs", "fat32", "fat"}]
    if strong:
        return True, f"{len(strong)} strong signature(s)"
    return False, "no filesystem signature after decrypt"


def gate_fs_present(rec: ExecutionRecord, _: Claim) -> tuple[bool, str]:
    if not rec.ok:
        return False, "partition scan failed"
    parts = rec.result or []
    good = [p for p in parts if isinstance(p, dict) and p.get("fs_type")]
    return (
        (True, f"{len(good)} partition(s) with FS")
        if good
        else (False, "no FS partitions")
    )


def gate_anomaly(rec: ExecutionRecord, _: Claim) -> tuple[bool, str]:
    return (True, "tool ok") if rec.ok else (False, "tool failed")


def gate_summary(rec: ExecutionRecord, _: Claim) -> tuple[bool, str]:
    return (True, "ok") if rec.ok else (False, "tool failed")


GATE_REGISTRY: dict[str, Gate] = {
    "deleted_claim": gate_deleted_claim,
    "recovered_complete": gate_recovered_complete,
    "xor_signature_valid": gate_xor_valid,
    "fs_present": gate_fs_present,
    "anomaly": gate_anomaly,
    "summary": gate_summary,
}


def validate_claim(
    claim: Claim, executions: dict[str, ExecutionRecord]
) -> tuple[bool, str]:
    rec = executions.get(claim.execution_id)
    if rec is None:
        return False, f"fabricated citation: {claim.execution_id}"
    gate = GATE_REGISTRY.get(claim.type)
    if gate is None:
        return False, f"no gate for claim type: {claim.type}"
    ok, reason = gate(rec, claim)
    log.info("gate[%s] %s → %s (%s)", claim.type, claim.execution_id, ok, reason)
    return ok, reason


# =============================================================================
# 5. TOOL REGISTRY (shared by MCP + OpenAI function calling)
# =============================================================================
TOOLS: dict[str, dict[str, Any]] = {}
_EXEC_COUNTER = {"n": 0}


def _next_exec_id() -> str:
    _EXEC_COUNTER["n"] += 1
    return f"exec_{_EXEC_COUNTER['n']:04d}"


def register_tool(name: str, description: str, schema: dict[str, Any]) -> Callable:
    def deco(fn: Callable) -> Callable:
        TOOLS[name] = {
            "fn": fn,
            "openai": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                },
            },
        }
        return fn

    return deco


def dispatch(name: str, args: dict[str, Any]) -> ExecutionRecord:
    exec_id = _next_exec_id()
    if name not in TOOLS:
        rec = ExecutionRecord(
            execution_id=exec_id,
            tool=name,
            args=args,
            ok=False,
            error=f"unknown tool {name}",
        )
        AUDIT.write("tool", **rec.model_dump())
        return rec
    try:
        result = TOOLS[name]["fn"](**args)
        rec = ExecutionRecord(
            execution_id=exec_id, tool=name, args=args, ok=True, result=result
        )
    except Exception as e:
        log.exception("tool %s failed", name)
        rec = ExecutionRecord(
            execution_id=exec_id, tool=name, args=args, ok=False, error=str(e)
        )
    AUDIT.write("tool", **rec.model_dump())
    return rec


def tool_schemas() -> list[dict]:
    return [t["openai"] for t in TOOLS.values()]


# ---- Tool implementations -------------------------------------------------
@register_tool(
    "list_partitions",
    "Enumerate partitions. Returns byte offsets for list_directory/search/extract.",
    {
        "type": "object",
        "properties": {"image_path": {"type": "string"}},
        "required": ["image_path"],
    },
)
def _t_list_partitions(image_path: str) -> list[dict]:
    img = CONFIG.check(image_path)
    return [p.model_dump() for p in list_partitions(img)]


@register_tool(
    "list_directory",
    "List directory entries. deleted=true for UNALLOC files.",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "offset": {"type": "integer"},
            "path": {"type": "string", "default": "/"},
        },
        "required": ["image_path", "offset"],
    },
)
def _t_list_directory(image_path: str, offset: int, path: str = "/") -> list[dict]:
    return [
        f.model_dump() for f in list_directory(CONFIG.check(image_path), offset, path)
    ]


@register_tool(
    "search_files",
    "Recursively search filenames by Python regex (e.g. '\\\\.pdf$').",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "offset": {"type": "integer"},
            "pattern": {"type": "string"},
            "start": {"type": "string", "default": "/"},
            "include_deleted": {"type": "boolean", "default": True},
        },
        "required": ["image_path", "offset", "pattern"],
    },
)
def _t_search_files(
    image_path: str,
    offset: int,
    pattern: str,
    start: str = "/",
    include_deleted: bool = True,
) -> list[dict]:
    hits = search_files(
        CONFIG.check(image_path), offset, pattern, start, include_deleted
    )
    return [f.model_dump() for f in hits]


@register_tool(
    "extract_file",
    "Extract a live file to workspace. Requires MOUNTFILE_READONLY=0.",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "offset": {"type": "integer"},
            "path": {"type": "string"},
        },
        "required": ["image_path", "offset", "path"],
    },
)
def _t_extract_file(image_path: str, offset: int, path: str) -> dict:
    dest = get_file(CONFIG.check(image_path), offset, path)
    return {"dest": str(dest), "size": dest.stat().st_size, "sha256": hash_file(dest)}


@register_tool(
    "recover_deleted_file",
    "Recover an unallocated file by inode. Requires MOUNTFILE_READONLY=0.",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "offset": {"type": "integer"},
            "inode": {"type": "integer"},
            "original_size": {"type": "integer"},
        },
        "required": ["image_path", "offset", "inode"],
    },
)
def _t_recover(
    image_path: str, offset: int, inode: int, original_size: int | None = None
) -> dict:
    r = recover_deleted_file(CONFIG.check(image_path), offset, inode, original_size)
    return r.model_dump()


@register_tool(
    "detect_signatures",
    "Detect FS boot-sector signatures at image start. Run this FIRST.",
    {
        "type": "object",
        "properties": {"image_path": {"type": "string"}},
        "required": ["image_path"],
    },
)
def _t_detect_signatures(image_path: str) -> list[dict]:
    return [h.model_dump() for h in detect_signatures(CONFIG.check(image_path))]


@register_tool(
    "try_xor_decrypt",
    "Decrypt with a named XOR key; reports signatures on output.",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "key_name": {"type": "string"},
            "output": {"type": "string"},
        },
        "required": ["image_path", "key_name", "output"],
    },
)
def _t_try_xor(image_path: str, key_name: str, output: str) -> dict:
    destination = (CONFIG.workspace / Path(output).name).resolve()
    return try_xor_decrypt(CONFIG.check(image_path), key_name, destination)


@register_tool(
    "auto_decrypt",
    "Iterate XOR keys; return first producing a valid FS signature.",
    {
        "type": "object",
        "properties": {"image_path": {"type": "string"}},
        "required": ["image_path"],
    },
)
def _t_auto_decrypt(image_path: str) -> dict | None:
    return auto_decrypt(CONFIG.check(image_path))


@register_tool(
    "hash_file",
    "SHA-256 of a workspace file (for chain-of-custody).",
    {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
def _t_hash_file(path: str) -> dict:
    p = (CONFIG.workspace / Path(path).name).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    return {"path": str(p), "sha256": hash_file(p), "size": p.stat().st_size}


# =============================================================================
# 6. MULTI-AGENT ORCHESTRATION
# =============================================================================
def _client() -> Any:
    if OpenAI is None:
        raise RuntimeError("pip install openai")
    return OpenAI(
        api_key=os.getenv("OPENAI_API_KEY", "ollama"),
        base_url=os.getenv("OPENAI_BASE_URL") or None,
    )


def _chat(
    system: str, user: str, tools: list[dict] | None = None, temperature: float = 0.2
) -> dict:
    resp = _client().chat.completions.create(
        model=CONFIG.model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=tools or [],
        tool_choice="auto" if tools else None,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content or "{}")


PLANNER_PROMPT = """You are the PLANNER agent in a forensic investigation.
Given an image path and a user query, produce an ordered plan of tool calls.

RULES:
1. FIRST call: detect_signatures. If no strong FS signature → auto_decrypt, then detect again.
2. Then list_partitions to obtain offsets.
3. THEN explore: list_directory / search_files as needed.
4. ONLY extract/recover files the user asked for.
5. NEVER invent offsets; they must come from prior results.
6. Output JSON:
{"hypothesis": "...", "steps": [{"tool": "...", "args": {...}, "rationale": "..."}]}
"""

ANALYST_PROMPT = """You are the ANALYST agent.
Given tool executions, emit atomic claims. Each claim MUST cite execution_id.

Output JSON: {"claims": [{"type": "<type>", "statement": "...",
"execution_id": "exec_NNNN", "confidence": 0.0-1.0}]}

Valid types: deleted_claim, recovered_complete, xor_signature_valid,
fs_present, anomaly, summary.

Do NOT emit a claim unless a corresponding execution_id exists in the input.
"""

SKEPTIC_PROMPT = """You are the SKEPTIC agent. Your job is to FALSIFY claims.
For each claim, try to find a reason it may be wrong:
 - weak evidence, partial recovery, anti-forensic artifacts, ambiguous signature.
Return JSON: {"verdicts": {"exec_NNNN": {"keep": true|false, "reason": "..."}}}
"""


class Planner:
    def plan(self, query: str, image: Path) -> Plan:
        raw = _chat(PLANNER_PROMPT, f"IMAGE: {image}\nQUERY: {query}")
        try:
            return Plan(**raw)
        except Exception as e:
            log.error("planner invalid plan: %s / %s", e, raw)
            return Plan(
                steps=[
                    ToolCall(tool="detect_signatures", args={"image_path": str(image)}),
                    ToolCall(tool="list_partitions", args={"image_path": str(image)}),
                ],
                hypothesis="fallback baseline plan",
            )


class Executor:
    def run(self, plan: Plan, image: Path) -> list[ExecutionRecord]:
        records: list[ExecutionRecord] = []
        limit = max(1, CONFIG.max_agent_rounds)
        if len(plan.steps) > limit:
            raise RuntimeError(
                "agent plan exceeds MOUNTFILE_MAX_ROUNDS=%d; refusing to execute"
                % limit
            )
        for step in plan.steps:
            args = dict(step.args)
            spec_props = (
                TOOLS.get(step.tool, {})
                .get("openai", {})
                .get("function", {})
                .get("parameters", {})
                .get("properties", {})
            )
            if "image_path" in spec_props:
                args.setdefault("image_path", str(image))
            log.info("exec %s(%s)", step.tool, args)
            records.append(dispatch(step.tool, args))
        return records


class Analyst:
    def analyze(self, query: str, records: list[ExecutionRecord]) -> list[Claim]:
        payload = [r.model_dump() for r in records]
        raw = _chat(
            ANALYST_PROMPT, f"QUERY: {query}\nEXECUTIONS: {json.dumps(payload)[:12000]}"
        )
        out: list[Claim] = []
        for c in raw.get("claims", []):
            try:
                out.append(Claim(**c))
            except Exception as e:
                log.warning("invalid claim: %s / %s", e, c)
        return out


class Skeptic:
    def challenge(
        self, claims: list[Claim], records: list[ExecutionRecord]
    ) -> list[Claim]:
        if not claims:
            return []
        raw = _chat(
            SKEPTIC_PROMPT,
            f"CLAIMS: {json.dumps([c.model_dump() for c in claims])}\n"
            f"EXECUTIONS: {json.dumps([r.model_dump() for r in records])[:12000]}",
        )
        verdicts = raw.get("verdicts", {})
        kept: list[Claim] = []
        for c in claims:
            v = verdicts.get(c.execution_id, {"keep": True, "reason": "no verdict"})
            if v.get("keep", True):
                kept.append(c)
            else:
                log.info("skeptic rejected %s: %s", c.execution_id, v.get("reason"))
        return kept


class Orchestrator:
    def __init__(self) -> None:
        self.planner = Planner()
        self.executor = Executor()
        self.analyst = Analyst()
        self.skeptic = Skeptic()

    def investigate(self, query: str, image: Path) -> InvestigationReport:
        image = CONFIG.allow(image)
        sha = Audit.sha256_file(image)
        AUDIT.write("investigation_start", image=str(image), sha256=sha, query=query)

        plan = self.planner.plan(query, image)
        executions = self.executor.run(plan, image)
        exec_map = {r.execution_id: r for r in executions}

        claims = self.analyst.analyze(query, executions)
        claims = self.skeptic.challenge(claims, executions)

        accepted, rejected = [], []
        for c in claims:
            ok, reason = validate_claim(c, exec_map)
            (accepted if ok else rejected).append(c)
            if not ok:
                log.info("gate rejected %s: %s", c.execution_id, reason)

        verdict = self._verdict(accepted, rejected)
        summary = self._summary(query, image, sha, accepted, rejected, executions)
        report = InvestigationReport(
            query=query,
            image=str(image),
            sha256=sha,
            plan=plan,
            executions=executions,
            accepted_claims=accepted,
            rejected_claims=rejected,
            verdict=verdict,
            summary_md=summary,
        )
        AUDIT.write(
            "investigation_end",
            verdict=verdict,
            accepted=len(accepted),
            rejected=len(rejected),
        )
        return report

    @staticmethod
    def _verdict(accepted: list[Claim], rejected: list[Claim]) -> str:
        if not accepted and rejected:
            return "REJECTED"
        if accepted and rejected:
            return "PARTIAL"
        if accepted:
            return "CONFIRMED"
        return "NEEDS_MORE_DATA"

    @staticmethod
    def _summary(query, image, sha, accepted, rejected, executions) -> str:
        lines = [
            "# Investigation report",
            "",
            f"**Image:** `{image}`",
            f"**SHA-256:** `{sha}`",
            f"**Query:** {query}",
            f"**Tool calls:** {len(executions)}",
            "",
            "## Accepted claims",
        ]
        if not accepted:
            lines.append("_none_")
        for c in accepted:
            lines.append(
                f"- **{c.type}** ({c.confidence:.2f}) — {c.statement} "
                f"_[`{c.execution_id}`]_"
            )
        lines += ["", "## Rejected claims"]
        if not rejected:
            lines.append("_none_")
        for c in rejected:
            lines.append(f"- ~~{c.statement}~~ — gate failed on `{c.execution_id}`")
        return "\n".join(lines)


# =============================================================================
# 7. MCP SERVER
# =============================================================================
def build_mcp(image: Path):
    if FastMCP is None:
        raise RuntimeError("pip install 'mcp[cli]'")
    CONFIG.allow(image)
    mcp = FastMCP("mount-file-ai")

    for name, spec in TOOLS.items():
        fn = spec["fn"]
        desc = spec["openai"]["function"]["description"]
        mcp.tool(name=name, description=desc)(fn)

    @mcp.tool()
    def investigate(query: str) -> dict:
        """Run the full multi-agent investigation on the allowlisted image."""
        report = Orchestrator().investigate(query, image)
        return report.model_dump()

    return mcp


def serve_mcp(image: Path) -> None:
    mcp = build_mcp(image)
    AUDIT.write("mcp_start", image=str(image))
    log.info("MCP server ready on stdio (image=%s)", image)
    mcp.run()


# =============================================================================
# 8. CLI
# =============================================================================
def cmd_inspect(args: argparse.Namespace) -> int:
    img = CONFIG.allow(args.image)
    print(f"image : {img}")
    print(f"sha256: {Audit.sha256_file(img)}")
    print("\nsignatures:")
    hits = detect_signatures(img)
    print(
        "\n".join(f"  {h.kind:10s} @0x{h.offset:x}  {h.hint}" for h in hits)
        or "  (none)"
    )
    print("\npartitions:")
    try:
        parts = list_partitions(img)
        if not parts:
            print("  (empty)")
        for p in parts:
            print(f"  #{p.addr} off=0x{p.offset:x} fs={p.fs_type or '?'} {p.desc}")
    except Exception as e:
        print(f"  (unreadable: {e})")
        print(
            "  → try: python mountfile_ai.py agent --image "
            f"{img} --query 'decrypt and inspect'"
        )
        return 1
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    report = Orchestrator().investigate(args.query, Path(args.image))
    print(report.summary_md)
    if args.json:
        print()
        print(json.dumps(report.model_dump(), indent=2, default=str))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    serve_mcp(Path(args.image))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mountfile_ai")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="human inspection (no AI)")
    pi.add_argument("--image", required=True)
    pi.set_defaults(func=cmd_inspect)

    pa = sub.add_parser("agent", help="autonomous multi-agent analysis")
    pa.add_argument("--image", required=True)
    pa.add_argument("--query", required=True)
    pa.add_argument("--json", action="store_true")
    pa.set_defaults(func=cmd_agent)

    pm = sub.add_parser("mcp", help="run MCP server on stdio")
    pm.add_argument("--image", required=True)
    pm.set_defaults(func=cmd_mcp)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
