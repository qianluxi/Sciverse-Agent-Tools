from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_app.ai import AIError, OpenAICompatibleClient, parse_json_object


TRANSLATION_PROMPT_VERSION = "title-translation-v1"
ABSTRACT_TRANSLATION_PROMPT_VERSION = "abstract-translation-v1"
MAX_TITLES_PER_REQUEST = 20
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_MAX_CACHE_ENTRIES = 5000


def needs_translation(title: str) -> bool:
    text = title.strip()
    return bool(text) and not bool(_CJK_RE.search(text))


def cache_path() -> Path:
    override = os.environ.get("SCIVERSE_TRANSLATION_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".sciverse" / "title-translations.json"


def _cache_key(title: str, model: str) -> str:
    normalized = " ".join(title.split()).casefold()
    payload = f"{normalized}\0{model}\0{TRANSLATION_PROMPT_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache() -> dict[str, dict[str, str]]:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        key: value
        for key, value in entries.items()
        if isinstance(value, dict) and isinstance(value.get("translation"), str)
    }


def cached_translations(titles: list[str], model: str) -> dict[str, str]:
    cache = _load_cache()
    result = {}
    for title in titles:
        entry = cache.get(_cache_key(title, model))
        if entry:
            result[title] = entry["translation"]
    return result


def save_translations(translations: dict[str, str], model: str) -> None:
    if not translations:
        return
    cache = _load_cache()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for title, translation in translations.items():
        cache[_cache_key(title, model)] = {
            "source_title": title,
            "translation": translation,
            "model": model,
            "prompt_version": TRANSLATION_PROMPT_VERSION,
            "updated_at": now,
        }
    if len(cache) > _MAX_CACHE_ENTRIES:
        cache = dict(list(cache.items())[-_MAX_CACHE_ENTRIES:])
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"entries": cache}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _translation_messages(titles: list[str]) -> list[dict[str, str]]:
    numbered = "\n".join(f"{index}. {title}" for index, title in enumerate(titles, start=1))
    return [
        {
            "role": "system",
            "content": (
                "你是学术论文标题翻译助手。把英文或其他非中文标题翻译成简体中文。"
                "保留缩写、模型名、材料名和专业术语；不要解释，不要添加原文没有的信息。"
                "只输出 JSON：{\"translations\":[{\"index\":1,\"title\":\"中文标题\"}]}。"
            ),
        },
        {"role": "user", "content": numbered},
    ]


def _parse_translations(raw: str, count: int) -> dict[str, str]:
    payload = parse_json_object(raw)
    items = payload.get("translations")
    if isinstance(items, dict):
        pairs = items.items()
    elif isinstance(items, list):
        pairs = (
            (item.get("index"), item.get("title"))
            for item in items
            if isinstance(item, dict)
        )
    else:
        raise AIError("翻译响应缺少 translations。")
    result = {}
    for key, value in pairs:
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if 1 <= index <= count and isinstance(value, str) and value.strip():
            result[str(index)] = value.strip()
    return result


def translate_titles(
    client: OpenAICompatibleClient,
    titles: list[str],
) -> dict[str, str]:
    translations: dict[str, str] = {}
    for start in range(0, len(titles), MAX_TITLES_PER_REQUEST):
        batch = titles[start : start + MAX_TITLES_PER_REQUEST]
        raw = client.chat(
            _translation_messages(batch),
            max_tokens=4096,
        )
        parsed = _parse_translations(raw, len(batch))
        for index, title in enumerate(batch, start=1):
            translation = parsed.get(str(index))
            if translation:
                translations[title] = translation
    return translations


def abstract_cache_path() -> Path:
    override = os.environ.get("SCIVERSE_ABSTRACT_TRANSLATION_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".sciverse" / "abstract-translations.json"


def _abstract_cache_key(abstract: str, model: str) -> str:
    normalized = " ".join(abstract.split()).casefold()
    payload = (
        f"{normalized}\0{model}\0{ABSTRACT_TRANSLATION_PROMPT_VERSION}\0abstract"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_abstract_cache() -> dict[str, dict[str, str]]:
    path = abstract_cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        key: value
        for key, value in entries.items()
        if isinstance(value, dict) and isinstance(value.get("translation"), str)
    }


def cached_abstract_translations(
    abstracts: list[str],
    model: str,
) -> dict[str, str]:
    cache = _load_abstract_cache()
    result = {}
    for abstract in abstracts:
        entry = cache.get(_abstract_cache_key(abstract, model))
        if entry:
            result[abstract] = entry["translation"]
    return result


def save_abstract_translations(
    translations: dict[str, str],
    model: str,
) -> None:
    if not translations:
        return
    cache = _load_abstract_cache()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for abstract, translation in translations.items():
        cache[_abstract_cache_key(abstract, model)] = {
            "source_abstract": abstract,
            "translation": translation,
            "model": model,
            "prompt_version": ABSTRACT_TRANSLATION_PROMPT_VERSION,
            "updated_at": now,
        }
    if len(cache) > _MAX_CACHE_ENTRIES:
        cache = dict(list(cache.items())[-_MAX_CACHE_ENTRIES:])
    path = abstract_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"entries": cache}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _abstract_messages(abstract: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是学术摘要翻译助手。把英文摘要准确翻译成简体中文，"
                "保留数值、单位、专业术语和逻辑关系；不要总结、解释或补充原文没有的信息。"
                "只输出 JSON：{\"translation\":\"中文译文\"}。"
            ),
        },
        {"role": "user", "content": abstract},
    ]


def translate_abstracts(
    client: OpenAICompatibleClient,
    abstracts: list[str],
) -> dict[str, str]:
    translations = {}
    for abstract in abstracts:
        raw = client.chat(_abstract_messages(abstract), max_tokens=4096)
        payload = parse_json_object(raw)
        translation = payload.get("translation")
        if not isinstance(translation, str) or not translation.strip():
            raise AIError("摘要翻译响应缺少 translation。")
        translations[abstract] = translation.strip()
    return translations
