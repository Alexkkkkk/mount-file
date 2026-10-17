#!/usr/bin/env python3
"""
mount-file AI Deep Integration (single file)
============================================

Turns mount-file's TSK-based raw-image tooling into a fully agentic MCP server
with autonomous planning, XOR auto-decryption for MStar/MediaTek dumps,
signature detection, read-only safety mode and append-only audit log.

Run modes
---------
  inspect  -> one-shot human inspection (no AI)
  mcp      -> stdio MCP server (Claude Desktop / Cursor / Open WebUI)
  agent    -> autonomous agent loop (OpenAI / Ollama / vLLM)

Env vars
--------
  OPENAI_API_KEY      required for `agent` (use "ollama" for local)
  OPENAI_BASE_URL     e.g. http://localhost:11434/v1 for Ollama
  MOUNTFILE_MODEL     default: gpt-4o-mini
  MOUNTFILE_AUDIT     default: ./mountfile_audit.jsonl
  MOUNTFILE_WORKDIR   default: ./ai_workspace
  MOUNTFILE_READONLY  default: 1 (set 0 to enable extraction / decryption)

Dependencies
------------
  pip install "mcp[cli]" pydantic pytsk3 openai

Examples
--------
  # 1) Human inspection
  python mountfile_ai.py inspect --image dump.bin

  # 2) Autonomous agent (local Ollama)
  export OPENAI_API_KEY=ollama
  export OPENAI_BASE_URL=http://localhost:11434/v1
  export MOUNTFILE_MODEL=qwen2.5-coder:7b
  python mountfile_ai.py agent --image dump.bin \
      --query "find all deleted PDFs and extract them"

  # 3) MCP server for Claude Desktop (add to claude_desktop_config.json):
  #   "mount-file": {
  #     "command": "python",
  #     "args": ["/abs/path/mountfile_ai.py", "mcp", "--image", "/abs/dump.bin"]
  #   }
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# --- Optional heavy deps: degrade gracefully ---------------------------------
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
# CONFIG
# =============================================================================
class Config:
    def __init__(self) -> None:
        self.read_only = os.getenv("MOUNTFILE_READONLY", "1") != "0"
        self.audit_path = Path(os.getenv("MOUNTFILE_AUDIT", "./mountfile_audit.jsonl"))
        self.workspace = Path(os.getenv("MOUNTFILE_WORKDIR", "./ai_workspace"))
        self.model = os.getenv("MOUNTFILE_MODEL", "gpt-4o-mini")
        self.allowed_images: set[Path] = set()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def allow(self, path: str | Path) -> Path:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        if not p.is_file():
            raise ValueError(f"{p} is not a file")
        self.allowed_images.add(p)
        return p

    def check(self, path: str | Path) -> Path:
        p = Path(path).expanduser().resolve()
        if p not in self.allowed_images:
            raise PermissionError(f"image {p} not in allowlist")
        return p


CONFIG = Config()


# =============================================================================
# AUDIT (append-only JSONL with chain-of-custody hashes)
# =============================================================================
class Audit:
    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def write(self, event: str, **data: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")


AUDIT = Audit(CONFIG.audit_path)


# =============================================================================
# PYDANTIC MODELS (tool I/O contracts)
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
    hint: str


# =============================================================================
# CORE TSK WRAPPER
# =============================================================================
def _tsk_require() -> None:
    if pytsk3 is None:
        raise RuntimeError("pytsk3 is not installed (pip install pytsk3)")


def _open_fs(image: Path, offset: int) -> Any:
    _tsk_require()
    img = pytsk3.Img_Info(str(image))
    return pytsk3.FS_Info(img, offset=offset)


def list_partitions(image: Path) -> list[PartitionInfo]:
    _tsk_require()
    img = pytsk3.Img_Info(str(image))
    try:
        vol = pytsk3.Volume_Info(img)
    except Exception as e:
        raise RuntimeError(f"cannot read volume table: {e}") from e
    parts: list[PartitionInfo] = []
    for p in vol:
        if p.len == 0:
            continue
        bs = getattr(vol.info, "block_size", 512) or 512
        offset = p.start * bs
        fs_type = None
        try:
            fs = pytsk3.FS_Info(img, offset=offset)
            fs_type = str(fs.info.ftype)
        except Exception:
            pass
        parts.append(PartitionInfo(
            addr=p.addr,
            offset=offset,
            length=p.len * bs,
            desc=(p.desc or b"").decode("utf-8", "replace").strip() or f"part{p.addr}",
            fs_type=fs_type,
        ))
    return parts


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
        out.append(FileEntry(
            name=decoded,
            path=path.rstrip("/") + "/" + decoded,
            inode=meta.addr,
            size=meta.size,
            is_directory=(meta.type == pytsk3.TSK_FS_META_TYPE_DIR),
            deleted=bool(meta.flags & pytsk3.TSK_FS_META_FLAG_UNALLOC),
            mtime=float(meta.mtime) if meta.mtime else None,
        ))
    return out


def get_file(image: Path, offset: int, path: str, output: Path | None = None) -> Path:
    if CONFIG.read_only:
        raise PermissionError("read-only mode; set MOUNTFILE_READONLY=0")
    fs = _open_fs(image, offset)
    f = fs.open(path=path)
    data = f.read_random(0, f.info.meta.size)
    dest = output or (CONFIG.workspace / Path(path).name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    AUDIT.write(
        "extract", image=str(image), offset=offset, path=path,
        dest=str(dest), sha256=hashlib.sha256(data).hexdigest(),
    )
    return dest


# =============================================================================
# SIGNATURE DETECTION
# =============================================================================
SIGNATURES: list[tuple[bytes, int, str, str]] = [
    (b"\x53\xEF", 0x438, "ext2/3/4", "superblock magic at 0x438"),
    (b"NTFS    ", 3, "ntfs", "NTFS OEM at offset 3"),
    (b"MSWIN4.1", 3, "fat", "FAT boot sector"),
    (b"FAT12", 54, "fat12", ""),
    (b"FAT16", 54, "fat16", ""),
    (b"FAT32", 82, "fat32", ""),
    (b"\x55\xAA", 510, "mbr", "MBR boot sig at 0x1FE"),
]


def detect_signatures(image: Path, span: int = 1 << 20) -> list[SignatureHit]:
    hits: list[SignatureHit] = []
    with image.open("rb") as f:
        head = f.read(span)
    for magic, off, kind, hint in SIGNATURES:
        if off + len(magic) <= len(head) and head[off:off + len(magic)] == magic:
            hits.append(SignatureHit(offset=off, kind=kind, hint=hint))
    return hits


# =============================================================================
# XOR (MStar / MediaTek)
# =============================================================================
# !!! PLACEHOLDER KEYS !!!
# Replace these with the real keys from
#   https://github.com/Alexkkkkk/mount-file (resources/XOR_MST_*.bin)
# The structure is: name -> repeating byte key.
XOR_KEYS: dict[str, bytes] = {
    "mstar_zero": b"\x00" * 16,   # identity (no-op, for smoke tests)
    "mstar_a":    b"\xA5" * 16,   # example — REPLACE with real key
    "mtk_a":      b"\x5A" * 16,   # example — REPLACE with real key
}


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    k = (key * (len(data) // len(key) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, k))


def try_xor_decrypt(image: Path, key_name: str, output: Path) -> dict[str, Any]:
    if key_name not in XOR_KEYS:
        raise KeyError(f"unknown key {key_name}; known: {list(XOR_KEYS)}")
    if CONFIG.read_only:
        raise PermissionError("read-only mode; set MOUNTFILE_READONLY=0")
    key = XOR_KEYS[key_name]
    with image.open("rb") as src, output.open("wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(_xor_bytes(chunk, key))
    hits = detect_signatures(output)
    AUDIT.write(
        "xor_decrypt", image=str(image), key=key_name,
        output=str(output), hits=[h.model_dump() for h in hits],
    )
    return {
        "output": str(output),
        "key": key_name,
        "signatures": [h.model_dump() for h in hits],
    }


def auto_decrypt(image: Path) -> dict[str, Any] | None:
    """Try every known key; return the first that yields an FS signature."""
    if CONFIG.read_only:
        raise PermissionError("read-only mode; set MOUNTFILE_READONLY=0")
    for name in XOR_KEYS:
        out = CONFIG.workspace / f"{image.stem}.{name}.bin"
        try:
            res = try_xor_decrypt(image, name, out)
        except Exception as e:
            log.warning("xor %s failed: %s", name, e)
            continue
        strong = [h for h in res["signatures"] if h["kind"] != "mbr"]
        if strong:
            return res
    return None


# =============================================================================
# MCP SERVER INSTANCE (created before tools so the decorator can use it)
# =============================================================================
mcp = FastMCP("mount-file-ai") if FastMCP is not None else None


# =============================================================================
# TOOL REGISTRY (shared between MCP and Agent)
# =============================================================================
TOOLS: dict[str, dict[str, Any]] = {}


def tool(name: str, description: str, params_schema: dict[str, Any]):
    """Register a function as both an OpenAI tool and an MCP tool."""
    def deco(fn):
        TOOLS[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": params_schema,
                },
            },
        }
        if mcp is not None:
            try:
                mcp.tool(name=name, description=description)(fn)
            except Exception as e:  # pragma: no cover
                log.warning("MCP registration failed for %s: %s", name, e)
        return fn
    return deco


# ---- Tool implementations --------------------------------------------------
@tool(
    "list_partitions",
    "Enumerate partitions in a raw disk image. Returns byte offsets usable by "
    "list_directory / search_files / extract_file.",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to raw image"},
        },
        "required": ["image_path"],
    },
)
def tool_list_partitions(image_path: str) -> list[dict]:
    img = CONFIG.check(image_path)
    return [p.model_dump() for p in list_partitions(img)]


@tool(
    "list_directory",
    "List entries of a directory inside a mounted filesystem. Deleted files "
    "have deleted=true. Use offset from list_partitions.",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "offset": {"type": "integer", "description": "Partition byte offset"},
            "path": {"type": "string", "default": "/"},
        },
        "required": ["image_path", "offset"],
    },
)
def tool_list_directory(image_path: str, offset: int, path: str = "/") -> list[dict]:
    img = CONFIG.check(image_path)
    return [f.model_dump() for f in list_directory(img, offset, path)]


@tool(
    "search_files",
    "Recursively search filenames matching a Python regex. Example pattern: "
    r"\.(pdf|docx)$",
    {
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "offset": {"type": "integer"},
            "pattern": {"type": "string", "description": "Python regex"},
            "start": {"type": "string", "default": "/"},
        },
        "required": ["image_path", "offset", "pattern"],
    },
)
def tool_search_files(
    image_path: str, offset: int, pattern: str, start: str = "/"
) -> list[dict]:
    img = CONFIG.check(image_path)
    rx = re.compile(pattern, re.IGNORECASE)
    matches: list[dict] = []
    stack = [start]
    seen: set[str] = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        try:
            entries = list_directory(img, offset, cur)
        except Exception:
            continue
        for e in entries:
            if rx.search(e.name):
                matches.append(e.model_dump())
            if e.is_directory and not e.deleted:
                stack.append(e.path)
    return matches


@tool(
    "extract_file",
    "Extract a single file from the image into the workspace. Requires "
    "MOUNTFILE_READONLY=0.",
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
def tool_extract_file(image_path: str, offset: int, path: str) -> dict:
    img = CONFIG.check(image_path)
    dest = get_file(img, offset, path)
    return {"dest": str(dest), "size": dest.stat().st_size}


@tool(
    "detect_signatures",
    "Detect filesystem boot-sector signatures at the start of an image "
    "(MBR/FAT/NTFS/EXT). Run this FIRST for every unknown image.",
    {
        "type": "object",
        "properties": {"image_path": {"type": "string"}},
        "required": ["image_path"],
    },
)
def tool_detect_signatures(image_path: str) -> list[dict]:
    img = CONFIG.check(image_path)
    return [h.model_dump() for h in detect_signatures(img)]


@tool(
    "try_xor_decrypt",
    "Decrypt the image with one known XOR key and report detected signatures "
    "on the result. Use when the original image has no FS signature.",
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
def tool_try_xor_decrypt(image_path: str, key_name: str, output: str) -> dict:
    img = CONFIG.check(image_path)
    return try_xor_decrypt(img, key_name, Path(output))


@tool(
    "auto_decrypt",
    "Iterate all known XOR keys and return the first that produces a valid "
    "filesystem signature. Requires MOUNTFILE_READONLY=0.",
    {
        "type": "object",
        "properties": {"image_path": {"type": "string"}},
        "required": ["image_path"],
    },
)
def tool_auto_decrypt(image_path: str) -> dict | None:
    img = CONFIG.check(image_path)
    return auto_decrypt(img)


# =============================================================================
# AGENT LOOP (OpenAI-compatible: OpenAI / Ollama / vLLM)
# =============================================================================
SYSTEM_PROMPT = """You are a forensic filesystem analyst working with mount-file.
You have typed tools to inspect raw disk images. Follow this protocol:

1. ALWAYS call detect_signatures first.
2. If no strong filesystem signature is present: call auto_decrypt to try
   XOR keys. If it succeeds, re-run detect_signatures on the new image.
3. Call list_partitions to obtain byte offsets.
4. Use list_directory / search_files to explore.
5. Only call extract_file for files the user actually asked for.
6. Never invent offsets — always derive them from previous tool outputs.
7. When finished, reply with a concise markdown summary table:
   path | size | status | notes.

All extraction is audited (append-only JSONL). Do not modify the source image.
"""


def _dispatch(name: str, args: dict) -> Any:
    if name not in TOOLS:
        return {"error": f"unknown tool {name}"}
    try:
        result = TOOLS[name]["fn"](**args)
        AUDIT.write(
            "tool_call", tool=name, args=args, ok=True,
            result_size=len(json.dumps(result, default=str)),
        )
        return result
    except Exception as e:
        AUDIT.write("tool_call", tool=name, args=args, ok=False, error=str(e))
        return {"error": str(e), "tool": name}


def run_agent(query: str, image: Path, max_iters: int = 20) -> str:
    if OpenAI is None:
        raise RuntimeError("openai package required for agent mode")
    if not (os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL")):
        raise RuntimeError("set OPENAI_API_KEY (or OPENAI_BASE_URL for Ollama)")

    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY", "ollama"),
        base_url=os.getenv("OPENAI_BASE_URL") or None,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"IMAGE: {image}\n\nTASK: {query}"},
    ]
    tool_schemas = [t["schema"] for t in TOOLS.values()]

    for i in range(max_iters):
        log.info("agent iteration %d/%d", i + 1, max_iters)
        resp = client.chat.completions.create(
            model=CONFIG.model,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
        )
        msg = resp.choices[0].message

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            return msg.content or "(no response)"

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            log.info("→ %s(%s)", name, args)
            result = _dispatch(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str)[:8000],
            })
    return "Max iterations reached without final answer."


# =============================================================================
# CLI COMMANDS
# =============================================================================
def cmd_mcp(args: argparse.Namespace) -> int:
    if mcp is None:
        print("error: mcp package not installed", file=sys.stderr)
        return 2
    img = CONFIG.allow(args.image)
    AUDIT.write("mcp_start", image=str(img), sha256=Audit.sha256(img))
    log.info("starting MCP server for %s (read_only=%s)", img, CONFIG.read_only)
    mcp.run()
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    img = CONFIG.allow(args.image)
    AUDIT.write(
        "agent_start", image=str(img), sha256=Audit.sha256(img), query=args.query,
    )
    print(run_agent(args.query, img))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    img = CONFIG.allow(args.image)
    print(f"image : {img}")
    print(f"sha256: {Audit.sha256(img)}")
    print("\nsignatures:")
    hits = detect_signatures(img)
    if not hits:
        print("  (none)")
    for h in hits:
        print(f"  {h.kind:10s} @0x{h.offset:x}  {h.hint}")
    print("\npartitions:")
    try:
        parts = list_partitions(img)
        for p in parts:
            print(f"  #{p.addr}  off=0x{p.offset:x}  len=0x{p.length:x}  "
                  f"fs={p.fs_type or '?'}  {p.desc}")
        if not parts:
            print("  (empty)")
    except Exception as e:
        print(f"  (volume table unreadable: {e})")
        print("  → try: python mountfile_ai.py agent "
              f"--image {img} --query 'decrypt and inspect'")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mountfile_ai")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mcp", help="run stdio MCP server")
    pm.add_argument("--image", required=True)
    pm.set_defaults(func=cmd_mcp)

    pa = sub.add_parser("agent", help="run autonomous agent")
    pa.add_argument("--image", required=True)
    pa.add_argument("--query", required=True)
    pa.set_defaults(func=cmd_agent)

    pi = sub.add_parser("inspect", help="one-shot human inspection (no AI)")
    pi.add_argument("--image", required=True)
    pi.set_defaults(func=cmd_inspect)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
