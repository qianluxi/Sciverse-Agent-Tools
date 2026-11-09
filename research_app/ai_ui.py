from __future__ import annotations

import streamlit as st

from research_app.ai_settings import model_options, record_ai_model


CUSTOM_MODEL_LABEL = "自定义模型（手动输入）"


def model_selector(key: str, default: str) -> str:
    current = str(st.session_state.get(key) or default).strip()
    options = model_options(current)
    choices = [*options, CUSTOM_MODEL_LABEL]
    choice_key = f"{key}__choice"
    stored_choice = st.session_state.get(choice_key)
    if stored_choice not in choices:
        stored_choice = current if current in options else CUSTOM_MODEL_LABEL
    choice = st.selectbox(
        "模型名",
        choices,
        index=choices.index(stored_choice),
        help="可选择常用模型，也可选“自定义模型”输入其他模型名；使用过的模型会记录在本机。",
    )
    st.session_state[choice_key] = choice
    if choice == CUSTOM_MODEL_LABEL:
        custom_key = f"{key}__custom"
        if custom_key not in st.session_state:
            st.session_state[custom_key] = current
        model = st.text_input(
            "自定义模型名",
            key=custom_key,
            placeholder="例如 deepseek-v4.1-flash",
        )
    else:
        model = choice
    model = str(model or "").strip()
    st.session_state[key] = model
    if model:
        record_ai_model(model)
    return model
