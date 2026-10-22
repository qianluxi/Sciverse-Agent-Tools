import os
import sqlite3
import stat

import pytest

import research_app.storage as storage


def _use_database(monkeypatch, tmp_path):
    path = tmp_path / "workbench.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: path)
    return path


def test_saved_search_roundtrip_and_delete(monkeypatch, tmp_path):
    _use_database(monkeypatch, tmp_path)
    params = {"query": "气候适应", "authors": ["Hinton"], "page_size": 20}

    search_id = storage.save_search("  Climate review  ", "structured", params)
    saved = storage.list_saved_searches()

    assert len(saved) == 1
    assert saved[0]["id"] == search_id
    assert saved[0]["name"] == "Climate review"
    assert saved[0]["params"] == params
    assert storage.delete_saved_search(search_id) is True
    assert storage.delete_saved_search(search_id) is False


def test_search_history_is_bounded_and_keeps_latest(monkeypatch, tmp_path):
    _use_database(monkeypatch, tmp_path)

    for index in range(storage._MAX_HISTORY_ENTRIES + 5):
        storage.record_search("semantic", {"query": f"question {index}"}, index)

    recent = storage.list_recent_searches(limit=100)

    assert len(recent) == storage._MAX_HISTORY_ENTRIES
    assert recent[0]["params"]["query"] == "question 54"
    assert recent[-1]["params"]["query"] == "question 5"


def test_database_file_is_user_only_on_posix(monkeypatch, tmp_path):
    path = _use_database(monkeypatch, tmp_path)
    if os.name == "nt":
        pytest.skip("POSIX file mode check is not supported on Windows")

    storage.record_search("semantic", {"query": "private research"}, 1)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_search_storage_rejects_credentials(monkeypatch, tmp_path):
    _use_database(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="cannot contain credentials"):
        storage.record_search("structured", {"query": "x", "token": "secret"}, 1)


def test_search_storage_bounds_serialized_params(monkeypatch, tmp_path):
    _use_database(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="16 KiB"):
        storage.save_search("Large", "semantic", {"query": "x" * (17 * 1024)})


def test_newer_schema_version_is_not_overwritten(monkeypatch, tmp_path):
    path = _use_database(monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(RuntimeError, match="newer than supported"):
        storage.list_saved_searches()


def test_saved_search_backup_restores_and_is_idempotent(monkeypatch, tmp_path):
    _use_database(monkeypatch, tmp_path / "source")
    storage.save_search("Topic", "semantic", {"query": "research question"})
    backup = storage.export_saved_searches_backup()

    monkeypatch.setattr(
        storage, "database_path", lambda: tmp_path / "restored" / "workbench.sqlite3"
    )
    assert storage.restore_saved_searches_backup(backup) == 1
    assert storage.list_saved_searches()[0]["name"] == "Topic"
    assert storage.restore_saved_searches_backup(backup) == 0
    assert len(storage.list_saved_searches()) == 1


def test_saved_search_backup_rejects_credentials(monkeypatch, tmp_path):
    _use_database(monkeypatch, tmp_path)
    backup = b'{"format":"sciverse-research-workbench","schema_version":2,"token":"secret"}'

    with pytest.raises(ValueError, match="cannot contain credentials"):
        storage.restore_saved_searches_backup(backup)


def test_v1_saved_search_backup_remains_restorable(monkeypatch, tmp_path):
    _use_database(monkeypatch, tmp_path)
    backup = (
        b'{"format":"sciverse-research-workbench","schema_version":1,'
        b'"saved_searches":[{"id":"legacy","name":"Legacy","mode":"semantic",'
        b'"params":{"query":"old search"},"created_at":"2026-01-01T00:00:00+00:00",'
        b'"updated_at":"2026-01-01T00:00:00+00:00"}]}'
    )

    assert storage.restore_saved_searches_backup(backup) == 1
    assert storage.list_saved_searches()[0]["name"] == "Legacy"
