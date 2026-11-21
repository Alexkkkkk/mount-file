# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""#387 / #510 D2+D6: two *consecutive scheduled* nightly runs as evidence.

The remaining D2/D6 gap was never "do the tests pass in a PR" -- they do.  It is
that no scheduled run existed and that the #387 acceptance criterion (two
consecutive nightly runs) had no machine check behind it.  This module turns
that criterion into a reproducible, fail-closed query against the GitHub
Actions API instead of a prose claim in the tracker.

Contract:

* the aggregate verdict is always ``acceptance: "not_claimed"`` -- this script
  reports *scheduling evidence*, it never promotes a checklist item;
* a missing token, a malformed payload, or fewer than ``required`` completed
  scheduled runs is a typed ``unavailable`` outcome, never a pass;
* no network access happens at import time and the transport is injectable, so
  the behaviour above is covered by offline unit tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

API_ROOT = "https://api.github.com"
DEFAULT_WORKFLOW = "replay-nightly.yml"
REQUIRED_RUNS = 2
OUTCOME_SATISFIED = "satisfied"
OUTCOME_UNSATISFIED = "unsatisfied"
OUTCOME_UNAVAILABLE = "unavailable"

Transport = Callable[[str, str], dict]


class EvidenceUnavailable(RuntimeError):
    """The scheduling evidence could not be obtained at all."""


def _http_json(url: str, token: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mountfile-replay-nightly-evidence",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # pragma: no cover - network path
        raise EvidenceUnavailable(f"GitHub API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover
        raise EvidenceUnavailable(f"GitHub API is unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise EvidenceUnavailable("GitHub API returned malformed JSON") from exc


def resolve_token(token: str | None = None) -> str | None:
    """Return an explicit token, else the ambient workflow token, else ``None``."""
    return token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def fetch_scheduled_runs(
    repo: str,
    workflow: str = DEFAULT_WORKFLOW,
    token: str | None = None,
    per_page: int = 50,
    transport: Transport | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent scheduled runs of ``workflow`` in ``repo``."""
    if not repo or "/" not in repo:
        raise EvidenceUnavailable("a repository in owner/name form is required")
    if not workflow:
        raise EvidenceUnavailable("a workflow file name is required")
    resolved = resolve_token(token)
    if not resolved:
        raise EvidenceUnavailable(
            "no GitHub token: the nightly evidence needs actions:read"
        )
    if not 1 <= per_page <= 100:
        raise EvidenceUnavailable("per_page must be between 1 and 100")
    url = (
        f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow}/runs"
        f"?event=schedule&per_page={per_page}"
    )
    payload = (transport or _http_json)(url, resolved)
    if not isinstance(payload, dict):
        raise EvidenceUnavailable("GitHub API payload is not an object")
    runs = payload.get("workflow_runs")
    if runs is None:
        raise EvidenceUnavailable("GitHub API payload has no workflow_runs")
    if not isinstance(runs, list):
        raise EvidenceUnavailable("workflow_runs is not a list")
    normalised: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            raise EvidenceUnavailable("workflow_runs contains a non-object entry")
        event = run.get("event")
        if not isinstance(event, str) or not event:
            raise EvidenceUnavailable(
                "workflow_runs contains an entry without an explicit event"
            )
        if event != "schedule":
            raise EvidenceUnavailable(
                f"workflow_runs contains unexpected event: {event!r}"
            )
        run_id = run.get("id")
        status = run.get("status")
        conclusion = run.get("conclusion")
        created_at = run.get("created_at")
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or not isinstance(status, str)
            or not status
            or (conclusion is not None and not isinstance(conclusion, str))
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise EvidenceUnavailable(
                "workflow_runs contains an entry with an invalid run schema"
            )
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceUnavailable(
                "workflow_runs contains an entry with an invalid created_at"
            ) from exc
        normalised.append(
            {
                "id": run_id,
                "status": status,
                "conclusion": conclusion,
                "event": event,
                "created_at": created_at,
                "head_sha": run.get("head_sha"),
                "head_branch": run.get("head_branch"),
                "html_url": run.get("html_url"),
            }
        )
    return normalised


def evaluate(
    runs: list[dict[str, Any]], required: int = REQUIRED_RUNS
) -> dict[str, Any]:
    """Count the current streak of consecutive successful scheduled runs."""
    if required < 1:
        raise EvidenceUnavailable("required must be at least 1")
    completed = [
        run
        for run in runs
        if run.get("status") == "completed" and run.get("event") == "schedule"
    ]
    completed.sort(
        key=lambda run: (str(run.get("created_at") or ""), run.get("id") or 0),
        reverse=True,
    )
    streak = 0
    for run in completed:
        if run.get("conclusion") == "success":
            streak += 1
        else:
            break
    satisfied = streak >= required
    return {
        "required_consecutive": required,
        "completed_scheduled_runs": len(completed),
        "consecutive_successes": streak,
        "criterion_satisfied": satisfied,
        "outcome": OUTCOME_SATISFIED if satisfied else OUTCOME_UNSATISFIED,
        "evidence": completed[:required],
        "acceptance": "not_claimed",
    }


def build_report(
    repo: str,
    workflow: str = DEFAULT_WORKFLOW,
    token: str | None = None,
    required: int = REQUIRED_RUNS,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Full report: fetch, evaluate, and never raise past the typed boundary."""
    base = {
        "schema": 1,
        "tracker": 510,
        "issue": 387,
        "criterion": (
            "two consecutive scheduled nightly runs of the replay failure/kill "
            "matrix, both successful"
        ),
        "repository": repo,
        "workflow": workflow,
        "required_consecutive": required,
        "acceptance": "not_claimed",
    }
    try:
        runs = fetch_scheduled_runs(
            repo, workflow=workflow, token=token, transport=transport
        )
        base.update(evaluate(runs, required=required))
    except EvidenceUnavailable as exc:
        base.update(
            {
                "outcome": OUTCOME_UNAVAILABLE,
                "reason": str(exc),
                "criterion_satisfied": False,
                "consecutive_successes": 0,
                "completed_scheduled_runs": 0,
                "evidence": [],
            }
        )
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name of the repository (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--required", type=int, default=REQUIRED_RUNS)
    parser.add_argument("--json-out", default="")
    parser.add_argument(
        "--fail-on-unsatisfied",
        action="store_true",
        help="exit non-zero unless the criterion is satisfied (#387 hard gate)",
    )
    args = parser.parse_args(argv)
    report = build_report(args.repo, workflow=args.workflow, required=args.required)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    if args.fail_on_unsatisfied and not report["criterion_satisfied"]:
        print(
            f"nightly criterion not satisfied ({report['outcome']}); "
            f"{report['consecutive_successes']}/{report['required_consecutive']} "
            "consecutive scheduled successes",
            file=sys.stderr,
        )
        return 2 if report["outcome"] == OUTCOME_UNAVAILABLE else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
