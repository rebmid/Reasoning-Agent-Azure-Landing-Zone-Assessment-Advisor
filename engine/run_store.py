import os, json, re
from datetime import datetime, timezone


def _slugify(name: str) -> str:
    """Convert a display name to a filesystem-safe folder name."""
    slug = re.sub(r"[^\w\s-]", "", name.strip())
    slug = re.sub(r"[\s]+", "_", slug)
    return slug[:64] or "unknown"


def save_run(out_root, tenant_id, payload, tenant_name: str = ""):
    slug = _slugify(tenant_name) if tenant_name else (tenant_id or "unknown")
    path = os.path.join(out_root, slug)
    os.makedirs(path, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    file = os.path.join(path, f"{ts}.json")

    with open(file, "w") as f:
        json.dump(payload, f, indent=2)

    return file

def get_last_run(out_root, tenant_id, tenant_name: str = ""):
    slug = _slugify(tenant_name) if tenant_name else (tenant_id or "unknown")
    path = os.path.join(out_root, slug)
    if not os.path.isdir(path):
        # Fall back to GUID folder for backward compat
        if tenant_id:
            path = os.path.join(out_root, tenant_id)
        if not os.path.isdir(path):
            return None

    files = sorted(os.listdir(path))
    if not files:
        return None

    return os.path.join(path, files[-1])
