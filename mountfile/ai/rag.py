# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Small, optional retrieval over Mount File's own documentation.

The retriever deliberately has no third-party dependency and does not inspect
mounted files or raw disk images.  By default it uses a small BM25
implementation over ``README.md``, ``CHANGELOG.md`` and Python docstrings
under a supplied repository directory.  An application can inject an
``embedding_backend`` with an ``embed(text)`` method (or a callable with the
same signature) when semantic retrieval is desired.

Typical use::

    from mountfile.ai.rag import DocumentationRetriever

    docs = DocumentationRetriever(repository_root)
    snippets = docs.retrieve("how do I configure the AI backend?")
    answer = docs.answer("how do I configure the AI backend?")

Returned snippets always carry a source path, section and one-based line
range.  ``answer`` only reports retrieved evidence; when no indexed evidence
matches it returns an explicit no-evidence message rather than guessing.
"""

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


_TOKEN_RE = re.compile(r"(?u)\b[\w]+\b")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
_DEFAULT_K1 = 1.5
_DEFAULT_B = 0.75
_DEFAULT_TOP_K = 5

# This is intentionally a message, not an answer.  Callers can use it to
# distinguish a missing documentation match from a low-confidence assertion.
NO_EVIDENCE_MESSAGE = "No evidence found in indexed documentation for this question."


@dataclass(frozen=True)
class Citation:
    """A stable human-readable citation for one documentation chunk."""

    source: str
    section: str
    start_line: int
    end_line: int

    @property
    def lines(self) -> Tuple[int, int]:
        """Return the inclusive one-based line range."""

        return self.start_line, self.end_line

    def __str__(self) -> str:
        section = self.section or "Document"
        if self.start_line == self.end_line:
            line_label = "line %d" % self.start_line
        else:
            line_label = "lines %d-%d" % (self.start_line, self.end_line)
        return "%s — %s (%s)" % (self.source, section, line_label)


@dataclass(frozen=True)
class DocumentationChunk:
    """One indexed source fragment.

    ``source`` is relative to the retriever root and never contains an
    absolute path.  The text comes only from an approved Markdown document or
    a Python docstring.
    """

    source: str
    section: str
    start_line: int
    end_line: int
    text: str

    @property
    def citation(self) -> str:
        """Render the source, section and inclusive line range."""

        return str(
            Citation(
                self.source,
                self.section,
                self.start_line,
                self.end_line,
            )
        )

    @property
    def source_file(self) -> str:
        """Alias useful to callers that prefer an explicit field name."""

        return self.source

    @property
    def line(self) -> int:
        """The first one-based source line."""

        return self.start_line

    @property
    def content(self) -> str:
        """Alias for the retrieved source text."""

        return self.text


@dataclass(frozen=True)
class RetrievedSnippet:
    """A documentation chunk and its ranking score."""

    chunk: DocumentationChunk
    score: float

    @property
    def source(self) -> str:
        return self.chunk.source

    @property
    def section(self) -> str:
        return self.chunk.section

    @property
    def start_line(self) -> int:
        return self.chunk.start_line

    @property
    def end_line(self) -> int:
        return self.chunk.end_line

    @property
    def text(self) -> str:
        return self.chunk.text

    @property
    def snippet(self) -> str:
        return self.chunk.text

    @property
    def citation(self) -> str:
        return self.chunk.citation

    def to_dict(self) -> Dict[str, Any]:
        """Return a serialization-safe representation for UI/API callers."""

        return {
            "source": self.source,
            "section": self.section,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "text": self.text,
            "score": self.score,
            "citation": self.citation,
        }


# An embedding backend is deliberately a tiny protocol.  No package is
# imported here; an application may wrap any local or remote model itself.
EmbeddingBackend = Callable[[str], Sequence[float]]


@dataclass(frozen=True)
class _IndexedChunk:
    chunk: DocumentationChunk
    term_counts: Mapping[str, int]
    length: int


def _tokens(text: str) -> List[str]:
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(text)]


def _relative_source(root: Path, path: Path) -> str:
    """Return a portable relative path without exposing the repository root."""

    return path.relative_to(root).as_posix()


def _safe_child(root: Path, path: Path, suffix: str) -> bool:
    """Require an in-root regular file whose resolved target has ``suffix``."""

    try:
        resolved_root = root.resolve()
        resolved = path.resolve()
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return resolved.is_file() and resolved.suffix.casefold() == suffix.casefold()


def _read_text(path: Path, max_file_bytes: int) -> Optional[str]:
    """Read a bounded UTF-8 text file, returning ``None`` on unsafe input."""

    try:
        if path.stat().st_size > max_file_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def _make_chunk(
    source: str,
    section: str,
    start_line: int,
    end_line: int,
    text: str,
) -> Optional[DocumentationChunk]:
    cleaned = text.strip()
    if not cleaned:
        return None
    return DocumentationChunk(
        source=source,
        section=section.strip() or "Document",
        start_line=max(1, int(start_line)),
        end_line=max(max(1, int(start_line)), int(end_line)),
        text=cleaned,
    )


def _markdown_chunks(source: str, text: str) -> Iterable[DocumentationChunk]:
    """Split a Markdown file at headings while retaining heading line ranges."""

    lines = text.splitlines()
    if not lines:
        return

    section = "Document"
    start = 1
    buffer: List[str] = []
    for line_number, line in enumerate(lines, start=1):
        heading = _HEADING_RE.match(line)
        if heading:
            previous = _make_chunk(source, section, start, line_number - 1, "\n".join(buffer))
            if previous is not None:
                yield previous
            section = heading.group(2).strip()
            start = line_number
            buffer = [line]
        else:
            buffer.append(line)

    final = _make_chunk(source, section, start, len(lines), "\n".join(buffer))
    if final is not None:
        yield final


def _docstring_expression(node: ast.AST) -> Optional[ast.AST]:
    body = getattr(node, "body", ())
    if not body:
        return None
    first = body[0]
    value = getattr(first, "value", None)
    if isinstance(first, ast.Expr) and isinstance(value, ast.Constant):
        return first if isinstance(value.value, str) else None
    # Python 3.8 compatibility: string literals are represented by Constant,
    # but keep the Str branch for alternate AST producers used in tests/tools.
    if isinstance(first, ast.Expr) and isinstance(value, ast.Str):  # pragma: no cover
        return first
    return None


def _walk_documented_nodes(tree: ast.Module) -> Iterable[Tuple[str, ast.AST]]:
    """Yield module/class/function nodes with their qualified section names."""

    yield "module", tree

    def visit(body: Iterable[ast.stmt], parent: str = "") -> Iterable[Tuple[str, ast.AST]]:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = "%s.%s" % (parent, node.name) if parent else node.name
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                yield "%s %s" % (kind, qualified), node
                yield from visit(getattr(node, "body", ()), qualified)

    yield from visit(tree.body)


def _docstring_chunks(source: str, text: str) -> Iterable[DocumentationChunk]:
    try:
        tree = ast.parse(text, filename=source)
    except (SyntaxError, ValueError, TypeError, MemoryError):
        return

    for section, node in _walk_documented_nodes(tree):
        expression = _docstring_expression(node)
        if expression is None:
            continue
        literal = getattr(expression, "value", None)
        value = getattr(literal, "value", literal)
        if not isinstance(value, str) or not value.strip():
            continue
        start = int(getattr(expression, "lineno", getattr(node, "lineno", 1)))
        end = int(getattr(expression, "end_lineno", start))
        chunk = _make_chunk(source, section, start, end, value)
        if chunk is not None:
            yield chunk


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    try:
        left_values = [float(value) for value in left]
        right_values = [float(value) for value in right]
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(value) for value in left_values + right_values):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left_values, right_values)) / (left_norm * right_norm)


class DocumentationRetriever:
    """Index approved repository documentation and retrieve evidence.

    Args:
        root: Repository/documentation root.  Only the root-level
            ``README.md``, ``CHANGELOG.md`` and Markdown files below ``docs/``
            plus ``*.py`` docstrings below it are read.
        embedding_backend: Optional callable ``text -> vector`` or object with
            ``embed(text)``.  Its vectors are ranked with cosine similarity;
            BM25 remains the fallback if it is absent or fails.
        top_k: Default number of snippets returned by :meth:`retrieve`.
        max_file_bytes: Upper bound for each approved source file.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        embedding_backend: Optional[Any] = None,
        top_k: int = _DEFAULT_TOP_K,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
        k1: float = _DEFAULT_K1,
        b: float = _DEFAULT_B,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if k1 < 0 or b < 0:
            raise ValueError("BM25 parameters must be non-negative")
        self.embedding_backend = embedding_backend
        self.top_k = int(top_k)
        self.max_file_bytes = int(max_file_bytes)
        self.k1 = float(k1)
        self.b = float(b)
        self.root: Optional[Path] = None
        self.chunks: Tuple[DocumentationChunk, ...] = ()
        self.documents = self.chunks
        self._indexed: Tuple[_IndexedChunk, ...] = ()
        self._document_frequency: Counter[str] = Counter()
        self._average_length = 0.0
        if root is not None:
            self.build(root)

    def build(self, root: Path) -> "DocumentationRetriever":
        """(Re)build the index and return ``self`` for convenient setup."""

        selected_root = Path(root).expanduser().resolve()
        if not selected_root.is_dir():
            raise ValueError("documentation root must be a directory")

        chunks: List[DocumentationChunk] = []
        approved_markdown = [selected_root / "README.md", selected_root / "CHANGELOG.md"]
        docs_root = selected_root / "docs"
        if docs_root.is_dir():
            try:
                approved_markdown.extend(sorted(docs_root.rglob("*.md")))
            except (OSError, ValueError):
                pass
        for path in approved_markdown:
            if not _safe_child(selected_root, path, ".md"):
                continue
            text = _read_text(path, self.max_file_bytes)
            if text is not None:
                chunks.extend(_markdown_chunks(_relative_source(selected_root, path), text))

        python_paths: List[Path] = []
        try:
            candidates = list(selected_root.rglob("*.py"))
        except (OSError, ValueError):
            candidates = []
        for path in sorted(candidates, key=lambda item: item.as_posix()):
            if _safe_child(selected_root, path, ".py"):
                python_paths.append(path)
        for path in python_paths:
            text = _read_text(path, self.max_file_bytes)
            if text is not None:
                chunks.extend(_docstring_chunks(_relative_source(selected_root, path), text))

        indexed = []
        frequencies: Counter[str] = Counter()
        for chunk in chunks:
            term_counts = Counter(_tokens(chunk.text))
            if not term_counts:
                continue
            indexed_chunk = _IndexedChunk(chunk, term_counts, sum(term_counts.values()))
            indexed.append(indexed_chunk)
            frequencies.update(term_counts.keys())

        self.root = selected_root
        self.chunks = tuple(item.chunk for item in indexed)
        self.documents = self.chunks
        self._indexed = tuple(indexed)
        self._document_frequency = frequencies
        self._average_length = (
            sum(item.length for item in self._indexed) / len(self._indexed)
            if self._indexed
            else 0.0
        )
        return self

    # Common spelling for callers that treat indexing as an action.
    index = build

    def _bm25_scores(self, query_tokens: Sequence[str]) -> List[float]:
        if not self._indexed or not query_tokens:
            return [0.0] * len(self._indexed)
        count = len(self._indexed)
        scores: List[float] = []
        unique_query_tokens = set(query_tokens)
        for item in self._indexed:
            score = 0.0
            for token in unique_query_tokens:
                frequency = item.term_counts.get(token, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequency.get(token, 0)
                inverse_frequency = math.log(
                    1.0 + (count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + self.k1 * (
                    1.0 - self.b + self.b * item.length / max(self._average_length, 1.0)
                )
                score += inverse_frequency * frequency * (self.k1 + 1.0) / denominator
            scores.append(score)
        return scores

    def _embedding_scores(self, query: str) -> Optional[List[float]]:
        backend = self.embedding_backend
        if backend is None or not self._indexed:
            return None
        try:
            embed = getattr(backend, "embed", backend)
            query_vector = embed(query)
            return [
                _cosine(query_vector, embed(item.chunk.text))
                for item in self._indexed
            ]
        except Exception:  # pragma: no cover - depends on the injected backend
            # A broken optional backend must not make ordinary lexical search
            # unavailable.  Do not let arbitrary model/library exceptions
            # escape from a user-facing documentation search either.
            return None

    def retrieve(self, query: str, top_k: Optional[int] = None) -> List[RetrievedSnippet]:
        """Return ranked evidence with positive lexical or semantic scores."""

        if not isinstance(query, str):
            raise TypeError("query must be a string")
        limit = self.top_k if top_k is None else int(top_k)
        if limit < 1:
            return []
        normalized_query = query.strip()
        query_tokens = _tokens(normalized_query)
        if not query_tokens:
            return []

        lexical_scores = self._bm25_scores(query_tokens)
        semantic_scores = self._embedding_scores(normalized_query)
        if semantic_scores is not None and any(score > 0.0 for score in semantic_scores):
            scores = semantic_scores
        else:
            scores = lexical_scores

        ranked = sorted(
            ((score, index) for index, score in enumerate(scores) if score > 0.0),
            key=lambda item: (-item[0], item[1]),
        )
        return [
            RetrievedSnippet(self._indexed[index].chunk, float(score))
            for score, index in ranked[:limit]
        ]

    # Search is a convenient alias and keeps the class friendly to simple UIs.
    search = retrieve

    def answer(self, query: str, top_k: Optional[int] = None) -> str:
        """Return cited snippets, or an explicit no-evidence response."""

        snippets = self.retrieve(query, top_k=top_k)
        if not snippets:
            return NO_EVIDENCE_MESSAGE

        lines = ["Evidence from indexed documentation:"]
        for item in snippets:
            text = " ".join(item.text.split())
            if len(text) > 500:
                text = text[:497].rstrip() + "..."
            lines.append("- %s [%s]" % (text, item.citation))
        return "\n".join(lines)

    # ``respond`` is useful where answer() is used as a drop-in text provider.
    respond = answer


# Descriptive aliases for integrations that call the feature a RAG index.
DocumentationRAG = DocumentationRetriever
DocumentationIndex = DocumentationRetriever


def build_documentation_index(
    root: Path,
    *,
    embedding_backend: Optional[Any] = None,
    top_k: int = _DEFAULT_TOP_K,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> DocumentationRetriever:
    """Build a documentation retriever without importing optional libraries."""

    return DocumentationRetriever(
        root,
        embedding_backend=embedding_backend,
        top_k=top_k,
        max_file_bytes=max_file_bytes,
    )


def retrieve_documentation(
    query: str,
    root: Path,
    *,
    top_k: int = _DEFAULT_TOP_K,
    embedding_backend: Optional[Any] = None,
) -> List[RetrievedSnippet]:
    """One-shot helper for callers that do not need to retain an index."""

    return DocumentationRetriever(
        root,
        embedding_backend=embedding_backend,
        top_k=top_k,
    ).retrieve(query)


def answer_documentation(
    query: str,
    root: Path,
    *,
    top_k: int = _DEFAULT_TOP_K,
    embedding_backend: Optional[Any] = None,
) -> str:
    """One-shot cited answer helper."""

    return DocumentationRetriever(
        root,
        embedding_backend=embedding_backend,
        top_k=top_k,
    ).answer(query)


__all__ = [
    "Citation",
    "DocumentationChunk",
    "RetrievedSnippet",
    "EmbeddingBackend",
    "DocumentationRetriever",
    "DocumentationRAG",
    "DocumentationIndex",
    "NO_EVIDENCE_MESSAGE",
    "build_documentation_index",
    "retrieve_documentation",
    "answer_documentation",
]
