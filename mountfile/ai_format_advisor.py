# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Metadata-only format advice for files opened by mountfile.

The advisor deliberately accepts a :class:`FileFingerprint` rather than a file
path that it can inspect.  It never opens, hashes, or transmits the file being
diagnosed.  Callers may provide a short, already-derived magic-byte summary
(for example ``"89504e47"`` for PNG) together with ordinary metadata.

``Remember`` stores only those metadata fingerprints and the user's selected
recipe in ``~/.mountfile/ai_kb.json`` by default.  Its path is injectable for
applications and tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


DEFAULT_REMEMBER_PATH = Path("~/.mountfile/ai_kb.json")
MAX_MAGIC_SUMMARY_LENGTH = 256
MAX_REMEMBERED_ENTRIES = 500


# ---------------------------------------------------------------------------
# Metadata fingerprint


def _normalise_extension(value: Optional[Union[str, os.PathLike]]) -> str:
    """Return an extension in one predictable, lower-case form.

    This function only handles the supplied filename string.  It does not
    perform a filesystem lookup.
    """

    if value is None:
        return ""
    extension = os.fspath(value).strip().lower()
    if extension and not extension.startswith("."):
        extension = "." + extension
    return extension


def _extension_from_filename(filename: str) -> str:
    # A leading dot in a filename such as ``.profile`` is not an extension.
    suffix = os.path.splitext(filename)[1]
    return _normalise_extension(suffix)


def _normalise_magic_summary(value: Any) -> str:
    """Normalise a caller-provided magic summary without inspecting a file.

    Bytes are accepted only as an explicitly supplied summary and converted to
    hex.  The advisor never obtains bytes from a path and never retains a raw
    file prefix.  A bounded value also prevents an accidental large payload
    from being persisted or sent to another component.
    """

    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.hex()
    else:
        text = str(value).strip()
    return text[:MAX_MAGIC_SUMMARY_LENGTH]


@dataclass(frozen=True, init=False)
class FileFingerprint:
    """A file fingerprint made exclusively from caller-provided metadata.

    Parameters are intentionally metadata fields, not a path to inspect:

    ``filename``
        Display name of the file.  A path-like value is reduced to its final
        component, without opening it.
    ``size``
        Size supplied by the caller, in bytes.  ``None`` is allowed when the
        caller does not know it.
    ``extension``
        Optional extension; when omitted it is derived from ``filename``.
    ``magic_summary``
        Optional short summary supplied by the caller, never read by this
        module from the file itself.

    ``name``, ``magic``, ``magic_bytes``, ``magic_bytes_summary`` and ``path``
    are accepted as compatibility aliases for callers with different metadata
    vocabularies.
    """

    filename: str
    size: Optional[int]
    extension: str
    magic_summary: str

    def __init__(
        self,
        filename: Optional[Union[str, os.PathLike]] = None,
        size: Optional[int] = None,
        extension: Optional[Union[str, os.PathLike]] = None,
        magic_summary: Any = "",
        *,
        name: Optional[Union[str, os.PathLike]] = None,
        path: Optional[Union[str, os.PathLike]] = None,
        ext: Optional[Union[str, os.PathLike]] = None,
        magic: Any = None,
        magic_bytes: Any = None,
        magic_bytes_summary: Any = None,
        magic_signature: Any = None,
    ) -> None:
        supplied_name = filename if filename is not None else name
        if supplied_name is None:
            supplied_name = path if path is not None else ""
        raw_name = os.fspath(supplied_name)
        display_name = os.path.basename(raw_name) or raw_name

        if ext is not None and extension is None:
            extension = ext
        if magic is not None:
            magic_summary = magic
        if magic_bytes is not None:
            magic_summary = magic_bytes
        if magic_bytes_summary is not None:
            magic_summary = magic_bytes_summary
        if magic_signature is not None:
            magic_summary = magic_signature

        if size is not None:
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("size must be a non-negative integer or None")
        normalised_extension = _normalise_extension(extension)
        if not normalised_extension:
            normalised_extension = _extension_from_filename(display_name)

        object.__setattr__(self, "filename", display_name)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "extension", normalised_extension)
        object.__setattr__(self, "magic_summary", _normalise_magic_summary(magic_summary))

    @property
    def name(self) -> str:
        """Alias for ``filename`` used by older advisor integrations."""

        return self.filename

    @property
    def ext(self) -> str:
        return self.extension

    @property
    def magic(self) -> str:
        """Alias for the non-sensitive, caller-provided magic summary."""

        return self.magic_summary

    @property
    def magic_bytes(self) -> str:
        return self.magic_summary

    @property
    def magic_bytes_summary(self) -> str:
        return self.magic_summary

    def to_dict(self) -> Dict[str, Any]:
        """Return only metadata suitable for JSON persistence or prompts."""

        return {
            "filename": self.filename,
            "size": self.size,
            "extension": self.extension,
            "magic_summary": self.magic_summary,
        }

    as_dict = to_dict

    @classmethod
    def from_metadata(
        cls,
        filename: Union[str, os.PathLike],
        size: Optional[int] = None,
        extension: Optional[Union[str, os.PathLike]] = None,
        magic_summary: Any = "",
        **aliases: Any,
    ) -> "FileFingerprint":
        """Construct a fingerprint without doing any filesystem I/O."""

        return cls(filename, size, extension, magic_summary, **aliases)


def build_fingerprint(
    filename: Union[str, os.PathLike],
    size: Optional[int] = None,
    extension: Optional[Union[str, os.PathLike]] = None,
    magic_summary: Any = "",
    **aliases: Any,
) -> FileFingerprint:
    """Compatibility factory for metadata supplied by a caller.

    Unlike similarly named helpers in older advisor code, this function never
    opens ``filename`` and never calls ``stat``.  The caller must provide the
    size when it is needed.
    """

    return FileFingerprint(filename, size, extension, magic_summary, **aliases)


# ---------------------------------------------------------------------------
# Knowledge base and actionable recipes


@dataclass(frozen=True)
class FormatSpec:
    """Knowledge-base entry for one common format."""

    key: str
    name: str
    extensions: Tuple[str, ...]
    magic_prefixes: Tuple[str, ...]
    summary: str
    steps: Tuple[str, ...]
    tools: Tuple[str, ...] = ("file", "xxd")

    @property
    def recipe(self) -> str:
        return self.steps[0]


# Signature strings are summaries, not bytes read by this module.  The set is
# intentionally broad enough for a useful first-pass advisor while remaining
# deterministic and offline.
_FORMAT_SPECS: Tuple[FormatSpec, ...] = (
    FormatSpec("png", "PNG image", (".png",), ("89504e47", "png"), "Portable lossless image.", ("Check the PNG signature and inspect dimensions with a read-only metadata tool.", "If the image is trusted, render a copy rather than modifying the original.")),
    FormatSpec("jpeg", "JPEG image", (".jpg", ".jpeg", ".jpe"), ("ffd8ff", "jpeg", "jpg"), "Compressed photographic image.", ("Check the JPEG marker sequence and read dimensions without rewriting the file.", "Keep the original read-only while testing a decoder.")),
    FormatSpec("gif", "GIF image", (".gif",), ("47494638", "gif"), "Indexed or animated image.", ("Check the GIF header and version with a metadata-only parser.", "If animation matters, inspect frame count on a working copy.")),
    FormatSpec("webp", "WebP image", (".webp",), ("52494646", "webp"), "RIFF-based WebP image.", ("Confirm the RIFF/WebP markers and query image dimensions read-only.", "Use an isolated decoder for an untrusted image.")),
    FormatSpec("pdf", "PDF document", (".pdf",), ("25504446", "%pdf", "pdf"), "Portable document container.", ("Confirm the PDF header and inspect page metadata with a sandboxed reader.", "Do not enable active content or scripts from an untrusted document.")),
    FormatSpec("zip", "ZIP archive", (".zip", ".docx", ".xlsx", ".pptx", ".jar", ".apk"), ("504b0304", "504b0506", "504b0708", "pk"), "ZIP container used by archives and office packages.", ("List the archive entries without extracting them first.", "Extract only into a disposable directory after checking the source.")),
    FormatSpec("gzip", "gzip stream", (".gz", ".tgz"), ("1f8b", "gzip"), "Gzip-compressed stream.", ("Check the gzip header and compression metadata without overwriting the source.", "Decompress into a new destination and verify the result separately.")),
    FormatSpec("tar", "tar archive", (".tar",), ("ustar", "tar"), "Tape archive, often paired with compression.", ("Inspect the archive listing and member names before extraction.", "Extract into a directory with path traversal protections enabled.")),
    FormatSpec("7z", "7-Zip archive", (".7z",), ("377abcaf271c", "7z"), "7-Zip compressed container.", ("List 7z entries without extracting untrusted data.", "Use a current read-only archive tool and a disposable output directory.")),
    FormatSpec("rar", "RAR archive", (".rar",), ("526172211a0700", "526172211a0701", "rar"), "RAR compressed container.", ("List RAR entries and headers before extracting.", "Extract only to a disposable directory and reject unsafe paths.")),
    FormatSpec("elf", "ELF executable", (".elf", ".so"), ("7f454c46", "elf"), "Unix executable or shared object.", ("Inspect ELF class, architecture, and sections without executing it.", "Analyze an untrusted executable in an isolated environment.")),
    FormatSpec("pe", "PE/Windows executable", (".exe", ".dll", ".sys"), ("4d5a", "mz", "pe"), "Windows executable or library.", ("Inspect PE headers and architecture without launching the file.", "Use an isolated analysis environment for untrusted binaries.")),
    FormatSpec("sqlite", "SQLite database", (".sqlite", ".sqlite3", ".db"), ("53514c69746520666f726d6174203300", "sqlite"), "SQLite database file.", ("Open a copy read-only and run an integrity check before queries.", "Do not run application extensions or triggers from an untrusted database.")),
    FormatSpec("mp3", "MP3 audio", (".mp3",), ("494433", "mp3", "fffb"), "MPEG audio stream.", ("Check the ID3/audio frame markers and read duration with a metadata parser.", "Keep the source unchanged while validating playback.")),
    FormatSpec("wav", "WAV audio", (".wav",), ("52494646", "wav"), "RIFF/WAVE audio container.", ("Confirm RIFF/WAVE markers and inspect sample metadata read-only.", "Decode a copy if playback validation is required.")),
)

# Public mapping is useful for callers that want to inspect or extend the
# offline knowledge base.  ``FORMAT_KNOWLEDGE_BASE`` is an explicit alias for
# users who prefer a descriptive name.
KNOWLEDGE_BASE: Mapping[str, FormatSpec] = OrderedDict((spec.key, spec) for spec in _FORMAT_SPECS)
FORMAT_KNOWLEDGE_BASE = KNOWLEDGE_BASE
FORMAT_SPECS = _FORMAT_SPECS


class KnowledgeBase:
    """Small deterministic wrapper around format specifications."""

    def __init__(self, specs: Optional[Union[Mapping[str, FormatSpec], Iterable[FormatSpec]]] = None) -> None:
        if specs is None:
            self._specs = OrderedDict(KNOWLEDGE_BASE)
        elif isinstance(specs, Mapping):
            self._specs = OrderedDict((str(key), value) for key, value in specs.items())
        else:
            self._specs = OrderedDict((spec.key, spec) for spec in specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self):
        return iter(self._specs.values())

    def __contains__(self, key: object) -> bool:
        return key in self._specs

    def get(self, key: str, default: Optional[FormatSpec] = None) -> Optional[FormatSpec]:
        return self._specs.get(key, default)

    def values(self):
        return self._specs.values()

    def items(self):
        return self._specs.items()

    def add(self, spec: FormatSpec) -> None:
        if not isinstance(spec, FormatSpec):
            raise TypeError("spec must be a FormatSpec")
        self._specs[spec.key] = spec


FormatKnowledgeBase = KnowledgeBase


class ActionableRecipe(dict):
    """Dict-shaped recipe with attribute access for integration convenience."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


_GENERIC_RECIPES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "metadata check",
        "Unknown format",
        ("Compare the supplied extension with a trusted magic summary.", "Keep the file read-only until its type is established."),
    ),
    (
        "text check",
        "Text or structured data candidate",
        ("Use a bounded, offline text/encoding probe on a copy if content inspection is authorized.", "Treat parser errors as evidence, not as a reason to modify the source."),
    ),
    (
        "container check",
        "Binary or container candidate",
        ("Use a format-specific listing or metadata command without extraction or execution.", "Work in a disposable directory for any later validation."),
    ),
    (
        "sandbox check",
        "Untrusted binary candidate",
        ("If the file must be opened, use an isolated environment with execution disabled.", "Preserve the original and record only the resulting metadata."),
    ),
)


def _magic_matches(summary: str, prefixes: Sequence[str]) -> bool:
    if not summary:
        return False
    raw = summary.strip().lower()
    compact = re.sub(r"[^0-9a-z%]", "", raw)
    for prefix in prefixes:
        token = prefix.lower()
        token_compact = re.sub(r"[^0-9a-z%]", "", token)
        if raw.startswith(token) or compact.startswith(token_compact):
            return True
    return False


class HypothesisEngine:
    """Rank 3–5 safe, actionable recipes from metadata only."""

    def __init__(
        self,
        knowledge_base: Optional[Any] = None,
        remember: Optional["Remember"] = None,
        fingerprint: Optional[Union[FileFingerprint, Mapping[str, Any]]] = None,
    ) -> None:
        # ``None`` also keeps construction compatible with the earlier v2
        # engine, whose first argument was an adapter.
        self._fingerprint = knowledge_base if isinstance(knowledge_base, FileFingerprint) else fingerprint
        self.knowledge_base = knowledge_base if isinstance(knowledge_base, KnowledgeBase) else KnowledgeBase(knowledge_base if isinstance(knowledge_base, (Mapping, list, tuple)) else None)
        self.remember = remember

    @staticmethod
    def _coerce_fingerprint(value: Union[FileFingerprint, Mapping[str, Any]]) -> FileFingerprint:
        if isinstance(value, FileFingerprint):
            return value
        if isinstance(value, Mapping):
            return FileFingerprint(
                value.get("filename", value.get("name", value.get("path", ""))),
                value.get("size"),
                value.get("extension", value.get("ext")),
                value.get(
                    "magic_summary",
                    value.get("magic", value.get("magic_bytes", value.get("magic_bytes_summary", ""))),
                ),
            )
        raise TypeError("expected FileFingerprint or metadata mapping")

    def suggest(self, fingerprint: Optional[Union[FileFingerprint, Mapping[str, Any]]] = None) -> List[ActionableRecipe]:
        if fingerprint is None:
            fingerprint = self._fingerprint
        if fingerprint is None:
            raise TypeError("a FileFingerprint is required")
        fp = self._coerce_fingerprint(fingerprint)
        candidates: List[ActionableRecipe] = []

        remembered = self.remember.recall(fp) if self.remember is not None else None
        if isinstance(remembered, Mapping) and remembered.get("format"):
            candidates.append(self._recipe_from_remembered(remembered))

        for spec in self.knowledge_base:
            evidence: List[str] = []
            score = 0.0
            if fp.extension and fp.extension in spec.extensions:
                score += 0.45
                evidence.append("extension %s" % fp.extension)
            if _magic_matches(fp.magic_summary, spec.magic_prefixes):
                score += 0.55
                evidence.append("magic summary")
            if not evidence:
                continue
            confidence = min(0.99, score)
            candidates.append(
                ActionableRecipe(
                    {
                        "id": spec.key,
                        "format": spec.name,
                        "type": spec.name,
                        "name": spec.name,
                        "title": spec.name,
                        "score": round(confidence, 3),
                        "confidence": round(confidence, 3),
                        "evidence": "; ".join(evidence),
                        "reason": "; ".join(evidence),
                        "recipe": spec.recipe,
                        "action": spec.recipe,
                        "steps": list(spec.steps),
                        "tools": list(spec.tools),
                    }
                )
            )

        if not candidates:
            candidates.extend(self._generic_recipe(item, fp) for item in _GENERIC_RECIPES)
        else:
            # Fill the shortlist with safe alternatives so callers always have
            # several next actions, while keeping the detected format first.
            candidates.sort(key=lambda item: (-float(item["score"]), str(item["id"])))
            existing_ids = {item["id"] for item in candidates}
            for item in _GENERIC_RECIPES:
                if len(candidates) >= 5:
                    break
                if item[0] not in existing_ids:
                    candidates.append(self._generic_recipe(item, fp))
                    existing_ids.add(item[0])

        candidates.sort(key=lambda item: (-float(item["score"]), str(item["id"])))
        # Four is the normal result; this guard protects custom knowledge bases
        # with unusual input while preserving the documented 3–5 range.
        if len(candidates) < 3:
            candidates.extend(self._generic_recipe(item, fp) for item in _GENERIC_RECIPES[: 3 - len(candidates)])
        candidates = candidates[:5]
        for rank, item in enumerate(candidates, 1):
            item["rank"] = rank
        return candidates

    def _recipe_from_remembered(self, remembered: Mapping[str, Any]) -> ActionableRecipe:
        steps = remembered.get("steps") or [str(remembered.get("action") or "Apply the remembered read-only check.")]
        action = str(remembered.get("action") or remembered.get("recipe") or "Apply the remembered read-only check.")
        return ActionableRecipe(
            {
                "id": "remembered",
                "format": str(remembered.get("format")),
                "type": str(remembered.get("format")),
                "name": str(remembered.get("format")),
                "title": str(remembered.get("format")),
                "score": 0.995,
                "confidence": 0.995,
                "evidence": "remembered metadata match",
                "reason": "remembered metadata match",
                "recipe": action,
                "action": action,
                "steps": list(steps),
                "tools": list(remembered.get("tools", [])),
            }
        )

    @staticmethod
    def _generic_recipe(item: Tuple[str, str, Tuple[str, ...]], fp: FileFingerprint) -> ActionableRecipe:
        recipe_id, name, steps = item
        evidence = "no matching extension or magic summary"
        if fp.extension:
            evidence = "no known match for extension %s or magic summary" % fp.extension
        return ActionableRecipe(
            {
                "id": recipe_id,
                "format": name,
                "type": name,
                "name": name,
                "title": name,
                "score": 0.2 if recipe_id == "metadata check" else 0.1,
                "confidence": 0.2 if recipe_id == "metadata check" else 0.1,
                "evidence": evidence,
                "reason": evidence,
                "recipe": steps[0],
                "action": steps[0],
                "steps": list(steps),
                "tools": ["file", "xxd"],
            }
        )

    # Common names used by integrations and by the roadmap wording.
    diagnose = suggest
    analyze = suggest
    hypothesize = suggest
    infer = suggest
    recommend = suggest
    hypotheses = suggest
    ranked_hypotheses = suggest
    get_hypotheses = suggest
    recipes = suggest
    generate = suggest
    get_recipes = suggest
    run = suggest
    __call__ = suggest


# ---------------------------------------------------------------------------
# Local persistence


def _json_safe(value: Any) -> Any:
    if isinstance(value, FileFingerprint):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fingerprint_key(fingerprint: FileFingerprint) -> str:
    payload = json.dumps(fingerprint.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Remember:
    """Persist selected recipes without persisting file contents.

    ``Remember(path)`` loads existing entries.  ``remember(fp, recipe)`` writes
    immediately, so a caller does not lose a choice if the application closes
    before an explicit save.  The JSON file contains only metadata and recipe
    text; it is written atomically with restrictive permissions where the
    platform permits them.
    """

    DEFAULT_PATH = DEFAULT_REMEMBER_PATH

    def __init__(
        self,
        path: Optional[Union[str, os.PathLike]] = None,
        *,
        storage_path: Optional[Union[str, os.PathLike]] = None,
        kb_path: Optional[Union[str, os.PathLike]] = None,
        max_entries: int = MAX_REMEMBERED_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if path is not None and (storage_path is not None or kb_path is not None):
            raise TypeError("provide only one Remember path")
        path = path if path is not None else (storage_path if storage_path is not None else kb_path)
        self.path = Path(path if path is not None else self.DEFAULT_PATH).expanduser()
        self.max_entries = int(max_entries)
        self._entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.last_error: Optional[str] = None
        self.load()

    @property
    def entries(self) -> List[Dict[str, Any]]:
        return [dict(entry) for entry in self._entries.values()]

    def load(self) -> List[Dict[str, Any]]:
        """Load the JSON knowledge file; missing or malformed data is ignored safely."""

        self.last_error = None
        self._entries.clear()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return self.entries
        except (OSError, ValueError, TypeError) as exc:
            self.last_error = str(exc)[:200]
            return self.entries

        raw_entries = payload.get("entries", []) if isinstance(payload, Mapping) else []
        if isinstance(raw_entries, Mapping):
            raw_entries = list(raw_entries.values())
        if not isinstance(raw_entries, list):
            return self.entries
        for raw in raw_entries:
            if not isinstance(raw, Mapping):
                continue
            fingerprint = raw.get("fingerprint")
            recipe = raw.get("recipe")
            if not isinstance(fingerprint, Mapping) or recipe is None:
                continue
            try:
                fp = HypothesisEngine._coerce_fingerprint(fingerprint)
            except (TypeError, ValueError):
                continue
            key = str(raw.get("key") or _fingerprint_key(fp))
            self._entries[key] = {
                "key": key,
                "fingerprint": fp.to_dict(),
                "recipe": _json_safe(recipe),
            }
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return self.entries

    def save(
        self,
        fingerprint: Optional[Union[FileFingerprint, Mapping[str, Any]]] = None,
        recipe: Any = None,
    ) -> Path:
        """Atomically save metadata entries and return the injected/default path.

        Passing both optional arguments is a convenience equivalent to
        :meth:`remember`; the usual no-argument form only flushes current
        entries.
        """

        if fingerprint is not None:
            if recipe is None:
                raise TypeError("recipe is required when saving a fingerprint")
            self.remember(fingerprint, recipe)
            return self.path

        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "entries": list(self._entries.values())}
        temp_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(parent),
                prefix=".ai_kb.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, self.path)
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        return self.path

    def remember(self, fingerprint: Union[FileFingerprint, Mapping[str, Any]], recipe: Any) -> Dict[str, Any]:
        fp = HypothesisEngine._coerce_fingerprint(fingerprint)
        key = _fingerprint_key(fp)
        entry = {"key": key, "fingerprint": fp.to_dict(), "recipe": _json_safe(recipe)}
        self._entries.pop(key, None)
        self._entries[key] = entry
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        self.save()
        return dict(entry)

    # Friendly aliases for callers that use a verb other than remember().
    add = remember
    learn = remember
    record = remember

    def recall(self, fingerprint: Union[FileFingerprint, Mapping[str, Any]]) -> Any:
        fp = HypothesisEngine._coerce_fingerprint(fingerprint)
        entry = self._entries.get(_fingerprint_key(fp))
        if entry is None:
            return None
        return entry.get("recipe")

    lookup = recall
    get = recall

    def forget(self, fingerprint: Union[FileFingerprint, Mapping[str, Any]]) -> bool:
        fp = HypothesisEngine._coerce_fingerprint(fingerprint)
        key = _fingerprint_key(fp)
        if key not in self._entries:
            return False
        del self._entries[key]
        self.save()
        return True


PersistentKnowledgeBase = Remember


def advise_from_metadata(
    filename: Union[str, os.PathLike],
    size: Optional[int] = None,
    *,
    extension: Optional[Union[str, os.PathLike]] = None,
    magic_summary: Any = "",
    remember: Optional[Remember] = None,
) -> Tuple[FileFingerprint, List[ActionableRecipe]]:
    """Return deterministic format advice from caller-supplied metadata only.

    This entrypoint performs no filesystem reads, model calls, or network
    requests.  Callers must supply any optional magic summary themselves.
    """

    fingerprint = build_fingerprint(
        filename, size=size, extension=extension, magic_summary=magic_summary
    )
    return fingerprint, HypothesisEngine(remember=remember).suggest(fingerprint)


__all__ = [
    "ActionableRecipe",
    "DEFAULT_REMEMBER_PATH",
    "FileFingerprint",
    "FormatKnowledgeBase",
    "FormatSpec",
    "FORMAT_KNOWLEDGE_BASE",
    "FORMAT_SPECS",
    "HypothesisEngine",
    "KnowledgeBase",
    "KNOWLEDGE_BASE",
    "PersistentKnowledgeBase",
    "Remember",
    "advise_from_metadata",
    "build_fingerprint",
]
