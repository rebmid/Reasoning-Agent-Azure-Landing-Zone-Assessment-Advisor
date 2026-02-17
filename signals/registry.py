"""Signal registry — maps signal names to provider functions.

Controls never call Azure directly. They request signals by name.
The registry handles dispatch, caching, consistent evidence formatting,
multi-subscription aggregation, and parallel execution.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from signals.types import EvalScope, SignalResult, SignalStatus
from signals.cache import SignalCache

# ── Provider imports ──────────────────────────────────────────────
from signals.providers.resource_graph import (
    fetch_azure_firewalls,
    fetch_vnets,
    fetch_public_ips,
    fetch_route_tables,
    fetch_nsg_list,
)
from signals.providers.management_groups import fetch_mg_hierarchy
from signals.providers.policy import fetch_policy_assignments, fetch_policy_compliance
from signals.providers.defender import fetch_defender_pricings, fetch_secure_score
from signals.providers.diagnostics import fetch_diagnostics_coverage
from signals.providers.storage import fetch_storage_posture
from signals.providers.keyvault import fetch_keyvault_posture
from signals.providers.private_endpoints import fetch_private_endpoint_coverage
from signals.providers.nsg_coverage import fetch_nsg_coverage
from signals.providers.resource_locks import fetch_resource_locks
from signals.providers.rbac import fetch_rbac_hygiene
from signals.providers.app_services import fetch_app_service_posture
from signals.providers.sql import fetch_sql_posture
from signals.providers.containers import fetch_aks_posture, fetch_acr_posture
from signals.providers.backup import fetch_backup_coverage
from signals.providers.entra_logs import fetch_entra_log_availability, fetch_pim_usage
from signals.providers.identity_graph import (
    fetch_pim_maturity,
    fetch_breakglass_validation,
    fetch_sp_owner_risk,
    fetch_admin_ca_coverage,
)
from signals.providers.monitor_topology import fetch_workspace_topology
from signals.providers.activity_log import fetch_activity_log_analysis
from signals.providers.alert_coverage import (
    fetch_alert_action_mapping,
    fetch_action_group_coverage,
    fetch_availability_signals,
)
from signals.providers.change_tracking import fetch_change_tracking
from signals.providers.cost_management import (
    fetch_cost_management_posture,
    fetch_cost_forecast_accuracy,
    fetch_idle_resources,
)
from signals.providers.network_watcher import fetch_network_watcher_posture
from signals.providers.update_manager import fetch_update_manager_posture


# Type: (scope) -> SignalResult
# Each provider receives the scope and extracts what it needs.
ProviderFn = Callable[[EvalScope], SignalResult]


# ══════════════════════════════════════════════════════════════════
#  Multi-subscription aggregation helpers
# ══════════════════════════════════════════════════════════════════

def _merge_raw_dicts(raw_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge raw dicts from per-subscription results.

    Strategy:
      int/float → sum
      bool      → OR (any True wins)
      list      → concatenate
      dict named "coverage" → sum applicable/compliant, recompute ratio
      dict (other) → recursive merge
      str       → first non-empty
    Then recompute known percentage fields from the summed counts.
    """
    if not raw_list:
        return {}
    if len(raw_list) == 1:
        return raw_list[0]

    merged: dict[str, Any] = {}
    # Collect all keys across all dicts
    all_keys: set[str] = set()
    for d in raw_list:
        if d:
            all_keys.update(d.keys())

    for key in all_keys:
        values = [d.get(key) for d in raw_list if d and key in d]
        if not values:
            continue

        first = values[0]
        if key == "coverage" and isinstance(first, dict):
            # Standard coverage merge — sum applicable+compliant, recompute ratio
            total_app = sum(v.get("applicable", 0) for v in values if isinstance(v, dict))
            total_comp = sum(v.get("compliant", 0) for v in values if isinstance(v, dict))
            merged[key] = {
                "applicable": total_app,
                "compliant": total_comp,
                "ratio": round(total_comp / max(total_app, 1), 4),
            }
        elif isinstance(first, bool):
            merged[key] = any(v for v in values if isinstance(v, bool))
        elif isinstance(first, int):
            merged[key] = sum(v for v in values if isinstance(v, (int, float)))
        elif isinstance(first, float):
            merged[key] = sum(v for v in values if isinstance(v, (int, float)))
        elif isinstance(first, list):
            acc: list = []
            for v in values:
                if isinstance(v, list):
                    acc.extend(v)
            merged[key] = acc
        elif isinstance(first, dict):
            merged[key] = _merge_raw_dicts([v for v in values if isinstance(v, dict)])
        elif isinstance(first, str):
            merged[key] = next((v for v in values if v), first)
        else:
            merged[key] = first

    # ── Recompute derived percentage fields from summed counts ─────
    _PERCENT_RECOMPUTE: dict[str, tuple[str, str]] = {
        # "percent_field": ("numerator_field", "denominator_field")
        "compliance_percent": ("noncompliant_resources", "total_resources"),
        "diag_coverage_percent": ("diag_enabled_count", "sample_size"),
    }
    for pf, (num_field, denom_field) in _PERCENT_RECOMPUTE.items():
        if pf in merged and denom_field in merged:
            denom = merged[denom_field]
            if denom and denom > 0:
                if pf == "compliance_percent":
                    # compliance_percent = (total - noncompliant) / total * 100
                    noncompliant = merged.get(num_field, 0)
                    merged[pf] = round((denom - noncompliant) / denom * 100, 1)
                elif pf == "diag_coverage_percent":
                    # diag_coverage_percent = enabled / sample_size * 100
                    enabled = merged.get(num_field, 0)
                    merged[pf] = round(enabled / denom * 100, 1)

    return merged


def _merge_signal_results(results: list[SignalResult]) -> SignalResult:
    """Merge SignalResults from per-subscription calls into one aggregate.

    In addition to the existing merge logic, this now builds a
    ``_per_subscription`` array in the merged raw dict so downstream
    consumers (aggregation layer, workbook, AI) can see which
    subscriptions contributed data and their individual status.
    """
    if not results:
        return SignalResult(
            signal_name="",
            status=SignalStatus.NOT_AVAILABLE,
            error_msg="No subscription results",
        )
    if len(results) == 1:
        return results[0]

    ok_results = [r for r in results if r.status == SignalStatus.OK]
    err_results = [r for r in results if r.status == SignalStatus.ERROR]
    total_ms = sum(r.duration_ms for r in results)

    if not ok_results:
        # All failed
        return SignalResult(
            signal_name=results[0].signal_name,
            status=SignalStatus.ERROR,
            error_msg="; ".join(r.error_msg for r in err_results if r.error_msg),
            duration_ms=total_ms,
        )

    # Merge items (concatenate)
    merged_items: list[dict[str, Any]] = []
    for r in ok_results:
        merged_items.extend(r.items)

    # Merge raw dicts
    raw_list = [r.raw for r in ok_results if r.raw]
    merged_raw = _merge_raw_dicts(raw_list)

    # Track subscription count in raw
    merged_raw["_subscriptions_assessed"] = len(ok_results)
    if err_results:
        merged_raw["_subscription_errors"] = len(err_results)

    # ── Per-subscription breakdown (enterprise aggregation) ───────
    per_sub: list[dict[str, Any]] = []
    for r in results:
        sub_id = (r.raw or {}).get("_subscription_id", "unknown")
        sub_cov = (r.raw or {}).get("coverage", {})
        per_sub.append({
            "subscription_id": sub_id,
            "status": r.status.value,
            "item_count": len(r.items),
            "coverage": sub_cov if isinstance(sub_cov, dict) else {},
        })
    merged_raw["_per_subscription"] = per_sub

    status = SignalStatus.OK
    error_msg = ""
    if err_results:
        error_msg = f"{len(err_results)} sub(s) failed: " + "; ".join(
            r.error_msg for r in err_results[:3] if r.error_msg
        )

    return SignalResult(
        signal_name=ok_results[0].signal_name,
        status=status,
        items=merged_items,
        raw=merged_raw,
        error_msg=error_msg,
        duration_ms=total_ms,
    )


# ── Defender-specific merge (worst-case plan tiers) ──────────────

def _merge_defender_pricings(results: list[SignalResult]) -> SignalResult:
    """Merge Defender pricing results with worst-case tier logic.

    A plan is only "Standard" if it's Standard in ALL subscriptions.
    """
    ok_results = [r for r in results if r.status == SignalStatus.OK]
    if not ok_results:
        return _merge_signal_results(results)

    # Collect per-plan tier across all subscriptions
    # Key: plan name (lowercase) → list of tiers
    plan_tiers: dict[str, list[str]] = {}
    for r in ok_results:
        for item in r.items:
            name = (item.get("name") or item.get("plan") or "").lower()
            tier = (item.get("tier") or item.get("pricingTier") or "Free").capitalize()
            if name:
                plan_tiers.setdefault(name, []).append(tier)

    # Worst-case: if ANY subscription has "Free", entire plan is "Free"
    merged_items = []
    plans_total = 0
    plans_enabled = 0
    for plan_name, tiers in plan_tiers.items():
        worst_tier = "Standard" if all(t == "Standard" for t in tiers) else "Free"
        plans_total += 1
        if worst_tier == "Standard":
            plans_enabled += 1
        merged_items.append({
            "name": plan_name,
            "tier": worst_tier,
            "pricingTier": worst_tier,
            "subscriptions_standard": sum(1 for t in tiers if t == "Standard"),
            "subscriptions_total": len(tiers),
        })

    return SignalResult(
        signal_name="defender:pricings",
        status=SignalStatus.OK,
        items=merged_items,
        raw={
            "plans_total": plans_total,
            "plans_enabled": plans_enabled,
            "coverage": {
                "applicable": plans_total,
                "compliant": plans_enabled,
                "ratio": round(plans_enabled / max(plans_total, 1), 4),
            },
            "_subscriptions_assessed": len(ok_results),
        },
        duration_ms=sum(r.duration_ms for r in results),
    )


def _merge_defender_scores(results: list[SignalResult]) -> SignalResult:
    """Merge Secure Score results as weighted average."""
    ok_results = [r for r in results if r.status == SignalStatus.OK]
    if not ok_results:
        return _merge_signal_results(results)

    total_current = 0.0
    total_max = 0.0
    all_items = []
    for r in ok_results:
        for item in r.items:
            current = item.get("current", 0) or 0
            max_score = item.get("max", 0) or 0
            total_current += current
            total_max += max_score
            all_items.append(item)

    composite_pct = round(total_current / max(total_max, 1) * 100, 1)

    # Return a single composite score as items[0] (evaluators read items[0])
    composite_item = {
        "name": "composite",
        "percentage": composite_pct,
        "current": round(total_current, 2),
        "max": round(total_max, 2),
        "_subscriptions_assessed": len(ok_results),
    }

    return SignalResult(
        signal_name="defender:secure_score",
        status=SignalStatus.OK,
        items=[composite_item] + all_items,
        raw={
            "composite_percentage": composite_pct,
            "total_current": round(total_current, 2),
            "total_max": round(total_max, 2),
            "_subscriptions_assessed": len(ok_results),
        },
        duration_ms=sum(r.duration_ms for r in results),
    )


# ── Workspace topology merge (special booleans) ──────────────────

def _merge_workspace_topology(results: list[SignalResult]) -> SignalResult:
    """Merge workspace topology with worst-case booleans (AND for is_centralized)."""
    base = _merge_signal_results(results)
    if base.status != SignalStatus.OK or not base.raw:
        return base

    ok_raws = [r.raw for r in results if r.status == SignalStatus.OK and r.raw is not None]

    # is_centralized: TRUE only if ALL subs have ≤2 workspaces
    base.raw["is_centralized"] = all(
        d.get("is_centralized", False) for d in ok_raws
    )
    # sentinel_enabled: TRUE only if ALL subs have Sentinel
    base.raw["sentinel_enabled"] = all(
        d.get("sentinel_enabled", False) for d in ok_raws
    )
    # max_retention: minimum across subs (worst-case)
    retentions = [d.get("max_retention_days", 0) for d in ok_raws if d.get("max_retention_days")]
    if retentions:
        base.raw["max_retention_days"] = min(retentions)

    return base


# ══════════════════════════════════════════════════════════════════
#  Provider wrappers
# ══════════════════════════════════════════════════════════════════

def _rg_provider(fetch_fn: Callable) -> ProviderFn:
    """Wrap a Resource Graph fetch that needs subscription list.

    RG queries are already cross-subscription — pass all IDs at once.
    """
    def _inner(scope: EvalScope) -> SignalResult:
        subs = scope.subscription_ids
        if not subs:
            return SignalResult(
                signal_name="",
                status=SignalStatus.NOT_AVAILABLE,
                error_msg="No subscriptions in scope",
            )
        return fetch_fn(subs)
    return _inner


def _multi_sub_provider(
    fetch_fn: Callable,
    *,
    merge_fn: Callable[[list[SignalResult]], SignalResult] | None = None,
) -> ProviderFn:
    """Wrap a subscription-scoped provider so it runs across ALL visible subscriptions.

    For single-subscription scopes: identical to the old _sub_provider.
    For multi-subscription scopes: calls the provider for each subscription
    in parallel using a thread pool, then merges results using *merge_fn*.
    """
    merger = merge_fn or _merge_signal_results

    def _inner(scope: EvalScope) -> SignalResult:
        subs = scope.subscription_ids
        if not subs:
            return SignalResult(
                signal_name="",
                status=SignalStatus.NOT_AVAILABLE,
                error_msg="No subscriptions in scope",
            )
        if len(subs) == 1:
            r = fetch_fn(subs[0])
            r.raw = r.raw or {}
            r.raw["_subscription_id"] = subs[0]
            return r

        # ── Run across all subscriptions in parallel ──────────────
        results: list[SignalResult] = []
        max_workers = min(len(subs), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(fetch_fn, sub): sub for sub in subs}
            for future in as_completed(futures):
                sub_id = futures[future]
                try:
                    r = future.result()
                    r.raw = r.raw or {}
                    r.raw["_subscription_id"] = sub_id
                    results.append(r)
                except Exception as exc:
                    results.append(SignalResult(
                        signal_name="",
                        status=SignalStatus.ERROR,
                        error_msg=f"{sub_id[:8]}: {exc}",
                        raw={"_subscription_id": sub_id},
                    ))

        return merger(results)
    return _inner


def _tenant_provider(fetch_fn: Callable) -> ProviderFn:
    """Wrap a tenant-scoped provider (e.g. Graph API).

    These only need to be called ONCE regardless of subscription count,
    since they query Entra ID or other tenant-level data.
    """
    def _inner(scope: EvalScope) -> SignalResult:
        sub = scope.subscription_ids[0] if scope.subscription_ids else None
        if not sub:
            return SignalResult(
                signal_name="",
                status=SignalStatus.NOT_AVAILABLE,
                error_msg="No subscriptions in scope",
            )
        return fetch_fn(sub)
    return _inner


def _mg_provider(scope: EvalScope) -> SignalResult:
    sub = scope.subscription_ids[0] if scope.subscription_ids else None
    return fetch_mg_hierarchy(subscription_id=sub)


def _diag_provider(scope: EvalScope) -> SignalResult:
    """Diagnostics coverage across all subscriptions.

    Limits per-subscription sample size to keep total calls manageable.
    """
    subs = scope.subscription_ids
    if not subs:
        return SignalResult(
            signal_name="monitor:diag_coverage_sample",
            status=SignalStatus.NOT_AVAILABLE,
            error_msg="No subscriptions in scope",
        )

    # Scale per-sub sample: 200 for ≤3 subs, reduce for larger tenants
    per_sub_limit = max(20, min(200, 500 // len(subs)))

    def _fetch_one(sub_id: str) -> SignalResult:
        return fetch_diagnostics_coverage(sub_id, max_resources=per_sub_limit)

    if len(subs) == 1:
        return _fetch_one(subs[0])

    results: list[SignalResult] = []
    max_workers = min(len(subs), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, sub): sub for sub in subs}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(SignalResult(
                    signal_name="monitor:diag_coverage_sample",
                    status=SignalStatus.ERROR,
                    error_msg=str(exc),
                ))

    return _merge_signal_results(results)


# ── Master registry ──────────────────────────────────────────────
SIGNAL_PROVIDERS: dict[str, ProviderFn] = {
    # Resource Graph signals (already cross-subscription)
    "resource_graph:azure_firewall": _rg_provider(fetch_azure_firewalls),
    "resource_graph:vnets":          _rg_provider(fetch_vnets),
    "resource_graph:public_ips":     _rg_provider(fetch_public_ips),
    "resource_graph:route_tables":   _rg_provider(fetch_route_tables),
    "resource_graph:nsgs":           _rg_provider(fetch_nsg_list),

    # ARM / Management Groups (tenant-scoped — single call)
    "arm:mg_hierarchy":              _mg_provider,

    # Policy (multi-sub aggregation — sum counts)
    "policy:assignments":            _multi_sub_provider(fetch_policy_assignments),
    "policy:compliance_summary":     _multi_sub_provider(fetch_policy_compliance),

    # Defender (special merge — worst-case plan tiers / weighted scores)
    "defender:pricings":             _multi_sub_provider(
                                         fetch_defender_pricings,
                                         merge_fn=_merge_defender_pricings,
                                     ),
    "defender:secure_score":         _multi_sub_provider(
                                         fetch_secure_score,
                                         merge_fn=_merge_defender_scores,
                                     ),

    # Diagnostics / Monitoring (custom multi-sub with sample limit)
    "monitor:diag_coverage_sample":  _diag_provider,

    # ── Posture / Coverage signals (RG — already cross-sub) ──────
    # Data & PaaS protection
    "resource_graph:storage_posture":     _rg_provider(fetch_storage_posture),
    "resource_graph:keyvault_posture":    _rg_provider(fetch_keyvault_posture),
    "resource_graph:sql_posture":         _rg_provider(fetch_sql_posture),
    "resource_graph:app_service_posture": _rg_provider(fetch_app_service_posture),
    "resource_graph:acr_posture":         _rg_provider(fetch_acr_posture),
    "resource_graph:aks_posture":         _rg_provider(fetch_aks_posture),

    # Networking / Security coverage (RG — already cross-sub)
    "resource_graph:private_endpoints":   _rg_provider(fetch_private_endpoint_coverage),
    "resource_graph:nsg_coverage":        _rg_provider(fetch_nsg_coverage),
    "resource_graph:resource_locks":      _rg_provider(fetch_resource_locks),

    # Resilience (RG — already cross-sub)
    "resource_graph:backup_coverage":     _rg_provider(fetch_backup_coverage),

    # Identity / RBAC (RG — already cross-sub)
    "identity:rbac_hygiene":              _rg_provider(fetch_rbac_hygiene),

    # ── New signal categories ─────────────────────────────────────
    # Identity — Entra ID logs & PIM (tenant-scoped via Graph API)
    "identity:entra_log_availability":    _tenant_provider(fetch_entra_log_availability),
    "identity:pim_usage":                 _tenant_provider(fetch_pim_usage),
    "identity:pim_maturity":             _tenant_provider(fetch_pim_maturity),
    "identity:breakglass_validation":     _tenant_provider(fetch_breakglass_validation),
    "identity:sp_owner_risk":             _rg_provider(fetch_sp_owner_risk),
    "identity:admin_ca_coverage":         _tenant_provider(fetch_admin_ca_coverage),

    # Management / Monitor (multi-sub aggregation)
    "monitor:workspace_topology":         _multi_sub_provider(
                                              fetch_workspace_topology,
                                              merge_fn=_merge_workspace_topology,
                                          ),
    "monitor:activity_log_analysis":      _multi_sub_provider(fetch_activity_log_analysis),
    "monitor:alert_action_mapping":       _rg_provider(fetch_alert_action_mapping),
    "monitor:action_group_coverage":      _multi_sub_provider(fetch_action_group_coverage),
    "monitor:availability_signals":       _multi_sub_provider(fetch_availability_signals),
    "monitor:change_tracking":            _multi_sub_provider(fetch_change_tracking),

    # Cost Management (multi-sub aggregation)
    "cost:management_posture":            _multi_sub_provider(fetch_cost_management_posture),
    "cost:forecast_accuracy":             _multi_sub_provider(fetch_cost_forecast_accuracy),
    "cost:idle_resources":                _rg_provider(fetch_idle_resources),

    # Network Watcher (multi-sub aggregation)
    "network:watcher_posture":            _multi_sub_provider(fetch_network_watcher_posture),

    # Update Manager (multi-sub aggregation)
    "manage:update_manager":              _multi_sub_provider(fetch_update_manager_posture),
}


# ══════════════════════════════════════════════════════════════════
#  SignalBus — dispatch + cache + parallel fetch
# ══════════════════════════════════════════════════════════════════

class SignalBus:
    """
    Fetch signals by name with automatic caching.
    If 10 controls require "arm:mg_hierarchy", it's queried once.
    """

    def __init__(self, cache: SignalCache | None = None):
        self.cache = cache or SignalCache()
        self.events: list[dict[str, Any]] = []  # for streaming
        self._lock = threading.Lock()

    def fetch(
        self,
        signal_name: str,
        scope: EvalScope,
        *,
        freshness_seconds: int | None = None,
    ) -> SignalResult:
        """Fetch a signal, returning from cache if fresh."""
        scope_dict = {
            "tenant_id": scope.tenant_id,
            "mg_id": scope.management_group_id,
            "subs": sorted(scope.subscription_ids),
            "rg": scope.resource_group,
        }

        # Check cache first
        cached = self.cache.get(signal_name, scope_dict, freshness_seconds=freshness_seconds)
        if cached is not None:
            self._emit("signal_returned", signal_name, cache_hit=True, ms=0)
            return cached

        # Fetch from provider
        provider = SIGNAL_PROVIDERS.get(signal_name)
        if provider is None:
            result = SignalResult(
                signal_name=signal_name,
                status=SignalStatus.ERROR,
                error_msg=f"Unknown signal: {signal_name}",
            )
            return result

        self._emit("signal_requested", signal_name)
        result = provider(scope)
        result.signal_name = signal_name  # ensure consistent naming

        # Cache it
        self.cache.put(signal_name, scope_dict, result)
        self._emit("signal_returned", signal_name, cache_hit=False, ms=result.duration_ms)

        return result

    def fetch_many(
        self,
        signal_names: list[str],
        scope: EvalScope,
    ) -> dict[str, SignalResult]:
        """Fetch multiple signals in parallel. Returns {name: result}.

        Uses a thread pool with bounded concurrency so multiple signals
        can query Azure simultaneously.  Each signal's internal per-sub
        parallelism is preserved.
        """
        # Determine how many to run concurrently
        max_workers = min(len(signal_names), 6)
        if max_workers <= 1:
            return {name: self.fetch(name, scope) for name in signal_names}

        results: dict[str, SignalResult] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.fetch, name, scope): name
                for name in signal_names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = SignalResult(
                        signal_name=name,
                        status=SignalStatus.ERROR,
                        error_msg=str(exc),
                    )
        return results

    def _emit(self, event_type: str, signal_name: str, **kwargs: Any) -> None:
        with self._lock:
            self.events.append({"type": event_type, "signal": signal_name, **kwargs})

    def reset_events(self) -> list[dict[str, Any]]:
        with self._lock:
            events = self.events.copy()
            self.events.clear()
        return events
