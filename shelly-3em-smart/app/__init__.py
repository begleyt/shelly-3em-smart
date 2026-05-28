"""Shelly 3EM Smart Monitor add-on package."""
import re
from pathlib import Path


def _load_version() -> str:
    """Read the add-on version from config.yaml so /api/info and the
    dashboard's cache-bust query string never drift from what HA Supervisor
    sees. If anything goes wrong, return 'unknown' rather than crashing."""
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        text = cfg_path.read_text(encoding="utf-8")
        m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', text, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"


APP_VERSION = _load_version()
