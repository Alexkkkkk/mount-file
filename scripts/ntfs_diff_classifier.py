# SPDX-License-Identifier: MIT
"""C6 draft: typed classification of $MFT/$BITMAP stream differences.

The comparator currently reports raw byte diffs and leaves their meaning to
the reader. This draft module adds the class taxonomy from the #510 work
order so a future acceptance artifact can report *numbers*, not prose:

* ``replay_relevant`` - a differing byte lies inside an LCN range the bounded
  replay actually wrote. Must be zero for a parity claim, otherwise the gate
  fails.
* ``checker_lifecycle`` - a differing byte inside an explicitly allowlisted
  checker-owned region (for example the $Volume dirty flag). Allowed, but
  listed by name.
* ``outside_scope`` - anything not covered by the two classes above. Never
  counted as a match.

The integration into ``scripts/compare_ntfs_snapshots.py`` and the Windows
harness is deliberately NOT done in this draft: the replay write-set is not
yet exported by the executor, so wiring an empty write-set into the live
comparator would mislabel everything as ``outside_scope``. This module lands
the taxonomy and its invariants first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

REPLAY_RELEVANT = "replay_relevant"
CHECKER_LIFECYCLE = "checker_lifecycle"
OUTSIDE_SCOPE = "outside_scope"
CLASSES = (REPLAY_RELEVANT, CHECKER_LIFECYCLE, OUTSIDE_SCOPE)

# Checker-owned regions are an explicit, reviewable allowlist. It starts
# empty on purpose: populating it requires the E1 checker inventory, and an
# invented allowlist would silently reclassify real replay bugs.
CHECKER_LIFECYCLE_REGIONS: dict[str, tuple[tuple[int, int], ...]] = {
    # "$Volume": ((0x0, 0x100),),  # example shape, intentionally disabled
}


def _in_ranges(offset: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def classify_diffs(
    stream: str,
    diff_offsets: Sequence[int],
    replay_write_set: Mapping[str, tuple[tuple[int, int], ...]],
) -> dict[str, object]:
    """Classify the differing byte offsets of one stream.

    ``replay_write_set`` maps a stream name to the byte ranges the bounded
    replay wrote on its disposable copy. Without a write-set the classifier
    must degrade to ``outside_scope``; it must never return a pass-shaped
    result from missing data.
    """
    ranges = replay_write_set.get(stream, ())
    allowlist = CHECKER_LIFECYCLE_REGIONS.get(stream, ())
    classification: dict[str, list[int]] = {
        REPLAY_RELEVANT: [],
        CHECKER_LIFECYCLE: [],
        OUTSIDE_SCOPE: [],
    }
    for offset in sorted(diff_offsets):
        if _in_ranges(offset, ranges):
            classification[REPLAY_RELEVANT].append(offset)
        elif _in_ranges(offset, allowlist):
            classification[CHECKER_LIFECYCLE].append(offset)
        else:
            classification[OUTSIDE_SCOPE].append(offset)
    return {
        "stream": stream,
        "replay_relevant_offsets": classification[REPLAY_RELEVANT],
        "checker_lifecycle_offsets": classification[CHECKER_LIFECYCLE],
        "outside_scope_offsets": classification[OUTSIDE_SCOPE],
        "replay_relevant_count": len(classification[REPLAY_RELEVANT]),
        "write_set_present": bool(ranges),
    }


def parity_verdict(per_stream: Sequence[dict[str, object]]) -> str:
    """Gate verdict over classified streams.

    ``pass`` requires zero replay_relevant bytes across every stream. Streams
    with no write-set evidence cannot produce a pass verdict at all: missing
    data must never look like parity, so they report ``unverified`` instead.
    """
    for item in per_stream:
        if item["replay_relevant_count"]:
            return "parity_bug"
    if per_stream and not all(item["write_set_present"] for item in per_stream):
        return "unverified"
    return "pass"
