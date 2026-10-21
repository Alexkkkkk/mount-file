# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Deterministic, evidence-gated investigation orchestration.

The module contains no model client, network access, or filesystem access.  An
application supplies four ordinary callables: a planner creates tool calls, an
executor runs one call, an analyst proposes claims, and a skeptic accepts or
rejects each supported claim.  The orchestrator assigns execution identifiers
itself, so a role cannot manufacture evidence by choosing an identifier.

Only metadata and tool results move between roles.  In particular, this layer
does not read an image (or any other file) and never puts image bytes in a
prompt.  A caller that needs file analysis should expose a bounded, explicit
tool through ``executor``.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)


@dataclass(frozen=True, init=False)
class ToolCall:
    """One planned tool invocation.

    ``tool`` is the canonical name.  ``name`` is accepted as a keyword alias
    because it is a common spelling for an executable operation; the public
    record always exposes both ``tool`` and the read-only ``name`` property.
    """

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        tool: Optional[str] = None,
        args: Optional[Mapping[str, Any]] = None,
        *,
        name: Optional[str] = None,
    ) -> None:
        selected = tool if tool is not None else name
        if selected is None:
            raise TypeError("ToolCall requires a tool name")
        if name is not None and tool is not None and name != tool:
            raise ValueError("tool and name must refer to the same operation")
        if not isinstance(selected, str) or not selected.strip():
            raise ValueError("tool name must be a non-empty string")
        if args is not None and not isinstance(args, Mapping):
            raise TypeError("tool args must be a mapping")
        object.__setattr__(self, "tool", selected)
        object.__setattr__(self, "args", MappingProxyType(dict(args or {})))

    @property
    def name(self) -> str:
        """Alias for :attr:`tool`."""

        return self.tool


@dataclass(frozen=True)
class Plan:
    """Normalized planner output."""

    steps: Tuple[ToolCall, ...]
    hypothesis: str = ""

    @property
    def tool_calls(self) -> Tuple[ToolCall, ...]:
        """A descriptive alias for ``steps``."""

        return self.steps


@dataclass(frozen=True)
class ExecutionRecord:
    """The immutable audit record for one executor call."""

    execution_id: str
    tool: str
    args: Mapping[str, Any]
    ok: bool
    result: Any = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("execution_id must be non-empty")
        if not self.tool:
            raise ValueError("tool must be non-empty")
        object.__setattr__(self, "args", MappingProxyType(dict(self.args)))

    @property
    def name(self) -> str:
        """Alias for the executed tool name."""

        return self.tool


@dataclass(frozen=True)
class Claim:
    """A statement proposed by the analyst and tied to one execution."""

    statement: str
    execution_id: str
    confidence: float = 0.5
    type: str = "claim"

    def __post_init__(self) -> None:
        if not isinstance(self.statement, str):
            raise TypeError("claim statement must be a string")
        if not isinstance(self.execution_id, str):
            raise TypeError("claim execution_id must be a string")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("claim confidence must be between 0 and 1")


@dataclass
class InvestigationResult:
    """Result of one deterministic investigation run."""

    query: str
    plan: Plan
    executions: List[ExecutionRecord]
    accepted_claims: List[Claim]
    rejected_claims: List[Claim]

    @property
    def execution_records(self) -> List[ExecutionRecord]:
        """Descriptive alias for :attr:`executions`."""

        return self.executions

    @property
    def accepted(self) -> List[Claim]:
        """Descriptive alias for :attr:`accepted_claims`."""

        return self.accepted_claims

    @property
    def rejected(self) -> List[Claim]:
        """Descriptive alias for :attr:`rejected_claims`."""

        return self.rejected_claims


# These aliases are deliberately small: dependency injection is the only
# extension point.  Implementations may be functions, bound methods, or
# callable objects.
Planner = Callable[[str], Union[Plan, Iterable[Union[ToolCall, Mapping[str, Any]]]]]
Executor = Callable[[ToolCall], Any]
Analyst = Callable[..., Iterable[Union[Claim, Mapping[str, Any]]]]
Skeptic = Callable[..., Union[bool, Iterable[Union[Claim, Mapping[str, Any]]]]]


class InvestigationOrchestrator:
    """Run Planner -> Executor -> Analyst -> Skeptic without hidden I/O.

    The canonical injectable signatures are::

        planner(query) -> Plan | iterable[ToolCall]
        executor(tool_call) -> tool result
        analyst(query, executions) -> iterable[Claim]
        skeptic(claim, executions) -> bool

    For small integrations an analyst accepting only ``executions`` and a
    skeptic accepting only ``claim`` are also supported.  A skeptic whose
    first parameter is named ``claims`` or ``candidates`` may instead accept
    the full candidate collection and return the claims to keep.
    """

    def __init__(
        self,
        planner: Planner,
        executor: Executor,
        analyst: Analyst,
        skeptic: Skeptic,
    ) -> None:
        for role_name, role in (
            ("planner", planner),
            ("executor", executor),
            ("analyst", analyst),
            ("skeptic", skeptic),
        ):
            if not callable(role):
                raise TypeError("%s must be callable" % role_name)
        self._planner = planner
        self._executor = executor
        self._analyst = analyst
        self._skeptic = skeptic

    def investigate(self, query: str) -> InvestigationResult:
        """Execute one investigation and return only evidence-gated claims."""

        if not isinstance(query, str):
            raise TypeError("query must be a string")

        plan = _coerce_plan(self._planner(query))
        executions: List[ExecutionRecord] = []
        for number, call in enumerate(plan.steps, start=1):
            execution_id = "exec_%04d" % number
            try:
                result = self._executor(call)
            except Exception as exc:  # a failed tool remains auditable
                executions.append(
                    ExecutionRecord(
                        execution_id=execution_id,
                        tool=call.tool,
                        args=call.args,
                        ok=False,
                        error="%s: %s" % (type(exc).__name__, exc),
                    )
                )
            else:
                executions.append(
                    ExecutionRecord(
                        execution_id=execution_id,
                        tool=call.tool,
                        args=call.args,
                        ok=True,
                        result=result,
                    )
                )

        analyst_output = _call_analyst(self._analyst, query, executions)
        candidates = _coerce_claims(analyst_output)
        execution_map = {record.execution_id: record for record in executions}

        # Evidence validation happens before the skeptic.  A skeptic can
        # weaken a supported claim, but it cannot whitelist fabricated or
        # failed evidence.
        supported: List[Claim] = []
        for claim in candidates:
            record = execution_map.get(claim.execution_id)
            if record is not None and record.ok and claim.execution_id:
                supported.append(claim)

        accepted = _skeptic_filter(self._skeptic, supported, executions)
        accepted_keys = {_claim_key(claim) for claim in accepted}
        # Build both lists in analyst order.  This makes the report stable even
        # though evidence validation and skepticism are separate passes.
        accepted_claims = [
            claim for claim in candidates if _claim_key(claim) in accepted_keys
        ]
        rejected = [
            claim for claim in candidates if _claim_key(claim) not in accepted_keys
        ]

        return InvestigationResult(
            query=query,
            plan=plan,
            executions=executions,
            accepted_claims=accepted_claims,
            rejected_claims=rejected,
        )

    # ``run`` is intentionally an alias rather than a second code path.
    run = investigate


def _coerce_plan(raw: Any) -> Plan:
    if isinstance(raw, Plan):
        return raw
    if isinstance(raw, Mapping):
        steps = raw.get("steps", raw.get("tool_calls", ()))
        hypothesis = raw.get("hypothesis", "")
    else:
        steps = raw
        hypothesis = ""
    if steps is None or isinstance(steps, (str, bytes)):
        raise TypeError("planner must return an iterable of tool calls")
    try:
        normalized = tuple(_coerce_tool_call(step) for step in steps)
    except TypeError as exc:
        raise TypeError("planner must return an iterable of tool calls") from exc
    return Plan(steps=normalized, hypothesis=str(hypothesis or ""))


def _coerce_tool_call(raw: Any) -> ToolCall:
    if isinstance(raw, ToolCall):
        return raw
    if isinstance(raw, Mapping):
        tool = raw.get("tool", raw.get("name"))
        return ToolCall(tool=tool, args=raw.get("args", {}))
    if hasattr(raw, "tool"):
        return ToolCall(tool=getattr(raw, "tool"), args=getattr(raw, "args", {}))
    if hasattr(raw, "name"):
        return ToolCall(tool=getattr(raw, "name"), args=getattr(raw, "args", {}))
    raise TypeError("each planned step must be a ToolCall or mapping")


def _coerce_claims(raw: Any) -> List[Claim]:
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = raw.get("claims", ())
    if isinstance(raw, (str, bytes)):
        raise TypeError("analyst must return claims, not text")
    try:
        return [_coerce_claim(item) for item in raw]
    except TypeError as exc:
        raise TypeError("analyst must return an iterable of claims") from exc


def _coerce_claim(raw: Any) -> Claim:
    if isinstance(raw, Claim):
        return raw
    if isinstance(raw, Mapping):
        if "statement" not in raw or "execution_id" not in raw:
            raise ValueError("each claim requires statement and execution_id")
        return Claim(
            statement=raw["statement"],
            execution_id=raw["execution_id"],
            confidence=raw.get("confidence", 0.5),
            type=raw.get("type", "claim"),
        )
    if hasattr(raw, "statement") and hasattr(raw, "execution_id"):
        return Claim(
            statement=getattr(raw, "statement"),
            execution_id=getattr(raw, "execution_id"),
            confidence=getattr(raw, "confidence", 0.5),
            type=getattr(raw, "type", "claim"),
        )
    raise TypeError("each analyst output must be a Claim or mapping")


def _call_analyst(
    analyst: Analyst, query: str, executions: Sequence[ExecutionRecord]
) -> Any:
    """Call the canonical two-argument analyst, with a narrow one-arg form."""

    if _can_bind(analyst, query, executions):
        return analyst(query, executions)
    if _can_bind(analyst, executions):
        return analyst(executions)
    raise TypeError("analyst must accept (query, executions) or (executions)")


def _skeptic_filter(
    skeptic: Skeptic,
    candidates: Sequence[Claim],
    executions: Sequence[ExecutionRecord],
) -> List[Claim]:
    if not candidates:
        return []

    if _uses_collection_form(skeptic):
        decision = _call_with_supported_arity(skeptic, candidates, executions)
        if isinstance(decision, bool):
            return list(candidates) if decision else []
        kept = _coerce_claims(decision)
        kept_keys = {_claim_key(claim) for claim in kept}
        return [claim for claim in candidates if _claim_key(claim) in kept_keys]

    kept: List[Claim] = []
    for claim in candidates:
        decision = _call_with_supported_arity(skeptic, claim, executions)
        if isinstance(decision, Mapping):
            decision = decision.get("keep", decision.get("accepted", False))
        if decision is True:
            kept.append(claim)
    return kept


def _call_with_supported_arity(
    role: Callable[..., Any], first: Any, second: Any
) -> Any:
    if _can_bind(role, first, second):
        return role(first, second)
    if _can_bind(role, first):
        return role(first)
    raise TypeError("injected role has an unsupported callable signature")


def _can_bind(role: Callable[..., Any], *args: Any) -> bool:
    try:
        inspect.signature(role).bind(*args)
    except (TypeError, ValueError):
        return False
    return True


def _uses_collection_form(skeptic: Skeptic) -> bool:
    try:
        parameters = list(inspect.signature(skeptic).parameters.values())
    except (TypeError, ValueError):
        return False
    if not parameters:
        return False
    name = parameters[0].name.lower()
    return name in {"claims", "candidates", "proposals"}


def _claim_key(claim: Claim) -> Tuple[str, str, str, float]:
    return (claim.statement, claim.execution_id, claim.type, float(claim.confidence))


# Compatibility spellings used by the earlier single-file prototype.
Orchestrator = InvestigationOrchestrator
InvestigationReport = InvestigationResult


# A concise import surface for callers that do not need the implementation
# helpers above.
__all__ = [
    "Analyst",
    "Claim",
    "ExecutionRecord",
    "Executor",
    "InvestigationReport",
    "InvestigationOrchestrator",
    "InvestigationResult",
    "Orchestrator",
    "Plan",
    "Planner",
    "Skeptic",
    "ToolCall",
]
