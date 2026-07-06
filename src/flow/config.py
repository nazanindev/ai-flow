import os
import shutil
from pathlib import Path
from dotenv import load_dotenv
import yaml

# ── State directory + one-time migration ───────────────────────────────────
# State lives in ~/.flow (formerly ~/.autopilot). If only the legacy dir exists,
# move it once so existing run history / .env / style.yaml carry over untouched.
_LEGACY_DIR = Path.home() / ".autopilot"
STATE_DIR = Path.home() / ".flow"
if not STATE_DIR.exists() and _LEGACY_DIR.exists():
    try:
        shutil.move(str(_LEGACY_DIR), str(STATE_DIR))
    except Exception:
        STATE_DIR = _LEGACY_DIR  # migration failed — keep legacy so state is never lost
STATE_DIR.mkdir(parents=True, exist_ok=True)

# Load env from the state dir, then the legacy dir (if migration was skipped),
# then a local .env (dev override).
load_dotenv(STATE_DIR / ".env")
load_dotenv(_LEGACY_DIR / ".env", override=False)
load_dotenv(override=False)

# Back-compat: mirror any legacy AP_* env var onto its FLOW_* name (FLOW_ wins).
for _k, _v in list(os.environ.items()):
    if _k.startswith("AP_") and len(_k) > 3:
        os.environ.setdefault("FLOW_" + _k[3:], _v)

DB_PATH = Path(os.getenv("FLOW_DB_PATH", str(STATE_DIR / "costs.sqlite"))).expanduser()
# If an env override still points inside the legacy dir, remap into the new one
# (the file itself was moved with the directory).
try:
    if DB_PATH.parent == _LEGACY_DIR or _LEGACY_DIR in DB_PATH.parents:
        DB_PATH = STATE_DIR / DB_PATH.name
except Exception:
    pass
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_ROOT = Path(__file__).parent.parent.parent
_STYLE_PATH = STATE_DIR / "style.yaml"

# Fallback caps used when constraints.yaml is unavailable
_DEFAULT_PLAN_WINDOW_CAPS: dict = {
    "pro":      {"msgs": 45,   "tokens_per_window": 2_000_000},
    "max5":     {"msgs": 225,  "tokens_per_window": 10_000_000},
    "max20":    {"msgs": 900,  "tokens_per_window": 40_000_000},
    "api_only": {"msgs": 0,    "tokens_per_window": 0},
}


def _load_yaml(name: str) -> dict:
    p = _ROOT / name
    if p.exists():
        with open(p) as f:
            return yaml.safe_load(f) or {}
    return {}


def routing() -> dict:
    return _load_yaml("routing.yaml")


def constraints() -> dict:
    return _load_yaml("constraints.yaml")


def model_for_phase(phase: str) -> str:
    r = routing()
    return r.get("phases", {}).get(phase, "claude-sonnet-4-6")


def get_project_id() -> str:
    """Normalized project ID from git remote, falls back to directory name."""
    import subprocess
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        # github.com/user/repo-name(.git) -> repo-name
        normalized = url.rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        return normalized.split("/")[-1]
    except Exception:
        return Path.cwd().name


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base in-place; override wins on any shared key."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_style() -> dict:
    """Load global ~/.flow/style.yaml, deep-merge .flow-style.yaml from cwd on top."""
    global_style: dict = {}
    if _STYLE_PATH.exists():
        with open(_STYLE_PATH) as f:
            global_style = yaml.safe_load(f) or {}

    local_path = Path.cwd() / ".flow-style.yaml"
    if local_path.exists():
        with open(local_path) as f:
            local_style = yaml.safe_load(f) or {}
        _deep_merge(global_style, local_style)

    return global_style


def style_prompt(style: dict, sections: list) -> "str | None":
    """Serialize only the requested sections to a system-prompt string.

    Sections can be dotted (e.g. "agent.verbosity") to pick a single sub-key.
    Returns None if every requested section is null or absent — callers should
    skip passing a system prompt entirely in that case.
    """
    parts = []
    for section in sections:
        if "." in section:
            key, subkey = section.split(".", 1)
            val = style.get(key)
            if isinstance(val, dict):
                val = val.get(subkey)
            else:
                val = None
        else:
            val = style.get(section)

        if val is None:
            continue

        if isinstance(val, dict):
            serialized = yaml.dump(val, default_flow_style=False).strip()
        elif isinstance(val, list):
            serialized = yaml.dump(val, default_flow_style=False).strip()
        else:
            serialized = str(val)

        parts.append(f"[Style: {section}]\n{serialized}")

    return "\n\n".join(parts) if parts else None


def get_plan() -> str:
    """Return the user's subscription plan from FLOW_PLAN env (default: pro)."""
    return os.getenv("FLOW_PLAN", "pro").lower()


def get_plan_window_caps() -> dict:
    """Return per-plan message + token caps for 5-hour quota windows."""
    return constraints().get("plan_window_caps", _DEFAULT_PLAN_WINDOW_CAPS)


# Convenience alias populated at import time; use get_plan_window_caps() for
# live reads when constraints.yaml may have changed.
PLAN_WINDOW_CAPS = _DEFAULT_PLAN_WINDOW_CAPS


def get_branch() -> str:
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"
