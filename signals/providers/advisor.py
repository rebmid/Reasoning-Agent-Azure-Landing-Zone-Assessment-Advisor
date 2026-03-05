"""Azure Advisor signal provider — cross-pillar recommendations."""
from __future__ import annotations

import time
from signals.types import SignalResult, SignalStatus


def fetch_advisor_recommendations(subscription_id: str) -> SignalResult:
    """Fetch Azure Advisor recommendations for a subscription.

    Categorizes by impact (High/Medium/Low) and category
    (Security, Cost, Reliability, OperationalExcellence, Performance).
    """
    from collectors.azure_client import build_client

    start = time.perf_counter_ns()
    try:
        client = build_client(subscription_id)
        path = f"/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations"
        resp = client.get(path, api_version="2023-01-01", params={"$top": "200"})
        items = resp.get("value", [])

        recs = []
        by_category: dict[str, int] = {}
        by_impact: dict[str, int] = {}
        for item in items:
            props = item.get("properties", {})
            category = props.get("category", "Unknown")
            impact = props.get("impact", "Unknown")
            by_category[category] = by_category.get(category, 0) + 1
            by_impact[impact] = by_impact.get(impact, 0) + 1
            recs.append({
                "name": props.get("shortDescription", {}).get("problem", ""),
                "category": category,
                "impact": impact,
                "resourceId": props.get("resourceMetadata", {}).get("resourceId", ""),
            })

        ms = (time.perf_counter_ns() - start) // 1_000_000
        return SignalResult(
            signal_name="advisor:recommendations",
            status=SignalStatus.OK,
            items=recs[:100],
            raw={
                "total": len(recs),
                "by_category": by_category,
                "by_impact": by_impact,
            },
            duration_ms=ms,
        )
    except Exception as e:
        ms = (time.perf_counter_ns() - start) // 1_000_000
        return SignalResult(
            signal_name="advisor:recommendations",
            status=SignalStatus.ERROR,
            error_msg=str(e)[:200],
            duration_ms=ms,
        )


def fetch_defender_assessments(subscription_id: str) -> SignalResult:
    """Fetch Microsoft Defender for Cloud security assessments."""
    from collectors.azure_client import build_client

    start = time.perf_counter_ns()
    try:
        client = build_client(subscription_id)
        path = f"/subscriptions/{subscription_id}/providers/Microsoft.Security/assessments"
        resp = client.get(path, api_version="2021-06-01", params={"$top": "200"})
        items = resp.get("value", [])

        assessments = []
        healthy = 0
        unhealthy = 0
        for item in items:
            props = item.get("properties", {})
            status_code = props.get("status", {}).get("code", "")
            if status_code.lower() == "healthy":
                healthy += 1
            elif status_code.lower() == "unhealthy":
                unhealthy += 1
            assessments.append({
                "name": props.get("displayName", ""),
                "status": status_code,
                "severity": props.get("metadata", {}).get("severity", ""),
                "resourceId": props.get("resourceDetails", {}).get("id", ""),
            })

        ms = (time.perf_counter_ns() - start) // 1_000_000
        return SignalResult(
            signal_name="defender:assessments",
            status=SignalStatus.OK,
            items=assessments[:100],
            raw={
                "total": len(assessments),
                "healthy": healthy,
                "unhealthy": unhealthy,
                "health_pct": round(healthy / max(healthy + unhealthy, 1) * 100, 1),
            },
            duration_ms=ms,
        )
    except Exception as e:
        ms = (time.perf_counter_ns() - start) // 1_000_000
        return SignalResult(
            signal_name="defender:assessments",
            status=SignalStatus.ERROR,
            error_msg=str(e)[:200],
            duration_ms=ms,
        )
