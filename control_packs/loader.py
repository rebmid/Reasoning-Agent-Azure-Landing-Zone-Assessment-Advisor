"""Control pack loader — discovers and loads versioned control packs.

Usage:
    from control_packs.loader import load_pack, list_packs
    pack = load_pack("alz", "v1.0")
    pack.signals   # signal definitions
    pack.controls  # dict[str, ControlDefinition] — frozen, typed

Taxonomy enforcement:
    Every ``load_pack()`` call runs ``validate_and_build_controls()``
    which validates raw JSON dicts and constructs frozen
    ``ControlDefinition`` instances.  If ANY control has a missing or
    invalid taxonomy field the loader raises ``TaxonomyViolation`` —
    the assessment never starts.

Version locking:
    The ALZ v1.0 pack is frozen.  A SHA-256 checksum of controls.json
    is verified at load time.  If the file changes without an explicit
    version bump the loader raises ``ControlPackVersionError``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from schemas.taxonomy import ControlDefinition
from engine.taxonomy_validator import validate_and_build_controls

_log = logging.getLogger(__name__)

# ── Version-locked checksums ──────────────────────────────────────
# SHA-256 of the canonical controls.json for each frozen version.
# If a pack is listed here, any content change requires an explicit
# version bump (new directory under control_packs/<family>/).
_FROZEN_CHECKSUMS: dict[str, str] = {
    "alz/v1.0": "52aca64690261eed",  # 59 controls, 8 design areas (v1.4.0)
}


class ControlPackVersionError(Exception):
    """Raised when a frozen control pack's checksum does not match."""
    pass


@dataclass
class ControlPack:
    """A loaded control pack with its signal and control definitions.

    ``controls`` is ``dict[str, ControlDefinition]`` — every value is a
    frozen, typed dataclass.  No ``dict[str, Any]`` access patterns.
    """
    pack_id: str
    name: str
    version: str
    description: str
    signals: dict[str, dict[str, Any]]
    controls: dict[str, ControlDefinition]
    design_areas: dict[str, dict[str, Any]]
    manifest: dict[str, Any]
    controls_checksum: str = ""

    @property
    def version_tag(self) -> str:
        """Canonical version identifier for run metadata (e.g. 'alz-v1.0')."""
        return self.pack_id or f"{self.manifest.get('pack_id', 'unknown')}"

    def signal_bus_names(self) -> list[str]:
        """Return all signal_bus_name values (non-null) for preflight cross-ref."""
        return [
            s["signal_bus_name"]
            for s in self.signals.values()
            if s.get("signal_bus_name")
        ]

    def signals_for_preflight_probe(self, probe_name: str) -> list[str]:
        """Return signal names that depend on a specific preflight probe."""
        return [
            name for name, s in self.signals.items()
            if s.get("preflight_probe") == probe_name
        ]

    def controls_in_area(self, area: str) -> list[str]:
        """Return control short IDs in a design area."""
        da = self.design_areas.get(area, {})
        return da.get("controls", [])

    def control_count(self) -> int:
        return len(self.controls)


PACKS_DIR = Path(__file__).parent


def list_packs() -> list[dict[str, str]]:
    """Discover all available control packs under control_packs/."""
    packs = []
    for manifest_path in PACKS_DIR.rglob("manifest.json"):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                m = json.load(f)
            packs.append({
                "pack_id": m.get("pack_id", "unknown"),
                "name": m.get("name", ""),
                "version": m.get("version", ""),
                "path": str(manifest_path.parent),
            })
        except Exception:
            continue
    return packs


def load_pack(family: str = "alz", version: str = "v1.0") -> ControlPack:
    """
    Load a control pack by family and version.

    Flow:
      1. Read manifest, signals, controls JSON from disk.
      2. ``validate_and_build_controls()`` validates raw dicts and
         constructs frozen ``ControlDefinition`` instances.
      3. Pack is returned with typed ``controls``.

    Args:
        family: Pack family directory name (e.g. "alz")
        version: Version directory name (e.g. "v1.0")

    Returns:
        ControlPack with all definitions loaded.
    """
    pack_dir = PACKS_DIR / family / version

    # Load manifest
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Control pack not found: {pack_dir}")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    # Load signals
    signals_path = pack_dir / manifest.get("signals_ref", "signals.json")
    with open(signals_path, encoding="utf-8") as f:
        signals_data = json.load(f)

    # Load controls
    controls_path = pack_dir / manifest.get("controls_ref", "controls.json")
    with open(controls_path, encoding="utf-8") as f:
        controls_data = json.load(f)

    raw_controls = controls_data.get("controls", {})
    design_areas = controls_data.get("design_areas", {})

    # ── Taxonomy enforcement + typed construction ─────────────────
    # Validates every field, every enum, every cross-reference.
    # Returns dict[str, ControlDefinition] or raises TaxonomyViolation.
    typed_controls = validate_and_build_controls(raw_controls, design_areas)

    # ── Version-lock guardrail ────────────────────────────────────
    # Frozen packs must not change on disk without a version bump.
    with open(controls_path, "rb") as fb:
        controls_checksum = hashlib.sha256(fb.read()).hexdigest()[:16]

    pack_key = f"{family}/{version}"
    expected = _FROZEN_CHECKSUMS.get(pack_key)
    if expected and controls_checksum != expected:
        raise ControlPackVersionError(
            f"Control pack '{pack_key}' is version-locked (expected checksum "
            f"{expected}, got {controls_checksum}).  If you modified controls.json, "
            f"create a new version directory (e.g. {family}/v1.1/) and update "
            f"_FROZEN_CHECKSUMS in control_packs/loader.py."
        )
    if expected:
        _log.debug("Control pack %s: checksum verified (%s)", pack_key, controls_checksum)

    pack = ControlPack(
        pack_id=manifest.get("pack_id", ""),
        name=manifest.get("name", ""),
        version=manifest.get("version", ""),
        description=manifest.get("description", ""),
        signals=signals_data.get("signals", {}),
        controls=typed_controls,
        design_areas=design_areas,
        manifest=manifest,
        controls_checksum=controls_checksum,
    )

    return pack
