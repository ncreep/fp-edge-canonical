from functools import partial
from typing import Optional, Sequence

from result import Result

type WithErrors[A] = Result[Sequence[str], A]


class In1: ...


class In2: ...


class B1: ...


class B2: ...


class C1: ...


class C2: ...


class C3: ...


class D1: ...


class D2: ...


class D3: ...


class D4: ...


class Out: ...


optional = Result.optional
lift_safe = Result.lift_safe
safe = Result.safe
traverse = Result.traverse
combine3 = Result.combine3


def step1_1(in1: In1) -> Optional[B1]: ...
def step1_2(in2: In2, b1: B1) -> WithErrors[B2]: ...


def step2_1(in1: In1) -> Sequence[C1]: ...
def step2_2(c1: C1) -> WithErrors[C2]: ...
def step2_3(c2s: Sequence[C2]) -> WithErrors[C3]: ...


def step3_1(in1: In1) -> WithErrors[D1]: ...
def step3_2(d1: D1) -> WithErrors[D2]: ...
def step3_3(d2: D2) -> WithErrors[D3]: ...
def step3_4(d1: D1, d2: D2, d3: D3) -> D4: ...


def step4(b2: B2, c3: C3, d4: D4) -> Out: ...


def missing_error() -> Sequence[str]: ...


def pipeline(in1: In1, in2: In2) -> WithErrors[Out]:
    step1 = optional(step1_1(in1), missing_error).flat_map(partial(step1_2, in2))

    step2 = traverse(step2_1(in1), step2_2).flat_map(step2_3)

    # fmt: off
    step3 = (
        step3_1(in1)
        .flat_map((lambda d1:
            step3_2(d1)
            .flat_map(lambda d2:
                step3_3(d2)
                .map(lambda d3:
                    step3_4(d1, d2, d3))))))
    # fmt: on

    # Result.do(
    #     step3_4(d1, d2, d3)
    #     for d1 in step3_1(in1)
    #     for d2 in step3_2(d1)
    #     for d3 in step3_3(d2)
    # )

    return combine3(step1, step2, step3).using(step4)
