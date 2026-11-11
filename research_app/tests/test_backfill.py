import json

import pytest

from research_app import backfill, storage


def _corpus_record(**overrides):
    record = {
        "unique_id": "paper:10.1/a",
        "doi": "10.1/A",
        "title": "地下车库埋深对造价的影响",
        "author": [{"name": "张三"}, {"name": "李四"}],
        "publication_venue_unified": "",
        "publication_venue_name_unified": "建筑结构",
        "publication_published_year": 2020.0,
        "citation_count": 7.0,
        "keywords": ["埋深"],
        "language": "zh",
    }
    record.update(overrides)
    return record


def _legacy_paper(**overrides):
    paper = {
        "unique_id": "paper:10.1/a",
        "doi": "10.1/A",
        "title": "地下车库埋深对造价的影响",
        "year": 2020.0,
        "abstract": "摘要",
        "doc_id": None,
    }
    paper.update(overrides)
    return paper


def _project_with_snapshot(papers, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "database_path", lambda: tmp_path / "workbench.sqlite3")
    project_id = storage.create_screening_project("项目", "问题", "I1", "E1")
    storage.save_search_run(
        project_id,
        "structured",
        {"query": "地下车库"},
        [
            {
                "title": p.get("title", ""),
                "doi": p.get("doi"),
                "unique_id": p.get("unique_id"),
            }
            for p in papers
        ],
        len(papers),
    )
    # 模拟旧版快照：只留 6 个字段。
    with storage._connection() as connection:  # noqa: SLF001 - 测试需要构造旧格式
        connection.execute(
            "UPDATE search_runs SET snapshot_json = ? WHERE project_id = ?",
            (json.dumps(papers, ensure_ascii=False), project_id),
        )
    return project_id


def test_merge_only_fills_blanks():
    paper = _legacy_paper(year=1999.0)
    record = _corpus_record()

    assert backfill.merge_bibliography(paper, record) is True
    assert paper["author"][0]["name"] == "张三"
    assert paper["publication_venue_name_unified"] == "建筑结构"
    # 已有值不被覆盖。
    assert paper["year"] == 1999.0
    # 再次合并没有任何改动，保证幂等。
    assert backfill.merge_bibliography(paper, record) is False


def test_zero_citation_count_is_treated_as_present():
    paper = _legacy_paper(citation_count=0.0)
    paper["author"] = [{"name": "原有作者"}]
    record = _corpus_record(citation_count=99.0)

    backfill.merge_bibliography(paper, record)

    # 0 是有效值，不能被 corpus 的 99 覆盖。
    assert paper["citation_count"] == 0.0
    assert paper["author"] == [{"name": "原有作者"}]


def test_published_year_alone_is_not_a_gap():
    """新检索路径直接落 publication_published_year，不该被误判成缺 year。"""
    record = _corpus_record()
    paper = dict(record)  # 与 corpus 同源的完整记录，只有 publication_published_year

    assert "year" not in paper
    assert backfill.merge_bibliography(paper, record) is False
    assert "year" not in paper


def test_lookup_prefers_identifier_then_title():
    index = backfill.build_corpus_index([_corpus_record()])

    by_uid = backfill.find_record({"unique_id": "paper:10.1/a", "title": "别的题名"}, index)
    by_doi = backfill.find_record({"doi": "https://doi.org/10.1/a"}, index)
    by_title = backfill.find_record({"title": "地下车库埋深对造价的影响"}, index)
    missing = backfill.find_record({"title": "无关论文"}, index)

    assert by_uid is not None and by_doi is not None and by_title is not None
    assert missing is None


def test_ambiguous_title_is_not_used():
    first = _corpus_record(unique_id="paper:x", doi="10.1/x", title="同名论文")
    second = _corpus_record(unique_id="paper:y", doi="10.1/y", title="同名 论文")
    index = backfill.build_corpus_index([first, second])

    assert backfill.find_record({"title": "同名论文"}, index) is None
    # DOI 仍然可以精确定位。
    assert backfill.find_record({"doi": "10.1/y"}, index)["unique_id"] == "paper:y"


def test_load_corpus_accepts_wrapped_payload(tmp_path):
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps({"records": [_corpus_record()]}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert len(backfill.load_corpus(path)) == 1


def test_load_corpus_rejects_unknown_shape(tmp_path):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({"oops": 1}), encoding="utf-8")

    with pytest.raises(ValueError):
        backfill.load_corpus(path)


def test_backfill_writes_into_snapshot(monkeypatch, tmp_path):
    project_id = _project_with_snapshot([_legacy_paper()], monkeypatch, tmp_path)
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps([_corpus_record()], ensure_ascii=False), encoding="utf-8")

    report = backfill.backfill_bibliography(corpus_path=corpus_path, project_id=project_id)

    assert report["papers_changed"] == 1
    assert report["runs_updated"] == 1
    assert report["papers_unmatched"] == 0
    papers = storage.project_papers(project_id)
    assert papers[0]["author"][0]["name"] == "张三"
    assert papers[0]["publication_venue_name_unified"] == "建筑结构"
    # 检索身份字段保持原样。
    assert papers[0]["abstract"] == "摘要"

    again = backfill.backfill_bibliography(corpus_path=corpus_path, project_id=project_id)
    assert again["papers_changed"] == 0
    assert again["runs_updated"] == 0


def test_backfill_dry_run_does_not_write(monkeypatch, tmp_path):
    project_id = _project_with_snapshot([_legacy_paper()], monkeypatch, tmp_path)
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps([_corpus_record()], ensure_ascii=False), encoding="utf-8")

    report = backfill.backfill_bibliography(
        corpus_path=corpus_path, project_id=project_id, dry_run=True
    )

    assert report["papers_changed"] == 1
    assert report["runs_updated"] == 0
    assert "author" not in storage.project_papers(project_id)[0]


def test_backfill_counts_unmatched(monkeypatch, tmp_path):
    project_id = _project_with_snapshot(
        [_legacy_paper(unique_id="paper:zzz", doi="10.9/zzz", title="查不到的论文")],
        monkeypatch,
        tmp_path,
    )
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps([_corpus_record()], ensure_ascii=False), encoding="utf-8")

    report = backfill.backfill_bibliography(corpus_path=corpus_path, project_id=project_id)

    assert report["papers_unmatched"] == 1
    assert report["papers_changed"] == 0
