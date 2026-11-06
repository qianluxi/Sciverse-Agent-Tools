from __future__ import annotations

import streamlit as st

from research_app import storage
from research_app.ai_screening import CONFIDENCE_LABELS, SUGGESTION_LABELS
from research_app.collaboration import (
    export_blind_decisions_csv,
    export_resolutions_csv,
)
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
    "reason_conflict": "原因分歧",
    "conflict": "冲突",
}

st.set_page_config(page_title="协作审核 · Sciverse", page_icon="S", layout="wide")
st.title("协作审核")
st.caption(
    "可选方法学模块：个人使用可继续走单人筛选，不需要本页。"
    "双人盲筛与冲突解决中，审核员独立提交决定，协调者查看对比并做最终确认；"
    "AI 仅提供优先级和预填建议，所有决定由人工确认并保留审计。"
)

if "collab_message" in st.session_state:
    st.success(st.session_state.pop("collab_message"))
if "collab_error" in st.session_state:
    st.error(st.session_state.pop("collab_error"))

reviewers = storage.list_reviewers()

with st.sidebar:
    project_id = project_selector("collab_project_selector")
    st.divider()
    st.markdown("### 审核人身份")
    current = None
    selected_id = None
    if reviewers:
        selected_id = st.selectbox(
            "选择审核人",
            [item["id"] for item in reviewers],
            format_func=lambda value: next(
                f"{item['name']} · {ROLE_LABELS.get(item['role'], item['role'])}"
                for item in reviewers
                if item["id"] == value
            ),
            key="collab_current_reviewer",
        )
        unlocked_id = st.session_state.get("collab_unlocked_reviewer")
        if unlocked_id != selected_id:
            pin = st.text_input(
                "PIN",
                type="password",
                key=f"collab_pin_{selected_id}",
            )
            if st.button("解锁身份", key=f"collab_unlock_{selected_id}"):
                if storage.verify_reviewer_pin(selected_id, pin):
                    st.session_state["collab_unlocked_reviewer"] = selected_id
                    st.rerun()
                else:
                    st.error("PIN 不正确，或该审核人尚未设置 PIN。")
        else:
            current = storage.get_reviewer(unlocked_id)
            if st.button("锁定身份", key="collab_lock"):
                st.session_state.pop("collab_unlocked_reviewer", None)
                st.rerun()
    else:
        st.info("还没有审核人，请先创建首个协调者并设置 PIN。")

    can_create = not reviewers or (
        current is not None and current["role"] == "coordinator"
    )
    with st.expander("新建审核人"):
        if not can_create:
            st.caption("只有已解锁的协调者可以创建审核人。")
        else:
            name = st.text_input("姓名", key="collab_new_name")
            role = st.selectbox(
                "角色",
                ["reviewer", "coordinator", "viewer"],
                format_func=lambda value: ROLE_LABELS.get(value, value),
                key="collab_new_role",
            )
            new_pin = st.text_input(
                "PIN（4–32 个字符）",
                type="password",
                key="collab_new_pin",
            )
            if st.button("创建审核人", key="collab_create_reviewer"):
                try:
                    storage.create_reviewer(name, role, new_pin)
                    st.session_state["collab_message"] = "审核人已创建。"
                    st.rerun()
                except Exception as error:  # noqa: BLE001
                    st.error(str(error))
    if current and current["role"] == "coordinator" and reviewers:
        with st.expander("设置/重置审核人 PIN"):
            target = st.selectbox(
                "审核人",
                [item["id"] for item in reviewers],
                format_func=lambda value: next(
                    item["name"] for item in reviewers if item["id"] == value
                ),
                key="collab_pin_target",
            )
            reset_pin = st.text_input(
                "新 PIN（4–32 个字符）",
                type="password",
                key="collab_pin_reset",
            )
            if st.button("保存 PIN", key="collab_pin_save"):
                try:
                    storage.set_reviewer_pin(target, reset_pin)
                    st.session_state["collab_message"] = "PIN 已更新。"
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
    st.info("请在左侧选择审核人并输入 PIN 解锁身份后才能操作。")
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

st.markdown("### AI 辅助优先队列")
if current["role"] == "viewer":
    st.caption("只读角色不能提交决定或使用批量预填。")
else:
    own_priority = storage.reviewer_priority_candidates(
        project_id, current["id"]
    )
    if current["role"] == "coordinator":
        priority = storage.ai_priority_candidates(project_id)
        table_rows = [
            {
                "优先级": item["priority_label"],
                "文献": item["title"] or item["candidate_key"],
                "AI 建议": SUGGESTION_LABELS.get(
                    item["ai_suggestion"], item["ai_suggestion"] or "—"
                ),
                "置信度": CONFIDENCE_LABELS.get(
                    item["ai_confidence"], item["ai_confidence"] or "—"
                ),
                "盲筛状态": STATE_LABELS.get(
                    item["blind_state"], item["blind_state"]
                ),
                "候选状态": STATUS_LABELS.get(
                    item["candidate_status"], item["candidate_status"]
                ),
            }
            for item in priority
        ]
    else:
        priority = own_priority
        table_rows = [
            {
                "优先级": item["priority_label"],
                "文献": item["title"] or item["candidate_key"],
                "AI 建议": SUGGESTION_LABELS.get(
                    item["ai_suggestion"], item["ai_suggestion"] or "—"
                ),
                "置信度": CONFIDENCE_LABELS.get(
                    item["ai_confidence"], item["ai_confidence"] or "—"
                ),
                "我的状态": STATUS_LABELS.get(
                    item["own_status"], item["own_status"]
                ),
            }
            for item in priority
        ]
    if priority:
        st.dataframe(table_rows, hide_index=True, use_container_width=True)
        priority_map = {
            item["candidate_key"]: item for item in own_priority
        }
        prefill_pool = [
            item
            for item in own_priority
            if item["own_status"] == "pending"
            and item["ai_suggestion"] in {"include", "exclude"}
        ]
        if prefill_pool:
            selected_prefill = st.multiselect(
                "选择要按 AI 建议预填的文献",
                [item["candidate_key"] for item in prefill_pool],
                format_func=lambda value: (
                    f"{priority_map[value]['title'] or value} · "
                    f"{SUGGESTION_LABELS.get(priority_map[value]['ai_suggestion'], '')}"
                ),
                key=f"collab_prefill_{project_id}_{current['id']}",
            )
            confirm_prefill = st.checkbox(
                "我已逐条核对 AI 建议，确认这些盲筛决定由我保存",
                key=f"collab_prefill_confirm_{project_id}_{current['id']}",
            )
            if st.button(
                "批量保存预填决定",
                disabled=not selected_prefill or not confirm_prefill,
                key=f"collab_prefill_save_{project_id}_{current['id']}",
            ):
                saved = 0
                skipped = 0
                for key in selected_prefill:
                    item = priority_map[key]
                    blind_status = (
                        "included"
                        if item["ai_suggestion"] == "include"
                        else "excluded"
                    )
                    try:
                        storage.set_blind_decision(
                            project_id,
                            key,
                            current["id"],
                            blind_status,
                            reason="其他" if blind_status == "excluded" else None,
                            notes="AI 建议预填，已人工确认",
                            expected_revision=item["own_revision"],
                            ai_task_id=item["ai_task_id"],
                            ai_suggestion=item["ai_suggestion"],
                            ai_confidence=item["ai_confidence"],
                            ai_assisted=True,
                        )
                        saved += 1
                    except Exception:  # noqa: BLE001
                        skipped += 1
                st.session_state["collab_message"] = (
                    f"已保存 {saved} 条 AI 预填决定"
                    + (f"，{skipped} 条失败" if skipped else "")
                    + "；排除原因默认为“其他”，可逐条修改。"
                )
                st.rerun()
        else:
            st.caption(
                "没有可批量预填的文献（需有 AI 明确建议且本人尚未提交决定）。"
            )
    else:
        st.caption("暂无 AI 筛选建议，无法生成优先队列。")

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
    if own.get("ai_assisted"):
        st.caption(
            "AI 辅助来源："
            f"{SUGGESTION_LABELS.get(own.get('ai_suggestion') or '', own.get('ai_suggestion') or '—')}"
            f" · 置信度 {CONFIDENCE_LABELS.get(own.get('ai_confidence') or '', own.get('ai_confidence') or '—')}"
            f" · 任务 `{own.get('ai_task_id') or '—'}`"
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
                expected_revision=own["revision"] if own else 0,
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
reason_conflicts = [
    item for item in summary if item["state"] == "reason_conflict"
]


def _decisions_text(item):
    parts = []
    for decision in item["decisions"]:
        text = (
            f"{decision.get('reviewer_name') or decision['reviewer_id']}: "
            f"{STATUS_LABELS.get(decision['status'], decision['status'])}"
        )
        if decision.get("status") == "excluded" and decision.get("exclusion_reason"):
            text += f"（{decision['exclusion_reason']}）"
        parts.append(text)
    return "；".join(parts)


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

if reason_conflicts:
    st.markdown("#### 排除原因分歧")
    reason_key = st.selectbox(
        "选择原因分歧文献",
        [item["candidate_key"] for item in reason_conflicts],
        format_func=lambda value: summary_map[value]["title"] or value,
        key=f"collab_reason_conflict_{project_id}",
    )
    reason_conflict = summary_map[reason_key]
    st.caption("审核决定：" + _decisions_text(reason_conflict))
    with st.form(f"reason_form_{reason_key}"):
        final_reason = st.selectbox(
            "最终排除原因",
            EXCLUSION_REASONS,
            key=f"reason_conflict_reason_{reason_key}",
        )
        reason_notes = st.text_area(
            "协调备注",
            height=70,
            key=f"reason_conflict_notes_{reason_key}",
        )
        confirmed = st.form_submit_button("确认最终排除原因")
    if confirmed:
        try:
            storage.confirm_blind_agreement(
                project_id,
                reason_key,
                current["id"],
                reason=final_reason,
                notes=reason_notes,
                expected_fingerprint=reason_conflict["fingerprint"],
            )
            st.session_state["collab_message"] = "排除原因已确认。"
            st.rerun()
        except Exception as error:  # noqa: BLE001
            st.error(str(error))

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
                "AI 辅助": "是" if item.get("ai_suggestion") else "否",
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

download_columns = st.columns(2)
with download_columns[0]:
    st.download_button(
        "下载盲筛决定 CSV",
        data=export_blind_decisions_csv(project_id),
        file_name="sciverse-blind-decisions.csv",
        mime="text/csv",
        key="download_blind_decisions",
    )
with download_columns[1]:
    st.download_button(
        "下载冲突/一致确认 CSV",
        data=export_resolutions_csv(project_id),
        file_name="sciverse-conflict-resolutions.csv",
        mime="text/csv",
        key="download_conflict_resolutions",
    )
