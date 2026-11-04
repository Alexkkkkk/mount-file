# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""AI analysis backends for Mount File.

    from mountfile.ai import build_backend, collect
    backend = build_backend(tc_style.SETTINGS)
    data, truncated = collect(reader, max_bytes=settings["ai_max_bytes"])
    result = backend.analyze("dump.bin", data, prompt="что это?")

Nothing here imports Qt and nothing talks to the network on import.
"""

from __future__ import annotations

from .agents import (
    Claim,
    ExecutionRecord,
    InvestigationOrchestrator,
    InvestigationResult,
    Plan,
    ToolCall,
)
from .backend import (
    BACKENDS,
    DEFAULT_MAX_BYTES,
    AiBackend,
    AiResult,
    NullBackend,
    OllamaBackend,
    OpenAiCompatBackend,
    build_backend,
    build_prompt,
    collect,
    configure_local_ollama,
    discover_ollama,
    local_reader,
    looks_textual,
    max_bytes,
    summarize_bytes,
)
from .copilot import CopilotSummaryBuilder
from .rag import DocumentationRetriever, retrieve_documentation

__all__ = [
    "BACKENDS",
    "DEFAULT_MAX_BYTES",
    "configure_local_ollama",
    "discover_ollama",
    "AiBackend",
    "AiResult",
    "NullBackend",
    "OllamaBackend",
    "OpenAiCompatBackend",
    "build_backend",
    "build_prompt",
    "collect",
    "local_reader",
    "looks_textual",
    "max_bytes",
    "summarize_bytes",
    "Claim",
    "ExecutionRecord",
    "InvestigationOrchestrator",
    "InvestigationResult",
    "Plan",
    "ToolCall",
    "CopilotSummaryBuilder",
    "DocumentationRetriever",
    "retrieve_documentation",
]
