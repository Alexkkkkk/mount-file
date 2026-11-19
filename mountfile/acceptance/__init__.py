# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Executable acceptance machinery for the replay checklist in issue #510.

The tracker in ``docs/replay-acceptance.json`` names 32 items across five
blocks.  Until now only prose and file lists backed them.  This package turns
each item into a probe that either proves something about the tree or refuses
with a typed state, so a stale claim cannot survive a run:

* :mod:`mountfile.acceptance.contract` - manifest parsing and truthfulness rules;
* :mod:`mountfile.acceptance.hashing` - source identity and atomic publication;
* :mod:`mountfile.acceptance.matrix` - failure matrix over the declared seam;
* :mod:`mountfile.acceptance.checkers` - independent checker availability;
* :mod:`mountfile.acceptance.journal` - ext4/JBD2 gate evidence;
* :mod:`mountfile.acceptance.ntfs` - NTFS replay refusals (never enables replay);
* :mod:`mountfile.acceptance.blocks` - one probe per acceptance item;
* :mod:`mountfile.acceptance.artifact` - machine-readable combined artifact.

Nothing here claims filesystem parity or power-loss safety: a cell that cannot
be exercised is reported as ``not_exercised``/``unavailable``, never as green.
"""

from __future__ import annotations

from mountfile.acceptance.artifact import build_artifact, write_artifact
from mountfile.acceptance.blocks import BLOCK_CHECKS, BlockCheck, RepoContext, run_all
from mountfile.acceptance.checkers import CheckerOutcome, probe_checker
from mountfile.acceptance.contract import (
    Manifest,
    ManifestError,
    ManifestItem,
    load_manifest,
)
from mountfile.acceptance.hashing import SourceIdentity, publish_atomically, sha256_file
from mountfile.acceptance.matrix import FailureCell, run_failure_matrix

__all__ = [
    "BLOCK_CHECKS",
    "BlockCheck",
    "CheckerOutcome",
    "FailureCell",
    "Manifest",
    "ManifestError",
    "ManifestItem",
    "RepoContext",
    "SourceIdentity",
    "build_artifact",
    "load_manifest",
    "probe_checker",
    "publish_atomically",
    "run_all",
    "run_failure_matrix",
    "sha256_file",
    "write_artifact",
]
