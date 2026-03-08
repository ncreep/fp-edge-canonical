"""Tests for Result, Ok, and Err."""

import pytest

from step2_functional.result import Err, Ok, Result

# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, f, expected",
    [
        (Ok(1), lambda x: x * 2, Ok(2)),
        (Err("e"), lambda x: x * 2, Err("e")),
    ],
    ids=["ok", "err"],
)
def test_map(result, f, expected):
    assert result.map(f) == expected


# ---------------------------------------------------------------------------
# flat_map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, f, expected",
    [
        (Ok(1), lambda x: Ok(x + 1), Ok(2)),
        (Ok(1), lambda x: Err("inner"), Err("inner")),
        (Err("e"), lambda x: Ok(x + 1), Err("e")),
        (Err("e"), lambda x: Err("inner"), Err("e")),
    ],
    ids=["ok_to_ok", "ok_to_err", "err_to_ok", "err_to_err"],
)
def test_flat_map(result, f, expected):
    assert result.flat_map(f) == expected


# ---------------------------------------------------------------------------
# then
# ---------------------------------------------------------------------------


def test_then_ok_continues():
    assert Ok(1).then(lambda: Ok(2)) == Ok(2)


def test_then_ok_to_err():
    assert Ok(1).then(lambda: Err("e")) == Err("e")


def test_then_err_short_circuits():
    called = []
    result = Err("e").then(lambda: Ok(called.append(1) or 2))
    assert result == Err("e")
    assert called == []


# ---------------------------------------------------------------------------
# map_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, expected",
    [
        (Ok(1).map_error(str), Ok(1)),
        (Err(42).map_error(str), Err("42")),
    ],
    ids=["ok_unchanged", "err_transformed"],
)
def test_map_error(result, expected):
    assert result == expected


# ---------------------------------------------------------------------------
# or_else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, f, expected",
    [
        (Ok(1), lambda e: Ok(0), Ok(1)),
        (Err("e"), lambda e: Ok(0), Ok(0)),
        (Err("e"), lambda e: Err(f"wrapped:{e}"), Err("wrapped:e")),
    ],
    ids=["ok_unchanged", "err_recovered", "err_re_raised"],
)
def test_or_else(result, f, expected):
    assert result.or_else(f) == expected


# ---------------------------------------------------------------------------
# get_or_else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, expected",
    [
        (Ok(1), 1),
        (Err("e"), 99),
    ],
    ids=["ok", "err_uses_default"],
)
def test_get_or_else(result, expected):
    assert result.get_or_else(99) == expected


# ---------------------------------------------------------------------------
# on_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, f, expected",
    [
        (Ok(42), lambda e: -1, 42),
        (Err("e"), lambda e: -1, -1),
    ],
    ids=["ok_unchanged", "err_handled"],
)
def test_on_error(result, f, expected):
    assert result.on_error(f) == expected


# ---------------------------------------------------------------------------
# fold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, expected",
    [
        (Ok(1), "ok:1"),
        (Err("e"), "err:e"),
    ],
    ids=["ok", "err"],
)
def test_fold(result, expected):
    assert result.fold(lambda e: f"err:{e}", lambda v: f"ok:{v}") == expected


# ---------------------------------------------------------------------------
# tap
# ---------------------------------------------------------------------------


def test_tap_ok_calls_side_effect():
    called = []
    Ok(1).tap(lambda: called.append(True))
    assert called == [True]


def test_tap_err_skips_side_effect():
    called = []
    Err("e").tap(lambda: called.append(True))
    assert called == []


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_passes():
    assert Result.validate(5, lambda x: x > 0, lambda x: f"got {x}") == Ok(5)


def test_validate_fails():
    assert Result.validate(-1, lambda x: x > 0, lambda x: f"got {x}") == Err("got -1")


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------


def test_chain_empty():
    assert Result.chain() == Ok(None)


def test_chain_all_ok():
    calls = []
    result = Result.chain(
        lambda: Ok(calls.append(1)),
        lambda: Ok(calls.append(2)),
    )
    assert result == Ok(None)
    assert calls == [1, 2]


def test_chain_stops_on_first_err():
    calls = []
    result = Result.chain(
        lambda: Ok(calls.append(1)),
        lambda: Err("oops"),
        lambda: Ok(calls.append(3)),
    )
    assert result == Err("oops")
    assert calls == [1]


# ---------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "r1, r2, expected",
    [
        (Ok(1), Ok("a"), Ok((1, "a"))),
        (Ok(1), Err(["e2"]), Err(["e2"])),
        (Err(["e1"]), Ok(1), Err(["e1"])),
        (Err(["e1"]), Err(["e2"]), Err(["e1", "e2"])),
    ],
    ids=["ok_ok", "ok_err", "err_ok", "err_err"],
)
def test_zip(r1, r2, expected):
    assert Result.zip(r1, r2) == expected


# ---------------------------------------------------------------------------
# zip3 — all 2³ = 8 input combinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "r1, r2, r3, expected",
    [
        (Ok(1), Ok(2), Ok(3), Ok((1, 2, 3))),
        (Err(["e1"]), Ok(2), Ok(3), Err(["e1"])),
        (Ok(1), Err(["e2"]), Ok(3), Err(["e2"])),
        (Ok(1), Ok(2), Err(["e3"]), Err(["e3"])),
        (Err(["e1"]), Err(["e2"]), Ok(3), Err(["e1", "e2"])),
        (Err(["e1"]), Ok(2), Err(["e3"]), Err(["e1", "e3"])),
        (Ok(1), Err(["e2"]), Err(["e3"]), Err(["e2", "e3"])),
        (Err(["e1"]), Err(["e2"]), Err(["e3"]), Err(["e1", "e2", "e3"])),
    ],
    ids=["all_ok", "r1_err", "r2_err", "r3_err", "r1_r2_err", "r1_r3_err", "r2_r3_err", "all_err"],
)
def test_zip3(r1, r2, r3, expected):
    assert Result.zip3(r1, r2, r3) == expected


# ---------------------------------------------------------------------------
# zip4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "r1, r2, r3, r4, expected",
    [
        (Ok(1), Ok(2), Ok(3), Ok(4), Ok((1, 2, 3, 4))),
        (Err(["e1"]), Err(["e2"]), Err(["e3"]), Err(["e4"]), Err(["e1", "e2", "e3", "e4"])),
        (Ok(1), Err(["e2"]), Err(["e3"]), Ok(4), Err(["e2", "e3"])),
    ],
    ids=["all_ok", "all_err", "middle_err"],
)
def test_zip4(r1, r2, r3, r4, expected):
    assert Result.zip4(r1, r2, r3, r4) == expected


# ---------------------------------------------------------------------------
# zip5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "r1, r2, r3, r4, r5, expected",
    [
        (Ok(1), Ok(2), Ok(3), Ok(4), Ok(5), Ok((1, 2, 3, 4, 5))),
        (
            Err(["e1"]),
            Err(["e2"]),
            Err(["e3"]),
            Err(["e4"]),
            Err(["e5"]),
            Err(["e1", "e2", "e3", "e4", "e5"]),
        ),
        (Ok(1), Ok(2), Ok(3), Ok(4), Err(["e5"]), Err(["e5"])),
    ],
    ids=["all_ok", "all_err", "last_err"],
)
def test_zip5(r1, r2, r3, r4, r5, expected):
    assert Result.zip5(r1, r2, r3, r4, r5) == expected


# ---------------------------------------------------------------------------
# map2 / map3 / map4 / map5
# ---------------------------------------------------------------------------


def test_map2_ok():
    assert Result.map2(lambda a, b: a + b, Ok(1), Ok(2)) == Ok(3)


def test_map2_err_accumulates():
    assert Result.map2(lambda a, b: a + b, Err(["e1"]), Err(["e2"])) == Err(["e1", "e2"])


def test_map3_ok():
    assert Result.map3(lambda a, b, c: (a, b, c), Ok("x"), Ok("y"), Ok("z")) == Ok(("x", "y", "z"))


def test_map4_ok():
    assert Result.map4(lambda a, b, c, d: a + b + c + d, Ok(1), Ok(2), Ok(3), Ok(4)) == Ok(10)


def test_map5_ok():
    assert Result.map5(
        lambda a, b, c, d, e: a + b + c + d + e, Ok(1), Ok(2), Ok(3), Ok(4), Ok(5)
    ) == Ok(15)


# ---------------------------------------------------------------------------
# combine2 / combine3 / combine4 / combine5  (.using interface)
# ---------------------------------------------------------------------------


def test_combine2_ok():
    assert Result.combine2(Ok(1), Ok(2)).using(lambda a, b: a + b) == Ok(3)


def test_combine2_err():
    assert Result.combine2(Err(["e1"]), Err(["e2"])).using(lambda a, b: a + b) == Err(["e1", "e2"])


def test_combine3_ok():
    assert Result.combine3(Ok(1), Ok(2), Ok(3)).using(lambda a, b, c: a + b + c) == Ok(6)


def test_combine3_partial_err():
    assert Result.combine3(Ok(1), Err(["e2"]), Err(["e3"])).using(
        lambda a, b, c: a + b + c
    ) == Err(["e2", "e3"])


def test_combine4_ok():
    assert Result.combine4(Ok(1), Ok(2), Ok(3), Ok(4)).using(
        lambda a, b, c, d: (a, b, c, d)
    ) == Ok((1, 2, 3, 4))


def test_combine5_ok():
    assert Result.combine5(Ok(1), Ok(2), Ok(3), Ok(4), Ok(5)).using(
        lambda a, b, c, d, e: (a, b, c, d, e)
    ) == Ok((1, 2, 3, 4, 5))


def test_combine5_all_err():
    assert Result.combine5(Err(["e1"]), Err(["e2"]), Err(["e3"]), Err(["e4"]), Err(["e5"])).using(
        lambda a, b, c, d, e: None
    ) == Err(["e1", "e2", "e3", "e4", "e5"])


# ---------------------------------------------------------------------------
# traverse
# ---------------------------------------------------------------------------


def test_traverse_all_ok():
    assert Result.traverse([1, 2, 3], lambda x: Ok(x * 2)) == Ok([2, 4, 6])


def test_traverse_empty():
    assert Result.traverse([], lambda x: Ok(x)) == Ok([])


def test_traverse_all_err():
    assert Result.traverse(
        [1, 2, 3], lambda x: Err([f"e{x}"])
    ) == Err(["e1", "e2", "e3"])


def test_traverse_partial_err_accumulates():
    assert Result.traverse(
        [1, 2, 3], lambda x: Ok(x) if x % 2 != 0 else Err([f"e{x}"])
    ) == Err(["e2"])


def test_traverse_multiple_errs_accumulate():
    assert Result.traverse(
        [1, 2, 3, 4], lambda x: Ok(x) if x % 2 != 0 else Err([f"e{x}"])
    ) == Err(["e2", "e4"])


# ---------------------------------------------------------------------------
# sequence
# ---------------------------------------------------------------------------


def test_sequence_all_ok():
    assert Result.sequence([Ok(1), Ok(2), Ok(3)]) == Ok([1, 2, 3])


def test_sequence_empty():
    assert Result.sequence([]) == Ok([])


def test_sequence_all_err():
    assert Result.sequence([Err(["e1"]), Err(["e2"]), Err(["e3"])]) == Err(["e1", "e2", "e3"])


def test_sequence_partial_err_accumulates():
    assert Result.sequence([Ok(1), Err(["e2"]), Ok(3)]) == Err(["e2"])


def test_sequence_multiple_errs_accumulate():
    assert Result.sequence([Ok(1), Err(["e2"]), Ok(3), Err(["e4"])]) == Err(["e2", "e4"])


# ---------------------------------------------------------------------------
# from_optional
# ---------------------------------------------------------------------------


def test_from_optional_value_present():
    assert Result.optional(42, lambda: "missing") == Ok(42)


def test_from_optional_none():
    assert Result.optional(None, lambda: "missing") == Err("missing")


def test_from_optional_falsy_value_is_ok():
    assert Result.optional(0, lambda: "missing") == Ok(0)
