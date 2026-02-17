"""CSA Workbook builder — template-based, data-only writer.

Copies the pre-built ``.xlsm`` template and writes **only** data rows
into the Checklist table (row 10 +, columns A–U).  Enrichment metadata
(columns V–Y) is written in the same open-workbook pass.

The template owns **all** visualisation: Dashboard formulas, charts,
conditional formatting, data validation, and VBA macros.  Python never
creates, modifies, or deletes those artefacts.

After saving, a ZIP-level restoration step re-injects any x14
extensions that openpyxl strips during its load / save cycle so the
workbook opens in Excel without corruption warnings.

Usage::

    from reporting.csa_workbook import build_csa_workbook
    build_csa_workbook(
        run_path="out/run.json",
        output_path="out/CSA_Workbook_v1.xlsm",
    )
"""
from __future__ import annotations

import json
import os
import re
import shutil
import warnings
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _load_json(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_get(obj: Any, dotpath: str, default: Any = "") -> Any:
    for key in dotpath.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key, {})
        else:
            return default
    return obj if obj != {} else default


def _join_list(value) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    return str(value) if value else ""


# ══════════════════════════════════════════════════════════════════
# Template + Checklist constants
# ══════════════════════════════════════════════════════════════════

_TEMPLATE_DIR = Path(__file__).resolve().parent
_TEMPLATE_NAME = "Landing_Zone_Assessment.xlsm"
_TEMPLATE_PATH = _TEMPLATE_DIR / _TEMPLATE_NAME

_CHECKLIST_SHEET = "Checklist"
_CHECKLIST_HEADER_ROW = 9
_CHECKLIST_DATA_START = 10

_STATUS_MAP: dict[str, str] = {
    "Pass":         "Fulfilled",
    "Fail":         "Open",
    "Manual":       "Not verified",
    "Partial":      "Open",
    "Fulfilled":    "Fulfilled",
    "Open":         "Open",
    "Not verified": "Not verified",
    "Not required": "Not required",
    "N/A":          "N/A",
}


def _map_status(raw: str) -> str:
    return _STATUS_MAP.get(raw, "Not verified")


# ══════════════════════════════════════════════════════════════════
# Data writing  (Checklist rows only — columns A–U)
# ══════════════════════════════════════════════════════════════════

def _clear_data_rows(ws, start_row: int = _CHECKLIST_DATA_START):
    """Clear data rows without touching headers or table structure."""
    for row in range(start_row, ws.max_row + 1):
        for col in range(1, 26):          # A–Y (include enrichment cols)
            ws.cell(row=row, column=col).value = None


def _write_checklist_rows(
    ws,
    results: list[dict],
    checklist_lookup: dict[str, dict],
) -> int:
    """Populate the Checklist sheet starting at row 10.

    Every row represents exactly one control — no summary or aggregate
    rows are written.  Returns the number of rows written.
    """
    row = _CHECKLIST_DATA_START

    for ctrl in results:
        cid = ctrl.get("control_id", "")
        cl = checklist_lookup.get(cid, {})

        ws.cell(row=row, column=1,  value=cl.get("id", ""))
        ws.cell(row=row, column=2,  value=cl.get(
            "category", ctrl.get("category", ctrl.get("section", ""))))
        ws.cell(row=row, column=3,  value=cl.get("subcategory", ""))
        ws.cell(row=row, column=4,  value=cl.get("waf", ""))
        ws.cell(row=row, column=5,  value=cl.get("service", ""))
        ws.cell(row=row, column=6,  value=cl.get(
            "text", ctrl.get("text", ctrl.get("question", ""))))
        ws.cell(row=row, column=7,  value="")
        ws.cell(row=row, column=8,  value=ctrl.get(
            "severity", cl.get("severity", "")))
        ws.cell(row=row, column=9,  value=_map_status(
            ctrl.get("status", "Manual")))

        # Comment / evidence
        evidence = ctrl.get("evidence", [])
        parts: list[str] = []
        notes = ctrl.get("notes", "")
        if notes:
            parts.append(notes)
        for ev in evidence[:2]:
            if isinstance(ev, dict):
                s = ev.get("summary", ev.get("resource_id", ""))
                if s:
                    parts.append(str(s)[:120])
        ws.cell(row=row, column=10, value="\n".join(parts))

        ws.cell(row=row, column=11, value="")                       # AMMP
        ws.cell(row=row, column=12, value=cl.get("link", ""))       # Learn link
        ws.cell(row=row, column=13, value=cl.get("training", ""))   # Training
        ws.cell(row=row, column=14, value=ctrl.get("signal_used", ""))
        ws.cell(row=row, column=15, value=cid)                      # GUID
        for c in range(16, 21):
            ws.cell(row=row, column=c, value="")                    # P–T
        ws.cell(row=row, column=21, value="lz-assessor")            # Source

        row += 1

    return row - _CHECKLIST_DATA_START


# ══════════════════════════════════════════════════════════════════
# ZIP-level extension restoration
# ══════════════════════════════════════════════════════════════════

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL  = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _zip_sheet_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """Return ``{sheet_name: zip_path}`` from workbook.xml + rels."""
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_target: dict[str, str] = {}
    for r in rels:
        t = r.get("Target", "")
        # Normalise to ZIP-entry path (no leading /, relative to xl/)
        t = t.lstrip("/")
        if not t.startswith("xl/"):
            t = "xl/" + t
        rid = r.get("Id", "")
        if rid:
            rid_target[rid] = t

    result: dict[str, str] = {}
    for sh in wb.findall(f".//{{{_NS_MAIN}}}sheet"):
        name = sh.get("name", "")
        rid = sh.get(f"{{{_NS_REL}}}id")
        if name and rid and rid in rid_target:
            result[name] = rid_target[rid]
    return result


def _extract_ws_extlst(data: bytes) -> bytes | None:
    """Extract the worksheet-level ``<extLst>…</extLst>`` block.

    The worksheet-level extLst is always the last child element before
    ``</worksheet>``.  Handles nested extLst elements correctly.
    """
    ws_end = data.rfind(b"</worksheet>")
    if ws_end == -1:
        return None
    region = data[:ws_end]

    start = region.rfind(b"<extLst")
    if start == -1:
        return None

    # Walk forward to find the matching </extLst> (handles nesting)
    depth = 0
    pos = start
    OPEN = b"<extLst"
    CLOSE = b"</extLst>"
    # Move past the initial tag
    scan_from = start + len(OPEN)
    while scan_from < ws_end:
        next_open = data.find(OPEN, scan_from)
        next_close = data.find(CLOSE, scan_from)

        if next_close == -1:
            return None

        if next_open != -1 and next_open < next_close:
            depth += 1
            scan_from = next_open + len(OPEN)
        else:
            if depth == 0:
                return data[start : next_close + len(CLOSE)]
            depth -= 1
            scan_from = next_close + len(CLOSE)
    return None


def _extract_ns_decls(data: bytes) -> list[bytes]:
    """Extract ``xmlns:*`` declarations from the root element tag."""
    # Skip XML declaration if present
    if data.startswith(b"<?"):
        decl_end = data.find(b"?>")
        root_start = data.find(b"<", decl_end + 2)
    else:
        root_start = 0
    root_end = data.find(b">", root_start)
    if root_end == -1:
        return []
    root_tag = data[root_start:root_end]
    return re.findall(rb'xmlns:\w+="[^"]*"', root_tag)


def _extract_mc_ignorable(data: bytes) -> bytes | None:
    """Extract ``mc:Ignorable`` attribute value from the root tag."""
    if data.startswith(b"<?"):
        decl_end = data.find(b"?>")
        root_start = data.find(b"<", decl_end + 2)
    else:
        root_start = 0
    root_end = data.find(b">", root_start)
    if root_end == -1:
        return None
    root_tag = data[root_start:root_end]
    m = re.search(rb'mc:Ignorable="([^"]*)"', root_tag)
    return m.group(1) if m else None


def _root_tag_end(data: bytes) -> int:
    """Return the byte offset of the first ``>`` in the root element."""
    if data.startswith(b"<?"):
        decl_end = data.find(b"?>")
        root_start = data.find(b"<", decl_end + 2)
    else:
        root_start = 0
    return data.find(b">", root_start)


def _restore_extensions(template_path: str, output_path: str) -> int:
    """Re-inject x14 extensions that openpyxl strips on load / save.

    For every worksheet present in **both** the template and the output,
    copies the ``<extLst>`` block (x14 conditional formatting, data
    validation, etc.) from the template back into the output.  Also
    ensures the required ``xmlns:*`` and ``mc:Ignorable`` declarations
    exist on the ``<worksheet>`` root element.

    The output ZIP is rewritten in-place (via a temp file).
    Returns the number of sheets patched.
    """
    # ── Read template sheet data ──────────────────────────────────
    with zipfile.ZipFile(template_path, "r") as zt:
        tpl_map = _zip_sheet_map(zt)
        tpl_data: dict[str, bytes] = {}
        for name, path in tpl_map.items():
            try:
                tpl_data[name] = zt.read(path)
            except KeyError:
                pass

    # ── Read all output ZIP entries (preserving order) ────────────
    with zipfile.ZipFile(output_path, "r") as zo:
        out_map = _zip_sheet_map(zo)
        entry_order = zo.namelist()
        entries: dict[str, bytes] = {n: zo.read(n) for n in entry_order}

    # ── Patch each sheet ──────────────────────────────────────────
    patched = 0
    for sheet_name, tpl_bytes in tpl_data.items():
        if sheet_name not in out_map or sheet_name == "ARG":
            continue
        out_path = out_map[sheet_name]
        if out_path not in entries:
            continue

        extlst = _extract_ws_extlst(tpl_bytes)
        if extlst is None:
            continue

        out_bytes = entries[out_path]

        # Remove any partial extLst openpyxl may have left
        existing = _extract_ws_extlst(out_bytes)
        if existing:
            out_bytes = out_bytes.replace(existing, b"")

        # Inject template extLst before </worksheet>
        ws_end = out_bytes.rfind(b"</worksheet>")
        out_bytes = out_bytes[:ws_end] + extlst + b"\n" + out_bytes[ws_end:]

        # Ensure namespace declarations from template are present
        tpl_ns = _extract_ns_decls(tpl_bytes)
        for ns_decl in tpl_ns:
            rte = _root_tag_end(out_bytes)
            if ns_decl not in out_bytes[:rte]:
                out_bytes = out_bytes[:rte] + b" " + ns_decl + out_bytes[rte:]

        # Ensure mc:Ignorable matches the template
        tpl_mc = _extract_mc_ignorable(tpl_bytes)
        if tpl_mc:
            out_mc = _extract_mc_ignorable(out_bytes)
            if out_mc != tpl_mc:
                rte = _root_tag_end(out_bytes)
                root_tag = out_bytes[:rte]
                if out_mc:
                    root_tag = root_tag.replace(
                        b'mc:Ignorable="' + out_mc + b'"',
                        b'mc:Ignorable="' + tpl_mc + b'"',
                    )
                else:
                    root_tag += b' mc:Ignorable="' + tpl_mc + b'"'
                out_bytes = root_tag + out_bytes[rte:]

        entries[out_path] = out_bytes
        patched += 1

    # ── Rewrite the ZIP ───────────────────────────────────────────
    if patched > 0:
        tmp = output_path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in entry_order:
                if name in entries:
                    zout.writestr(name, entries[name])
        os.replace(tmp, output_path)

    return patched


# ══════════════════════════════════════════════════════════════════
# Main builder
# ══════════════════════════════════════════════════════════════════

def build_csa_workbook(
    run_path: str = "out/run.json",
    target_path: str = "out/target_architecture.json",
    output_path: str = "out/CSA_Workbook_v1.xlsm",
    why_payloads: list[dict] | None = None,
    template_path: str | None = None,
) -> str:
    """Build the CSA workbook: copy template, write Checklist data only.

    The template contains the Dashboard, formulas, charts, conditional
    formatting, data validation, and VBA.  Python writes **only** the
    Checklist data rows (one per control) and enrichment metadata
    columns (V–Y).

    Parameters ``target_path`` and ``why_payloads`` are accepted for
    backward compatibility but are not used for Excel output.  Those
    payloads feed the JSON / HTML reports instead.
    """
    run = _load_json(run_path)

    # ── Resolve template ──────────────────────────────────────────
    tpl = Path(template_path) if template_path else _TEMPLATE_PATH
    if not tpl.exists():
        raise FileNotFoundError(
            f"Template not found: {tpl}\n"
            f"Place {_TEMPLATE_NAME} in {_TEMPLATE_DIR}/"
        )

    # ── Copy template → output (byte-for-byte) ───────────────────
    out = Path(output_path)
    if out.suffix.lower() != ".xlsm":
        out = out.with_suffix(".xlsm")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(tpl), str(out))

    # ── Single openpyxl pass (suppress extension warnings) ────────
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = load_workbook(str(out), keep_vba=True)

    # Delete the legacy ARG sheet (not used in output)
    if "ARG" in wb.sheetnames:
        del wb["ARG"]

    # ── Load ALZ checklist for rich per-control fields ────────────
    checklist_lookup: dict[str, dict] = {}
    try:
        from alz.loader import load_alz_checklist
        cl = load_alz_checklist()
        for item in cl.get("items", []):
            guid = item.get("guid", "")
            if guid:
                checklist_lookup[guid] = item
    except Exception:
        pass

    results = run.get("results", [])

    # ── Write Checklist data rows (A–U) + enrichment (V–Y) ───────
    n_written = 0
    if _CHECKLIST_SHEET in wb.sheetnames:
        ws = wb[_CHECKLIST_SHEET]
        _clear_data_rows(ws)
        n_written = _write_checklist_rows(ws, results, checklist_lookup)

        # Enrich in the same open workbook (no second load/save)
        from reporting.enrich import enrich_open_worksheet
        e_stats = enrich_open_worksheet(ws)

        print(
            f"  ✓ Checklist: {n_written} rows "
            f"({e_stats.get('alz', 0)} ALZ, "
            f"{e_stats.get('derived', 0)} derived)"
        )
    else:
        print(f"  ⚠ Sheet '{_CHECKLIST_SHEET}' not found — skipping")

    # ── Save ──────────────────────────────────────────────────────
    try:
        wb.save(str(out))
    except PermissionError:
        ts = datetime.now().strftime("%H%M%S")
        fallback = out.with_name(f"{out.stem}_{ts}.xlsm")
        wb.save(str(fallback))
        print(f"  ⚠ Saved as {fallback.name} (original locked)")
        out = fallback

    # ── Restore x14 extensions stripped by openpyxl ───────────────
    patched = _restore_extensions(str(tpl), str(out))
    if patched:
        print(f"  ✓ Extensions restored for {patched} sheet(s)")

    print(f"  ✓ CSA workbook → {out}")
    return str(out)
