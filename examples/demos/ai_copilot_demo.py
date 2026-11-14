#!/usr/bin/env python3
"""Offline demo: summarize already-derived metadata, never evidence bytes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mountfile.ai.copilot import build_summary  # noqa: E402
from mountfile.ai.copilot import summary_to_markdown

summary = build_summary(
    image_fingerprint={
        "name": "synthetic-case.img",
        "size_bytes": 4096,
        "sha256": "0" * 64,
        "filesystem_hint": "FAT32",
    },
    timeline_events=[
        {
            "timestamp": "2026-10-18T09:14:00Z",
            "event": "deleted-entry-found",
            "path": "/DELETED.TXT",
            "execution_id": "demo-timeline-001",
        }
    ],
    anomaly_findings=[
        {
            "kind": "timestamp-inconsistency",
            "severity": "medium",
            "path": "/DELETED.TXT",
            "execution_id": "demo-check-001",
        }
    ],
    max_items=5,
)

print("Offline metadata-only summary; no image bytes or model are used.\n")
print(summary_to_markdown(summary))
