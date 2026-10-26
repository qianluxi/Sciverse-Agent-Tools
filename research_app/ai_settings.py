from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "glm-5.3-flash"
_MAX_PROFILES = 20


def settings_path() -> Path:
    override = os.environ.get("SCIVERSE_AI_SETTINGS")
    if override:
        return Path(override)
    return Path.home() / ".sciverse" / "ai-settings.json"


def _load() -> list[dict[str, str]]:
    path = settings_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        return []
    valid = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        base_url = profile.get("base_url")
        model = profile.get("model")
        if isinstance(base_url, str) and base_url.startswith(("http://", "https://")):
            valid.append(
                {
                    "base_url": base_url.rstrip("/"),
                    "model": model.strip() if isinstance(model, str) else "",
                    "last_used": str(profile.get("last_used") or ""),
                }
            )
    return valid


def list_ai_profiles() -> list[dict[str, str]]:
    profiles = _load()
    profiles.sort(key=lambda item: item.get("last_used", ""), reverse=True)
    return profiles


def default_profile() -> dict[str, str]:
    profiles = list_ai_profiles()
    if profiles:
        return profiles[0]
    return {
        "base_url": os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
        "model": os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
        "last_used": "",
    }


def record_ai_profile(base_url: str, model: str) -> None:
    base_url = base_url.strip().rstrip("/")
    model = model.strip()
    if not base_url.startswith(("http://", "https://")):
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    profiles = [item for item in _load() if item["base_url"] != base_url]
    profiles.insert(0, {"base_url": base_url, "model": model, "last_used": now})
    profiles = profiles[:_MAX_PROFILES]
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"profiles": profiles}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
