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
    normalize_title,
    paper_id,
    strong_identifier_keys,
)


_SCHEMA_VERSION = 3
_BACKUP_SCHEMA_VERSION = 2
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


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
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
                   identified_count, duplicate_count, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)""",
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
        return {"kind": "conflict", "matches": strong_matches}
    if strong_matches:
        candidate_key, identifiers = next(iter(strong_matches.items()))
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


def _conflict_key(matches: dict[str, list[str]]) -> str:
    raw = "|".join(
        f"{identifier}->{candidate}"
        for candidate, identifiers in sorted(matches.items())
        for identifier in sorted(identifiers)
    )
    return "conflict:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _conflict_detail(matches: dict[str, list[str]]) -> str:
    parts = [
        f"{identifier}→{candidate}"
        for candidate, identifiers in sorted(matches.items())
        for identifier in sorted(identifiers)
    ]
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


def add_screening_candidates(project_id: str, papers: list[dict[str, Any]]) -> dict[str, int]:
    if len(papers) > 500:
        raise ValueError("单次最多加入 500 条候选文献。")
    now = _timestamp()
    added = 0
    duplicates = 0
    conflicts = 0
    with _connection() as connection:
        _screening_project(connection, project_id)
        for index, paper in enumerate(papers):
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
                canonical_key = _conflict_key(decision["matches"])
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
                        _conflict_detail(decision["matches"]),
                        title,
                        now,
                    ),
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
            for identifier in identifier_keys(paper):
                connection.execute(
                    """INSERT OR IGNORE INTO screening_candidate_identifiers(
                           project_id, identifier_key, candidate_key
                       ) VALUES (?, ?, ?)""",
                    (project_id, identifier, canonical_key),
                )
        identified = len(papers)
        connection.execute(
            """UPDATE screening_projects
               SET identified_count = identified_count + ?,
                   duplicate_count = duplicate_count + ?,
                   updated_at = ?
               WHERE id = ?""",
            (identified, duplicates, now, project_id),
        )
    return {
        "identified": len(papers),
        "added": added,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }


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
