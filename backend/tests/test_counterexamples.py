import ast

from hypothesis import strategies as st

from sparring_partner.testing import (
    NO_CRASH,
    Counterexample,
    Property,
    complexity,
    dedupe,
    find_counterexamples,
    rank,
)


# Demo bug: AI-generated merge_intervals
def merge_intervals(intervals):
    if not intervals:
        return []
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start < merged[-1][1]:  # bug: touching intervals should merge(<=)
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def merge_intervals_fixed(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def points(intervals):
    return {x for start, end in intervals for x in range(start, end + 1)}


NO_OVERLAP = Property(
    "output intervals never overlap",
    lambda args, out: all(a[1] < b[0] for a, b in zip(out, out[1:], strict=False)),
)
COVERED = Property(
    "every input point is still covered",
    lambda args, out: points(args[0]) == points(out),
)

MERGE_PROPERTIES = [NO_OVERLAP, COVERED]
interval = st.tuples(st.integers(0, 20), st.integers(0, 5)).map(lambda p: [p[0], p[0] + p[1]])
sorted_intervals = st.lists(interval, max_size=6).map(sorted).map(lambda xs: (xs,))
any_intervals = st.lists(interval, max_size=6).map(lambda xs: (xs,))


def test_finds_touching_interval_bug_with_minimal_input():
    [ce] = find_counterexamples(merge_intervals, sorted_intervals, MERGE_PROPERTIES)

    assert ce.violated_property == NO_OVERLAP.name
    assert ce.violated_properties == [NO_OVERLAP.name]
    intervals = ast.literal_eval(ce.args[0])
    # Shrunk to two intervals that touch: the second starts where the first ends.
    assert len(intervals) == 2
    assert intervals[1][0] == intervals[0][1]
    assert ce.output == repr(merge_intervals(intervals))
    assert ce.call == f"merge_intervals({ce.args[0]})"
    assert not ce.raised


def test_unsorted_input_finds_a_second_distinct_failure():
    touching, unsorted = find_counterexamples(merge_intervals, any_intervals, MERGE_PROPERTIES)

    assert touching.violated_properties == [NO_OVERLAP.name]
    assert unsorted.violated_properties == [COVERED.name]
    intervals = ast.literal_eval(unsorted.args[0])
    assert intervals != sorted(intervals)


def test_correct_code_has_no_counterexamples():
    assert find_counterexamples(merge_intervals_fixed, any_intervals, MERGE_PROPERTIES) == []


def test_same_seed_gives_same_counterexample():
    first = find_counterexamples(merge_intervals, any_intervals, MERGE_PROPERTIES, random_seed=7)
    second = find_counterexamples(merge_intervals, any_intervals, MERGE_PROPERTIES, random_seed=7)
    assert [ce.call for ce in first] == [ce.call for ce in second]


# Exceptions, predicate errors, mutation
def average(xs):
    return sum(xs) / len(xs)


def test_records_exception_and_user_line():
    [ce] = find_counterexamples(average, st.lists(st.integers()).map(lambda xs: (xs,)), [])

    assert ce.call == "average([])"
    assert ce.violated_property == NO_CRASH
    assert ce.exception_type == "ZeroDivisionError"
    assert ce.exception_line == average.__code__.co_firstlineno + 1
    assert ce.output is None


def test_non_tuple_strategy_is_a_single_argument():
    [ce] = find_counterexamples(average, st.lists(st.integers()), [])
    assert ce.args == ["[]"]


def test_predicate_error_counts_as_violation():
    first_is_smallest = Property("first is smallest", lambda args, out: out[0] == min(args[0]))

    def maybe_none(xs):
        return xs or None

    [ce] = find_counterexamples(
        maybe_none, st.lists(st.integers(), max_size=3).map(lambda xs: (xs,)), [first_is_smallest]
    )

    assert ce.call == "maybe_none([])"
    assert ce.violated_properties == ["first is smallest"]
    assert ce.check_errors["first is smallest"].startswith("TypeError")


def test_input_mutation_does_not_change_recorded_args():
    def sort_in_place_and_drop_last(xs):
        xs.sort()
        return xs[:-1]

    keeps_length = Property("keeps length", lambda args, out: len(out) == len(args[0]))
    [ce] = find_counterexamples(
        sort_in_place_and_drop_last,
        st.lists(st.integers(), min_size=2).map(lambda xs: (xs,)),
        [keeps_length],
    )

    assert ce.args == ["[0, 0]"]
    assert ce.output == "[0]"


# Ranking and deduplication
def make(call, prop="p", *, raised=False, violated=None, line=None, cx=1):
    return Counterexample(
        function="f",
        args=[call],
        call=f"f({call})",
        violated_property=prop,
        violated_properties=violated if violated is not None else [prop],
        exception_type="ValueError" if raised else None,
        exception_line=line,
        complexity=cx,
    )


def test_complexity_prefers_short_small_inputs():
    assert complexity([]) < complexity([0]) < complexity([0, 0]) < complexity([[0, 0], [0, 0]])
    assert complexity(1) < complexity(1000)
    assert complexity("") < complexity("abc")


def test_rank_simplest_then_wrong_output_then_focused_then_property_order():
    big = make("big", cx=10)
    crash = make("crash", NO_CRASH, raised=True, cx=2)
    wrong = make("wrong", "b", cx=2)
    broad = make("broad", "a", violated=["a", "b"], cx=2)
    tiny = make("tiny", cx=1)

    assert rank([big, crash, broad, wrong, tiny], ["a", "b"]) == [tiny, wrong, broad, crash, big]
    assert rank([make("x", "b"), y := make("y", "a")], ["a", "b"])[0] is y


def test_dedupe_merges_same_input_and_same_signature():
    a = make("[1]", "a", violated=["a", "b"], cx=1)
    b_same_input = make("[1]", "b", violated=["a", "b"], cx=1)
    a_same_props = make("[1, 2]", "a", violated=["a", "b"], cx=3)
    crash1 = make("[]", NO_CRASH, raised=True, line=5, cx=1)
    crash2 = make("[0]", NO_CRASH, raised=True, line=5, cx=2)
    other_crash = make("[9]", NO_CRASH, raised=True, line=8, cx=2)

    kept = dedupe([a_same_props, b_same_input, crash2, a, other_crash, crash1])

    assert [ce.call for ce in kept] == ["f([1])", "f([])", "f([9])"]


def test_counterexample_serialises():
    data = make("[]", raised=True).to_dict()
    assert data["call"] == "f([])"
    assert data["exception_type"] == "ValueError"


def test_call_timeout_turns_infinite_loop_into_counterexample():
    def countdown(n):
        while n != 0:
            n -= 2
        return n

    [ce] = find_counterexamples(countdown, st.integers(0, 10), [], call_timeout_s=0.1)

    assert ce.call == "countdown(1)"
    assert ce.exception_type == "TimeoutError"
    loop = countdown.__code__.co_firstlineno + 1
    assert ce.exception_line in (loop, loop + 1)


def test_predicate_that_runs_out_of_time_or_memory_is_not_blamed_on_the_code():
    def identity(n):
        return n

    def slow_check(args, output):
        while True:  # a Python-level loop, so the time limit can interrupt it
            pass

    def hungry_check(args, output):
        raise MemoryError

    properties = [Property("slow", slow_check), Property("hungry", hungry_check)]
    found = find_counterexamples(
        identity, st.integers(0, 3), properties, max_examples=5, call_timeout_s=0.1
    )
    assert found == []
