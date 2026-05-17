import os
from pathlib import Path
from dotenv import load_dotenv
import yaml

# Load from ~/.autopilot/.env (portable) then local .env (dev override)
load_dotenv(Path.home() / ".autopilot" / ".env")
load_dotenv(override=False)

DB_PATH = Path(os.getenv("AP_DB_PATH", "~/.autopilot/costs.sqlite")).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_ROOT = Path(__file__).parent.parent.parent
_STYLE_PATH = Path.home() / ".autopilot" / "style.yaml"

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
    """Load global ~/.autopilot/style.yaml, deep-merge .ap-style.yaml from cwd on top."""
    global_style: dict = {}
    if _STYLE_PATH.exists():
        with open(_STYLE_PATH) as f:
            global_style = yaml.safe_load(f) or {}

    local_path = Path.cwd() / ".ap-style.yaml"
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
    """Return the user's subscription plan from AP_PLAN env (default: pro)."""
    return os.getenv("AP_PLAN", "pro").lower()


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
