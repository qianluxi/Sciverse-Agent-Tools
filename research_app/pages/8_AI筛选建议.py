from __future__ import annotations

import hashlib
import json
import os

import streamlit as st

from research_app import storage
from research_app.ai import OpenAICompatibleClient
from research_app.ai_review import quote_status
from research_app.ai_screening import (
    CONFIDENCE_LABELS,
    SCREENING_PROMPT_VERSION,
    SUGGESTION_LABELS,
    suggest_screening,
)
from research_app.ai_settings import default_profile
from research_app.project_ui import project_selector
from research_app.screening import (
    EXCLUSION_REASONS,
    STATUS_LABELS,
    candidate_key,
    identifier_keys,
    paper_id,
)
from research_app.screening_eval import review_summary


st.set_page_config(page_title="AI 筛选建议 · Sciverse", page_icon="S", layout="wide")
st.title("AI 筛选建议")
st.caption(
    "根据项目纳入/排除标准生成可核对建议。AI 只提供建议，"
    "最终状态必须由人工保存，不会自动纳入或排除文献。"
)

if "ai_screening_message" in st.session_state:
    st.success(st.session_state.pop("ai_screening_message"))

_default_profile = default_profile()
st.session_state.setdefault("ai_base_url", _default_profile["base_url"])
st.session_state.setdefault("ai_model", _default_profile["model"])
st.session_state.setdefault("ai_key_session", os.environ.get("OPENAI_API_KEY", ""))

with st.sidebar:
    project_id = project_selector("ai_screening_project_selector")
    st.divider()
    st.markdown("### AI 服务")
    entered_base_url = st.text_input(
        "Base URL",
        value=st.session_state.get("ai_base_url", _default_profile["base_url"]),
        key="ai_screening_base_url_input",
    )
    st.session_state["ai_base_url"] = entered_base_url.strip().rstrip("/")
    entered_key = st.text_input(
        "API Key",
        type="password",
        value=st.session_state.get("ai_key_session", ""),
        key="ai_screening_key_input",
        help="只在当前浏览器会话使用，不写入本地数据库。",
    )
    st.session_state["ai_key_session"] = entered_key
    entered_model = st.text_input(
        "模型名",
        value=st.session_state.get("ai_model", _default_profile["model"]),
        key="ai_screening_model_input",
    )
    st.session_state["ai_model"] = entered_model.strip()

if not project_id:
    st.info("请先在“研究项目”页创建或选择一个项目。")
    st.stop()

project = storage.get_screening_project(project_id)
st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")
st.caption(f"纳入标准：{project['inclusion_criteria'] or '未填写'}")
st.caption(f"排除标准：{project['exclusion_criteria'] or '未填写'}")

base_url = st.session_state["ai_base_url"]
ai_key = st.session_state["ai_key_session"]
model = st.session_state["ai_model"]

all_candidates = storage.list_all_screening_candidates(project_id)
pending_candidates = [
    candidate for candidate in all_candidates if candidate["status"] == "pending"
]
conflict_count = sum(
    1
    for candidate in pending_candidates
    if candidate["candidate_key"].startswith("conflict:")
)
eligible_candidates = [
    candidate
    for candidate in pending_candidates
    if not candidate["candidate_key"].startswith("conflict:")
]
if conflict_count:
    st.warning(f"{conflict_count} 条身份冲突候选未进入 AI 建议流程，请先人工解决冲突。")
if not eligible_candidates:
    st.info("当前没有可生成 AI 建议的待定候选；仍可查看历史建议和审计。")

fallback_papers: list[dict] = []
session_papers = st.session_state.get("papers", [])
latest_run = storage.latest_search_run(project_id)
if latest_run:
    fallback_papers.extend(latest_run.get("papers") or [])
fallback_papers.extend(session_papers)
paper_lookup = {}
for index, paper in enumerate(fallback_papers):
    key = candidate_key(paper, index)
    paper_lookup[key] = paper
    paper_lookup[paper_id(paper, key)] = paper
    for identifier in identifier_keys(paper):
        paper_lookup[identifier] = paper

candidate_map = {candidate["candidate_key"]: candidate for candidate in eligible_candidates}
for candidate in eligible_candidates:
    fallback = (
        paper_lookup.get(candidate["candidate_key"])
        or paper_lookup.get(candidate.get("paper_id") or "")
        or {}
    )
    candidate["resolved_abstract"] = (
        candidate.get("abstract")
        or fallback.get("abstract")
        or fallback.get("chunk")
        or ""
    )

selected_keys = st.multiselect(
    "选择待筛选文献",
    list(candidate_map),
    format_func=lambda value: candidate_map[value].get("title") or value,
    max_selections=30,
    key=f"ai_screening_selected_{project_id}",
)
if len(candidate_map) > 30:
    st.caption(f"单次最多 30 篇；当前有 {len(candidate_map)} 条待定候选。")
missing_abstract = sum(
    1 for key in selected_keys if not candidate_map[key].get("resolved_abstract")
)
if missing_abstract:
    st.caption(f"所选文献中有 {missing_abstract} 篇没有摘要，将仅按标题判断。")

generate_clicked = st.button(
    "生成 AI 筛选建议",
    type="primary",
    disabled=not selected_keys or not ai_key or not base_url or not model,
)
if not ai_key:
    st.caption("请先在侧栏配置 AI API Key。")

if generate_clicked:
    documents = []
    for key in selected_keys:
        candidate = candidate_map[key]
        documents.append(
            {
                "candidate_key": key,
                "paper_id": candidate.get("paper_id") or key,
                "title": candidate.get("title") or "",
                "year": candidate.get("year"),
                "abstract": str(candidate.get("resolved_abstract") or "")[:4000],
            }
        )
    criteria = {
        "question": project["question"],
        "inclusion": project["inclusion_criteria"],
        "exclusion": project["exclusion_criteria"],
    }
    input_hash = hashlib.sha256(
        json.dumps(
            {"criteria": criteria, "candidates": documents},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    task_id = storage.create_ai_task(
        project_id,
        kind="screening",
        model=model,
        prompt_version=SCREENING_PROMPT_VERSION,
        input_hash=input_hash,
    )
    try:
        client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=ai_key,
            model=model,
        )
        with st.spinner("正在按纳入/排除标准生成筛选建议…"):
            result = suggest_screening(client, criteria, documents)
        result_items = result.get("items") or []
        failed_items = [item for item in result_items if item.get("error")]
        if not result_items or len(failed_items) == len(result_items):
            task_status = "failed"
            first_error = next(
                (item.get("error") for item in failed_items if item.get("error")),
                "未返回分析结果",
            )
            task_error = (
                "扫描未返回任何筛选建议。"
                if not result_items
                else f"全部 {len(result_items)} 篇文献分析失败：{first_error}"
            )
        else:
            task_status = "completed"
            task_error = None
        storage.save_screening_suggestions(project_id, task_id, result_items)
        storage.finish_ai_task(task_id, status=task_status, error=task_error)
        st.session_state["ai_screening_message"] = "AI 筛选建议已生成，请逐条人工核对。"
        st.rerun()
    except Exception as error:
        try:
            storage.finish_ai_task(task_id, status="failed", error=str(error))
        except Exception:
            pass
        st.error(str(error))

saved = storage.load_latest_screening_suggestions(project_id)
if not saved:
    st.info("还没有 AI 筛选建议。选择待定文献后生成建议。")
    st.stop()

st.caption(
    f"最近任务：{saved['task']['created_at']} · 模型：{saved['task']['model']} · "
    f"提示词：{saved['task']['prompt_version']}"
)
if saved["task"].get("error"):
    st.error(saved["task"]["error"])
suggestions = saved["suggestions"]
failed_suggestions = [item for item in suggestions if item.get("error")]
if failed_suggestions and len(failed_suggestions) < len(suggestions):
    st.warning(
        f"部分文献建议生成失败：{len(failed_suggestions)}/{len(suggestions)}。"
        f"首个错误：{failed_suggestions[0].get('error')}"
    )

counts = {
    "include": sum(1 for item in suggestions if item.get("suggestion") == "include"),
    "exclude": sum(1 for item in suggestions if item.get("suggestion") == "exclude"),
    "uncertain": sum(1 for item in suggestions if item.get("suggestion") == "uncertain"),
    "pending": sum(1 for item in suggestions if item.get("review_status") == "pending"),
}
metrics = st.columns(4)
metrics[0].metric("建议纳入", counts["include"])
metrics[1].metric("建议排除", counts["exclude"])
metrics[2].metric("需人工判断", counts["uncertain"])
metrics[3].metric("待人工处理", counts["pending"])
review = review_summary(suggestions)
if review["reviewed"]:
    st.caption(
        f"已处理 {review['reviewed']} 条："
        f"采用率 {review['accepted_rate']:.1%}，"
        f"改判率 {review['modified_rate']:.1%}，"
        f"拒绝率 {review['rejected_rate']:.1%}。"
    )

review_labels = {
    "pending": "待处理",
    "accepted": "已采用",
    "modified": "已改判",
    "rejected": "已拒绝",
}
status_options = ["pending", "included", "excluded"]
for item in suggestions:
    key = item["candidate_key"]
    candidate = next(
        (value for value in all_candidates if value["candidate_key"] == key),
        None,
    )
    with st.container(border=True):
        st.markdown(f"#### {item.get('title') or '（无标题）'}")
        if candidate:
            st.caption(
                f"当前项目状态：{STATUS_LABELS.get(candidate['status'], candidate['status'])}"
            )
        else:
            st.error("该候选已不在项目中，无法保存人工决定。")
            continue
        if item.get("error"):
            st.error(item["error"])
            continue
        suggestion = item.get("suggestion") or "uncertain"
        confidence = CONFIDENCE_LABELS.get(
            str(item.get("confidence") or ""), str(item.get("confidence") or "")
        )
        st.markdown(
            f"**AI 建议：{SUGGESTION_LABELS.get(suggestion, suggestion)}**"
            f" · 置信度：{confidence or '—'}"
        )
        if item.get("criteria"):
            st.caption("命中标准：" + "；".join(str(value) for value in item["criteria"]))
        if item.get("rationale"):
            st.write(item["rationale"])
        quotes = item.get("evidence_quotes") or []
        checks = item.get("quote_checks") or []
        if quotes:
            st.caption("原文依据：")
            for index, quote in enumerate(quotes):
                check = checks[index] if index < len(checks) else {}
                st.markdown(f"> {quote.get('quote', '')}")
                st.caption(
                    f"{quote.get('section') or '位置未注明'} · {quote_status(check)}"
                )
        st.caption(f"人工处理状态：{review_labels.get(item.get('review_status'), '待处理')}")
        if item.get("review_note"):
            st.caption(f"上次处理备注：{item['review_note']}")
        st.caption("选择“待定”并保存表示拒绝该建议，候选状态不会改变。")

        with st.form(f"ai_screening_review_{saved['task']['id']}_{key}"):
            current_index = (
                status_options.index(candidate["status"])
                if candidate["status"] in status_options
                else 0
            )
            decision = st.radio(
                "人工最终决定",
                status_options,
                index=current_index,
                format_func=lambda value: STATUS_LABELS.get(value, value),
                horizontal=True,
                key=f"ai_screening_decision_{saved['task']['id']}_{key}",
            )
            reason_index = (
                EXCLUSION_REASONS.index(candidate["exclusion_reason"])
                if candidate.get("exclusion_reason") in EXCLUSION_REASONS
                else 0
            )
            reason = st.selectbox(
                "排除原因（仅排除时使用）",
                EXCLUSION_REASONS,
                index=reason_index,
                key=f"ai_screening_reason_{saved['task']['id']}_{key}",
            )
            note = st.text_area(
                "人工备注",
                value=item.get("review_note") or "",
                height=70,
                key=f"ai_screening_note_{saved['task']['id']}_{key}",
            )
            if st.form_submit_button("保存人工决定"):
                try:
                    expected = {
                        "include": "included",
                        "exclude": "excluded",
                    }.get(suggestion)
                    if decision == "pending":
                        review_status = "rejected"
                        review_decision = None
                    else:
                        review_status = (
                            "accepted" if decision == expected else "modified"
                        )
                        review_decision = decision
                        storage.set_screening_decision(
                            project_id,
                            key,
                            decision,
                            reason=reason if decision == "excluded" else None,
                            notes=note,
                            ai_task_id=saved["task"]["id"],
                            ai_action=review_status,
                        )
                    storage.review_screening_suggestion(
                        project_id,
                        saved["task"]["id"],
                        key,
                        review_status,
                        review_decision=review_decision,
                        note=note,
                    )
                    st.session_state["ai_screening_message"] = "人工决定已保存。"
                    st.rerun()
                except Exception as error:
                    st.error(str(error))

st.markdown("### 建议与人工决定审计")
audit_rows = [
    {
        "时间": item.get("reviewed_at") or "",
        "文献": item.get("title") or "",
        "AI 建议": SUGGESTION_LABELS.get(item.get("suggestion"), item.get("suggestion")),
        "AI 置信度": CONFIDENCE_LABELS.get(
            str(item.get("confidence") or ""), str(item.get("confidence") or "")
        ),
        "处理状态": review_labels.get(item.get("review_status"), "待处理"),
        "人工决定": STATUS_LABELS.get(
            item.get("review_decision") or "", item.get("review_decision") or ""
        ),
        "备注": item.get("review_note") or "",
    }
    for item in suggestions
]
st.dataframe(audit_rows, hide_index=True, use_container_width=True)

st.download_button(
    "下载 AI 筛选建议 JSON",
    data=(
        json.dumps(
            {"task": saved["task"], "suggestions": suggestions},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8"),
    file_name="sciverse-ai-screening.json",
    mime="application/json",
)
