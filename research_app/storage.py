from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from research_app.screening import (
    EXCLUSION_REASONS,
    FULLTEXT_EXCLUSION_REASONS,
    FULLTEXT_STATUSES,
    candidate_key,
    duplicate_basis,
    identifier_keys,
    normalize_doi,
    normalize_title,
    paper_id,
    strong_identifier_keys,
)


_SCHEMA_VERSION = 22
SCHEMA_VERSION = _SCHEMA_VERSION
_BACKUP_SCHEMA_VERSION = 2
_MAX_HISTORY_ENTRIES = 50
_MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
_MAX_SAVED_SEARCHES = 100
_MAX_PARAMS_BYTES = 16 * 1024
_MAX_BACKUP_BYTES = 2 * 1024 * 1024
_SENSITIVE_KEYS = {"token", "api_token", "sciverse_api_token", "authorization"}
_BACKUP_FORMAT = "sciverse-research-workbench"

REVIEW_STATUSES = frozenset(
    {"pending", "accepted", "overridden", "resolved_uncertain", "rejected"}
)
REVIEWER_ROLES = frozenset({"reviewer", "coordinator", "viewer"})
BLIND_STATUSES = frozenset({"pending", "included", "excluded"})


def _review_status_for(suggestion: str | None, final_status: str) -> str:
    if final_status == "pending":
        return "rejected"
    expected = {"include": "included", "exclude": "excluded"}.get(str(suggestion))
    if expected is None:
        return "resolved_uncertain"
    return "accepted" if final_status == expected else "overridden"


def _reclassify_review_status(
    suggestion: str | None,
    review_status: str | None,
    review_decision: str | None,
) -> str:
    if review_status != "modified":
        return review_status or "pending"
    if str(suggestion) in {"include", "exclude"}:
        return "overridden"
    return "resolved_uncertain"


_database_override = threading.local()


def database_path() -> Path:
    override = getattr(_database_override, "path", None)
    if override:
        return Path(override)
    override = os.environ.get("SCIVERSE_RESEARCH_DB")
    if override:
        return Path(override)
    return Path.home() / ".sciverse" / "research-workbench.sqlite3"


@contextmanager
def override_database_path(path: str | Path) -> Iterator[None]:
    previous = getattr(_database_override, "path", None)
    _database_override.path = str(path)
    try:
        yield
    finally:
        if previous is None:
            if hasattr(_database_override, "path"):
                delattr(_database_override, "path")
        else:
            _database_override.path = previous


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
    if version < 7:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_scan_reports (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                task_id TEXT PRIMARY KEY REFERENCES ai_task_runs(id) ON DELETE CASCADE,
                themes_json TEXT NOT NULL DEFAULT '[]',
                gaps_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        version = 7
        connection.execute("PRAGMA user_version = 7")
    if version < 8:
        report_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(ai_scan_reports)"
            ).fetchall()
        }
        if "aggregation_error" not in report_columns:
            connection.execute(
                "ALTER TABLE ai_scan_reports ADD COLUMN aggregation_error TEXT"
            )
        version = 8
        connection.execute("PRAGMA user_version = 8")
    if version < 9:
        candidate_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(screening_candidates)"
            ).fetchall()
        }
        if "abstract" not in candidate_columns:
            connection.execute(
                "ALTER TABLE screening_candidates ADD COLUMN abstract TEXT NOT NULL DEFAULT ''"
            )
        audit_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(screening_audit)").fetchall()
        }
        if "ai_task_id" not in audit_columns:
            connection.execute("ALTER TABLE screening_audit ADD COLUMN ai_task_id TEXT")
        if "ai_action" not in audit_columns:
            connection.execute("ALTER TABLE screening_audit ADD COLUMN ai_action TEXT")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_screening_suggestions (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL REFERENCES ai_task_runs(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                paper_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                suggestion TEXT NOT NULL DEFAULT 'uncertain'
                    CHECK (suggestion IN ('include', 'exclude', 'uncertain')),
                criteria_json TEXT NOT NULL DEFAULT '[]',
                rationale TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'low',
                evidence_quotes_json TEXT NOT NULL DEFAULT '[]',
                quote_checks_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                review_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (review_status IN ('pending', 'accepted', 'modified', 'rejected')),
                review_decision TEXT,
                review_note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, candidate_key)
            );
            CREATE INDEX IF NOT EXISTS ai_screening_suggestions_project
                ON ai_screening_suggestions(project_id, task_id);
            """
        )
        version = 9
        connection.execute("PRAGMA user_version = 9")
    if version < 10:
        suggestion_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(ai_screening_suggestions)"
            ).fetchall()
        }
        if "invalid_criteria_json" not in suggestion_columns:
            connection.execute(
                """ALTER TABLE ai_screening_suggestions
                   ADD COLUMN invalid_criteria_json TEXT NOT NULL DEFAULT '[]'"""
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_screening_snapshots (
                task_id TEXT PRIMARY KEY REFERENCES ai_task_runs(id) ON DELETE CASCADE,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                kind TEXT NOT NULL
                    CHECK (kind IN ('screening', 'screening_benchmark')),
                criteria_json TEXT NOT NULL,
                items_json TEXT NOT NULL,
                gold_json TEXT,
                dataset_hash TEXT NOT NULL,
                dataset_version TEXT NOT NULL,
                evaluation_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ai_screening_snapshots_project
                ON ai_screening_snapshots(project_id, kind, created_at DESC);
            CREATE TABLE IF NOT EXISTS ai_screening_review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL,
                candidate_key TEXT NOT NULL,
                ai_suggestion TEXT,
                review_status TEXT NOT NULL,
                review_decision TEXT,
                status_before TEXT,
                status_after TEXT,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ai_screening_review_events_project
                ON ai_screening_review_events(project_id, task_id, id DESC);
            """
        )
        version = 10
        connection.execute("PRAGMA user_version = 10")
    if version < 11:
        connection.executescript(
            """
            CREATE TABLE ai_screening_suggestions_v11 (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL REFERENCES ai_task_runs(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                paper_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                suggestion TEXT NOT NULL DEFAULT 'uncertain'
                    CHECK (suggestion IN ('include', 'exclude', 'uncertain')),
                criteria_json TEXT NOT NULL DEFAULT '[]',
                rationale TEXT NOT NULL DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'low',
                evidence_quotes_json TEXT NOT NULL DEFAULT '[]',
                quote_checks_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                invalid_criteria_json TEXT NOT NULL DEFAULT '[]',
                review_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (review_status IN ('pending', 'accepted', 'overridden',
                                             'resolved_uncertain', 'rejected')),
                review_decision TEXT,
                review_note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, candidate_key)
            );
            """
        )
        columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(ai_screening_suggestions)"
            ).fetchall()
        ]
        for row in connection.execute(
            "SELECT * FROM ai_screening_suggestions"
        ).fetchall():
            data = dict(row)
            data["review_status"] = _reclassify_review_status(
                data.get("suggestion"),
                data.get("review_status"),
                data.get("review_decision"),
            )
            connection.execute(
                f"""INSERT INTO ai_screening_suggestions_v11
                    ({', '.join(columns)})
                    VALUES ({', '.join('?' for _ in columns)})""",
                [data[column] for column in columns],
            )
        connection.execute("DROP TABLE ai_screening_suggestions")
        connection.execute(
            "ALTER TABLE ai_screening_suggestions_v11 RENAME TO ai_screening_suggestions"
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS ai_screening_suggestions_project
               ON ai_screening_suggestions(project_id, task_id)"""
        )
        for row in connection.execute(
            """SELECT id, ai_suggestion, review_status, review_decision
               FROM ai_screening_review_events
               WHERE review_status = 'modified'"""
        ).fetchall():
            connection.execute(
                "UPDATE ai_screening_review_events SET review_status = ? WHERE id = ?",
                (
                    _reclassify_review_status(
                        row["ai_suggestion"],
                        row["review_status"],
                        row["review_decision"],
                    ),
                    row["id"],
                ),
            )
        for row in connection.execute(
            """SELECT a.id AS aid, s.suggestion, s.review_decision, a.ai_action
               FROM screening_audit a
               LEFT JOIN ai_screening_suggestions s
                 ON a.ai_task_id = s.task_id AND a.candidate_key = s.candidate_key
               WHERE a.ai_action = 'modified'"""
        ).fetchall():
            connection.execute(
                "UPDATE screening_audit SET ai_action = ? WHERE id = ?",
                (
                    _reclassify_review_status(
                        row["suggestion"], row["ai_action"], row["review_decision"]
                    ),
                    row["aid"],
                ),
            )
        version = 11
        connection.execute("PRAGMA user_version = 11")
    if version < 12:
        project_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(screening_projects)"
            ).fetchall()
        }
        if "lock_status" not in project_columns:
            connection.execute(
                "ALTER TABLE screening_projects "
                "ADD COLUMN lock_status TEXT NOT NULL DEFAULT 'open'"
            )
        if "lock_reason" not in project_columns:
            connection.execute(
                "ALTER TABLE screening_projects "
                "ADD COLUMN lock_reason TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            """UPDATE screening_projects
               SET lock_status = 'locked', lock_reason = '已存在基准评估快照'
               WHERE lock_status = 'open'
                 AND id IN (
                     SELECT DISTINCT project_id FROM ai_screening_snapshots
                     WHERE kind = 'screening_benchmark'
                 )"""
        )
        version = 12
        connection.execute("PRAGMA user_version = 12")
    if version < 13:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL REFERENCES ai_task_runs(id) ON DELETE CASCADE,
                batch_index INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
                paper_keys_json TEXT NOT NULL DEFAULT '[]',
                request_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, batch_index)
            );
            CREATE INDEX IF NOT EXISTS evidence_batches_project
                ON evidence_batches(project_id, task_id, batch_index);
            CREATE TABLE IF NOT EXISTS evidence_item_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                paper_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                task_id TEXT NOT NULL,
                batch_id INTEGER,
                version_no INTEGER NOT NULL,
                source TEXT NOT NULL
                    CHECK (source IN ('ai_raw', 'human_corrected', 'final_adopted')),
                data_json TEXT NOT NULL,
                quote_checks_json TEXT NOT NULL DEFAULT '[]',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                reviewer_note TEXT NOT NULL DEFAULT '',
                error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, paper_id, source, version_no)
            );
            CREATE INDEX IF NOT EXISTS evidence_item_versions_paper
                ON evidence_item_versions(project_id, paper_id, source, version_no DESC);
            CREATE INDEX IF NOT EXISTS evidence_item_versions_task
                ON evidence_item_versions(project_id, task_id);
            """
        )
        version = 13
        connection.execute("PRAGMA user_version = 13")
    if version < 14:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS evidence_task_inputs (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                task_id TEXT NOT NULL REFERENCES ai_task_runs(id) ON DELETE CASCADE,
                paper_id TEXT NOT NULL,
                candidate_key TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                year INTEGER,
                doc_id TEXT NOT NULL DEFAULT '',
                source_run_ids_json TEXT NOT NULL DEFAULT '[]',
                input_source TEXT NOT NULL DEFAULT 'abstract'
                    CHECK (input_source IN ('full_text', 'abstract', 'none')),
                input_text TEXT NOT NULL DEFAULT '',
                input_text_hash TEXT NOT NULL DEFAULT '',
                input_text_length INTEGER NOT NULL DEFAULT 0,
                input_error TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, paper_id)
            );
            CREATE INDEX IF NOT EXISTS evidence_task_inputs_project
                ON evidence_task_inputs(project_id, task_id);
            """
        )
        batch_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(evidence_batches)"
            ).fetchall()
        }
        if "completed_count" not in batch_columns:
            connection.execute(
                "ALTER TABLE evidence_batches "
                "ADD COLUMN completed_count INTEGER NOT NULL DEFAULT 0"
            )
        if "failed_count" not in batch_columns:
            connection.execute(
                "ALTER TABLE evidence_batches "
                "ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0"
            )
        version = 14
        connection.execute("PRAGMA user_version = 14")
    if version < 15:
        version_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(evidence_item_versions)"
            ).fetchall()
        }
        if "error" not in version_columns:
            connection.execute(
                "ALTER TABLE evidence_item_versions ADD COLUMN error TEXT"
            )
        version = 15
        connection.execute("PRAGMA user_version = 15")
    if version < 16:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS screening_fulltext (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'not_sought'
                    CHECK (status IN ('not_sought', 'not_retrieved', 'included', 'excluded')),
                exclusion_reason TEXT,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, candidate_key)
            );
            CREATE INDEX IF NOT EXISTS screening_fulltext_project
                ON screening_fulltext(project_id, status);
            CREATE TABLE IF NOT EXISTS screening_fulltext_audit (
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
            CREATE INDEX IF NOT EXISTS screening_fulltext_audit_project
                ON screening_fulltext_audit(project_id, id DESC);
            """
        )
        version = 16
        connection.execute("PRAGMA user_version = 16")
    if version < 17:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS screening_fulltext_v17 (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'not_sought'
                    CHECK (status IN ('not_sought', 'not_retrieved',
                                      'retrieved_pending', 'included', 'excluded')),
                exclusion_reason TEXT,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, candidate_key)
            );
            """
        )
        for row in connection.execute(
            """SELECT project_id, candidate_key, status, exclusion_reason,
                      notes, updated_at FROM screening_fulltext"""
        ).fetchall():
            connection.execute(
                """INSERT OR IGNORE INTO screening_fulltext_v17(
                       project_id, candidate_key, status, exclusion_reason,
                       notes, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    row["project_id"],
                    row["candidate_key"],
                    row["status"],
                    row["exclusion_reason"],
                    row["notes"],
                    row["updated_at"],
                ),
            )
        connection.execute("DROP TABLE screening_fulltext")
        connection.execute(
            "ALTER TABLE screening_fulltext_v17 RENAME TO screening_fulltext"
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS screening_fulltext_project
               ON screening_fulltext(project_id, status)"""
        )
        converted = connection.execute(
            """SELECT project_id, candidate_key, notes
               FROM screening_fulltext
               WHERE status = 'excluded'
                 AND exclusion_reason = '无法获取全文'"""
        ).fetchall()
        connection.execute(
            """UPDATE screening_fulltext
               SET status = 'not_retrieved', exclusion_reason = NULL
               WHERE status = 'excluded' AND exclusion_reason = '无法获取全文'"""
        )
        migration_note = "Schema v17 数据迁移：无法获取全文 → 已寻求未获取"
        migration_time = _timestamp()
        for row in converted:
            connection.execute(
                """INSERT INTO screening_fulltext_audit(
                       project_id, candidate_key, old_status, new_status,
                       old_reason, new_reason, old_notes, new_notes, changed_at
                   ) VALUES (?, ?, 'excluded', 'not_retrieved',
                             '无法获取全文', NULL, ?, ?, ?)""",
                (
                    row["project_id"],
                    row["candidate_key"],
                    row["notes"],
                    migration_note,
                    migration_time,
                ),
            )
        version = 17
        connection.execute("PRAGMA user_version = 17")
    if version < 18:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'screening_audit'"
        ).fetchone() is not None:
            audit_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(screening_audit)"
                ).fetchall()
            }
            if "actor" not in audit_columns:
                connection.execute(
                    "ALTER TABLE screening_audit "
                    "ADD COLUMN actor TEXT NOT NULL DEFAULT ''"
                )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'screening_fulltext_audit'"
        ).fetchone() is not None:
            fulltext_audit_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(screening_fulltext_audit)"
                ).fetchall()
            }
            if "actor" not in fulltext_audit_columns:
                connection.execute(
                    "ALTER TABLE screening_fulltext_audit "
                    "ADD COLUMN actor TEXT NOT NULL DEFAULT ''"
                )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reviewers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL
                    CHECK (role IN ('reviewer', 'coordinator', 'viewer')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS screening_blind_decisions (
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                reviewer_id TEXT NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'included', 'excluded')),
                exclusion_reason TEXT,
                notes TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, candidate_key, reviewer_id)
            );
            CREATE INDEX IF NOT EXISTS screening_blind_decisions_project
                ON screening_blind_decisions(project_id, candidate_key);
            CREATE TABLE IF NOT EXISTS screening_blind_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                old_reason TEXT,
                new_reason TEXT,
                old_notes TEXT,
                new_notes TEXT,
                changed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS screening_blind_audit_project
                ON screening_blind_audit(project_id, id DESC);
            CREATE TABLE IF NOT EXISTS screening_conflict_resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES screening_projects(id) ON DELETE CASCADE,
                candidate_key TEXT NOT NULL,
                resolution_type TEXT NOT NULL
                    CHECK (resolution_type IN ('conflict', 'agreement')),
                reviewer_ids_json TEXT NOT NULL,
                decisions_json TEXT NOT NULL,
                final_status TEXT NOT NULL,
                final_reason TEXT,
                notes TEXT NOT NULL DEFAULT '',
                coordinator_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS screening_conflict_resolutions_project
                ON screening_conflict_resolutions(project_id, id DESC);
            """
        )
        version = 18
        connection.execute("PRAGMA user_version = 18")
    if version < 19:
        for table in ("screening_blind_decisions", "screening_blind_audit"):
            columns = {
                row["name"]
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for column, definition in (
                ("ai_task_id", "TEXT NOT NULL DEFAULT ''"),
                ("ai_suggestion", "TEXT NOT NULL DEFAULT ''"),
                ("ai_confidence", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
        decision_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(screening_blind_decisions)"
            ).fetchall()
        }
        if "ai_assisted" not in decision_columns:
            connection.execute(
                "ALTER TABLE screening_blind_decisions "
                "ADD COLUMN ai_assisted INTEGER NOT NULL DEFAULT 0"
            )
        version = 19
        connection.execute("PRAGMA user_version = 19")
    if version < 20:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reviewers)"
            ).fetchall()
        }
        if "pin_salt" not in columns:
            connection.execute(
                "ALTER TABLE reviewers ADD COLUMN pin_salt TEXT NOT NULL DEFAULT ''"
            )
        if "pin_hash" not in columns:
            connection.execute(
                "ALTER TABLE reviewers ADD COLUMN pin_hash TEXT NOT NULL DEFAULT ''"
            )
        version = 20
        connection.execute("PRAGMA user_version = 20")
    if version < 21:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reviewers)"
            ).fetchall()
        }
        if "pin_version" not in columns:
            connection.execute(
                "ALTER TABLE reviewers "
                "ADD COLUMN pin_version INTEGER NOT NULL DEFAULT 0"
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reviewer_pin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reviewer_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reviewer_pin_audit_reviewer
                ON reviewer_pin_audit(reviewer_id, id DESC);
            """
        )
        version = 21
        connection.execute("PRAGMA user_version = 21")
    if version < 22:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS collab_admin_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                key_salt TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        version = 22
        connection.execute("PRAGMA user_version = 22")


@contextmanager
def _connection(
    *,
    immediate: bool = False,
    migrate_backup: bool = True,
    restore_from: Path | None = None,
) -> Iterator[sqlite3.Connection]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 全新库（文件本来不存在）失败时可以直接删掉半成品；已有库必须靠备份恢复。
    existed_before = path.exists()
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    backup_path: Path | None = restore_from
    closed = False
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if backup_path is None and migrate_backup and 0 < current_version < _SCHEMA_VERSION:
            backup_path = _backup_database(path)
        try:
            _migrate(connection)
        except Exception:
            # 迁移全部走 executescript，而 executescript 会先 COMMIT 再执行脚本：
            # 中途失败会留下“表已建、user_version 还卡在旧值”的不可恢复中间态。
            # 迁移前做过整库备份（或调用方显式传入）就回滚到那份备份；
            # 全新库没有历史数据，直接删掉半成品，让用户能干净重试。
            connection.close()
            closed = True
            if backup_path is not None and backup_path.exists():
                shutil.copyfile(backup_path, path)
                if os.name != "nt":
                    try:
                        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
                    except OSError:
                        pass
            elif not existed_before:
                path.unlink(missing_ok=True)
            raise
        if os.name != "nt":
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        if immediate:
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        if not closed:
            connection.rollback()
        raise
    finally:
        if not closed:
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
    # 已经显式备份过，别让 _connection 再备份一份（每次升级只留一个 .bak），
    # 但要把这份备份交给它——否则显式升级路径失败时没人恢复。
    with _connection(migrate_backup=False, restore_from=backup_path) as connection:
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


def _require_unlocked(
    connection: sqlite3.Connection, project_id: str
) -> sqlite3.Row:
    row = _screening_project(connection, project_id)
    if row["lock_status"] == "locked":
        raise ValueError(
            "该项目已作为基准集冻结（只读），不能直接修改候选或金标准；"
            "如需修订请另建数据集版本。"
        )
    return row


def set_screening_project_lock(
    project_id: str,
    locked: bool,
    *,
    reason: str = "",
) -> None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        connection.execute(
            """UPDATE screening_projects
               SET lock_status = ?, lock_reason = ?, updated_at = ?
               WHERE id = ?""",
            (
                "locked" if locked else "open",
                str(reason or "").strip(),
                _timestamp(),
                project_id,
            ),
        )


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
        row = connection.execute(
            "SELECT lock_status FROM screening_projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return False
        if row["lock_status"] == "locked":
            raise ValueError(
                "该项目已作为基准集冻结（只读），不能直接删除；"
                "如需清理请先显式解锁或另建数据集版本。"
            )
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
        """SELECT title, year, doi, paper_id, abstract FROM screening_candidates
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
    abstract = str(paper.get("abstract") or paper.get("chunk") or "").strip()
    if not row["abstract"] and abstract:
        updates["abstract"] = abstract[:4000]
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
        _require_unlocked(connection, project_id)
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
            abstract = str(paper.get("abstract") or paper.get("chunk") or "").strip()[:4000]
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
                           project_id, candidate_key, paper_id, title, year, doi, abstract,
                           status, notes, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)""",
                    (
                        project_id,
                        canonical_key,
                        paper_id(paper, canonical_key),
                        title,
                        year,
                        doi,
                        abstract,
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
                           project_id, candidate_key, paper_id, title, year, doi, abstract,
                           status, notes, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)""",
                    (
                        project_id,
                        key,
                        paper_id(paper, key),
                        title,
                        year,
                        doi,
                        abstract,
                        now,
                        now,
                    ),
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
    order_by: str = "updated_at",
) -> tuple[list[dict[str, Any]], int]:
    if status is not None and status not in {"pending", "included", "excluded"}:
        raise ValueError("筛选状态无效。")
    if order_by not in {"updated_at", "title"}:
        raise ValueError("筛选排序无效。")
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    order_clause = (
        "title, candidate_key" if order_by == "title" else "updated_at, title"
    )
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
                ORDER BY {order_clause} LIMIT ? OFFSET ?""",
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
    ai_task_id: str | None = None,
    ai_action: str | None = None,
    actor: str = "",
) -> bool:
    if status not in {"pending", "included", "excluded"}:
        raise ValueError("筛选状态无效。")
    if ai_action not in {None, "accepted", "overridden", "resolved_uncertain", "rejected"}:
        raise ValueError("AI 建议处理动作无效。")
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
        _require_unlocked(connection, project_id)
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
        if row["status"] != status:
            fulltext = connection.execute(
                """SELECT status FROM screening_fulltext
                   WHERE project_id = ? AND candidate_key = ?""",
                (project_id, candidate_key_value),
            ).fetchone()
            if fulltext is not None:
                raise ValueError(
                    "该文献已有全文阶段记录，请先在研究报告页重置全文阶段，"
                    "再修改题名/摘要决定。"
                )
        connection.execute(
            """UPDATE screening_candidates
               SET status = ?, exclusion_reason = ?, notes = ?, updated_at = ?
               WHERE project_id = ? AND candidate_key = ?""",
            (status, reason, notes, now, project_id, candidate_key_value),
        )
        connection.execute(
            """INSERT INTO screening_audit(
                   project_id, candidate_key, old_status, new_status,
                   old_reason, new_reason, old_notes, new_notes, changed_at,
                   ai_task_id, ai_action, actor
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ai_task_id,
                ai_action,
                str(actor or ""),
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


def get_fulltext_decision(
    project_id: str,
    candidate_key_value: str,
) -> dict[str, Any] | None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        row = connection.execute(
            """SELECT * FROM screening_fulltext
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
    return dict(row) if row is not None else None


def set_fulltext_decision(
    project_id: str,
    candidate_key_value: str,
    status: str,
    *,
    reason: str | None = None,
    notes: str = "",
    actor: str = "",
) -> bool:
    if status not in FULLTEXT_STATUSES:
        raise ValueError("全文阶段状态无效。")
    if status == "excluded":
        if reason not in FULLTEXT_EXCLUSION_REASONS:
            raise ValueError("全文排除时必须选择标准排除原因。")
    else:
        reason = None
    notes = str(notes or "").strip()
    if len(notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    now = _timestamp()
    with _connection() as connection:
        _require_unlocked(connection, project_id)
        candidate = connection.execute(
            """SELECT status FROM screening_candidates
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
        if candidate is None:
            raise ValueError("候选文献不存在。")
        if candidate["status"] != "included":
            raise ValueError("只有题名/摘要纳入的候选才能进入全文阶段。")
        existing = connection.execute(
            """SELECT status, exclusion_reason, notes FROM screening_fulltext
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
        if (
            existing is not None
            and existing["status"] == status
            and (existing["exclusion_reason"] or None) == reason
            and existing["notes"] == notes
        ):
            return False
        old_status = existing["status"] if existing else None
        old_reason = existing["exclusion_reason"] if existing else None
        old_notes = existing["notes"] if existing else None
        connection.execute(
            """INSERT INTO screening_fulltext(
                   project_id, candidate_key, status, exclusion_reason, notes, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, candidate_key) DO UPDATE SET
                   status = excluded.status,
                   exclusion_reason = excluded.exclusion_reason,
                   notes = excluded.notes,
                   updated_at = excluded.updated_at""",
            (project_id, candidate_key_value, status, reason, notes, now),
        )
        connection.execute(
            """INSERT INTO screening_fulltext_audit(
                   project_id, candidate_key, old_status, new_status,
                   old_reason, new_reason, old_notes, new_notes, changed_at, actor
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                candidate_key_value,
                old_status,
                status,
                old_reason,
                reason,
                old_notes,
                notes,
                now,
                str(actor or ""),
            ),
        )
    return True


def list_fulltext_decisions(
    project_id: str,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        if active_only:
            rows = connection.execute(
                """SELECT f.* FROM screening_fulltext f
                   JOIN screening_candidates c
                     ON c.project_id = f.project_id
                    AND c.candidate_key = f.candidate_key
                   WHERE f.project_id = ? AND c.status = 'included'
                   ORDER BY f.candidate_key""",
                (project_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT * FROM screening_fulltext
                   WHERE project_id = ?
                   ORDER BY candidate_key""",
                (project_id,),
            ).fetchall()
    return [dict(row) for row in rows]


def list_stale_fulltext_decisions(project_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT f.*, c.status AS title_abstract_status,
                      c.title AS candidate_title
               FROM screening_fulltext f
               JOIN screening_candidates c
                 ON c.project_id = f.project_id
                AND c.candidate_key = f.candidate_key
               WHERE f.project_id = ? AND c.status != 'included'
               ORDER BY f.candidate_key""",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def reset_fulltext_decision(
    project_id: str,
    candidate_key_value: str,
    *,
    notes: str = "",
) -> bool:
    notes = str(notes or "").strip()
    if len(notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    now = _timestamp()
    with _connection() as connection:
        _require_unlocked(connection, project_id)
        existing = connection.execute(
            """SELECT status, exclusion_reason, notes FROM screening_fulltext
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
        if existing is None:
            return False
        connection.execute(
            """DELETE FROM screening_fulltext
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        )
        connection.execute(
            """INSERT INTO screening_fulltext_audit(
                   project_id, candidate_key, old_status, new_status,
                   old_reason, new_reason, old_notes, new_notes, changed_at
               ) VALUES (?, ?, ?, 'not_sought', ?, NULL, ?, ?, ?)""",
            (
                project_id,
                candidate_key_value,
                existing["status"],
                existing["exclusion_reason"],
                existing["notes"],
                notes or "重置全文阶段",
                now,
            ),
        )
    return True


def list_fulltext_audit(
    project_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM screening_fulltext_audit
               WHERE project_id = ?
               ORDER BY id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _reviewer(connection: sqlite3.Connection, reviewer_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM reviewers WHERE id = ?", (str(reviewer_id or ""),)
    ).fetchone()
    if row is None:
        raise ValueError("审核人不存在。")
    return row


def _pin_hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        pin.encode("utf-8"),
        bytes.fromhex(salt),
        120000,
    ).hex()


def _normalized_pin(pin: str) -> str:
    pin = str(pin or "").strip()
    if not pin:
        return ""
    if not 4 <= len(pin) <= 32:
        raise ValueError("PIN 需为 4 至 32 个字符。")
    return pin


def create_reviewer(name: str, role: str, pin: str) -> str:
    name = str(name or "").strip()
    if not name or len(name) > 100:
        raise ValueError("审核人名称需为 1 至 100 个字符。")
    if role not in REVIEWER_ROLES:
        raise ValueError("审核人角色无效。")
    pin = _normalized_pin(pin)
    if not pin:
        raise ValueError("创建审核人时必须设置 PIN。")
    salt = secrets.token_hex(16)
    pin_hash = _pin_hash(pin, salt)
    reviewer_id = uuid.uuid4().hex
    now = _timestamp()
    with _connection(immediate=True) as connection:
        try:
            connection.execute(
                """INSERT INTO reviewers(
                       id, name, role, pin_salt, pin_hash, pin_version,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (reviewer_id, name, role, salt, pin_hash, now, now),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("审核人名称已存在。") from error
        connection.execute(
            """INSERT INTO reviewer_pin_audit(
                   reviewer_id, action, actor_id, note, created_at
               ) VALUES (?, 'created', ?, '', ?)""",
            (reviewer_id, reviewer_id, now),
        )
    return reviewer_id


def set_reviewer_pin(
    reviewer_id: str,
    pin: str,
    *,
    actor: str = "",
    note: str = "",
) -> None:
    pin = _normalized_pin(pin)
    if not pin:
        raise ValueError("PIN 不能为空。")
    salt = secrets.token_hex(16)
    pin_hash = _pin_hash(pin, salt)
    now = _timestamp()
    with _connection(immediate=True) as connection:
        _reviewer(connection, reviewer_id)
        connection.execute(
            """UPDATE reviewers
               SET pin_salt = ?, pin_hash = ?,
                   pin_version = pin_version + 1, updated_at = ?
               WHERE id = ?""",
            (salt, pin_hash, now, reviewer_id),
        )
        connection.execute(
            """INSERT INTO reviewer_pin_audit(
                   reviewer_id, action, actor_id, note, created_at
               ) VALUES (?, 'set', ?, ?, ?)""",
            (reviewer_id, str(actor or ""), str(note or ""), now),
        )


def reviewers_without_pin() -> list[dict[str, Any]]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM reviewers WHERE pin_hash = '' ORDER BY name"
        ).fetchall()
    return [dict(row) for row in rows]


def legacy_pin_initialization_required() -> bool:
    with _connection() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM reviewers"
        ).fetchone()[0]
        missing = connection.execute(
            "SELECT COUNT(*) FROM reviewers WHERE pin_hash = ''"
        ).fetchone()[0]
    return total > 0 and missing == total


def _normalized_admin_key(key: str) -> str:
    key = str(key or "").strip()
    if not 8 <= len(key) <= 128:
        raise ValueError("管理员恢复密钥需为 8 至 128 个字符。")
    return key


def collab_admin_configured() -> bool:
    with _connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM collab_admin_settings WHERE id = 1"
        ).fetchone()
    return row is not None


def configure_collab_admin_key(key: str) -> None:
    key = _normalized_admin_key(key)
    salt = secrets.token_hex(16)
    now = _timestamp()
    with _connection(immediate=True) as connection:
        if connection.execute(
            "SELECT 1 FROM collab_admin_settings WHERE id = 1"
        ).fetchone() is not None:
            raise ValueError("管理员恢复密钥已存在，不能通过此接口覆盖。")
        connection.execute(
            """INSERT INTO collab_admin_settings(
                   id, key_salt, key_hash, created_at, updated_at
               ) VALUES (1, ?, ?, ?, ?)""",
            (salt, _pin_hash(key, salt), now, now),
        )


def verify_collab_admin_key(key: str) -> bool:
    key = str(key or "").strip()
    if not key:
        return False
    with _connection() as connection:
        row = connection.execute(
            "SELECT key_salt, key_hash FROM collab_admin_settings WHERE id = 1"
        ).fetchone()
    if row is None:
        return False
    return hmac.compare_digest(
        row["key_hash"], _pin_hash(key, row["key_salt"])
    )


def initialize_legacy_pins(
    pins: dict[str, str],
    *,
    admin_key: str,
    note: str = "",
) -> int:
    if not verify_collab_admin_key(admin_key):
        raise ValueError("管理员恢复密钥不正确。")
    now = _timestamp()
    with _connection(immediate=True) as connection:
        rows = connection.execute(
            "SELECT id, pin_hash FROM reviewers ORDER BY name"
        ).fetchall()
        if not rows:
            raise ValueError("没有审核人。")
        if any(row["pin_hash"] for row in rows):
            raise ValueError("已有审核人设置 PIN，请使用协调者重置流程。")
        normalized = {}
        for row in rows:
            pin = _normalized_pin(pins.get(row["id"], ""))
            if not pin:
                raise ValueError("初始化必须为所有审核人设置 PIN。")
            normalized[row["id"]] = pin
        for row in rows:
            pin = normalized[row["id"]]
            salt = secrets.token_hex(16)
            connection.execute(
                """UPDATE reviewers
                   SET pin_salt = ?, pin_hash = ?,
                       pin_version = pin_version + 1, updated_at = ?
                   WHERE id = ?""",
                (salt, _pin_hash(pin, salt), now, row["id"]),
            )
            connection.execute(
                """INSERT INTO reviewer_pin_audit(
                       reviewer_id, action, actor_id, note, created_at
                   ) VALUES (?, 'legacy_init', 'local-admin', ?, ?)""",
                (
                    row["id"],
                    str(note or "Schema v22 旧审核人 PIN 初始化"),
                    now,
                ),
            )
    return len(rows)


def admin_reset_reviewer_pin(
    reviewer_id: str,
    pin: str,
    *,
    admin_key: str,
    note: str = "",
) -> None:
    if not verify_collab_admin_key(admin_key):
        raise ValueError("管理员恢复密钥不正确。")
    pin = _normalized_pin(pin)
    if not pin:
        raise ValueError("PIN 不能为空。")
    salt = secrets.token_hex(16)
    now = _timestamp()
    with _connection(immediate=True) as connection:
        _reviewer(connection, reviewer_id)
        connection.execute(
            """UPDATE reviewers
               SET pin_salt = ?, pin_hash = ?,
                   pin_version = pin_version + 1, updated_at = ?
               WHERE id = ?""",
            (salt, _pin_hash(pin, salt), now, reviewer_id),
        )
        connection.execute(
            """INSERT INTO reviewer_pin_audit(
                   reviewer_id, action, actor_id, note, created_at
               ) VALUES (?, 'admin_reset', 'local-admin', ?, ?)""",
            (reviewer_id, str(note or "本机管理员恢复"), now),
        )


def list_reviewer_pin_audit(
    reviewer_id: str | None = None,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with _connection() as connection:
        if reviewer_id:
            rows = connection.execute(
                """SELECT * FROM reviewer_pin_audit
                   WHERE reviewer_id = ? ORDER BY id DESC LIMIT ?""",
                (reviewer_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM reviewer_pin_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def verify_reviewer_pin(reviewer_id: str, pin: str) -> bool:
    pin = str(pin or "").strip()
    if not pin:
        return False
    with _connection() as connection:
        row = connection.execute(
            "SELECT pin_salt, pin_hash FROM reviewers WHERE id = ?",
            (str(reviewer_id or ""),),
        ).fetchone()
    if row is None or not row["pin_salt"] or not row["pin_hash"]:
        return False
    return hmac.compare_digest(
        row["pin_hash"], _pin_hash(pin, row["pin_salt"])
    )


def list_reviewers() -> list[dict[str, Any]]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM reviewers ORDER BY role, name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def get_reviewer(reviewer_id: str) -> dict[str, Any] | None:
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM reviewers WHERE id = ?", (str(reviewer_id or ""),)
        ).fetchone()
    return dict(row) if row is not None else None


def _blind_fingerprint(decisions: list[dict[str, Any]]) -> str:
    payload = sorted(
        [
            [
                str(item.get("reviewer_id") or ""),
                str(item.get("status") or ""),
                str(item.get("exclusion_reason") or ""),
                int(item.get("revision") or 0),
            ]
            for item in decisions
        ]
    )
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def set_blind_decision(
    project_id: str,
    candidate_key_value: str,
    reviewer_id: str,
    status: str,
    *,
    reason: str | None = None,
    notes: str = "",
    expected_revision: int | None = 0,
    ai_task_id: str = "",
    ai_suggestion: str = "",
    ai_confidence: str = "",
    ai_assisted: bool = False,
) -> dict[str, Any]:
    if status not in BLIND_STATUSES:
        raise ValueError("盲筛状态无效。")
    if status == "excluded":
        if reason not in EXCLUSION_REASONS:
            raise ValueError("盲筛排除时必须选择标准排除原因。")
    else:
        reason = None
    notes = str(notes or "").strip()
    if len(notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    ai_task_value = str(ai_task_id or "").strip()
    ai_suggestion_value = str(ai_suggestion or "").strip()
    if ai_suggestion_value not in {"", "include", "exclude", "uncertain"}:
        ai_suggestion_value = ""
    ai_confidence_value = str(ai_confidence or "").strip().casefold()
    if ai_confidence_value not in {"", "high", "medium", "low"}:
        ai_confidence_value = ""
    ai_provided = bool(
        ai_task_value
        or ai_suggestion_value
        or ai_confidence_value
        or ai_assisted
    )
    ai_assisted_value = bool(ai_assisted and ai_suggestion_value)
    now = _timestamp()
    with _connection(immediate=True) as connection:
        _require_unlocked(connection, project_id)
        reviewer = _reviewer(connection, reviewer_id)
        if reviewer["role"] not in {"reviewer", "coordinator"}:
            raise ValueError("只读角色不能提交盲筛决定。")
        candidate = connection.execute(
            """SELECT 1 FROM screening_candidates
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
        if candidate is None:
            raise ValueError("候选文献不存在。")
        existing = connection.execute(
            """SELECT status, exclusion_reason, notes, revision,
                      ai_task_id, ai_suggestion, ai_confidence, ai_assisted
               FROM screening_blind_decisions
               WHERE project_id = ? AND candidate_key = ? AND reviewer_id = ?""",
            (project_id, candidate_key_value, reviewer["id"]),
        ).fetchone()
        revision = int(existing["revision"]) if existing else 0
        try:
            expected = int(
                0 if expected_revision is None else expected_revision
            )
        except (TypeError, ValueError) as error:
            raise ValueError("盲筛版本号无效。") from error
        if expected != revision:
            raise ValueError("盲筛决定已被他人更新，请刷新后重试。")
        same_content = (
            existing is not None
            and existing["status"] == status
            and (existing["exclusion_reason"] or None) == reason
            and existing["notes"] == notes
        )
        if ai_provided:
            new_ai = (
                ai_task_value,
                ai_suggestion_value,
                ai_confidence_value,
                1 if ai_assisted_value else 0,
            )
        elif same_content:
            new_ai = (
                existing["ai_task_id"],
                existing["ai_suggestion"],
                existing["ai_confidence"],
                int(existing["ai_assisted"] or 0),
            )
        else:
            new_ai = ("", "", "", 0)
        if same_content and new_ai == (
            existing["ai_task_id"],
            existing["ai_suggestion"],
            existing["ai_confidence"],
            int(existing["ai_assisted"] or 0),
        ):
            return {"revision": revision, "created": False}
        new_revision = revision + 1
        connection.execute(
            """INSERT INTO screening_blind_decisions(
                   project_id, candidate_key, reviewer_id, status,
                   exclusion_reason, notes, revision, ai_task_id,
                   ai_suggestion, ai_confidence, ai_assisted,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, candidate_key, reviewer_id) DO UPDATE SET
                   status = excluded.status,
                   exclusion_reason = excluded.exclusion_reason,
                   notes = excluded.notes,
                   revision = excluded.revision,
                   ai_task_id = excluded.ai_task_id,
                   ai_suggestion = excluded.ai_suggestion,
                   ai_confidence = excluded.ai_confidence,
                   ai_assisted = excluded.ai_assisted,
                   updated_at = excluded.updated_at""",
            (
                project_id,
                candidate_key_value,
                reviewer["id"],
                status,
                reason,
                notes,
                new_revision,
                new_ai[0],
                new_ai[1],
                new_ai[2],
                new_ai[3],
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO screening_blind_audit(
                   project_id, candidate_key, reviewer_id, old_status,
                   new_status, old_reason, new_reason, old_notes, new_notes,
                   changed_at, ai_task_id, ai_suggestion, ai_confidence
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                candidate_key_value,
                reviewer["id"],
                existing["status"] if existing else None,
                status,
                existing["exclusion_reason"] if existing else None,
                reason,
                existing["notes"] if existing else None,
                notes,
                now,
                new_ai[0],
                new_ai[1],
                new_ai[2],
            ),
        )
    return {"revision": new_revision, "created": True}


def list_blind_decisions(
    project_id: str,
    *,
    candidate_key_value: str | None = None,
    reviewer_id: str | None = None,
) -> list[dict[str, Any]]:
    where = "d.project_id = ?"
    params: list[Any] = [project_id]
    if candidate_key_value:
        where += " AND d.candidate_key = ?"
        params.append(candidate_key_value)
    if reviewer_id:
        where += " AND d.reviewer_id = ?"
        params.append(reviewer_id)
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            f"""SELECT d.*, r.name AS reviewer_name, r.role AS reviewer_role
                FROM screening_blind_decisions d
                JOIN reviewers r ON r.id = d.reviewer_id
                WHERE {where}
                ORDER BY d.candidate_key, r.name COLLATE NOCASE""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_blind_audit(
    project_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT a.*, r.name AS reviewer_name
               FROM screening_blind_audit a
               LEFT JOIN reviewers r ON r.id = a.reviewer_id
               WHERE a.project_id = ?
               ORDER BY a.id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def blind_screening_summary(project_id: str) -> list[dict[str, Any]]:
    decisions = list_blind_decisions(project_id)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for item in decisions:
        by_key.setdefault(item["candidate_key"], []).append(item)
    with _connection() as connection:
        _screening_project(connection, project_id)
        candidates = connection.execute(
            """SELECT candidate_key, title, status
               FROM screening_candidates WHERE project_id = ?
               ORDER BY title, candidate_key""",
            (project_id,),
        ).fetchall()
    summary = []
    for candidate in candidates:
        key = candidate["candidate_key"]
        items = by_key.get(key, [])
        active = [
            item
            for item in items
            if item["status"] in {"included", "excluded"}
        ]
        statuses = {item["status"] for item in active}
        if not items:
            state = "unreviewed"
        elif any(item["status"] == "pending" for item in items):
            state = "awaiting"
        elif len(active) < 2:
            state = "single"
        elif len(statuses) == 1:
            if "excluded" in statuses:
                reasons = {
                    str(item.get("exclusion_reason") or "") for item in active
                }
                if len(reasons) > 1 or "" in reasons:
                    state = "reason_conflict"
                else:
                    state = "agreed"
            else:
                state = "agreed"
        else:
            state = "conflict"
        summary.append(
            {
                "candidate_key": key,
                "title": candidate["title"],
                "candidate_status": candidate["status"],
                "state": state,
                "decisions": items,
                "fingerprint": _blind_fingerprint(items),
            }
        )
    return summary


_AI_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def ai_priority_candidates(project_id: str) -> list[dict[str, Any]]:
    saved = load_latest_screening_suggestions(project_id, kind="screening")
    suggestions: dict[str, dict[str, Any]] = {}
    task_id = ""
    if saved:
        task_id = saved["task"]["id"]
        for item in saved["suggestions"]:
            suggestions[item["candidate_key"]] = item
    rows = []
    for item in blind_screening_summary(project_id):
        suggestion = suggestions.get(item["candidate_key"]) or {}
        ai_suggestion = str(suggestion.get("suggestion") or "")
        ai_confidence = str(suggestion.get("confidence") or "")
        if item["state"] == "conflict":
            rank, label = 0, "盲筛冲突"
        elif ai_suggestion in {"include", "exclude"} and item["state"] != "agreed":
            rank, label = 1, "AI 明确建议"
        elif ai_suggestion == "uncertain":
            rank, label = 2, "AI 不确定"
        else:
            rank, label = 3, "无 AI 建议"
        rows.append(
            {
                "candidate_key": item["candidate_key"],
                "title": item["title"],
                "candidate_status": item["candidate_status"],
                "blind_state": item["state"],
                "priority_rank": rank,
                "priority_label": label,
                "ai_task_id": task_id,
                "ai_suggestion": ai_suggestion,
                "ai_confidence": ai_confidence,
                "decisions": item["decisions"],
            }
        )
    rows.sort(
        key=lambda row: (
            row["priority_rank"],
            _AI_CONFIDENCE_RANK.get(row["ai_confidence"], 3),
            row["title"] or "",
        )
    )
    return rows


def reviewer_priority_candidates(
    project_id: str,
    reviewer_id: str,
) -> list[dict[str, Any]]:
    """AI priority queue for a single reviewer without global blind state."""

    saved = load_latest_screening_suggestions(project_id, kind="screening")
    suggestions: dict[str, dict[str, Any]] = {}
    task_id = ""
    if saved:
        task_id = saved["task"]["id"]
        for item in saved["suggestions"]:
            suggestions[item["candidate_key"]] = item
    own = {
        item["candidate_key"]: item
        for item in list_blind_decisions(
            project_id, reviewer_id=reviewer_id
        )
    }
    rows = []
    for candidate in list_all_screening_candidates(project_id):
        key = candidate["candidate_key"]
        decision = own.get(key) or {}
        own_status = str(decision.get("status") or "pending")
        suggestion = suggestions.get(key) or {}
        ai_suggestion = str(suggestion.get("suggestion") or "")
        ai_confidence = str(suggestion.get("confidence") or "")
        if own_status == "pending" and ai_suggestion in {"include", "exclude"}:
            rank, label = 1, "AI 明确建议"
        elif own_status == "pending" and ai_suggestion == "uncertain":
            rank, label = 2, "AI 不确定"
        else:
            rank, label = 3, "无 AI 建议或已提交"
        rows.append(
            {
                "candidate_key": key,
                "title": candidate.get("title") or "",
                "candidate_status": candidate["status"],
                "own_status": own_status,
                "own_revision": int(decision.get("revision") or 0),
                "priority_rank": rank,
                "priority_label": label,
                "ai_task_id": task_id,
                "ai_suggestion": ai_suggestion,
                "ai_confidence": ai_confidence,
            }
        )
    rows.sort(
        key=lambda row: (
            row["priority_rank"],
            _AI_CONFIDENCE_RANK.get(row["ai_confidence"], 3),
            row["title"] or "",
        )
    )
    return rows


def _write_resolution(
    connection: sqlite3.Connection,
    project_id: str,
    candidate_key_value: str,
    coordinator: sqlite3.Row,
    decisions: list[dict[str, Any]],
    final_status: str,
    reason: str | None,
    notes: str,
    resolution_type: str,
    now: str,
) -> None:
    candidate = connection.execute(
        """SELECT status, exclusion_reason, notes FROM screening_candidates
           WHERE project_id = ? AND candidate_key = ?""",
        (project_id, candidate_key_value),
    ).fetchone()
    if candidate is None:
        raise ValueError("候选文献不存在。")
    if candidate["status"] != final_status:
        fulltext = connection.execute(
            """SELECT 1 FROM screening_fulltext
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
        if fulltext is not None:
            raise ValueError(
                "该文献已有全文阶段记录，请先重置全文阶段再修改题摘决定。"
            )
    connection.execute(
        """UPDATE screening_candidates
           SET status = ?, exclusion_reason = ?, notes = ?, updated_at = ?
           WHERE project_id = ? AND candidate_key = ?""",
        (final_status, reason, notes, now, project_id, candidate_key_value),
    )
    connection.execute(
        """INSERT INTO screening_audit(
               project_id, candidate_key, old_status, new_status,
               old_reason, new_reason, old_notes, new_notes, changed_at,
               ai_task_id, ai_action, actor
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
        (
            project_id,
            candidate_key_value,
            candidate["status"],
            final_status,
            candidate["exclusion_reason"],
            reason,
            candidate["notes"],
            notes,
            now,
            coordinator["id"],
        ),
    )
    connection.execute(
        """INSERT INTO screening_conflict_resolutions(
               project_id, candidate_key, resolution_type, reviewer_ids_json,
               decisions_json, final_status, final_reason, notes,
               coordinator_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            candidate_key_value,
            resolution_type,
            json.dumps(
                [item["reviewer_id"] for item in decisions], ensure_ascii=False
            ),
            json.dumps(
                [
                    {
                        "reviewer_id": item["reviewer_id"],
                        "reviewer_name": item.get("reviewer_name") or "",
                        "status": item["status"],
                        "exclusion_reason": item.get("exclusion_reason"),
                        "notes": item.get("notes") or "",
                        "revision": item.get("revision"),
                    }
                    for item in decisions
                ],
                ensure_ascii=False,
            ),
            final_status,
            reason,
            notes,
            coordinator["id"],
            now,
        ),
    )


def _blind_decisions_in_connection(
    connection: sqlite3.Connection,
    project_id: str,
    candidate_key_value: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT d.*, r.name AS reviewer_name, r.role AS reviewer_role
           FROM screening_blind_decisions d
           JOIN reviewers r ON r.id = d.reviewer_id
           WHERE d.project_id = ? AND d.candidate_key = ?
           ORDER BY r.name COLLATE NOCASE""",
        (project_id, candidate_key_value),
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_blind_conflict(
    project_id: str,
    candidate_key_value: str,
    coordinator_id: str,
    final_status: str,
    *,
    reason: str | None = None,
    notes: str = "",
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    if final_status not in {"included", "excluded"}:
        raise ValueError("冲突解决必须选择纳入或排除。")
    if final_status == "excluded":
        if reason not in EXCLUSION_REASONS:
            raise ValueError("排除时必须选择标准排除原因。")
    else:
        reason = None
    notes = str(notes or "").strip()
    if len(notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    with _connection(immediate=True) as connection:
        _require_unlocked(connection, project_id)
        coordinator = _reviewer(connection, coordinator_id)
        if coordinator["role"] != "coordinator":
            raise ValueError("只有协调者可以解决盲筛冲突。")
        decisions = _blind_decisions_in_connection(
            connection, project_id, candidate_key_value
        )
        active = [
            item for item in decisions if item["status"] in {"included", "excluded"}
        ]
        if len(active) < 2 or len({item["status"] for item in active}) < 2:
            raise ValueError("没有可解决的盲筛冲突。")
        fingerprint = _blind_fingerprint(decisions)
        if expected_fingerprint and str(expected_fingerprint) != fingerprint:
            raise ValueError("盲筛决定已变化，请刷新后重试。")
        _write_resolution(
            connection,
            project_id,
            candidate_key_value,
            coordinator,
            decisions,
            final_status,
            reason,
            notes,
            "conflict",
            _timestamp(),
        )
    return {"fingerprint": fingerprint, "final_status": final_status}


def confirm_blind_agreement(
    project_id: str,
    candidate_key_value: str,
    coordinator_id: str,
    *,
    reason: str | None = None,
    notes: str = "",
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    notes = str(notes or "").strip()
    if len(notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    with _connection(immediate=True) as connection:
        _require_unlocked(connection, project_id)
        coordinator = _reviewer(connection, coordinator_id)
        if coordinator["role"] != "coordinator":
            raise ValueError("只有协调者可以确认盲筛一致结果。")
        decisions = _blind_decisions_in_connection(
            connection, project_id, candidate_key_value
        )
        active = [
            item for item in decisions if item["status"] in {"included", "excluded"}
        ]
        if len(active) < 2 or len({item["status"] for item in active}) != 1:
            raise ValueError("盲筛决定尚未形成一致结果。")
        final_status = active[0]["status"]
        if final_status == "excluded":
            reasons = {
                str(item.get("exclusion_reason") or "") for item in active
            }
            if len(reasons) > 1 or "" in reasons:
                if reason not in EXCLUSION_REASONS:
                    raise ValueError(
                        "排除原因不一致，请协调者选择最终原因。"
                    )
            else:
                derived = next(iter(reasons), "")
                reason = (
                    reason
                    if reason in EXCLUSION_REASONS
                    else (derived or "其他")
                )
        else:
            reason = None
        fingerprint = _blind_fingerprint(decisions)
        if expected_fingerprint and str(expected_fingerprint) != fingerprint:
            raise ValueError("盲筛决定已变化，请刷新后重试。")
        _write_resolution(
            connection,
            project_id,
            candidate_key_value,
            coordinator,
            decisions,
            final_status,
            reason,
            notes,
            "agreement",
            _timestamp(),
        )
    return {"fingerprint": fingerprint, "final_status": final_status}


def list_conflict_resolutions(
    project_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM screening_conflict_resolutions
               WHERE project_id = ?
               ORDER BY id DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
    resolutions = []
    for row in rows:
        item = dict(row)
        item["reviewer_ids"] = json.loads(item.pop("reviewer_ids_json"))
        item["decisions"] = json.loads(item.pop("decisions_json"))
        resolutions.append(item)
    return resolutions


def prisma_counts(project_id: str) -> dict[str, Any]:
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
        import_events = [
            dict(row)
            for row in connection.execute(
                """SELECT id, run_id, identified_count, duplicate_count,
                          conflict_count, created_at
                   FROM project_import_events
                   WHERE project_id = ?
                   ORDER BY id""",
                (project_id,),
            ).fetchall()
        ]
        fulltext_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                """SELECT f.status, COUNT(*) AS count
                   FROM screening_fulltext f
                   JOIN screening_candidates c
                     ON c.project_id = f.project_id
                    AND c.candidate_key = f.candidate_key
                   WHERE f.project_id = ? AND c.status = 'included'
                   GROUP BY f.status""",
                (project_id,),
            ).fetchall()
        }
        fulltext_reasons = [
            dict(row)
            for row in connection.execute(
                """SELECT f.exclusion_reason AS reason, COUNT(*) AS count
                   FROM screening_fulltext f
                   JOIN screening_candidates c
                     ON c.project_id = f.project_id
                    AND c.candidate_key = f.candidate_key
                   WHERE f.project_id = ? AND c.status = 'included'
                     AND f.status = 'excluded'
                   GROUP BY f.exclusion_reason
                   ORDER BY count DESC, reason""",
                (project_id,),
            ).fetchall()
        ]
        search_run_count = connection.execute(
            "SELECT COUNT(*) FROM search_runs WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]

    pending_ta = counts.get("pending", 0)
    included_ta = counts.get("included", 0)
    excluded_ta = counts.get("excluded", 0)
    unique = pending_ta + included_ta + excluded_ta
    not_sought = fulltext_counts.get("not_sought", 0) + max(
        0, included_ta - sum(fulltext_counts.values())
    )
    not_retrieved = fulltext_counts.get("not_retrieved", 0)
    retrieved_pending = fulltext_counts.get("retrieved_pending", 0)
    fulltext_included = fulltext_counts.get("included", 0)
    fulltext_excluded = fulltext_counts.get("excluded", 0)
    reports_sought = (
        not_retrieved + retrieved_pending + fulltext_included + fulltext_excluded
    )
    reports_assessed = fulltext_included + fulltext_excluded
    fulltext_partition = (
        not_sought
        + not_retrieved
        + retrieved_pending
        + fulltext_included
        + fulltext_excluded
    )
    conservation_ok = fulltext_partition == included_ta

    warnings = []
    if included_ta and not fulltext_counts:
        warnings.append(
            "尚未记录全文阶段：题名/摘要纳入不等于最终纳入，完整 PRISMA 需要全文资格评估。"
        )
    if pending_ta:
        warnings.append(f"仍有 {pending_ta} 条题名/摘要待定，PRISMA 计数尚未完成。")
    if not_sought:
        warnings.append(f"有 {not_sought} 条题名/摘要纳入文献尚未记录全文阶段。")
    if retrieved_pending:
        warnings.append(
            f"有 {retrieved_pending} 条已获取全文、待资格评估。"
        )
    if not conservation_ok:
        warnings.append(
            "全文阶段状态合计与题名/摘要纳入数不一致，请检查全文阶段记录。"
        )

    return {
        "identified": project["identified_count"],
        "duplicates_removed": project["duplicate_count"],
        "unique_records": unique,
        "title_abstract": {
            "screened": unique,
            "pending": pending_ta,
            "included": included_ta,
            "excluded": excluded_ta,
        },
        "fulltext": {
            "not_sought": not_sought,
            "sought": reports_sought,
            "not_retrieved": not_retrieved,
            "retrieved_pending": retrieved_pending,
            "assessed": reports_assessed,
            "included": fulltext_included,
            "excluded": fulltext_excluded,
            "excluded_reasons": fulltext_reasons,
            "partition_total": fulltext_partition,
            "conservation_ok": conservation_ok,
        },
        "included_in_review": fulltext_included,
        "included_reports": fulltext_included,
        "included_studies": None,
        "study_level_supported": False,
        "warnings": warnings,
        "traceability": {
            "search_runs": search_run_count,
            "import_events": import_events,
            "candidate_table": "screening_candidates",
            "fulltext_table": "screening_fulltext",
            "evidence_table": "evidence_item_versions",
        },
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


def list_all_candidate_sources(project_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT candidate_key, run_id, discovered_at
               FROM screening_candidate_sources
               WHERE project_id = ?
               ORDER BY candidate_key, discovered_at""",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_screening_audit(project_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM screening_audit
               WHERE project_id = ?
               ORDER BY id""",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_fulltext_audit(project_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM screening_fulltext_audit
               WHERE project_id = ?
               ORDER BY id""",
            (project_id,),
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


def list_search_runs(project_id: str, limit: int | None = 20) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        sql = """SELECT id, mode, params_json, result_count, snapshot_count, created_at
                 FROM search_runs WHERE project_id = ?
                 ORDER BY created_at DESC, id DESC"""
        params: list[Any] = [project_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, min(limit, 100)))
        rows = connection.execute(sql, params).fetchall()
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


def enrich_search_run_snapshots(
    project_id: str,
    enrich: Callable[[dict[str, Any]], bool],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """就地补全一个项目所有检索快照里的论文记录。

    早期版本的快照只落 6 个字段，作者与期刊因此丢失。这里提供一个受控的
    补写入口：``enrich`` 就地修改单条记录并返回是否改动，遍历一遍后写回。
    没有改动就不写库，所以重复执行是幂等的；``dry_run`` 只统计不落盘。
    """
    report = {
        "runs_scanned": 0,
        "runs_skipped": 0,
        "runs_updated": 0,
        "papers_scanned": 0,
        "papers_changed": 0,
    }
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT id, snapshot_json FROM search_runs
               WHERE project_id = ?
               ORDER BY created_at ASC, id ASC""",
            (project_id,),
        ).fetchall()
        pending: list[tuple[str, str]] = []
        for row in rows:
            report["runs_scanned"] += 1
            try:
                papers = json.loads(row["snapshot_json"])
            except (TypeError, ValueError):
                report["runs_skipped"] += 1
                continue
            if not isinstance(papers, list):
                report["runs_skipped"] += 1
                continue
            changed = 0
            for paper in papers:
                if not isinstance(paper, dict):
                    continue
                report["papers_scanned"] += 1
                if enrich(paper):
                    changed += 1
            report["papers_changed"] += changed
            if not changed:
                continue
            snapshot = json.dumps(papers, ensure_ascii=False, sort_keys=True)
            # 快照限长与写入路径保持一致，超限宁可中止也不截断。
            if len(snapshot.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
                raise ValueError("回填后的检索快照超过 5 MiB 限制。")
            pending.append((snapshot, str(row["id"])))
        if not dry_run:
            for snapshot, run_id in pending:
                connection.execute(
                    "UPDATE search_runs SET snapshot_json = ? WHERE id = ?",
                    (snapshot, run_id),
                )
            report["runs_updated"] = len(pending)
    return report


def project_papers(project_id: str) -> list[dict[str, Any]]:
    """汇总项目所有检索快照里的论文元数据（只读，去重）。

    候选表只保存题名/年份/DOI/摘要，作者与期刊留在检索快照里。
    综述章节页需要书目信息，这里按 DOI 优先、题名次之去重后返回原始
    paper 记录，调用方自行决定取哪些字段。只读，不写库、不做迁移。
    """
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT snapshot_json FROM search_runs
               WHERE project_id = ?
               ORDER BY created_at ASC, id ASC""",
            (project_id,),
        ).fetchall()
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            papers = json.loads(row["snapshot_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(papers, list):
            continue
        for paper in papers:
            if not isinstance(paper, dict):
                continue
            key = str(paper.get("doi") or "").strip().lower() or str(
                paper.get("title") or ""
            ).strip()
            if not key:
                continue
            existing = merged.get(key)
            # 同一篇可能在多次检索中重复出现，保留书目信息更全的那条。
            if existing is None or _bibliography_score(paper) > _bibliography_score(existing):
                merged[key] = paper
    return list(merged.values())


def _bibliography_score(paper: dict[str, Any]) -> int:
    score = 0
    if paper.get("author") or paper.get("authors"):
        score += 2
    if paper.get("publication_venue_name_unified") or paper.get("publication_venue_name"):
        score += 2
    if paper.get("publication_published_year"):
        score += 1
    if paper.get("citation_count") is not None:
        score += 1
    return score


def latest_search_run(project_id: str) -> dict[str, Any] | None:
    runs = list_search_runs(project_id, limit=1)
    if not runs:
        return None
    return get_search_run(project_id, runs[0]["id"])


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
    runs = list_search_runs(project_id, limit=None)
    tasks = list_ai_tasks(project_id, limit=None)
    with _connection() as connection:
        _screening_project(connection, project_id)
        candidate_rows = connection.execute(
            """SELECT candidate_key, paper_id, title, year, doi, abstract, status,
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
        suggestion_rows = connection.execute(
            """SELECT * FROM ai_screening_suggestions
               WHERE project_id = ? ORDER BY created_at, candidate_key""",
            (project_id,),
        ).fetchall()
        snapshot_rows = connection.execute(
            """SELECT * FROM ai_screening_snapshots
               WHERE project_id = ? ORDER BY created_at, task_id""",
            (project_id,),
        ).fetchall()
        review_event_rows = connection.execute(
            """SELECT * FROM ai_screening_review_events
               WHERE project_id = ? ORDER BY id""",
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
    screening_suggestions = []
    for row in suggestion_rows:
        item = dict(row)
        item["criteria"] = json.loads(item.pop("criteria_json"))
        item["invalid_criteria"] = json.loads(
            item.pop("invalid_criteria_json", "[]")
        )
        item["evidence_quotes"] = json.loads(item.pop("evidence_quotes_json"))
        item["quote_checks"] = json.loads(item.pop("quote_checks_json"))
        screening_suggestions.append(item)
    screening_snapshots = []
    for row in snapshot_rows:
        item = dict(row)
        item["criteria"] = json.loads(item.pop("criteria_json"))
        item["items"] = json.loads(item.pop("items_json"))
        gold_json = item.pop("gold_json")
        item["gold"] = json.loads(gold_json) if gold_json else None
        evaluation_json = item.pop("evaluation_json")
        item["evaluation"] = (
            json.loads(evaluation_json) if evaluation_json else None
        )
        screening_snapshots.append(item)
    review_events = [dict(row) for row in review_event_rows]
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
            "search_runs_exported": len(runs),
            "ai_tasks_exported": len(tasks),
            "candidates_exported": len(candidates),
            "ai_screening_suggestions_exported": len(screening_suggestions),
            "ai_screening_snapshots_exported": len(screening_snapshots),
            "ai_screening_review_events_exported": len(review_events),
            "complete": True,
        },
        "search_runs": runs,
        "ai_tasks": tasks,
        "candidates": candidates,
        "ai_screening_suggestions": screening_suggestions,
        "ai_screening_snapshots": screening_snapshots,
        "ai_screening_review_events": review_events,
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def list_ai_tasks(project_id: str, limit: int | None = 20) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        sql = """SELECT id, kind, model, prompt_version, status, error, created_at, updated_at
                 FROM ai_task_runs WHERE project_id = ?
                 ORDER BY created_at DESC, id DESC"""
        params: list[Any] = [project_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, min(limit, 100)))
        rows = connection.execute(sql, params).fetchall()
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


EVIDENCE_BATCH_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "skipped"}
)
EVIDENCE_VERSION_SOURCES = frozenset(
    {"ai_raw", "human_corrected", "final_adopted"}
)


def create_evidence_batch(
    project_id: str,
    task_id: str,
    batch_index: int,
    *,
    paper_keys: list[str] | None = None,
) -> dict[str, Any]:
    if int(batch_index) < 0:
        raise ValueError("批次序号无效。")
    keys = [str(key) for key in paper_keys or [] if str(key)]
    now = _timestamp()
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            """SELECT 1 FROM ai_task_runs
               WHERE id = ? AND project_id = ?
                 AND kind IN ('evidence', 'evidence_matrix')""",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise ValueError("AI 任务不存在或不是证据任务。")
        row = connection.execute(
            """SELECT id, paper_keys_json FROM evidence_batches
               WHERE task_id = ? AND batch_index = ?""",
            (task_id, int(batch_index)),
        ).fetchone()
        if row is not None:
            if json.loads(row["paper_keys_json"]) != keys:
                raise ValueError("同一任务同一批次已存在不同论文清单，请新建任务。")
            return {"id": int(row["id"]), "created": False}
        cursor = connection.execute(
            """INSERT INTO evidence_batches(
                   project_id, task_id, batch_index, status, paper_keys_json,
                   request_count, created_at, updated_at
               ) VALUES (?, ?, ?, 'pending', ?, 0, ?, ?)""",
            (
                project_id,
                task_id,
                int(batch_index),
                json.dumps(keys, ensure_ascii=False),
                now,
                now,
            ),
        )
        return {"id": int(cursor.lastrowid), "created": True}


def update_evidence_batch(
    batch_id: int,
    *,
    status: str,
    error: str | None = None,
    request_count: int | None = None,
    completed_count: int | None = None,
    failed_count: int | None = None,
) -> None:
    if status not in EVIDENCE_BATCH_STATUSES:
        raise ValueError("证据批次状态无效。")
    now = _timestamp()
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM evidence_batches WHERE id = ?", (int(batch_id),)
        ).fetchone()
        if row is None:
            raise ValueError("证据批次不存在。")
        started_at = row["started_at"]
        finished_at = row["finished_at"]
        if status == "running":
            if not started_at:
                started_at = now
            finished_at = None
        if status in {"completed", "failed", "skipped"}:
            finished_at = now
        connection.execute(
            """UPDATE evidence_batches
               SET status = ?, error = ?, request_count = ?,
                   completed_count = ?, failed_count = ?,
                   started_at = ?, finished_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                status,
                str(error or "") or None,
                int(request_count) if request_count is not None else row["request_count"],
                int(completed_count)
                if completed_count is not None
                else row["completed_count"],
                int(failed_count)
                if failed_count is not None
                else row["failed_count"],
                started_at,
                finished_at,
                now,
                int(batch_id),
            ),
        )


def list_evidence_batches(
    project_id: str,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        if task_id:
            rows = connection.execute(
                """SELECT * FROM evidence_batches
                   WHERE project_id = ? AND task_id = ?
                   ORDER BY batch_index""",
                (project_id, task_id),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT * FROM evidence_batches
                   WHERE project_id = ?
                   ORDER BY task_id, batch_index""",
                (project_id,),
            ).fetchall()
    batches = []
    for row in rows:
        item = dict(row)
        item["paper_keys"] = json.loads(item.pop("paper_keys_json"))
        batches.append(item)
    return batches


def save_evidence_task_inputs(
    project_id: str,
    task_id: str,
    items: list[dict[str, Any]],
) -> dict[str, int]:
    now = _timestamp()
    saved = 0
    skipped = 0
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            """SELECT 1 FROM ai_task_runs
               WHERE id = ? AND project_id = ?
                 AND kind IN ('evidence', 'evidence_matrix')""",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise ValueError("AI 任务不存在或不是证据任务。")
        for item in items:
            paper_id_value = str(item.get("paper_id") or "")
            if not paper_id_value:
                continue
            text = str(item.get("input_text") or "")
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            existing = connection.execute(
                """SELECT input_text_hash FROM evidence_task_inputs
                   WHERE task_id = ? AND paper_id = ?""",
                (task_id, paper_id_value),
            ).fetchone()
            if existing is not None:
                if existing["input_text_hash"] != text_hash:
                    raise ValueError(
                        "任务输入已冻结，不能替换为不同文本；请新建任务。"
                    )
                skipped += 1
                continue
            year = item.get("year")
            try:
                year = int(year) if year is not None else None
            except (TypeError, ValueError):
                year = None
            connection.execute(
                """INSERT INTO evidence_task_inputs(
                       project_id, task_id, paper_id, candidate_key, title, year,
                       doc_id, source_run_ids_json, input_source, input_text,
                       input_text_hash, input_text_length, input_error, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    task_id,
                    paper_id_value,
                    str(item.get("candidate_key") or ""),
                    str(item.get("title") or ""),
                    year,
                    str(item.get("doc_id") or ""),
                    json.dumps(item.get("source_run_ids") or [], ensure_ascii=False),
                    str(item.get("input_source") or "abstract"),
                    text,
                    text_hash,
                    len(text),
                    str(item.get("input_error") or "") or None,
                    now,
                ),
            )
            saved += 1
    return {"saved": saved, "skipped": skipped}


def list_evidence_task_inputs(
    project_id: str,
    task_id: str,
) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM evidence_task_inputs
               WHERE project_id = ? AND task_id = ?
               ORDER BY paper_id""",
            (project_id, task_id),
        ).fetchall()
    inputs = []
    for row in rows:
        item = dict(row)
        item["source_run_ids"] = json.loads(item.pop("source_run_ids_json"))
        inputs.append(item)
    return inputs


def get_evidence_task_input(
    project_id: str,
    task_id: str,
    paper_id: str,
) -> dict[str, Any] | None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        row = connection.execute(
            """SELECT * FROM evidence_task_inputs
               WHERE project_id = ? AND task_id = ? AND paper_id = ?""",
            (project_id, task_id, str(paper_id)),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["source_run_ids"] = json.loads(item.pop("source_run_ids_json"))
    return item


def append_evidence_version(
    project_id: str,
    paper_id: str,
    task_id: str,
    *,
    source: str,
    data: dict[str, Any],
    quote_checks: list[dict[str, Any]] | None = None,
    model: str = "",
    prompt_version: str = "",
    reviewer_note: str = "",
    title: str = "",
    year: int | None = None,
    batch_id: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if source not in EVIDENCE_VERSION_SOURCES:
        raise ValueError("证据版本来源无效。")
    if not isinstance(data, dict):
        raise ValueError("证据内容必须为对象。")
    paper_id_value = str(paper_id or "")
    if not paper_id_value:
        raise ValueError("缺少论文标识。")
    data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
    checks_json = json.dumps(
        quote_checks or [], ensure_ascii=False, sort_keys=True
    )
    error_text = str(error or "") or None
    note = str(reviewer_note or "")
    if len(note) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    now = _timestamp()
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            """SELECT 1 FROM ai_task_runs
               WHERE id = ? AND project_id = ?
                 AND kind IN ('evidence', 'evidence_matrix')""",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise ValueError("AI 任务不存在或不是证据任务。")
        if batch_id is not None:
            batch = connection.execute(
                """SELECT paper_keys_json FROM evidence_batches
                   WHERE id = ? AND project_id = ? AND task_id = ?""",
                (int(batch_id), project_id, task_id),
            ).fetchone()
            if batch is None:
                raise ValueError("证据批次不存在或不属于该任务。")
            if paper_id_value not in json.loads(batch["paper_keys_json"]):
                raise ValueError("论文不属于该证据批次。")
        latest = connection.execute(
            """SELECT version_no, title, year, data_json, quote_checks_json,
                      model, prompt_version, reviewer_note, error
               FROM evidence_item_versions
               WHERE project_id = ? AND paper_id = ? AND source = ?
               ORDER BY version_no DESC LIMIT 1""",
            (project_id, paper_id_value, source),
        ).fetchone()
        if latest is not None:
            if not title and latest["title"]:
                title = latest["title"]
            if year is None and latest["year"] is not None:
                year = int(latest["year"])
        if (
            latest is not None
            and latest["data_json"] == data_json
            and latest["quote_checks_json"] == checks_json
            and latest["model"] == str(model or "")
            and latest["prompt_version"] == str(prompt_version or "")
            and latest["error"] == error_text
            and latest["reviewer_note"] == note
        ):
            if (
                str(title or "") != latest["title"]
                or (year is not None and year != latest["year"])
            ):
                connection.execute(
                    """UPDATE evidence_item_versions
                       SET title = ?, year = ?
                       WHERE project_id = ? AND paper_id = ? AND source = ?
                         AND version_no = ?""",
                    (
                        str(title or ""),
                        int(year) if year is not None else None,
                        project_id,
                        paper_id_value,
                        source,
                        int(latest["version_no"]),
                    ),
                )
            return {"version_no": int(latest["version_no"]), "created": False}
        if latest is None:
            version_no = 1
        else:
            version_no = int(latest["version_no"]) + 1
        cursor = connection.execute(
            """INSERT INTO evidence_item_versions(
                   project_id, paper_id, title, year, task_id, batch_id,
                   version_no, source, data_json, quote_checks_json,
                   model, prompt_version, reviewer_note, error, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                paper_id_value,
                str(title or ""),
                int(year) if year is not None else None,
                task_id,
                int(batch_id) if batch_id is not None else None,
                version_no,
                source,
                data_json,
                checks_json,
                str(model or ""),
                str(prompt_version or ""),
                note,
                error_text,
                now,
            ),
        )
        return {"id": int(cursor.lastrowid), "version_no": version_no, "created": True}


def list_evidence_versions(
    project_id: str,
    paper_id: str,
    *,
    source: str | None = None,
) -> list[dict[str, Any]]:
    if source is not None and source not in EVIDENCE_VERSION_SOURCES:
        raise ValueError("证据版本来源无效。")
    with _connection() as connection:
        _screening_project(connection, project_id)
        if source:
            rows = connection.execute(
                """SELECT * FROM evidence_item_versions
                   WHERE project_id = ? AND paper_id = ? AND source = ?
                   ORDER BY version_no DESC""",
                (project_id, str(paper_id), source),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT * FROM evidence_item_versions
                   WHERE project_id = ? AND paper_id = ?
                   ORDER BY source, version_no DESC""",
                (project_id, str(paper_id)),
            ).fetchall()
    versions = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item.pop("data_json"))
        item["quote_checks"] = json.loads(item.pop("quote_checks_json"))
        versions.append(item)
    return versions


def load_evidence_matrix(project_id: str) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT v.*, i.input_source AS input_source,
                      i.input_error AS input_error
               FROM evidence_item_versions v
               LEFT JOIN evidence_task_inputs i
                 ON i.task_id = v.task_id AND i.paper_id = v.paper_id
               JOIN (
                   SELECT paper_id, source, MAX(version_no) AS max_version
                   FROM evidence_item_versions
                   WHERE project_id = ?
                   GROUP BY paper_id, source
               ) mx
                 ON v.project_id = ?
                AND v.paper_id = mx.paper_id
                AND v.source = mx.source
                AND v.version_no = mx.max_version
               ORDER BY v.title, v.paper_id""",
            (project_id, project_id),
        ).fetchall()
    papers: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = papers.setdefault(
            row["paper_id"],
            {
                "paper_id": row["paper_id"],
                "title": row["title"],
                "year": row["year"],
                "versions": {},
            },
        )
        if row["title"]:
            entry["title"] = row["title"]
        if row["year"] is not None:
            entry["year"] = row["year"]
        entry["versions"][row["source"]] = {
            "version_no": int(row["version_no"]),
            "data": json.loads(row["data_json"]),
            "quote_checks": json.loads(row["quote_checks_json"]),
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "reviewer_note": row["reviewer_note"],
            "error": row["error"],
            "task_id": row["task_id"],
            "batch_id": row["batch_id"],
            "input_source": row["input_source"],
            "input_error": row["input_error"],
            "created_at": row["created_at"],
        }
    return list(papers.values())


def get_latest_ai_task(project_id: str, kind: str) -> dict[str, Any] | None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        row = connection.execute(
            """SELECT * FROM ai_task_runs
               WHERE project_id = ? AND kind = ?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (project_id, kind),
        ).fetchone()
    return dict(row) if row is not None else None


def list_ai_tasks_by_kind(
    project_id: str,
    kind: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT * FROM ai_task_runs
               WHERE project_id = ? AND kind = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (project_id, kind, max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def evidence_task_status(
    project_id: str,
    task_id: str,
    *,
    source: str = "ai_raw",
) -> dict[str, list[str]]:
    if source not in EVIDENCE_VERSION_SOURCES:
        raise ValueError("证据版本来源无效。")
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            """SELECT v.paper_id, v.error
               FROM evidence_item_versions v
               JOIN (
                   SELECT paper_id, MAX(version_no) AS max_version
                   FROM evidence_item_versions
                   WHERE project_id = ? AND task_id = ? AND source = ?
                   GROUP BY paper_id
               ) mx
                 ON v.paper_id = mx.paper_id
                AND v.version_no = mx.max_version
               WHERE v.project_id = ? AND v.task_id = ? AND v.source = ?""",
            (project_id, task_id, source, project_id, task_id, source),
        ).fetchall()
    completed = [row["paper_id"] for row in rows if not row["error"]]
    failed = [row["paper_id"] for row in rows if row["error"]]
    return {"completed": completed, "failed": failed, "processed": completed + failed}


def evidence_latest_errors(
    project_id: str,
    task_id: str,
    paper_ids: list[str],
    *,
    source: str = "ai_raw",
) -> dict[str, str]:
    if source not in EVIDENCE_VERSION_SOURCES:
        raise ValueError("证据版本来源无效。")
    ids = [str(paper) for paper in paper_ids if str(paper)]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with _connection() as connection:
        _screening_project(connection, project_id)
        rows = connection.execute(
            f"""SELECT v.paper_id, v.error
                FROM evidence_item_versions v
                JOIN (
                    SELECT paper_id, MAX(version_no) AS max_version
                    FROM evidence_item_versions
                    WHERE project_id = ? AND task_id = ? AND source = ?
                      AND paper_id IN ({placeholders})
                    GROUP BY paper_id
                ) mx
                  ON v.paper_id = mx.paper_id
                 AND v.version_no = mx.max_version
                WHERE v.project_id = ? AND v.task_id = ? AND v.source = ?
                  AND v.paper_id IN ({placeholders})""",
            (
                project_id,
                task_id,
                source,
                *ids,
                project_id,
                task_id,
                source,
                *ids,
            ),
        ).fetchall()
    return {
        row["paper_id"]: str(row["error"]) for row in rows if row["error"]
    }


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


def save_scan_report(
    project_id: str,
    task_id: str,
    themes: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    aggregation_error: str | None = None,
) -> None:
    now = _timestamp()
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            "SELECT 1 FROM ai_task_runs WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise ValueError("AI 扫描任务不存在。")
        connection.execute(
            """INSERT INTO ai_scan_reports(
                   project_id, task_id, themes_json, gaps_json, aggregation_error,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                   themes_json = excluded.themes_json,
                   gaps_json = excluded.gaps_json,
                   aggregation_error = excluded.aggregation_error,
                   updated_at = excluded.updated_at""",
            (
                project_id,
                task_id,
                json.dumps(themes, ensure_ascii=False),
                json.dumps(gaps, ensure_ascii=False),
                aggregation_error,
                now,
                now,
            ),
        )


def load_latest_scan(project_id: str) -> dict[str, Any] | None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            """SELECT * FROM ai_task_runs
               WHERE project_id = ? AND kind = 'field_scan'
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
        report = connection.execute(
            "SELECT * FROM ai_scan_reports WHERE task_id = ?",
            (task["id"],),
        ).fetchone()
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
            }
        )
    return {
        "task": dict(task),
        "items": items,
        "themes": json.loads(report["themes_json"]) if report else [],
        "gaps": json.loads(report["gaps_json"]) if report else [],
        "aggregation_error": report["aggregation_error"] if report else None,
    }


def save_screening_suggestions(
    project_id: str,
    task_id: str,
    suggestions: list[dict[str, Any]],
) -> int:
    now = _timestamp()
    saved = 0
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            """SELECT 1 FROM ai_task_runs
               WHERE id = ? AND project_id = ?
                 AND kind IN ('screening', 'screening_benchmark')""",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise ValueError("AI 筛选任务不存在。")
        for item in suggestions:
            candidate_key_value = str(item.get("candidate_key") or "")
            if not candidate_key_value:
                continue
            if candidate_key_value.startswith("conflict:"):
                raise ValueError("身份冲突候选不能生成 AI 筛选建议。")
            candidate = connection.execute(
                """SELECT 1 FROM screening_candidates
                   WHERE project_id = ? AND candidate_key = ?""",
                (project_id, candidate_key_value),
            ).fetchone()
            if candidate is None:
                raise ValueError(f"候选文献不存在：{candidate_key_value}")
            suggestion = str(item.get("suggestion") or "uncertain")
            if suggestion not in {"include", "exclude", "uncertain"}:
                suggestion = "uncertain"
            year = item.get("year")
            try:
                year = int(year) if year is not None else None
            except (TypeError, ValueError):
                year = None
            criteria = item.get("criteria")
            if not isinstance(criteria, list):
                criteria = []
            connection.execute(
                """INSERT INTO ai_screening_suggestions(
                       project_id, task_id, candidate_key, paper_id, title, year,
                         suggestion, criteria_json, rationale, confidence,
                         evidence_quotes_json, quote_checks_json, error,
                         invalid_criteria_json,
                         review_status, review_decision, review_note, reviewed_at,
                         created_at, updated_at
                     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               'pending', NULL, '', NULL, ?, ?)
                     ON CONFLICT(task_id, candidate_key) DO UPDATE SET
                         paper_id = excluded.paper_id,
                         title = excluded.title,
                         year = excluded.year,
                         suggestion = excluded.suggestion,
                         criteria_json = excluded.criteria_json,
                         rationale = excluded.rationale,
                         confidence = excluded.confidence,
                         evidence_quotes_json = excluded.evidence_quotes_json,
                         quote_checks_json = excluded.quote_checks_json,
                         error = excluded.error,
                         invalid_criteria_json = excluded.invalid_criteria_json,
                         updated_at = excluded.updated_at""",
                (
                    project_id,
                    task_id,
                    candidate_key_value,
                    str(item.get("paper_id") or ""),
                    str(item.get("title") or ""),
                    year,
                    suggestion,
                    json.dumps(criteria, ensure_ascii=False),
                    str(item.get("rationale") or ""),
                    str(item.get("confidence") or "low"),
                    json.dumps(item.get("evidence_quotes") or [], ensure_ascii=False),
                      json.dumps(item.get("quote_checks") or [], ensure_ascii=False),
                      item.get("error"),
                      json.dumps(
                          item.get("invalid_criteria") or [],
                          ensure_ascii=False,
                      ),
                      now,
                      now,
                  ),
            )
            saved += 1
    return saved


def load_latest_screening_suggestions(
    project_id: str,
    *,
    kind: str = "screening",
) -> dict[str, Any] | None:
    if kind not in {"screening", "screening_benchmark"}:
        raise ValueError("AI 筛选任务类型无效。")
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            """SELECT * FROM ai_task_runs
               WHERE project_id = ? AND kind = ?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (project_id, kind),
        ).fetchone()
        if task is None:
            return None
        rows = connection.execute(
            """SELECT * FROM ai_screening_suggestions
               WHERE project_id = ? AND task_id = ?
               ORDER BY title, candidate_key""",
            (project_id, task["id"]),
        ).fetchall()
    suggestions = []
    for row in rows:
        item = dict(row)
        item["criteria"] = json.loads(item.pop("criteria_json"))
        item["invalid_criteria"] = json.loads(
            item.pop("invalid_criteria_json", "[]")
        )
        item["evidence_quotes"] = json.loads(item.pop("evidence_quotes_json"))
        item["quote_checks"] = json.loads(item.pop("quote_checks_json"))
        suggestions.append(item)
    return {"task": dict(task), "suggestions": suggestions}


def review_screening_suggestion(
    project_id: str,
    task_id: str,
    candidate_key_value: str,
    review_status: str,
    *,
    review_decision: str | None = None,
    note: str = "",
) -> None:
    if review_status not in REVIEW_STATUSES:
        raise ValueError("AI 建议处理状态无效。")
    if review_decision not in {None, "pending", "included", "excluded"}:
        raise ValueError("人工筛选状态无效。")
    note = str(note or "").strip()
    if len(note) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    now = _timestamp()
    with _connection() as connection:
        _screening_project(connection, project_id)
        cursor = connection.execute(
            """UPDATE ai_screening_suggestions
               SET review_status = ?, review_decision = ?, review_note = ?,
                   reviewed_at = ?, updated_at = ?
               WHERE project_id = ? AND task_id = ? AND candidate_key = ?""",
            (
                review_status,
                review_decision,
                note,
                now,
                now,
                project_id,
                task_id,
                candidate_key_value,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError("AI 筛选建议不存在。")


def apply_screening_review(
    project_id: str,
    task_id: str,
    candidate_key_value: str,
    final_status: str,
    *,
    reason: str | None = None,
    note: str = "",
    actor: str = "",
) -> dict[str, Any]:
    if final_status not in {"pending", "included", "excluded"}:
        raise ValueError("筛选状态无效。")
    if final_status == "excluded":
        if reason not in EXCLUSION_REASONS:
            raise ValueError("排除时必须选择标准排除原因。")
    else:
        reason = None
    note = str(note or "").strip()
    if len(note) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    if candidate_key_value.startswith("conflict:"):
        raise ValueError("身份冲突尚未解决，不能保存筛选决定。")
    now = _timestamp()
    with _connection() as connection:
        _require_unlocked(connection, project_id)
        task = connection.execute(
            """SELECT 1 FROM ai_task_runs
               WHERE id = ? AND project_id = ? AND kind = 'screening'""",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise ValueError("AI 筛选任务不存在，或该任务不是普通筛选任务。")
        suggestion = connection.execute(
            """SELECT suggestion FROM ai_screening_suggestions
               WHERE project_id = ? AND task_id = ? AND candidate_key = ?""",
            (project_id, task_id, candidate_key_value),
        ).fetchone()
        if suggestion is None:
            raise ValueError("AI 筛选建议不存在。")
        candidate = connection.execute(
            """SELECT status, exclusion_reason, notes
               FROM screening_candidates
               WHERE project_id = ? AND candidate_key = ?""",
            (project_id, candidate_key_value),
        ).fetchone()
        if candidate is None:
            raise ValueError("候选文献不存在。")

        suggestion_value = str(suggestion["suggestion"])
        status_before = candidate["status"]
        if final_status == "pending":
            review_status = "rejected"
            review_decision = None
            status_after = status_before
        else:
            review_status = _review_status_for(suggestion_value, final_status)
            review_decision = final_status
            status_after = final_status
            if (
                status_before != final_status
                or (candidate["exclusion_reason"] or None) != reason
                or candidate["notes"] != note
            ):
                connection.execute(
                    """UPDATE screening_candidates
                       SET status = ?, exclusion_reason = ?, notes = ?, updated_at = ?
                       WHERE project_id = ? AND candidate_key = ?""",
                    (final_status, reason, note, now, project_id, candidate_key_value),
                )
                connection.execute(
                    """INSERT INTO screening_audit(
                           project_id, candidate_key, old_status, new_status,
                           old_reason, new_reason, old_notes, new_notes, changed_at,
                           ai_task_id, ai_action, actor
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        candidate_key_value,
                        status_before,
                        final_status,
                        candidate["exclusion_reason"],
                        reason,
                        candidate["notes"],
                        note,
                        now,
                        task_id,
                        review_status,
                        str(actor or ""),
                    ),
                )
                connection.execute(
                    "UPDATE screening_projects SET updated_at = ? WHERE id = ?",
                    (now, project_id),
                )

        connection.execute(
            """UPDATE ai_screening_suggestions
               SET review_status = ?, review_decision = ?, review_note = ?,
                   reviewed_at = ?, updated_at = ?
               WHERE project_id = ? AND task_id = ? AND candidate_key = ?""",
            (
                review_status,
                review_decision,
                note,
                now,
                now,
                project_id,
                task_id,
                candidate_key_value,
            ),
        )
        connection.execute(
            """INSERT INTO ai_screening_review_events(
                   project_id, task_id, candidate_key, ai_suggestion,
                   review_status, review_decision, status_before, status_after,
                   note, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                task_id,
                candidate_key_value,
                str(suggestion["suggestion"]),
                review_status,
                review_decision,
                status_before,
                status_after,
                note,
                now,
            ),
        )
    return {
        "review_status": review_status,
        "review_decision": review_decision,
        "status_before": status_before,
        "status_after": status_after,
    }


def create_screening_snapshot(
    project_id: str,
    task_id: str,
    *,
    kind: str,
    criteria: dict[str, Any],
    items: list[dict[str, Any]],
    gold: dict[str, str] | None = None,
    dataset_version: str = "",
) -> dict[str, Any]:
    if kind not in {"screening", "screening_benchmark"}:
        raise ValueError("AI 筛选任务类型无效。")
    if kind == "screening_benchmark" and not isinstance(gold, dict):
        raise ValueError("基准评估必须冻结金标准。")
    if kind != "screening_benchmark":
        gold = None
    payload = {
        "kind": kind,
        "criteria": criteria,
        "items": items,
        "gold": gold,
    }
    dataset_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if not dataset_version:
        dataset_version = f"{kind}-{dataset_hash[:12]}"
    now = _timestamp()
    with _connection() as connection:
        _screening_project(connection, project_id)
        task = connection.execute(
            "SELECT 1 FROM ai_task_runs WHERE id = ? AND project_id = ? AND kind = ?",
            (task_id, project_id, kind),
        ).fetchone()
        if task is None:
            raise ValueError("AI 筛选任务不存在或类型不匹配。")
        connection.execute(
            """INSERT INTO ai_screening_snapshots(
                   task_id, project_id, kind, criteria_json, items_json, gold_json,
                   dataset_hash, dataset_version, evaluation_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                task_id,
                project_id,
                kind,
                json.dumps(criteria, ensure_ascii=False),
                json.dumps(items, ensure_ascii=False),
                json.dumps(gold, ensure_ascii=False) if gold is not None else None,
                dataset_hash,
                dataset_version,
                now,
                now,
            ),
        )
        if kind == "screening_benchmark":
            connection.execute(
                """UPDATE screening_projects
                   SET lock_status = 'locked', lock_reason = ?, updated_at = ?
                   WHERE id = ?""",
                ("基准评估冻结", now, project_id),
            )
    return {
        "task_id": task_id,
        "kind": kind,
        "criteria": criteria,
        "items": items,
        "gold": gold,
        "dataset_hash": dataset_hash,
        "dataset_version": dataset_version,
        "evaluation": None,
    }


def load_screening_snapshot(project_id: str, task_id: str) -> dict[str, Any] | None:
    with _connection() as connection:
        _screening_project(connection, project_id)
        row = connection.execute(
            """SELECT * FROM ai_screening_snapshots
               WHERE project_id = ? AND task_id = ?""",
            (project_id, task_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "task_id": row["task_id"],
        "kind": row["kind"],
        "criteria": json.loads(row["criteria_json"]),
        "items": json.loads(row["items_json"]),
        "gold": json.loads(row["gold_json"]) if row["gold_json"] else None,
        "dataset_hash": row["dataset_hash"],
        "dataset_version": row["dataset_version"],
        "evaluation": (
            json.loads(row["evaluation_json"]) if row["evaluation_json"] else None
        ),
    }


def load_latest_screening_snapshot(
    project_id: str,
    *,
    kind: str,
) -> dict[str, Any] | None:
    if kind not in {"screening", "screening_benchmark"}:
        raise ValueError("AI 筛选任务类型无效。")
    with _connection() as connection:
        _screening_project(connection, project_id)
        row = connection.execute(
            """SELECT * FROM ai_screening_snapshots
               WHERE project_id = ? AND kind = ?
               ORDER BY created_at DESC, task_id DESC LIMIT 1""",
            (project_id, kind),
        ).fetchone()
    if row is None:
        return None
    return {
        "task_id": row["task_id"],
        "kind": row["kind"],
        "criteria": json.loads(row["criteria_json"]),
        "items": json.loads(row["items_json"]),
        "gold": json.loads(row["gold_json"]) if row["gold_json"] else None,
        "dataset_hash": row["dataset_hash"],
        "dataset_version": row["dataset_version"],
        "evaluation": (
            json.loads(row["evaluation_json"]) if row["evaluation_json"] else None
        ),
    }


def save_screening_snapshot_evaluation(
    task_id: str,
    evaluation: dict[str, Any],
) -> None:
    with _connection() as connection:
        cursor = connection.execute(
            """UPDATE ai_screening_snapshots
               SET evaluation_json = ?, updated_at = ?
               WHERE task_id = ?""",
            (
                json.dumps(evaluation, ensure_ascii=False),
                _timestamp(),
                task_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError("筛选快照不存在。")


def append_screening_review_event(
    project_id: str,
    task_id: str,
    candidate_key_value: str,
    *,
    ai_suggestion: str | None,
    review_status: str,
    review_decision: str | None,
    status_before: str | None,
    status_after: str | None,
    note: str = "",
) -> int:
    if review_status not in REVIEW_STATUSES:
        raise ValueError("AI 建议处理状态无效。")
    note = str(note or "").strip()
    if len(note) > 2000:
        raise ValueError("备注不能超过 2000 个字符。")
    with _connection() as connection:
        _screening_project(connection, project_id)
        cursor = connection.execute(
            """INSERT INTO ai_screening_review_events(
                   project_id, task_id, candidate_key, ai_suggestion,
                   review_status, review_decision, status_before, status_after,
                   note, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                task_id,
                candidate_key_value,
                ai_suggestion,
                review_status,
                review_decision,
                status_before,
                status_after,
                note,
                _timestamp(),
            ),
        )
        return int(cursor.lastrowid)


def list_screening_review_events(
    project_id: str,
    *,
    task_id: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 5000))
    with _connection() as connection:
        _screening_project(connection, project_id)
        if task_id:
            rows = connection.execute(
                """SELECT * FROM ai_screening_review_events
                   WHERE project_id = ? AND task_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (project_id, task_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT * FROM ai_screening_review_events
                   WHERE project_id = ? ORDER BY id DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


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
