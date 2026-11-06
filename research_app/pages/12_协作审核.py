from __future__ import annotations

import streamlit as st

from research_app import storage
from research_app.project_ui import project_selector
from research_app.screening import EXCLUSION_REASONS, STATUS_LABELS


ROLE_LABELS = {
    "reviewer": "审核员",
    "coordinator": "协调者",
    "viewer": "只读",
}
STATE_LABELS = {
    "unreviewed": "未提交",
    "awaiting": "等待其他审核人",
    "single": "仅一人提交",
    "agreed": "一致",
    "conflict": "冲突",
}

st.set_page_config(page_title="协作审核 · Sciverse", page_icon="S", layout="wide")
st.title("协作审核")
st.caption(
    "双人盲筛与冲突解决：审核员独立提交决定，协调者查看对比并做最终确认。"
    "AI 不会参与本页决定，所有操作保留审计。"
)

if "collab_message" in st.session_state:
    st.success(st.session_state.pop("collab_message"))
if "collab_error" in st.session_state:
    st.error(st.session_state.pop("collab_error"))

reviewers = storage.list_reviewers()

with st.sidebar:
    project_id = project_selector("collab_project_selector")
    st.divider()
    st.markdown("### 审核人")
    current_id = None
    if reviewers:
        current_id = st.selectbox(
            "当前审核人",
            [item["id"] for item in reviewers],
            format_func=lambda value: next(
                f"{item['name']} · {ROLE_LABELS.get(item['role'], item['role'])}"
                for item in reviewers
                if item["id"] == value
            ),
            key="collab_current_reviewer",
        )
    else:
        st.info("还没有审核人，请先创建。")

    current = storage.get_reviewer(current_id) if current_id else None
    can_create = not reviewers or (current and current["role"] == "coordinator")
    with st.expander("新建审核人"):
        if not can_create:
            st.caption("只有协调者可以创建审核人。")
        else:
            name = st.text_input("姓名", key="collab_new_name")
            role = st.selectbox(
                "角色",
                ["reviewer", "coordinator", "viewer"],
                format_func=lambda value: ROLE_LABELS.get(value, value),
                key="collab_new_role",
            )
            if st.button("创建审核人", key="collab_create_reviewer"):
                try:
                    storage.create_reviewer(name, role)
                    st.session_state["collab_message"] = "审核人已创建。"
                    st.rerun()
                except Exception as error:  # noqa: BLE001
                    st.error(str(error))

if not project_id:
    st.info("请先在“研究项目”页创建或选择一个项目。")
    st.stop()

project = storage.get_screening_project(project_id)
st.markdown(f"### {project['name']}")
st.write(f"**研究问题：** {project['question']}")

if current is None:
    st.info("请在左侧创建或选择审核人。")
    st.stop()

st.caption(
    f"当前审核人：{current['name']} · "
    f"{ROLE_LABELS.get(current['role'], current['role'])}"
)
if current["role"] == "viewer":
    st.warning("只读角色只能查看，不能提交盲筛决定或解决冲突。")

candidates = storage.list_all_screening_candidates(project_id)
if not candidates:
    st.info("当前项目还没有候选文献。")
    st.stop()
candidate_map = {
    candidate["candidate_key"]: candidate for candidate in candidates
}

st.markdown("### 我的盲筛决定")
selected_key = st.selectbox(
    "选择文献",
    list(candidate_map),
    format_func=lambda value: candidate_map[value].get("title") or value,
    key=f"collab_candidate_{project_id}",
)
own_decisions = storage.list_blind_decisions(
    project_id,
    candidate_key_value=selected_key,
    reviewer_id=current["id"],
)
own = own_decisions[0] if own_decisions else None
if own:
    st.caption(
        f"我的当前决定：{STATUS_LABELS.get(own['status'], own['status'])}"
        f" · 版本 {own['revision']}"
        + (
            f" · 原因：{own['exclusion_reason']}"
            if own.get("exclusion_reason")
            else ""
        )
    )
else:
    st.caption("尚未提交盲筛决定。")

if current["role"] != "viewer":
    with st.form(f"blind_form_{current['id']}_{selected_key}"):
        status_options = ["pending", "included", "excluded"]
        current_status = own["status"] if own else "pending"
        status = st.radio(
            "我的决定",
            status_options,
            index=status_options.index(current_status),
            format_func=lambda value: STATUS_LABELS.get(value, value),
            horizontal=True,
            key=f"blind_status_{current['id']}_{selected_key}",
        )
        reason_index = (
            EXCLUSION_REASONS.index(own["exclusion_reason"])
            if own and own.get("exclusion_reason") in EXCLUSION_REASONS
            else 0
        )
        reason = st.selectbox(
            "排除原因（仅排除时必填）",
            EXCLUSION_REASONS,
            index=reason_index,
            key=f"blind_reason_{current['id']}_{selected_key}",
        )
        notes = st.text_area(
            "备注",
            value=(own.get("notes") if own else "") or "",
            height=70,
            key=f"blind_notes_{current['id']}_{selected_key}",
        )
        submitted = st.form_submit_button("提交盲筛决定")
    if submitted:
        try:
            result = storage.set_blind_decision(
                project_id,
                selected_key,
                current["id"],
                status,
                reason=reason if status == "excluded" else None,
                notes=notes,
                expected_revision=own["revision"] if own else None,
            )
            st.session_state["collab_message"] = (
                f"盲筛决定已保存（版本 {result['revision']}）。"
            )
            st.rerun()
        except Exception as error:  # noqa: BLE001
            st.error(str(error))

st.markdown("### 协调者对比与最终确认")
if current["role"] != "coordinator":
    st.caption("只有协调者可以查看全部盲筛决定并做最终确认。")
    st.stop()

summary = storage.blind_screening_summary(project_id)
summary_map = {item["candidate_key"]: item for item in summary}

st.dataframe(
    [
        {
            "文献": item["title"] or item["candidate_key"],
            "状态": STATE_LABELS.get(item["state"], item["state"]),
            "审核决定": "；".join(
                f"{decision.get('reviewer_name') or decision['reviewer_id']}: "
                f"{STATUS_LABELS.get(decision['status'], decision['status'])}"
                for decision in item["decisions"]
            ),
            "指纹": item["fingerprint"][:12],
        }
        for item in summary
        if item["decisions"]
    ],
    hide_index=True,
    use_container_width=True,
)

conflicts = [
    item for item in summary if item["state"] == "conflict"
]
agreements = [
    item for item in summary if item["state"] == "agreed"
]


def _decisions_text(item):
    return "；".join(
        f"{decision.get('reviewer_name') or decision['reviewer_id']}: "
        f"{STATUS_LABELS.get(decision['status'], decision['status'])}"
        for decision in item["decisions"]
    )


if conflicts:
    st.markdown("#### 冲突解决")
    conflict_key = st.selectbox(
        "选择冲突文献",
        [item["candidate_key"] for item in conflicts],
        format_func=lambda value: summary_map[value]["title"] or value,
        key=f"collab_conflict_{project_id}",
    )
    conflict = summary_map[conflict_key]
    st.caption("审核决定：" + _decisions_text(conflict))
    with st.form(f"resolve_form_{conflict_key}"):
        final_status = st.radio(
            "最终决定",
            ["included", "excluded"],
            format_func=lambda value: STATUS_LABELS.get(value, value),
            horizontal=True,
            key=f"resolve_status_{conflict_key}",
        )
        final_reason_index = 0
        final_reason = st.selectbox(
            "最终排除原因（仅排除时使用）",
            EXCLUSION_REASONS,
            index=final_reason_index,
            key=f"resolve_reason_{conflict_key}",
        )
        final_notes = st.text_area(
            "协调备注",
            height=70,
            key=f"resolve_notes_{conflict_key}",
        )
        resolved = st.form_submit_button("提交冲突解决")
    if resolved:
        try:
            storage.resolve_blind_conflict(
                project_id,
                conflict_key,
                current["id"],
                final_status,
                reason=final_reason if final_status == "excluded" else None,
                notes=final_notes,
                expected_fingerprint=conflict["fingerprint"],
            )
            st.session_state["collab_message"] = "冲突已解决并保存。"
            st.rerun()
        except Exception as error:  # noqa: BLE001
            st.error(str(error))
else:
    st.caption("当前没有待解决的冲突。")

if agreements:
    st.markdown("#### 一致结果确认")
    agreement_key = st.selectbox(
        "选择一致文献",
        [item["candidate_key"] for item in agreements],
        format_func=lambda value: summary_map[value]["title"] or value,
        key=f"collab_agreement_{project_id}",
    )
    agreement = summary_map[agreement_key]
    st.caption("审核决定：" + _decisions_text(agreement))
    with st.form(f"agree_form_{agreement_key}"):
        agreement_notes = st.text_area(
            "确认备注",
            height=70,
            key=f"agree_notes_{agreement_key}",
        )
        confirmed = st.form_submit_button("确认一致结果")
    if confirmed:
        try:
            storage.confirm_blind_agreement(
                project_id,
                agreement_key,
                current["id"],
                notes=agreement_notes,
                expected_fingerprint=agreement["fingerprint"],
            )
            st.session_state["collab_message"] = "一致结果已确认。"
            st.rerun()
        except Exception as error:  # noqa: BLE001
            st.error(str(error))

st.markdown("### 盲筛与冲突审计")
blind_audit = storage.list_blind_audit(project_id, limit=100)
if blind_audit:
    st.dataframe(
        [
            {
                "时间": item["changed_at"],
                "审核人": item.get("reviewer_name") or item["reviewer_id"],
                "文献": item["candidate_key"],
                "原状态": STATUS_LABELS.get(item["old_status"] or "", ""),
                "新状态": STATUS_LABELS.get(item["new_status"], item["new_status"]),
                "备注": item["new_notes"] or "",
            }
            for item in blind_audit
        ],
        hide_index=True,
        use_container_width=True,
    )
else:
    st.caption("尚无盲筛审计记录。")

resolutions = storage.list_conflict_resolutions(project_id, limit=100)
if resolutions:
    st.markdown("#### 最终确认记录")
    st.dataframe(
        [
            {
                "时间": item["created_at"],
                "文献": item["candidate_key"],
                "类型": "冲突解决" if item["resolution_type"] == "conflict" else "一致确认",
                "最终状态": STATUS_LABELS.get(
                    item["final_status"], item["final_status"]
                ),
                "原因": item["final_reason"] or "",
                "协调者": item["coordinator_id"] or "",
                "备注": item["notes"] or "",
            }
            for item in resolutions
        ],
        hide_index=True,
        use_container_width=True,
    )
