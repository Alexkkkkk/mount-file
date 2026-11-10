# Self-learning and advisory layers

Mount File's learning features are package-level, deterministic statistics and rankings—not an autonomous writer. Current layers cover volume candidate ordering, historical preflight telemetry, Turbo directory-cache TTL, format-advisor calibration, OLMA code-repair suggestions, and optional aggregate CLI telemetry.

## Manage layers

```bash
python -m mountfile layers status
python -m mountfile layers status --json
python -m mountfile layers enable cache
python -m mountfile layers disable advisor
python -m mountfile layers reset volume
```

Layer state is stored outside the evidence/image directory under the current user's Mount File state directory. Set `MOUNTFILE_LAYERS_DIR` to choose a different store root. The JSON store is size-limited, atomically replaced, and written with owner-only permissions on POSIX systems. Corrupt or unreadable learned data falls back to deterministic behavior. Set `MOUNTFILE_DISABLE_LAYERS=1` to bypass runtime hooks globally.

`operations` is disabled by default. When explicitly enabled, it stores only aggregate command names, success/failure counts, and bounded duration averages; it does not store argv, paths, prompts, keys, file contents, or evidence bytes.

## Advisory decisions and explicit feedback

The typed control API is available from `mountfile.layers`: `LayerContext`, `LayerDecision`, `Feedback`, `Hooks`, and `DecisionOrchestrator`. Decisions are tagged `advisory_only` and have a one-time `decision_id` for explicit feedback. For example, after at least three recorded preflight observations for a filesystem family:

```bash
python -m mountfile layers decide evidence.img --operation preflight --fs NTFS --json
python -m mountfile layers feedback DECISION_ID --success
# or: --failure
```

The `path` argument is runtime context only and is not persisted. Pending feedback is bounded; persisted records exclude paths, candidate names/labels, prompts, raw bytes, and arbitrary context extras. A decision ID can be rated only once. If the corresponding layer is reset, pending feedback for that layer is invalidated.

`historical_pass_hint` is historical telemetry, not an eligibility result. A volume `rank_order_hint` never mounts or opens a volume; production verification remains mandatory. Cache TTL hints affect performance only; cache validity and invalidation checks are unchanged. No learned output can approve a write, mount, recovery operation, parser fallback, or evidence change.

## AI and code repair

AI analysis remains user-triggered and bounded under the configured backend/privacy settings. Learning does not send data to an LLM and does not run a model for each function call. OLMA's `code_repair` toggle controls learned repair suggestions and outcome learning:

```bash
python -m mountfile layers disable code_repair
python -m mountfile layers enable code_repair
```

The toggle does not apply a fix. OLMA's existing `--apply` gate remains required for code changes; dry-run, test/smoke validation, and explicit publishing gates remain separate. Learned `should_auto_apply` values are hints only and are never executed automatically.

## Current boundaries

- Preflight, capability, bounds, and filesystem-state checks remain deterministic and fail closed.
- Read providers remain read-only; layer output cannot change writer authorization or mutate evidence.
- Self-learning is feedback/statistics, not autonomous model fine-tuning.
- Layer failures are best-effort and fall back to existing logic; this does not weaken a deterministic refusal.
