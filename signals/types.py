"""Core types for the signal layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SignalStatus(str, Enum):
    OK = "OK"
    NOT_AVAILABLE = "NotAvailable"
    ERROR = "Error"
    SIGNAL_ERROR = "SignalError"  # distinct from ERROR — signal provider failure (not eval logic)


@dataclass
class SignalResult:
    """Unified result from any signal provider."""
    signal_name: str
    status: SignalStatus
    items: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    error_msg: str = ""
    duration_ms: int = 0


@dataclass
class CoveragePayload:
    """Normalized coverage summary returned by posture providers."""
    applicable: int = 0
    compliant: int = 0
    ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"applicable": self.applicable, "compliant": self.compliant, "ratio": self.ratio}


@dataclass
class EvalScope:
    """Scope targeting for on-demand evaluation."""
    tenant_id: str | None = None
    management_group_id: str | None = None
    subscription_ids: list[str] = field(default_factory=list)
    resource_group: str | None = None


# Confidence scale: 1.0 = direct resource evidence, 0.8 = inferred,
# 0.5 = partial sample, 0.3 = heuristic, 0.0 = manual/no evidence.
CONFIDENCE_LABEL = {
    "High": 1.0,
    "Medium": 0.7,
    "Low": 0.3,
}


@dataclass
class ControlResult:
    """Deterministic result from a single control evaluator."""
    status: str  # Pass | Fail | Partial | Manual | NotApplicable | Unknown | Error | SignalError
    severity: str = "Medium"
    confidence: str = "High"
    confidence_score: float = 1.0  # numeric 0-1 (overrides label when set)
    reason: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    signals_used: list[str] = field(default_factory=list)
    next_checks: list[dict[str, str]] = field(default_factory=list)
    coverage: CoveragePayload | None = None  # populated by coverage-based evaluators


@dataclass
class EvalContext:
    """Runtime context passed into every evaluator."""
    scope: EvalScope
    run_id: str = ""
    options: dict[str, Any] = field(default_factory=dict)
