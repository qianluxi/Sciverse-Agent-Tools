import pytest

from research_app.search import parse_catalog_value, split_filter_values


def test_split_filter_values_accepts_common_separators():
    assert split_filter_values("Nature, Science； PNAS") == ["Nature", "Science", "PNAS"]


@pytest.mark.parametrize(
    ("value", "field_type", "operator", "expected"),
    [
        ("42", "Integer", "FILTER_OP_EQ", 42),
        ("0.75", "Float", "FILTER_OP_GTE", 0.75),
        ("是", "Boolean", "FILTER_OP_EQ", True),
        ("Nature, Science", "String", "FILTER_OP_IN", ["Nature", "Science"]),
        ("2, 5", "Integer", "FILTER_OP_IN", [2, 5]),
        ("alpha", "String", "FILTER_OP_EQ", "alpha"),
    ],
)
def test_parse_catalog_filter_value(value, field_type, operator, expected):
    assert parse_catalog_value(value, field_type, operator) == expected


@pytest.mark.parametrize(
    ("value", "field_type", "operator"),
    [
        ("not-a-year", "Integer", "FILTER_OP_EQ"),
        ("maybe", "Boolean", "FILTER_OP_EQ"),
        ("", "String", "FILTER_OP_EQ"),
    ],
)
def test_parse_catalog_filter_value_rejects_invalid_values(value, field_type, operator):
    with pytest.raises(ValueError):
        parse_catalog_value(value, field_type, operator)
