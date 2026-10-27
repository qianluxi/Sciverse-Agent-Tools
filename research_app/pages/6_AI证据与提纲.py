from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import streamlit as st
from sciverse.credentials import resolve_token

from research_app import storage
from research_app.project_ui import project_selector

from research_app.ai import (
    AIError,
    EVIDENCE_PROMPT_VERSION,
    OUTLINE_PROMPT_VERSION,
    OpenAICompatibleClient,
    extract_evidence,
    generate_outline,
)
from research_app.ai_review import (
    FIELD_KEYS,
    FIELD_LABELS,
    export_evidence_csv,
    export_evidence_markdown,
    normalize_evidence_data,
    quote_status,
)
from research_app.ai_settings import (
    DEFAULT_BASE_URL,
    default_profile,
    list_ai_profiles,
    record_ai_profile,
)
from research_app.service import call_api


st.set_page_config(page_title="AI 证据与提纲 · Sciverse", page_icon="S", layout="wide")

st.title("AI 证据与提纲")
st.caption(
    "用你自备的 OpenAI 兼容模型从论文中提取结构化证据，再生成可人工复核的综述提纲。"
    "AI 输出不会自动写入 Zotero 或最终论文。"
)
ai_session_id = st.session_state.setdefault("ai_session_id", uuid.uuid4().hex)

_default_profile = default_profile()
st.session_state.setdefault("ai_base_url", _default_profile["base_url"])
st.session_state.setdefault("ai_model", _default_profile["model"])
st.session_state.setdefault("ai_key", os.environ.get("OPENAI_API_KEY", ""))

_profiles = list_ai_profiles()
_base_url_options = [profile["base_url"] for profile in _profiles]
if DEFAULT_BASE_URL not in _base_url_options:
    _base_url_options.insert(0, DEFAULT_BASE_URL)
_base_url_options.append("自定义 URL")
_current_base_url = st.session_state.get("ai_base_url", DEFAULT_BASE_URL)
if _current_base_url not in _base_url_options:
    _base_url_options.append(_current_base_url)


def _on_base_url_change() -> None:
    selected = st.session_state.get("ai_base_url_select")
    for profile in _profiles:
        if profile["base_url"] == selected and profile.get("model"):
            st.session_state["ai_model"] = profile["model"]
            break


with st.sidebar:
    st.markdown("### Sciverse")
    sciverse_token = st.text_input("Sciverse API Token", type="password", placeholder="sci_...")
    sciverse_token = sciverse_token.strip() or resolve_token()
    st.divider()
    project_id = project_selector("ai_project_selector")
    st.divider()
    st.markdown("### AI 服务（OpenAI 兼容）")
    selected_base_url = st.selectbox(
        "Base URL",
        _base_url_options,
        index=_base_url_options.index(_current_base_url),
        key="ai_base_url_select",
        on_change=_on_base_url_change,
    )
    if selected_base_url == "自定义 URL":
        base_url = st.text_input(
            "自定义 Base URL",
            key="ai_custom_base_url",
            placeholder="https://api.example.com/v1",
        ).strip()
    else:
        base_url = selected_base_url
        st.session_state["ai_base_url"] = base_url
    ai_key = st.text_input(
        "API Key",
        type="password",
        key="ai_key",
        help="只在当前浏览器会话使用，不写入本地数据库；切换页面不会丢失。",
    )
    model = st.text_input(
        "模型名",
        key="ai_model",
        help="OpenCode Go 推荐 glm-5.3-flash 等非推理模型；推理模型可能耗尽输出预算。",
    )
    st.caption("Base URL 和模型会记录在本机；API Key 只在当前浏览器会话保留。")
    if st.button(
        "测试 AI 连接",
        disabled=not base_url or not ai_key or not model,
    ):
        try:
            test_client = OpenAICompatibleClient(
                base_url=base_url,
                api_key=ai_key,
                model=model,
                session_id=ai_session_id,
            )
            record_ai_profile(base_url, model)
            reply = test_client.chat(
                [{"role": "user", "content": "只回复 OK"}],
                max_tokens=16,
            )
            st.success(f"连接成功：{reply[:80]}")
        except Exception as error:
            st.error(str(error))


if project_id and st.session_state.get("ai_loaded_project") != project_id:
    for key in (
        "ai_evidence",
        "ai_task_id",
        "ai_warnings",
        "ai_outline",
        "ai_editor_epoch",
    ):
        st.session_state.pop(key, None)
    st.session_state["ai_loaded_project"] = project_id
    try:
        saved = storage.load_latest_evidence(project_id)
    except Exception:
        saved = None
    if saved and saved["items"]:
        st.session_state["ai_evidence"] = saved["items"]
        st.session_state["ai_task_id"] = saved["task"]["id"]
        st.session_state["ai_warnings"] = []
        st.session_state["ai_outline"] = ""
        st.session_state["ai_editor_epoch"] = uuid.uuid4().hex


def _show_error(error: Exception) -> None:
    st.error(str(error))


def _paper_text(paper: dict[str, Any], warnings: list[str]) -> str:
    doc_id = paper.get("doc_id")
    if doc_id and sciverse_token:
        try:
            content = call_api(
                sciverse_token,
                "read_content",
                doc_id=doc_id,
                offset=0,
                limit=6000,
            )
            text = str(content.get("text") or "").strip()
            if text:
                return text
        except Exception as error:
            warnings.append(f"{paper.get('title') or doc_id}：读取全文失败，改用摘要（{error}）")
    text = str(paper.get("abstract") or paper.get("chunk") or "").strip()
    if text:
        return text
    warnings.append(f"{paper.get('title') or paper.get('unique_id')}：没有可用文本，已跳过")
    return ""


def _edited_evidence_item(
    item: dict[str, Any],
    epoch: str,
) -> dict[str, Any]:
    paper_id = str(item.get("paper_id") or "paper")
    title = item.get("title") or "（无标题）"
    key_prefix = f"ai_{epoch}_{paper_id}"
    with st.container(border=True):
        st.markdown(f"#### {title}")
        st.caption(
            f"来源：`{paper_id}` · 年份：{item.get('year') or '未知'} · "
            f"模型：{item.get('model') or '未知'} · "
            f"证据提示词：{item.get('prompt_version') or '未知'}"
        )
        if item.get("error"):
            st.error(item["error"])
            return item

        data = normalize_evidence_data(item.get("data"))
        original_quotes = data.get("evidence_quotes") or []
        original_checks = item.get("quote_checks") or []
        edited_data: dict[str, Any] = {}
        for field in FIELD_KEYS:
            edited_data[field] = st.text_area(
                FIELD_LABELS[field],
                value=str(data.get(field) or ""),
                height=68,
                key=f"{key_prefix}_{field}",
            )

        edited_quotes = []
        edited_checks = []
        for index, quote in enumerate(original_quotes):
            quote_text = str(quote.get("quote") or "")
            section = str(quote.get("section") or "")
            check = original_checks[index] if index < len(original_checks) else {}
            quote_column, section_column = st.columns([4, 1])
            with quote_column:
                edited_quote = st.text_area(
                    f"原文引句 {index + 1}",
                    value=quote_text,
                    height=68,
                    key=f"{key_prefix}_quote_{index}",
                )
            with section_column:
                edited_section = st.text_input(
                    "位置",
                    value=section,
                    key=f"{key_prefix}_section_{index}",
                )
            if edited_quote == quote_text and edited_section == section:
                edited_check = {
                    "quote": edited_quote,
                    "section": edited_section,
                    "verified": check.get("verified"),
                }
            else:
                edited_check = {
                    "quote": edited_quote,
                    "section": edited_section,
                    "verified": None,
                }
            edited_quotes.append({"quote": edited_quote, "section": edited_section})
            edited_checks.append(edited_check)
            status = quote_status(edited_check)
            if edited_check["verified"] is True:
                st.caption(f"引句 {index + 1} 自动校验：{status}")
            else:
                st.warning(f"引句 {index + 1} 自动校验：{status}")

        extra_quote = st.text_area(
            "人工补充原文引句（可选）",
            height=68,
            key=f"{key_prefix}_quote_extra",
            placeholder="把论文原文中的一句话粘贴到这里；留空则不添加。",
        ).strip()
        if extra_quote:
            extra_section = st.text_input(
                "补充引句位置",
                key=f"{key_prefix}_section_extra",
                placeholder="例如：Results, p. 5",
            ).strip()
            edited_quotes.append({"quote": extra_quote, "section": extra_section})
            edited_checks.append(
                {"quote": extra_quote, "section": extra_section, "verified": None}
            )
            st.caption("人工补充引句：未自动校验，导出时标记为人工修改。")

        if not original_quotes and not extra_quote:
            st.warning("AI 未提供原文引句，暂时无法自动核对；建议人工补充引句。")

        edited_data["evidence_quotes"] = edited_quotes
        reviewed = st.checkbox("我已核对这篇文献的 AI 证据", key=f"{key_prefix}_reviewed")
        reviewer_note = st.text_area(
            "核对备注",
            key=f"{key_prefix}_note",
            height=68,
            placeholder="可记录修改原因、存疑点或与原文不一致之处。",
        )
        return {
            **item,
            "data": edited_data,
            "raw_data": item.get("raw_data") or item.get("data"),
            "quote_checks": edited_checks,
            "reviewed": reviewed,
            "reviewer_note": reviewer_note,
        }


papers = st.session_state.get("papers", [])
if not papers and not st.session_state.get("ai_evidence"):
    st.info("主页还没有检索结果。请先检索，再回到此页提取证据。")
    st.stop()
if not papers:
    st.info("当前会话没有检索结果；下面显示的是已保存到项目的证据。")

paper_map: dict[str, dict[str, Any]] = {}
for paper in papers:
    key = str(paper.get("unique_id") or paper.get("doc_id") or paper.get("title"))
    if paper.get("unique_id") or paper.get("doc_id") or paper.get("abstract") or paper.get("chunk"):
        paper_map[key] = paper

st.markdown("### 选择论文")
st.caption("单次最多选择 5 篇；优先读取全文前 6000 字，失败时使用摘要。")
selected_ids = st.multiselect(
    "论文",
    list(paper_map),
    format_func=lambda value: paper_map[value].get("title") or value,
    max_selections=5,
)
extract_clicked = st.button(
    "提取结构化证据",
    type="primary",
    disabled=not selected_ids or not ai_key or not model or not base_url,
)

if extract_clicked:
    warnings: list[str] = []
    documents = []
    for paper_id in selected_ids:
        paper = paper_map[paper_id]
        text = _paper_text(paper, warnings)
        if not text:
            continue
        documents.append(
            {
                "paper_id": paper.get("unique_id") or paper.get("doc_id") or paper_id,
                "title": paper.get("title") or "",
                "year": paper.get("publication_published_year") or "",
                "text": text[:6000],
            }
        )
    task_id = None
    if project_id:
        input_hash = hashlib.sha256(
            json.dumps(documents, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        task_id = storage.create_ai_task(
            project_id,
            kind="evidence",
            model=model,
            prompt_version=EVIDENCE_PROMPT_VERSION,
            input_hash=input_hash,
        )
    try:
        client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=ai_key,
            model=model,
            session_id=ai_session_id,
        )
        record_ai_profile(base_url, model)
        with st.spinner("正在调用 AI 提取证据…"):
            evidence = extract_evidence(client, documents)
        if task_id:
            storage.finish_ai_task(task_id, status="completed")
        st.session_state["ai_evidence"] = evidence
        st.session_state["ai_task_id"] = task_id
        st.session_state["ai_warnings"] = warnings
        st.session_state["ai_outline"] = ""
        st.session_state["ai_editor_epoch"] = uuid.uuid4().hex
    except Exception as error:
        if task_id:
            try:
                storage.finish_ai_task(task_id, status="failed", error=str(error))
            except Exception:
                pass
        _show_error(error)

warnings = st.session_state.get("ai_warnings", [])
for warning in warnings:
    st.warning(warning)

evidence = st.session_state.get("ai_evidence", [])
if evidence:
    editor_epoch = st.session_state.setdefault("ai_editor_epoch", uuid.uuid4().hex)
    st.markdown("### 文献证据（可编辑）")
    st.caption(
        "AI 结果不会自动保存为最终结论；请核对每条引句是否确实来自原文。"
        + (
            "编辑和核对状态会保存到当前研究项目。"
            if project_id and st.session_state.get("ai_task_id")
            else "当前未关联研究项目，编辑仅保留在当前浏览器会话。"
        )
    )
    reviewed_evidence = []
    for item in evidence:
        reviewed_evidence.append(_edited_evidence_item(item, editor_epoch))
    if project_id and st.session_state.get("ai_task_id") and reviewed_evidence:
        try:
            storage.save_evidence_items(
                project_id,
                st.session_state["ai_task_id"],
                reviewed_evidence,
            )
            st.caption("证据版本已保存到当前研究项目。")
        except Exception as error:
            st.warning(f"证据已生成，但保存到项目失败：{error}")

    total_quotes = sum(
        len(item.get("quote_checks") or []) for item in reviewed_evidence if not item.get("error")
    )
    verified_quotes = sum(
        1
        for item in reviewed_evidence
        for check in item.get("quote_checks") or []
        if check.get("verified") is True
    )
    reviewed_count = sum(1 for item in reviewed_evidence if item.get("reviewed"))
    error_count = sum(1 for item in reviewed_evidence if item.get("error"))
    metric_columns = st.columns(3)
    metric_columns[0].metric("文献", len(reviewed_evidence))
    metric_columns[1].metric("已核对", f"{reviewed_count}/{len(reviewed_evidence)}")
    metric_columns[2].metric("引句自动匹配", f"{verified_quotes}/{total_quotes}")
    if error_count:
        st.warning(f"有 {error_count} 篇文献提取失败，未阻塞其他结果。")
    if total_quotes and verified_quotes < total_quotes:
        st.warning("存在未匹配的原文引句，请人工核对；修改后的引句会标记为人工校验。")
    st.progress(reviewed_count / len(reviewed_evidence))

    download_columns = st.columns(2)
    with download_columns[0]:
        st.download_button(
            "下载证据表 CSV",
            data=export_evidence_csv(reviewed_evidence),
            file_name="sciverse-ai-evidence.csv",
            mime="text/csv",
            key="download_ai_evidence_csv",
        )
    with download_columns[1]:
        st.download_button(
            "下载证据表 Markdown",
            data=export_evidence_markdown(reviewed_evidence),
            file_name="sciverse-ai-evidence.md",
            mime="text/markdown",
            key="download_ai_evidence_markdown",
        )

    st.markdown("### 综述提纲")
    default_question = str(st.session_state.get("search_params", {}).get("query") or "")
    question = st.text_area(
        "综述问题",
        value=default_question,
        placeholder="例如：现有研究如何评估该干预的效果？",
        height=80,
    )
    st.caption(
        f"生成模型：{model or '未知'} · 提纲提示词：{OUTLINE_PROMPT_VERSION} · "
        f"证据提示词：{EVIDENCE_PROMPT_VERSION}"
    )
    if st.button("生成综述提纲", type="primary"):
        try:
            client = OpenAICompatibleClient(
                base_url=base_url,
                api_key=ai_key,
                model=model,
                session_id=ai_session_id,
            )
            record_ai_profile(base_url, model)
            with st.spinner("正在生成提纲…"):
                outline = generate_outline(client, question, reviewed_evidence)
            st.session_state["ai_outline"] = outline
        except AIError as error:
            _show_error(error)
        except Exception as error:
            _show_error(error)

    outline = st.session_state.get("ai_outline", "")
    if outline:
        st.markdown(outline)
        outline_header = (
            f"<!-- model: {model} | prompt_version: {OUTLINE_PROMPT_VERSION} | "
            f"generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->\n\n"
        )
        st.download_button(
            "下载提纲 Markdown",
            data=(outline_header + outline).encode("utf-8"),
            file_name="sciverse-review-outline.md",
            mime="text/markdown",
            key="download_outline",
        )
        st.caption("提纲为 AI 辅助草稿，不包含最终论文正文；请逐条核对来源后再使用。")

    if st.button("清空 AI 结果"):
        st.session_state.pop("ai_evidence", None)
        st.session_state.pop("ai_warnings", None)
        st.session_state.pop("ai_outline", None)
        st.session_state.pop("ai_editor_epoch", None)
        st.session_state.pop("ai_task_id", None)
        st.rerun()
