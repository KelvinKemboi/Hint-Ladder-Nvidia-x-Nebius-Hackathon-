import pytest

from sparring_partner.agents.predict import Prediction, PredictionParseError, parse_prediction


@pytest.mark.parametrize(
    "answer, value",
    [
        ("[[1,5]]", [[1, 5]]),
        ("  [[1, 5]]  ", [[1, 5]]),
        ("`[[1, 5]]`", [[1, 5]]),
        ("```python\n[[1, 5]]\n```", [[1, 5]]),
        ("it should return [[1, 5]].", [[1, 5]]),
        ("returns [(1, 5)]", [(1, 5)]),
        ("=> []", []),
        ("I think it returns 3", 3),
        ("true", True),
        ("null", None),
        ('{"a": 1}', {"a": 1}),
        ("'abc'", "abc"),
    ],
)
def test_parses_values_with_formatting_differences(answer, value):
    assert parse_prediction(answer) == Prediction("value", value)


@pytest.mark.parametrize(
    "answer, name",
    [("it raises IndexError", "IndexError"), ("it crashes", None), ("an error", None)],
)
def test_parses_error_predictions(answer, name):
    assert parse_prediction(answer) == Prediction("error", error_name=name)


@pytest.mark.parametrize("answer", ["banana", "[[1, 5]", "the merged one"])
def test_unreadable_answers(answer):
    with pytest.raises(PredictionParseError, match="Python value"):
        parse_prediction(answer)
