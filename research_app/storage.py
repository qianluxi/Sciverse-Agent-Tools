from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


_SCHEMA_VERSION = 1
_MAX_HISTORY_ENTRIES = 50
_MAX_SAVED_SEARCHES = 100
_MAX_PARAMS_BYTES = 16 * 1024
_MAX_BACKUP_BYTES = 2 * 1024 * 1024
_SENSITIVE_KEYS = {"token", "api_token", "sciverse_api_token", "authorization"}
_BACKUP_FORMAT = "sciverse-research-workbench"


def database_path() -> Path:
    override = os.environ.get("SCIVERSE_RESEARCH_DB")
    if override:
        return Path(override)
    return Path.home() / ".sciverse" / "research-workbench.sqlite3"


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in _SENSITIVE_KEYS:
                raise ValueError("Search storage cannot contain credentials.")
            _reject_secrets(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_secrets(nested)


def _serialize_params(params: dict[str, Any]) -> str:
    if not isinstance(params, dict):
        raise ValueError("Search parameters must be an object.")
    _reject_secrets(params)
    serialized = json.dumps(params, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > _MAX_PARAMS_BYTES:
        raise ValueError("Search parameters exceed the 16 KiB storage limit.")
    return serialized


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > _SCHEMA_VERSION:
        raise RuntimeError(f"Research database version {version} is newer than supported.")
    if version == 0:
        connection.executescript(
            """
            CREATE TABLE search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL CHECK (mode IN ('semantic', 'structured')),
                params_json TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                searched_at TEXT NOT NULL
            );
            CREATE TABLE saved_searches (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('semantic', 'structured')),
                params_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX search_history_searched_at ON search_history(searched_at DESC);
            """
        )
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        _migrate(connection)
        if os.name != "nt":
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_search(mode: str, params: dict[str, Any], result_count: int) -> None:
    if mode not in {"semantic", "structured"}:
        raise ValueError("mode must be semantic or structured")
    serialized = _serialize_params(params)
    with _connection() as connection:
        connection.execute(
            "INSERT INTO search_history(mode, params_json, result_count, searched_at) VALUES (?, ?, ?, ?)",
            (mode, serialized, result_count, _timestamp()),
        )
        connection.execute(
            """DELETE FROM search_history WHERE id NOT IN
               (SELECT id FROM search_history ORDER BY id DESC LIMIT ?)""",
            (_MAX_HISTORY_ENTRIES,),
        )


def list_recent_searches(limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(limit, _MAX_HISTORY_ENTRIES))
    with _connection() as connection:
        rows = connection.execute(
            "SELECT id, mode, params_json, result_count, searched_at "
            "FROM search_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "mode": row["mode"],
            "params": json.loads(row["params_json"]),
            "result_count": row["result_count"],
            "searched_at": row["searched_at"],
        }
        for row in rows
    ]


def save_search(name: str, mode: str, params: dict[str, Any]) -> str:
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Saved search name must contain 1 to 100 characters.")
    if mode not in {"semantic", "structured"}:
        raise ValueError("mode must be semantic or structured")
    serialized = _serialize_params(params)
    search_id = uuid.uuid4().hex
    now = _timestamp()
    with _connection() as connection:
        saved_count = connection.execute("SELECT COUNT(*) FROM saved_searches").fetchone()[0]
        if saved_count >= _MAX_SAVED_SEARCHES:
            raise ValueError(f"最多保存 {_MAX_SAVED_SEARCHES} 条检索条件。")
        connection.execute(
            """INSERT INTO saved_searches(id, name, mode, params_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (search_id, name, mode, serialized, now, now),
        )
    return search_id


def list_saved_searches() -> list[dict[str, Any]]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT id, name, mode, params_json, created_at, updated_at "
            "FROM saved_searches ORDER BY updated_at DESC, name COLLATE NOCASE"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "mode": row["mode"],
            "params": json.loads(row["params_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def delete_saved_search(search_id: str) -> bool:
    with _connection() as connection:
        cursor = connection.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
    return cursor.rowcount > 0


def export_saved_searches_backup() -> bytes:
    payload = {
        "format": _BACKUP_FORMAT,
        "schema_version": _SCHEMA_VERSION,
        "saved_searches": list_saved_searches(),
    }
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(data) > _MAX_BACKUP_BYTES:
        raise ValueError("备份文件超过 2 MiB 限制；请先删除较大的已保存检索。")
    return data


def restore_saved_searches_backup(data: bytes) -> int:
    if len(data) > _MAX_BACKUP_BYTES:
        raise ValueError("备份文件超过 2 MiB 限制。")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("备份文件不是有效的 UTF-8 JSON。") from error
    if not isinstance(payload, dict) or payload.get("format") != _BACKUP_FORMAT:
        raise ValueError("备份文件格式不匹配。")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("备份文件版本不受支持。")
    _reject_secrets(payload)

    saved_searches = payload.get("saved_searches", [])
    if not isinstance(saved_searches, list) or len(saved_searches) > _MAX_SAVED_SEARCHES:
        raise ValueError("备份中的已保存检索数量无效。")

    validated_saved = []
    for item in saved_searches:
        if not isinstance(item, dict):
            raise ValueError("备份中的已保存检索记录无效。")
        name = item.get("name")
        mode = item.get("mode")
        params = item.get("params")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("备份中存在无效的检索名称。")
        if mode not in {"semantic", "structured"} or not isinstance(params, dict):
            raise ValueError("备份中存在无效的检索条件。")
        serialized = _serialize_params(params)
        now = _timestamp()
        search_id = item.get("id")
        validated_saved.append(
            (
                search_id if isinstance(search_id, str) and search_id else uuid.uuid4().hex,
                name.strip(),
                mode,
                serialized,
                item.get("created_at") if isinstance(item.get("created_at"), str) else now,
                item.get("updated_at") if isinstance(item.get("updated_at"), str) else now,
            )
        )

    saved_inserted = 0
    with _connection() as connection:
        for item in validated_saved:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO saved_searches
                   (id, name, mode, params_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                item,
            )
            saved_inserted += cursor.rowcount
    return saved_inserted
