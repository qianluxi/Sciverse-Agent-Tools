from research_app.screening_criteria import (
    build_project_criteria,
    criteria_map,
    criteria_prompt_text,
)


def test_build_project_criteria_numbers_inclusion_and_exclusion_items():
    criteria = build_project_criteria(
        {
            "question": "Q",
            "inclusion_criteria": "1. Randomized trials\n- Adults",
            "exclusion_criteria": "Not about X; Animal studies",
        }
    )

    assert criteria["items"] == [
        {"id": "I1", "kind": "inclusion", "text": "Randomized trials"},
        {"id": "I2", "kind": "inclusion", "text": "Adults"},
        {"id": "E1", "kind": "exclusion", "text": "Not about X"},
        {"id": "E2", "kind": "exclusion", "text": "Animal studies"},
    ]
    assert criteria_map(criteria)["I1"]["text"] == "Randomized trials"
    assert "E2: Animal studies" in criteria_prompt_text(criteria)
