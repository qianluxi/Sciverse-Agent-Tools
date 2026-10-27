from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from research_app.screening import (
    EXCLUSION_REASONS,
    candidate_key,
    duplicate_basis,
    identifier_keys,
    normalize_doi,
    normalize_title,
    paper_id,
    strong_identifier_keys,
)


_SCHEMA_VERSION = 6
_BACKUP_SCHEMA_VERSION = 2
_MAX_HISTORY_ENTRIES = 50
_MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
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
    if version < 1:
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
        version = 1
        connection.execute("PRAGMA user_version = 1")
    if version < 2:
        connection.executescript(
            """
            CREATE TABLE screening_projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                question TEXT NOT NULL,
                inclusion_criteria TEXT NOT NULL,
                exclusion_criteria TEXT NOT NULL,
                identified_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE screening_candidates (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                paper_id TEXT NOT NULL,
                title TEXT NOT NULL,
                year INTEGER,
                doi TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'included', 'excluded')),
                exclusion_reason TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, candidate_key)
            );
            CREATE INDEX screening_candidates_status
                ON screening_candidates(project_id, status, updated_at);
            CREATE TABLE screening_duplicates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                duplicate_of TEXT NOT NULL,
                basis TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX screening_duplicates_project
                ON screening_duplicates(project_id, id DESC);
            CREATE TABLE screening_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                old_reason TEXT,
                new_reason TEXT,
                old_notes TEXT,
                new_notes TEXT,
                changed_at TEXT NOT NULL
            );
            CREATE INDEX screening_audit_project
                ON screening_audit(project_id, id DESC);
            """
        )
        version = 2
        connection.execute("PRAGMA user_version = 2")
    if version < 3:
        connection.executescript(
            """
            CREATE TABLE screening_candidate_identifiers (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                identifier_key TEXT NOT NULL,
                candidate_key TEXT NOT NULL,
                PRIMARY KEY (project_id, identifier_key)
            );
            CREATE INDEX screening_candidate_identifiers_candidate
                ON screening_candidate_identifiers(project_id, candidate_key);
            """
        )
        for row in connection.execute(
            "SELECT project_id, candidate_key, paper_id, title, doi FROM screening_candidates"
        ).fetchall():
            paper: dict[str, Any] = {"doi": row["doi"], "title": row["title"]}
            key = str(row["candidate_key"] or "")
            if key.startswith("uid:"):
                paper["unique_id"] = key[4:]
            elif key.startswith("doc:"):
                paper["doc_id"] = key[4:]
            if str(row["paper_id"] or "").startswith("paper:"):
                paper.setdefault("unique_id", row["paper_id"])
            for identifier in identifier_keys(paper):
                connection.execute(
                    """INSERT OR IGNORE INTO screening_candidate_identifiers(
                           project_id, identifier_key, candidate_key
                       ) VALUES (?, ?, ?)""",
                    (row["project_id"], identifier, key),
                )
        version = 3
        connection.execute("PRAGMA user_version = 3")
    if version < 4:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                mode TEXT NOT NULL CHECK (mode IN ('semantic', 'structured')),
                params_json TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (project_id, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS search_runs_project
                ON search_runs(project_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS screening_candidate_sources (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
                discovered_at TEXT NOT NULL,
                PRIMARY KEY (project_id, candidate_key, run_id)
            );
            CREATE INDEX IF NOT EXISTS screening_candidate_sources_run
                ON screening_candidate_sources(run_id);
            CREATE TABLE IF NOT EXISTS ai_task_runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                input_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'completed',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ai_task_runs_project
                ON ai_task_runs(project_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS evidence_items (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL REFERENCES ai_task_runs(id) ON DELETE CASCADE,
                paper_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                raw_json TEXT NOT NULL,
                reviewed_json TEXT NOT NULL,
                final_json TEXT NOT NULL,
                quote_checks_json TEXT NOT NULL DEFAULT '[]',
                review_status INTEGER NOT NULL DEFAULT 0,
                reviewer_note TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, task_id, paper_id)
            );
            CREATE INDEX IF NOT EXISTS evidence_items_task
                ON evidence_items(project_id, task_id);
            """
        )
        version = 4
        connection.execute("PRAGMA user_version = 4")
    if version < 5:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(search_runs)").fetchall()
        }
        if "snapshot_count" not in columns:
            connection.execute(
                "ALTER TABLE search_runs ADD COLUMN snapshot_count INTEGER NOT NULL DEFAULT 0"
            )
        for row in connection.execute(
            "SELECT id, snapshot_json FROM search_runs"
        ).fetchall():
            try:
                snapshot_count = len(json.loads(row["snapshot_json"]))
            except (TypeError, json.JSONDecodeError):
                snapshot_count = 0
            connection.execute(
                "UPDATE search_runs SET snapshot_count = ? WHERE id = ?",
                (snapshot_count, row["id"]),
            )
        version = 5
        connection.execute("PRAGMA user_version = 5")
    if version < 6:
        project_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(screening_projects)").fetchall()
        }
        if "stats_tracked" not in project_columns:
            connection.execute(
                "ALTER TABLE screening_projects ADD COLUMN stats_tracked INTEGER NOT NULL DEFAULT 0"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_import_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                run_id TEXT,
                identified_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
                conflict_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS project_import_events_project
                ON project_import_events(project_id, id DESC);
            """
        )
        version = 6
        connection.execute("PRAGMA user_version = 6")


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if 0 < current_version < _SCHEMA_VERSION:
            _backup_database(path)
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


@contextmanager
def _readonly_connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def _canonical_strong_values(
    candidate: dict[str, Any],
    identifier_type: str,
) -> set[str]:
    key = str(candidate.get("candidate_key") or "")
    values: set[str] = set()
    if identifier_type == "doi":
        if key.startswith("doi:"):
            values.add(normalize_doi(key[4:]))
        doi = normalize_doi(candidate.get("doi"))
        if doi:
            values.add(doi)
    elif identifier_type == "uid":
        if key.startswith("uid:"):
            values.add(key[4:])
        paper_id_value = str(candidate.get("paper_id") or "")
        if paper_id_value.startswith("paper:"):
            values.add(paper_id_value)
    elif identifier_type == "doc":
        if key.startswith("doc:"):
            values.add(key[4:])
        paper_id_value = str(candidate.get("paper_id") or "")
        if paper_id_value.startswith("doc:"):
            values.add(paper_id_value)
    return values


def _mapping_value(identifier_key: str, identifier_type: str) -> str:
    value = identifier_key.split(":", 1)[1] if ":" in identifier_key else ""
    return normalize_doi(value) if identifier_type == "doi" else value


def inspect_identifier_integrity() -> dict[str, Any]:
    path = database_path()
    if not path.exists():
        return {
            "supported": True,
            "schema_version": None,
            "conflicts": [],
            "ambiguous": [],
            "orphans": [],
            "counts": {"conflicts": 0, "ambiguous": 0, "orphans": 0},
        }
    with _readonly_connection() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != _SCHEMA_VERSION:
            message = (
                f"数据库 Schema v{version} 不支持只读标识符检查；"
                "请先备份，再运行 `python -m research_app.maintenance upgrade`。"
            )
            return {
                "supported": False,
                "schema_version": version,
                "message": message,
                "conflicts": [],
                "ambiguous": [],
                "orphans": [],
                "counts": {"conflicts": 0, "ambiguous": 0, "orphans": 0},
            }
        try:
            candidates = {
                (row["project_id"], row["candidate_key"]): dict(row)
                for row in connection.execute(
                    """SELECT project_id, candidate_key, paper_id, title, doi
                       FROM screening_candidates"""
                )
            }
            mappings = [
                dict(row)
                for row in connection.execute(
                    """SELECT project_id, candidate_key, identifier_key
                       FROM screening_candidate_identifiers
                       ORDER BY project_id, candidate_key, identifier_key"""
                )
            ]
        except sqlite3.OperationalError:
            message = (
                "数据库缺少标识符检查所需表；请先备份，"
                "再运行 `python -m research_app.maintenance upgrade`。"
            )
            return {
                "supported": False,
                "schema_version": version,
                "message": message,
                "conflicts": [],
                "ambiguous": [],
                "orphans": [],
                "counts": {"conflicts": 0, "ambiguous": 0, "orphans": 0},
            }

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    orphans = []
    for mapping in mappings:
        identity = (mapping["project_id"], mapping["candidate_key"])
        if identity not in candidates:
            orphans.append(mapping)
            continue
        grouped.setdefault(identity, []).append(mapping)

    conflicts = []
    ambiguous = []
    for (project_id, candidate_key), items in grouped.items():
        candidate = candidates[(project_id, candidate_key)]
        by_type: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            identifier_type = item["identifier_key"].split(":", 1)[0]
            if identifier_type in {"doi", "uid", "doc"}:
                by_type.setdefault(identifier_type, []).append(item)
        for identifier_type, type_items in by_type.items():
            canonical = _canonical_strong_values(candidate, identifier_type)
            values = {
                _mapping_value(item["identifier_key"], identifier_type): item
                for item in type_items
            }
            if len(canonical) == 1:
                canonical_value = next(iter(canonical))
                aliases = {
                    value: item
                    for value, item in values.items()
                    if value != canonical_value
                }
                if aliases:
                    conflicts.append(
                        {
                            "project_id": project_id,
                            "candidate_key": candidate_key,
                            "type": identifier_type,
                            "canonical": canonical_value,
                            "aliases": sorted(aliases),
                            "identifier_keys": sorted(
                                item["identifier_key"] for item in aliases.values()
                            ),
                        }
                    )
            elif len(canonical) > 1:
                ambiguous.append(
                    {
                        "project_id": project_id,
                        "candidate_key": candidate_key,
                        "type": identifier_type,
                        "reason": "候选自身存在多个不同的规范标识",
                        "values": sorted(values),
                    }
                )
            elif values:
                ambiguous.append(
                    {
                        "project_id": project_id,
                        "candidate_key": candidate_key,
                        "type": identifier_type,
                        "reason": "候选缺少该类型规范标识，无法判断映射归属",
                        "values": sorted(values),
                    }
                )

    return {
        "supported": True,
        "schema_version": version,
        "conflicts": conflicts,
        "ambiguous": ambiguous,
        "orphans": orphans,
        "counts": {
            "conflicts": len(conflicts),
            "ambiguous": len(ambiguous),
            "orphans": len(orphans),
        },
    }


def _backup_database(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{timestamp}")
    suffix = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{timestamp}-{suffix}")
        suffix += 1
    source = sqlite3.connect(path)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    if os.name != "nt":
        try:
            backup.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as error:
            try:
                backup.unlink()
            except OSError:
                pass
            raise RuntimeError(
                "备份文件权限设置失败，已中止修复；请检查文件系统权限。"
            ) from error
    return backup


def upgrade_database() -> dict[str, Any]:
    path = database_path()
    if not path.exists():
        raise FileNotFoundError("研究数据库不存在，无需升级。")
    with _readonly_connection() as connection:
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version == _SCHEMA_VERSION:
        return {
            "upgraded": False,
            "from_version": current_version,
            "to_version": current_version,
            "backup_path": None,
        }
    if current_version > _SCHEMA_VERSION:
        raise RuntimeError("数据库版本高于当前应用支持范围，不能自动升级。")
    backup_path = _backup_database(path)
    with _connection() as connection:
        new_version = connection.execute("PRAGMA user_version").fetchone()[0]
    return {
        "upgraded": True,
        "from_version": current_version,
        "to_version": new_version,
        "backup_path": str(backup_path),
    }


def repair_identifier_integrity(*, backup: bool = True) -> dict[str, Any]:
    report = inspect_identifier_integrity()
    if not report.get("supported", True):
        raise ValueError(report.get("message") or "当前数据库不支持标识符修复。")
    removed_aliases = 0
    removed_orphans = 0
    backup_path = None
    if not report["conflicts"] and not report["orphans"]:
        return {
            **report,
            "backup_path": None,
            "removed_aliases": 0,
            "removed_orphans": 0,
        }
    path = database_path()
    if backup and path.exists():
        backup_path = _backup_database(path)
    with _connection() as connection:
        for conflict in report["conflicts"]:
            for identifier_key in conflict["identifier_keys"]:
                cursor = connection.execute(
                    """DELETE FROM screening_candidate_identifiers
                       WHERE project_id = ? AND candidate_key = ? AND identifier_key = ?""",
                    (
                        conflict["project_id"],
                        conflict["candidate_key"],
                        identifier_key,
                    ),
                )
                removed_aliases += cursor.rowcount
        for orphan in report["orphans"]:
            cursor = connection.execute(
                """DELETE FROM screening_candidate_identifiers
                   WHERE project_id = ? AND candidate_key = ? AND identifier_key = ?""",
                (
                    orphan["project_id"],
                    orphan["candidate_key"],
                    orphan["identifier_key"],
                ),
            )
            removed_orphans += cursor.rowcount
    return {
        **report,
        "backup_path": str(backup_path) if backup_path else None,
        "removed_aliases": removed_aliases,
        "removed_orphans": removed_orphans,
    }


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
        "schema_version": _BACKUP_SCHEMA_VERSION,
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
    if payload.get("schema_version") not in {1, _BACKUP_SCHEMA_VERSION}:
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


def _screening_project(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM screening_projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise ValueError("综述项目不存在。")
    return row


def create_screening_project(
    name: str,
    question: str,
    inclusion_criteria: str,
    exclusion_criteria: str,
) -> str:
    name = name.strip()
    question = question.strip()
    inclusion_criteria = inclusion_criteria.strip()
    exclusion_criteria = exclusion_criteria.strip()
    if not name or len(name) > 100:
        raise ValueError("项目名称需为 1 至 100 个字符。")
    if not question or len(question) > 500:
        raise ValueError("研究问题需为 1 至 500 个字符。")
    if len(inclusion_criteria) > 2000 or len(exclusion_criteria) > 2000:
        raise ValueError("纳入/排除标准不能超过 2000 个字符。")
    project_id = uuid.uuid4().hex
    now = _timestamp()
    with _connection() as connection:
        connection.execute(
            """INSERT INTO screening_projects(
                   id, name, question, inclusion_criteria, exclusion_criteria,
                   identified_count, duplicate_count, stats_tracked,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 0, 0, 1, ?, ?)""",
            (project_id, name, question, inclusion_criteria, exclusion_criteria, now, now),
        )
    return project_id


def list_screening_projects() -> list[dict[str, Any]]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM screening_projects ORDER BY updated_at DESC, name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def get_screening_project(project_id: str) -> dict[str, Any]:
    with _connection() as connection:
        return dict(_screening_project(connection, project_id))


def delete_screening_project(project_id: str) -> bool:
    with _connection() as connection:
        cursor = connection.execute(
            "DELETE FROM screening_projects WHERE id = ?",
            (project_id,),
        )
    return cursor.rowcount > 0


def _identifier_values(identifiers: list[str]) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    for identifier in identifiers:
        prefix, _, value = identifier.partition(":")
        if value:
            values.setdefault(prefix, set()).add(value)
    return values


def _strong_conflicts(
    existing: dict[str, set[str]],
    incoming: dict[str, set[str]],
) -> list[tuple[str, list[str], list[str]]]:
    conflicts = []
    for identifier_type in ("doi", "uid", "doc"):
        old = existing.get(identifier_type)
        new = incoming.get(identifier_type)
        if old and new and old.isdisjoint(new):
            conflicts.append((identifier_type, sorted(old), sorted(new)))
    return conflicts


def _find_duplicate(
    connection: sqlite3.Connection,
    project_id: str,
    paper: dict[str, Any],
) -> dict[str, Any] | None:
    strong = strong_identifier_keys(paper)
    strong_matches: dict[str, list[str]] = {}
    for identifier in strong:
        row = connection.execute(
            """SELECT candidate_key FROM screening_candidate_identifiers
               WHERE project_id = ? AND identifier_key = ?""",
            (project_id, identifier),
        ).fetchone()
        if row:
            strong_matches.setdefault(str(row["candidate_key"]), []).append(identifier)
    if len(strong_matches) > 1:
        return {"kind": "conflict", "matches": strong_matches, "conflicts": []}
    if strong_matches:
        candidate_key, identifiers = next(iter(strong_matches.items()))
        existing_keys = [
            str(row["identifier_key"])
            for row in connection.execute(
                """SELECT identifier_key FROM screening_candidate_identifiers
                   WHERE project_id = ? AND candidate_key = ?""",
                (project_id, candidate_key),
            )
        ]
        conflicts = _strong_conflicts(
            _identifier_values(existing_keys),
            _identifier_values(strong),
        )
        if conflicts:
            return {
                "kind": "conflict",
                "matches": strong_matches,
                "conflicts": conflicts,
            }
        return {
            "kind": "duplicate",
            "candidate_key": candidate_key,
            "basis": duplicate_basis(identifiers[0]),
            "matches": strong_matches,
        }

    title = normalize_title(paper.get("title"))
    title_identifier = f"title:{title}" if title else ""
    if not title_identifier:
        return None
    row = connection.execute(
        """SELECT candidate_key FROM screening_candidate_identifiers
           WHERE project_id = ? AND identifier_key = ?""",
        (project_id, title_identifier),
    ).fetchone()
    if not row:
        return None
    existing_has_strong = connection.execute(
        """SELECT 1 FROM screening_candidate_identifiers
           WHERE project_id = ? AND candidate_key = ?
             AND (identifier_key LIKE 'doi:%'
                  OR identifier_key LIKE 'uid:%'
                  OR identifier_key LIKE 'doc:%')
           LIMIT 1""",
        (project_id, row["candidate_key"]),
    ).fetchone()
    if strong and existing_has_strong:
        return None
    return {
        "kind": "duplicate",
        "candidate_key": str(row["candidate_key"]),
        "basis": duplicate_basis(title_identifier),
        "matches": {str(row["candidate_key"]): [title_identifier]},
    }


def _conflict_key(
    matches: dict[str, list[str]],
    conflicts: list[tuple[str, list[str], list[str]]] | None = None,
) -> str:
    raw_parts = [
        f"{identifier}->{candidate}"
        for candidate, identifiers in sorted(matches.items())
        for identifier in sorted(identifiers)
    ]
    for identifier_type, existing, incoming in conflicts or []:
        raw_parts.append(
            f"{identifier_type}:{','.join(existing)}!={','.join(incoming)}"
        )
    return "conflict:" + hashlib.sha1("|".join(raw_parts).encode("utf-8")).hexdigest()[:16]


def _conflict_detail(
    matches: dict[str, list[str]],
    conflicts: list[tuple[str, list[str], list[str]]] | None = None,
) -> str:
    parts = [
        f"{identifier}→{candidate}"
        for candidate, identifiers in sorted(matches.items())
        for identifier in sorted(identifiers)
    ]
    for identifier_type, existing, incoming in conflicts or []:
        parts.append(
            f"{identifier_type} 已登记 {', '.join(existing)}，新记录 {', '.join(incoming)}"
        )
    return "强标识符冲突，需人工处理：" + "；".join(parts)


def _merge_candidate_metadata(
    connection: sqlite3.Connection,
    project_id: str,
    candidate_key_value: str,
    paper: dict[str, Any],
) -> None:
    row = connection.execute(
        """SELECT title, year, doi, paper_id FROM screening_candidates
           WHERE project_id = ? AND candidate_key = ?""",
        (project_id, candidate_key_value),
    ).fetchone()
    if row is None:
        return
    updates: dict[str, Any] = {}
    title = str(paper.get("title") or "").strip()
    if (not row["title"] or row["title"] == "（暂无标题）") and title:
        updates["title"] = title[:1000]
    year = paper.get("publication_published_year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    if row["year"] is None and year is not None:
        updates["year"] = year
    doi = str(paper.get("doi") or "").strip()
    if not row["doi"] and doi:
        updates["doi"] = doi
    incoming_paper_id = paper_id(paper, candidate_key_value)
    existing_paper_id = str(row["paper_id"] or "")
    missing_stable_id = (
        not existing_paper_id
        or existing_paper_id == candidate_key_value
        or existing_paper_id.startswith(("title:", "unknown:"))
    )
    upgrades_to_paper_id = (
        not existing_paper_id.startswith("paper:") and incoming_paper_id.startswith("paper:")
    )
    if incoming_paper_id and (
        missing_stable_id or upgrades_to_paper_id
    ) and incoming_paper_id != existing_paper_id:
        updates["paper_id"] = incoming_paper_id
    if updates:
        assignments = ", ".join(f"{column} = ?" for column in updates)
        connection.execute(
            f"""UPDATE screening_candidates SET {assignments}
                WHERE project_id = ? AND candidate_key = ?""",
            (*updates.values(), project_id, candidate_key_value),
        )


def add_screening_candidates(
    project_id: str,
    papers: list[dict[str, Any]],
    *,
    source_run_id: str | None = None,
    source_run_ids: list[str | None] | None = None,
) -> dict[str, Any]:
    if len(papers) > 500:
        raise ValueError("单次最多加入 500 条候选文献。")
    source_tracking_requested = source_run_ids is not None or source_run_id is not None
    if source_run_ids is not None:
        if len(source_run_ids) != len(papers):
            raise ValueError("检索来源数量与文献数量不一致。")
        run_ids = list(source_run_ids)
    else:
        run_ids = [source_run_id] * len(papers)
    now = _timestamp()
    added = 0
    duplicates = 0
    conflicts = 0
    skipped = 0
    manual_source_created = False
    with _connection() as connection:
        _screening_project(connection, project_id)
        invalid_sources = []
        for index, run_id in enumerate(run_ids):
            if not run_id:
                if source_tracking_requested:
                    invalid_sources.append(index)
                continue
            run = connection.execute(
                "SELECT 1 FROM search_runs WHERE id = ? AND project_id = ?",
                (run_id, project_id),
            ).fetchone()
            if run is None:
                invalid_sources.append(index)
        if invalid_sources:
            manual_papers = [papers[index] for index in invalid_sources]
            manual = _insert_search_run(
                connection,
                project_id,
                "structured",
                {"source": "manual_selection"},
                manual_papers,
                len(manual_papers),
            )
            for index in invalid_sources:
                run_ids[index] = manual["id"]
            manual_source_created = True

        pre_existing: list[bool] = []
        for index, paper in enumerate(papers):
            run_id = run_ids[index]
            if not run_id:
                pre_existing.append(False)
                continue
            decision = _find_duplicate(connection, project_id, paper)
            if decision is None:
                pre_existing.append(False)
                continue
            canonical = (
                _conflict_key(decision["matches"], decision.get("conflicts"))
                if decision["kind"] == "conflict"
                else decision["candidate_key"]
            )
            row = connection.execute(
                """SELECT 1 FROM screening_candidate_sources
                   WHERE project_id = ? AND candidate_key = ? AND run_id = ?""",
                (project_id, canonical, run_id),
            ).fetchone()
            pre_existing.append(row is not None)

        for index, paper in enumerate(papers):
            if pre_existing[index]:
                skipped += 1
                continue
            key = candidate_key(paper, index)
            title = str(paper.get("title") or "（暂无标题）")[:1000]
            year = paper.get("publication_published_year")
            try:
                year = int(year) if year is not None else None
            except (TypeError, ValueError):
                year = None
            doi = str(paper.get("doi") or "") or None
            decision = _find_duplicate(connection, project_id, paper)
            canonical_key = key
            if decision and decision["kind"] == "conflict":
                conflicts += 1
                canonical_key = _conflict_key(
                    decision["matches"],
                    decision.get("conflicts"),
                )
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO screening_candidates(
                           project_id, candidate_key, paper_id, title, year, doi,
                           status, notes, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)""",
                    (
                        project_id,
                        canonical_key,
                        paper_id(paper, canonical_key),
                        title,
                        year,
                        doi,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount:
                    added += 1
                connection.execute(
                    """INSERT INTO screening_duplicates(
                           project_id, candidate_key, duplicate_of, basis, title, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        canonical_key,
                        " | ".join(sorted(decision["matches"])),
                        _conflict_detail(decision["matches"], decision.get("conflicts")),
                        title,
                        now,
                    ),
                )
                if run_ids[index]:
                    connection.execute(
                        """INSERT OR IGNORE INTO screening_candidate_sources(
                               project_id, candidate_key, run_id, discovered_at
                           ) VALUES (?, ?, ?, ?)""",
                        (project_id, canonical_key, run_ids[index], now),
                    )
                continue
            if decision and decision["kind"] == "duplicate":
                canonical_key = decision["candidate_key"]
                duplicates += 1
                _merge_candidate_metadata(connection, project_id, canonical_key, paper)
                connection.execute(
                    """INSERT INTO screening_duplicates(
                           project_id, candidate_key, duplicate_of, basis, title, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (project_id, key, canonical_key, decision["basis"], title, now),
                )
            else:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO screening_candidates(
                           project_id, candidate_key, paper_id, title, year, doi,
                           status, notes, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)""",
                    (project_id, key, paper_id(paper, key), title, year, doi, now, now),
                )
                if cursor.rowcount:
                    added += 1
                else:
                    duplicates += 1
                    _merge_candidate_metadata(connection, project_id, key, paper)
                    connection.execute(
                        """INSERT INTO screening_duplicates(
                               project_id, candidate_key, duplicate_of, basis, title, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (project_id, key, key, duplicate_basis(key), title, now),
                    )
            if run_ids[index]:
                connection.execute(
                    """INSERT OR IGNORE INTO screening_candidate_sources(
                           project_id, candidate_key, run_id, discovered_at
                       ) VALUES (?, ?, ?, ?)""",
                    (project_id, canonical_key, run_ids[index], now),
                )
            for identifier in identifier_keys(paper):
                connection.execute(
                    """INSERT OR IGNORE INTO screening_candidate_identifiers(
                           project_id, identifier_key, candidate_key
                       ) VALUES (?, ?, ?)""",
                    (project_id, identifier, canonical_key),
                )
        identified = len(papers) - skipped
        if identified or duplicates:
            connection.execute(
                """UPDATE screening_projects
                   SET identified_count = identified_count + ?,
                       duplicate_count = duplicate_count + ?,
                       updated_at = ?
                   WHERE id = ?""",
                (identified, duplicates, now, project_id),
            )
            ledger_run_id = (
                run_ids[0]
                if run_ids and all(run_id == run_ids[0] for run_id in run_ids)
                else None
            )
            connection.execute(
                """INSERT INTO project_import_events(
                       project_id, run_id, identified_count, duplicate_count,
                       conflict_count, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    ledger_run_id,
                    identified,
                    duplicates,
                    conflicts,
                    now,
                ),
            )
    result = {
        "identified": identified,
        "added": added,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }
    if manual_source_created:
        result["manual_source_created"] = True
    return result


def list_screening_candidates(
    project_id: str,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    if status is not None and status not in {"pending", "included", "excluded"}:
        raise ValueError("筛选状态无效。")
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    where = "project_id = ?"
    params: list[Any] = [project_id]
    if status:
        where += " AND status = ?"
        params.append(status)
    with _connection() as connection:
        _screening_project(connection, project_id)
        total = connection.execute(
            f"SELECT COUNT(*) FROM screening_candidates WHERE {where}", params
        ).fetchone()[0]
        rows = connection.execute(
            f"""SELECT * FROM screening_candidates WHERE {where}
                ORDER BY updated_at, title LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return [dict(row) for row in rows], total


def list_all_screening_candidates(project_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            "SELECT * FROM screening_candidates WHERE project_id = ? ORDER BY title",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_screening_decision(
    project_id: str,
    candidate_key_value: str,
    status: str,
    *,
    reason: str | None = None,
    notes: str = "",
) -> bool:
    if status not in {"pending", "included", "excluded"}:
        raise ValueError("筛选状态无效。")
    if candidate_key_value.startswith("conflict:"):
        raise ValueError("身份冲突尚未解决，不能保存筛选决定。")
    if status == "excluded":
        if reason not in EXCLUSION_REASONS:
            raise ValueError("排除时必须选择标准排除原因。")
    else:
        reason = None
    notes = str(notes or "").strip()
    if len(notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    now = _timestamp()
    with _connection() as connection:
        _screening_project(connection, project_id)
        row = connection.execute(
            """SELECT status, exclusion_reason, notes FROM screening_candidates
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
        if row is None:
            raise ValueError("候选文献不存在。")
        if (
            row["status"] == status
            and (row["exclusion_reason"] or None) == reason
            and row["notes"] == notes
        ):
            return False
        connection.execute(
            """UPDATE screening_candidates
               SET status = ?, exclusion_reason = ?, notes = ?, updated_at = ?
               WHERE project_id = ? AND candidate_key = ?""",
            (status, reason, notes, now, project_id, candidate_key_value),
        )
        connection.execute(
            """INSERT INTO screening_audit(
                   project_id, candidate_key, old_status, new_status,
                   old_reason, new_reason, old_notes, new_notes, changed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                candidate_key_value,
                row["status"],
                status,
                row["exclusion_reason"],
                reason,
                row["notes"],
                notes,
                now,
            ),
        )
        connection.execute(
            "UPDATE screening_projects SET updated_at = ? WHERE id = ?",
            (now, project_id),
        )
    return True


def screening_stats(project_id: str) -> dict[str, int]:
    with _connection() as connection:
        project = _screening_project(connection, project_id)
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                """SELECT status, COUNT(*) AS count FROM screening_candidates
                   WHERE project_id = ? GROUP BY status""",
                (project_id,),
            ).fetchall()
        }
    pending = counts.get("pending", 0)
    included = counts.get("included", 0)
    excluded = counts.get("excluded", 0)
    return {
        "identified": project["identified_count"],
        "duplicates": project["duplicate_count"],
        "unique": pending + included + excluded,
        "pending": pending,
        "included": included,
        "excluded": excluded,
        "screened": included + excluded,
    }


def list_screening_duplicates(project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM screening_duplicates WHERE project_id = ?
               ORDER BY id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def list_screening_audit(project_id: str, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM screening_audit WHERE project_id = ?
               ORDER BY id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _search_fingerprint(
    mode: str,
    params: dict[str, Any],
    papers: list[dict[str, Any]],
    total_count: int,
) -> str:
    identities = []
    for paper in papers:
        identities.append(
            [
                str(paper.get("unique_id") or ""),
                str(paper.get("doc_id") or ""),
                normalize_doi(paper.get("doi")),
                normalize_title(paper.get("title")),
            ]
        )
    payload = {
        "mode": mode,
        "params": params,
        "papers": sorted(identities),
        "total_count": int(total_count),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _insert_search_run(
    connection: sqlite3.Connection,
    project_id: str,
    mode: str,
    params: dict[str, Any],
    papers: list[dict[str, Any]],
    total_count: int,
) -> dict[str, Any]:
    if mode not in {"semantic", "structured"}:
        raise ValueError("检索方式无效。")
    serialized_params = _serialize_params(params)
    snapshot = json.dumps(papers, ensure_ascii=False, sort_keys=True)
    if len(snapshot.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("检索结果快照超过 5 MiB 限制。")
    fingerprint = _search_fingerprint(mode, params, papers, total_count)
    now = _timestamp()
    run_id = uuid.uuid4().hex
    cursor = connection.execute(
        """INSERT OR IGNORE INTO search_runs(
               id, project_id, mode, params_json, result_count,
               fingerprint, snapshot_json, snapshot_count, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            project_id,
            mode,
            serialized_params,
            int(total_count),
            fingerprint,
            snapshot,
            len(papers),
            now,
        ),
    )
    if cursor.rowcount:
        return {"id": run_id, "created": True}
    row = connection.execute(
        """SELECT id FROM search_runs
           WHERE project_id = ? AND fingerprint = ?""",
        (project_id, fingerprint),
    ).fetchone()
    return {"id": str(row["id"]), "created": False}


def save_search_run(
    project_id: str,
    mode: str,
    params: dict[str, Any],
    papers: list[dict[str, Any]],
    total_count: int,
) -> dict[str, Any]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        return _insert_search_run(connection, project_id, mode, params, papers, total_count)


def list_search_runs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT id, mode, params_json, result_count, snapshot_count, created_at
               FROM search_runs WHERE project_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "mode": row["mode"],
            "params": json.loads(row["params_json"]),
            "result_count": row["result_count"],
            "snapshot_count": row["snapshot_count"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_search_run(project_id: str, run_id: str) -> dict[str, Any] | None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        row = connection.execute(
            """SELECT * FROM search_runs
               WHERE project_id = ? AND id = ?""",
            (project_id, run_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "mode": row["mode"],
        "params": json.loads(row["params_json"]),
        "papers": json.loads(row["snapshot_json"]),
        "result_count": row["result_count"],
        "snapshot_count": row["snapshot_count"],
        "created_at": row["created_at"],
    }


def list_candidate_sources(project_id: str, candidate_key_value: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT r.id, r.mode, r.params_json, r.result_count,
                      r.snapshot_count, r.created_at, s.discovered_at
               FROM screening_candidate_sources s
               JOIN search_runs r ON r.id = s.run_id
               WHERE s.project_id = ? AND s.candidate_key = ?
               ORDER BY s.discovered_at DESC, r.created_at DESC""",
            (project_id, candidate_key_value),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "mode": row["mode"],
            "params": json.loads(row["params_json"]),
            "result_count": row["result_count"],
            "snapshot_count": row["snapshot_count"],
            "created_at": row["created_at"],
            "discovered_at": row["discovered_at"],
        }
        for row in rows
    ]


def project_manifest(project_id: str) -> bytes:
    overview = project_overview(project_id)
    runs = list_search_runs(project_id, limit=100)
    tasks = list_ai_tasks(project_id, limit=100)
    with _connection() as connection:
        _screening_project(connection, project_id)
        candidate_rows = connection.execute(
            """SELECT candidate_key, paper_id, title, year, doi, status,
                      exclusion_reason, notes, updated_at
               FROM screening_candidates WHERE project_id = ?
               ORDER BY title, candidate_key""",
            (project_id,),
        ).fetchall()
        source_rows = connection.execute(
            """SELECT candidate_key, run_id, discovered_at
               FROM screening_candidate_sources WHERE project_id = ?""",
            (project_id,),
        ).fetchall()
    sources_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        sources_by_candidate.setdefault(row["candidate_key"], []).append(
            {"run_id": row["run_id"], "discovered_at": row["discovered_at"]}
        )
    candidates = [
        {
            **dict(row),
            "sources": sources_by_candidate.get(row["candidate_key"], []),
        }
        for row in candidate_rows
    ]
    manifest = {
        "format": "sciverse-project-manifest",
        "schema_version": _SCHEMA_VERSION,
        "generated_at": _timestamp(),
        "project": overview["project"],
        "summary": {
            "identified": overview["identified"],
            "duplicates": overview["duplicates"],
            "unique": overview["unique"],
            "pending": overview["pending"],
            "included": overview["included"],
            "excluded": overview["excluded"],
            "conflicts": overview["conflicts"],
            "search_runs": overview["search_runs"],
            "evidence_items": overview["evidence_items"],
            "ai_tasks": overview["ai_tasks"],
        },
        "search_runs": runs,
        "ai_tasks": tasks,
        "candidates": candidates,
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def list_ai_tasks(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT id, kind, model, prompt_version, status, error, created_at, updated_at
               FROM ai_task_runs WHERE project_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def create_ai_task(
    project_id: str,
    *,
    kind: str,
    model: str,
    prompt_version: str,
    input_hash: str,
    status: str = "running",
) -> str:
    task_id = uuid.uuid4().hex
    now = _timestamp()
    with _connection() as connection:
        _screening_project(connection, project_id)
        connection.execute(
            """INSERT INTO ai_task_runs(
                   id, project_id, kind, model, prompt_version, input_hash,
                   status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                project_id,
                kind,
                model,
                prompt_version,
                input_hash,
                status,
                now,
                now,
            ),
        )
    return task_id


def finish_ai_task(task_id: str, *, status: str = "completed", error: str | None = None) -> None:
    with _connection() as connection:
        connection.execute(
            """UPDATE ai_task_runs
               SET status = ?, error = ?, updated_at = ?
               WHERE id = ?""",
            (status, error, _timestamp(), task_id),
        )


def save_evidence_items(
    project_id: str,
    task_id: str,
    items: list[dict[str, Any]],
) -> int:
    now = _timestamp()
    saved = 0
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            "SELECT 1 FROM ai_task_runs WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise ValueError("AI 任务不存在。")
        for item in items:
            paper_id_value = str(item.get("paper_id") or "")
            if not paper_id_value:
                continue
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else data
            reviewed = bool(item.get("reviewed"))
            existing = connection.execute(
                """SELECT raw_json, final_json FROM evidence_items
                   WHERE project_id = ? AND task_id = ? AND paper_id = ?""",
                (project_id, task_id, paper_id_value),
            ).fetchone()
            if existing:
                raw = json.loads(existing["raw_json"])
                final = data if reviewed else json.loads(existing["final_json"])
            else:
                final = data if reviewed else {}
            year = item.get("year")
            try:
                year = int(year) if year is not None else None
            except (TypeError, ValueError):
                year = None
            connection.execute(
                """INSERT INTO evidence_items(
                       project_id, task_id, paper_id, title, year,
                       raw_json, reviewed_json, final_json, quote_checks_json,
                       review_status, reviewer_note, model, prompt_version,
                       error, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, task_id, paper_id) DO UPDATE SET
                       title = excluded.title,
                       year = excluded.year,
                       reviewed_json = excluded.reviewed_json,
                       final_json = excluded.final_json,
                       quote_checks_json = excluded.quote_checks_json,
                       review_status = excluded.review_status,
                       reviewer_note = excluded.reviewer_note,
                       model = excluded.model,
                       prompt_version = excluded.prompt_version,
                       error = excluded.error,
                       updated_at = excluded.updated_at""",
                (
                    project_id,
                    task_id,
                    paper_id_value,
                    str(item.get("title") or ""),
                    year,
                    json.dumps(raw, ensure_ascii=False, sort_keys=True),
                    json.dumps(data, ensure_ascii=False, sort_keys=True),
                    json.dumps(final, ensure_ascii=False, sort_keys=True),
                    json.dumps(item.get("quote_checks") or [], ensure_ascii=False),
                    1 if reviewed else 0,
                    str(item.get("reviewer_note") or ""),
                    str(item.get("model") or ""),
                    str(item.get("prompt_version") or ""),
                    str(item.get("error") or "") or None,
                    now,
                    now,
                ),
            )
            saved += 1
    return saved


def load_latest_evidence(project_id: str) -> dict[str, Any] | None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            """SELECT * FROM ai_task_runs
               WHERE project_id = ? AND kind = 'evidence'
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        if task is None:
            return None
        rows = connection.execute(
            """SELECT * FROM evidence_items
               WHERE project_id = ? AND task_id = ?
               ORDER BY title, paper_id""",
            (project_id, task["id"]),
        ).fetchall()
    items = []
    for row in rows:
        items.append(
            {
                "paper_id": row["paper_id"],
                "title": row["title"],
                "year": row["year"],
                "data": json.loads(
                    row["final_json"] if row["review_status"] else row["reviewed_json"]
                ),
                "raw_data": json.loads(row["raw_json"]),
                "quote_checks": json.loads(row["quote_checks_json"]),
                "reviewed": bool(row["review_status"]),
                "reviewer_note": row["reviewer_note"],
                "model": row["model"],
                "prompt_version": row["prompt_version"],
                "error": row["error"],
                "generated_at": row["created_at"],
            }
        )
    return {"task": dict(task), "items": items}


def inspect_project_statistics(project_id: str | None = None) -> list[dict[str, Any]]:
    with _connection() as connection:
        if project_id:
            projects = [dict(_screening_project(connection, project_id))]
        else:
            projects = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM screening_projects ORDER BY updated_at DESC, name"
                )
            ]
        reports = []
        for project in projects:
            pid = project["id"]
            event_row = connection.execute(
                """SELECT COUNT(*) AS event_count,
                          COALESCE(SUM(identified_count), 0) AS identified,
                          COALESCE(SUM(duplicate_count), 0) AS duplicates
                   FROM project_import_events WHERE project_id = ?""",
                (pid,),
            ).fetchone()
            stats_tracked = bool(project["stats_tracked"])
            event_count = event_row["event_count"]
            estimated_identified = event_row["identified"] if stats_tracked else None
            estimated_duplicates = event_row["duplicates"] if stats_tracked else None
            can_rebuild = stats_tracked and (event_count > 0 or project["identified_count"] == 0)
            notes = []
            if not stats_tracked:
                notes.append("旧版本项目没有导入事件账本，无法精确重建统计。")
            elif not can_rebuild:
                notes.append("项目有计数但没有导入事件，无法证明可精确重建。")
            elif (
                project["identified_count"] != estimated_identified
                or project["duplicate_count"] != estimated_duplicates
            ):
                notes.append("项目计数与导入事件账本不一致，可从账本精确重建。")
            reports.append(
                {
                    "project_id": pid,
                    "name": project["name"],
                    "stored_identified": project["identified_count"],
                    "stored_duplicates": project["duplicate_count"],
                    "stats_tracked": stats_tracked,
                    "import_events": event_count,
                    "estimated_identified": estimated_identified,
                    "estimated_duplicates": estimated_duplicates,
                    "can_rebuild": can_rebuild,
                    "needs_review": bool(notes),
                    "notes": notes,
                }
            )
        return reports


def rebuild_project_statistics(project_id: str, *, apply: bool = False) -> dict[str, Any]:
    report = inspect_project_statistics(project_id)[0]
    if not report["can_rebuild"]:
        raise ValueError(
            "该项目没有完整导入事件账本，无法精确重建统计；请保留人工核对结果。"
        )
    before = {
        "identified": report["stored_identified"],
        "duplicates": report["stored_duplicates"],
    }
    backup_path = None
    if apply and before != {
        "identified": report["estimated_identified"],
        "duplicates": report["estimated_duplicates"],
    }:
        backup_path = str(_backup_database(database_path()))
        with _connection() as connection:
            connection.execute(
                """UPDATE screening_projects
                   SET identified_count = ?, duplicate_count = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    report["estimated_identified"],
                    report["estimated_duplicates"],
                    _timestamp(),
                    project_id,
                ),
            )
    return {
        **report,
        "before": before,
        "after": {
            "identified": report["estimated_identified"],
            "duplicates": report["estimated_duplicates"],
        },
        "applied": apply,
        "backup_path": backup_path,
    }


def project_overview(project_id: str) -> dict[str, Any]:
    with _connection() as connection:
        project = dict(_screening_project(connection, project_id))
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                """SELECT status, COUNT(*) AS count FROM screening_candidates
                   WHERE project_id = ? GROUP BY status""",
                (project_id,),
            ).fetchall()
        }
        conflict_count = connection.execute(
            """SELECT COUNT(*) FROM screening_candidates
               WHERE project_id = ? AND candidate_key LIKE 'conflict:%'""",
            (project_id,),
        ).fetchone()[0]
        search_run_count = connection.execute(
            "SELECT COUNT(*) FROM search_runs WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        task_count = connection.execute(
            "SELECT COUNT(*) FROM ai_task_runs WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        latest_run = connection.execute(
            """SELECT id, mode, params_json, result_count, snapshot_count, created_at
               FROM search_runs WHERE project_id = ?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
    pending = counts.get("pending", 0)
    included = counts.get("included", 0)
    excluded = counts.get("excluded", 0)
    return {
        "project": project,
        "identified": project["identified_count"],
        "duplicates": project["duplicate_count"],
        "unique": pending + included + excluded,
        "pending": pending,
        "included": included,
        "excluded": excluded,
        "conflicts": conflict_count,
        "search_runs": search_run_count,
        "evidence_items": evidence_count,
        "ai_tasks": task_count,
        "latest_search": (
            {
                "id": latest_run["id"],
                "mode": latest_run["mode"],
                "params": json.loads(latest_run["params_json"]),
                "result_count": latest_run["result_count"],
                "snapshot_count": latest_run["snapshot_count"],
                "created_at": latest_run["created_at"],
            }
            if latest_run
            else None
        ),
    }
