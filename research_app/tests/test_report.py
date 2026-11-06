import hashlib
import io
import json
import sqlite3
import zipfile

import pytest

import research_app.report as report_module
from research_app import storage
from research_app.ai_review import export_matrix_versions_csv
from research_app.report import (
    build_project_report,
    export_prisma_records_csv,
    export_project_bundle,
    export_project_report_json,
    render_project_report_markdown,
)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")


def _setup(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    papers = [
        {"title": "Paper A", "unique_id": "paper:a", "abstract": "a"},
        {"title": "Paper B", "unique_id": "paper:b", "abstract": "b"},
    ]
    run = storage.save_search_run(
        project_id, "structured", {"query": "q"}, papers, len(papers)
    )
    storage.add_screening_candidates(
        project_id,
        papers,
        source_run_id=run["id"],
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    storage.set_screening_decision(project_id, "uid:paper:b", "excluded", reason="主题不相关")
    storage.set_fulltext_decision(project_id, "uid:paper:a", "included")

    task_id = storage.create_ai_task(
        project_id,
        kind="evidence_matrix",
        model="test-model",
        prompt_version="evidence-v1",
        input_hash="report-hash",
    )
    storage.append_evidence_version(
        project_id,
        "uid:paper:a",
        task_id,
        source="ai_raw",
        data={
            "research_question": "Q",
            "methods": "M",
            "sample": "S",
            "findings": "F",
            "limitations": "L",
            "evidence_quotes": [],
        },
        quote_checks=[{"verified": True}],
        model="test-model",
        prompt_version="evidence-v1",
        title="Paper A",
        year=2024,
    )
    storage.append_evidence_version(
        project_id,
        "uid:paper:a",
        task_id,
        source="human_corrected",
        data={
            "research_question": "Q",
            "methods": "M",
            "sample": "S",
            "findings": "HUMAN findings",
            "limitations": "L",
            "evidence_quotes": [],
        },
        reviewer_note="human edit",
        model="test-model",
        prompt_version="evidence-v1",
        title="Paper A",
        year=2024,
    )
    storage.append_evidence_version(
        project_id,
        "uid:paper:a",
        task_id,
        source="final_adopted",
        data={
            "research_question": "Q",
            "methods": "M",
            "sample": "S",
            "findings": "FINAL findings",
            "limitations": "L",
            "evidence_quotes": [],
        },
        model="test-model",
        prompt_version="evidence-v1",
        title="Paper A",
        year=2024,
    )
    return project_id


def test_build_project_report_contains_prisma_and_evidence(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)

    report = build_project_report(project_id)

    assert report["format"] == "sciverse-project-report"
    assert report["prisma"]["included_in_review"] == 1
    assert report["prisma"]["title_abstract"]["included"] == 1
    assert report["evidence_matrix"]["papers"] == 1
    assert report["evidence_matrix"]["coverage"]["ai_raw"] == 1
    assert report["evidence_matrix"]["coverage"]["human_corrected"] == 1
    assert report["evidence_matrix"]["coverage"]["final_adopted"] == 1
    assert report["evidence_matrix"]["quote_checks_scope"] == "ai_raw"
    assert report["evidence_matrix"]["quote_checks"] == {"total": 1, "verified": 1}
    assert report["analysis"]["trends"]["status"] == "generated"
    assert report["analysis"]["citation_network"]["status"] == "not_generated"

    markdown = render_project_report_markdown(report).decode("utf-8")
    assert "PRISMA 2020" in markdown
    assert "最终纳入报告数" in markdown
    assert "最终纳入研究数" in markdown
    assert "证据矩阵" in markdown
    assert "趋势分析" in markdown
    assert "引用网络" in markdown
    assert "统计口径" in markdown
    assert "溯源" in markdown

    payload = json.loads(export_project_report_json(report))
    assert payload["project"]["id"] == project_id
    assert payload["prisma"]["included_in_review"] == 1


def test_project_bundle_contains_manifest_and_files(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)

    data = export_project_bundle(project_id)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert {
            "report.md",
            "report.json",
            "prisma_records.csv",
            "search_runs.csv",
            "candidate_sources.csv",
            "screening_audit.csv",
            "fulltext_audit.csv",
            "evidence_matrix.csv",
            "evidence_matrix_versions.csv",
            "manifest.json",
        } <= names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["project_id"] == project_id
        assert manifest["snapshot"] is True
        for entry in manifest["files"]:
            content = archive.read(entry["name"])
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
            assert len(content) == entry["bytes"]

        report = json.loads(archive.read("report.json"))
        prisma_lines = archive.read("prisma_records.csv").decode(
            "utf-8-sig"
        ).strip().splitlines()
        assert len(prisma_lines) - 1 == report["prisma"]["unique_records"]


def test_override_database_path_is_thread_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("SCIVERSE_RESEARCH_DB", str(tmp_path / "a.sqlite3"))
    project_a = storage.create_screening_project("A", "Q", "I", "E")

    with storage.override_database_path(tmp_path / "b.sqlite3"):
        project_b = storage.create_screening_project("B", "Q", "I", "E")
        assert storage.get_screening_project(project_b)["name"] == "B"

    assert storage.get_screening_project(project_a)["name"] == "A"
    with pytest.raises(ValueError):
        storage.get_screening_project(project_b)


def test_bundle_uses_consistent_database_snapshot(monkeypatch, tmp_path):
    db_path = tmp_path / "workbench.sqlite3"
    monkeypatch.setenv("SCIVERSE_RESEARCH_DB", str(db_path))
    project_id = storage.create_screening_project("Review", "Q", "I", "E")
    storage.add_screening_candidates(
        project_id, [{"title": "Paper A", "unique_id": "paper:a"}]
    )
    storage.set_screening_decision(project_id, "uid:paper:a", "included")
    storage.set_fulltext_decision(project_id, "uid:paper:a", "included")
    before = storage.prisma_counts(project_id)["unique_records"]

    original = report_module.build_project_report

    def wrapper(pid):
        result = original(pid)
        # Simulate a concurrent change to the *source* database after the
        # snapshot was taken; bundle files must still reflect the snapshot.
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """INSERT INTO screening_candidates(
                       project_id, candidate_key, paper_id, title, status,
                       notes, created_at, updated_at, abstract)
                   VALUES (?, 'uid:extra', 'paper:extra', 'Extra', 'pending',
                           '', 't', 't', '')""",
                (pid,),
            )
        return result

    monkeypatch.setattr(report_module, "build_project_report", wrapper)
    data = report_module.export_project_bundle(project_id)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        report_payload = json.loads(archive.read("report.json"))
        prisma_lines = archive.read("prisma_records.csv").decode(
            "utf-8-sig"
        ).strip().splitlines()
        assert report_payload["prisma"]["unique_records"] == before
        assert len(prisma_lines) - 1 == before

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM screening_candidates WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == before + 1


def test_matrix_versions_csv_exports_all_sources(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)
    matrix = storage.load_evidence_matrix(project_id)

    csv_text = export_matrix_versions_csv(matrix).decode("utf-8-sig")
    rows = [line for line in csv_text.splitlines() if "uid:paper:a" in line]

    assert len(rows) == 3
    assert "AI 原始" in csv_text
    assert "人工校正" in csv_text
    assert "最终采用" in csv_text
    assert "FINAL findings" in csv_text
    assert "HUMAN findings" in csv_text
    assert "human edit" in csv_text


def test_prisma_records_csv_includes_fulltext_status(monkeypatch, tmp_path):
    project_id = _setup(monkeypatch, tmp_path)

    csv_text = export_prisma_records_csv(project_id).decode("utf-8-sig")

    assert "candidate_key" in csv_text
    assert "source_run_ids" in csv_text
    assert "title_abstract_status" in csv_text
    assert "latest_screening_audit_at" in csv_text
    assert "fulltext_status" in csv_text
    assert "latest_fulltext_audit_at" in csv_text
    assert "ai_review_status" in csv_text
    assert "uid:paper:a" in csv_text
    assert "全文纳入" in csv_text
