from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_MODELS = (
    "deepseek-v4.1-flash",
    "glm-5.3-flash",
    "gpt-4o-mini",
    "gpt-4.1-mini",
    "qwen-plus",
)
_MAX_PROFILES = 20
_MAX_MODELS = 20


def settings_path() -> Path:
    override = os.environ.get("SCIVERSE_AI_SETTINGS")
    if override:
        return Path(override)
    return Path.home() / ".sciverse" / "ai-settings.json"


def _load_payload() -> tuple[list[dict[str, str]], list[str]]:
    path = settings_path()
    if not path.exists():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    if not isinstance(payload, dict):
        return [], []

    profiles = payload.get("profiles")
    valid = []
    if isinstance(profiles, list):
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

    models = []
    raw_models = payload.get("models")
    if isinstance(raw_models, list):
        for value in raw_models:
            if isinstance(value, str) and value.strip():
                model = value.strip()
                if model not in models:
                    models.append(model)
    return valid, models[:_MAX_MODELS]


def _save_payload(profiles: list[dict[str, str]], models: list[str]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"profiles": profiles, "models": models[:_MAX_MODELS]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def list_ai_profiles() -> list[dict[str, str]]:
    profiles, _ = _load_payload()
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


def model_options(current: str = "") -> list[str]:
    _, recorded = _load_payload()
    options: list[str] = []
    for value in [*recorded, *DEFAULT_MODELS, current]:
        value = value.strip() if isinstance(value, str) else ""
        if value and value not in options:
            options.append(value)
    return options


def record_ai_model(model: str) -> None:
    model = model.strip()
    if not model or model in DEFAULT_MODELS:
        return
    profiles, models = _load_payload()
    if models and models[0] == model:
        return
    models = [item for item in models if item != model]
    models.insert(0, model)
    _save_payload(profiles, models)


def record_ai_profile(base_url: str, model: str) -> None:
    base_url = base_url.strip().rstrip("/")
    model = model.strip()
    if not base_url.startswith(("http://", "https://")):
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    profiles, models = _load_payload()
    profiles = [item for item in profiles if item["base_url"] != base_url]
    profiles.insert(0, {"base_url": base_url, "model": model, "last_used": now})
    if model:
        models = [item for item in models if item != model]
        models.insert(0, model)
    _save_payload(profiles[:_MAX_PROFILES], models)
