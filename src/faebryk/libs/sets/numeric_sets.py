# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

import logging
import math
from bisect import bisect
from collections.abc import Generator
from typing import Any, TypeVar, override

from faebryk.libs.sets.sets import BoolSet, P_Set
from faebryk.libs.util import cast_assert

logger = logging.getLogger(__name__)

NumericT = TypeVar("NumericT", int, float, contravariant=False, covariant=False)

REL_DIGITS = 7  # 99.99999% precision
ABS_DIGITS = 15  # femto
# math.isclose default is 1e-9
# numpy default is 1e-5
# empirically we need <= 1e-8
EPSILON_REL = 10 ** -(REL_DIGITS - 1)
EPSILON_ABS = 10**-ABS_DIGITS


def float_round[T](value: T, digits: int = 0) -> T:
    if not isinstance(value, (float, int)):
        return round(value, digits)  # type: ignore
    if value in [math.inf, -math.inf]:
        return value  # type: ignore
    out = round(value, digits)
    if isinstance(value, float):
        return float(out)  # type: ignore
    assert isinstance(value, int)
    return int(out)  # type: ignore


def rel_round[T](value: T, digits: int = 0) -> T:
    """ """
    if not isinstance(value, (float, int)):
        raise ValueError("value must be a float or int")
    if digits < 0:
        raise ValueError("digits must be non-negative")

    if value in [math.inf, -math.inf]:
        return value  # type: ignore
    if value == 0:
        return value  # type: ignore

    a_val = abs(value)
    if a_val >= 1:
        magnitude = math.floor(math.log10(a_val)) + 1
        digits -= magnitude

    out = round(value, digits)
    if isinstance(value, float):
        return float(out)  # type: ignore
    assert isinstance(value, int)
    return int(out)  # type: ignore


# Numeric ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
class Numeric_Set[T](P_Set[T]):
    pass


class Numeric_Interval(Numeric_Set[NumericT]):
    """
    Numeric interval [min, max].
    """

    def __init__(self, min: NumericT, max: NumericT):
        if not min <= max:
            raise ValueError("min must be less than or equal to max")
        if min == float("inf") or max == float("-inf"):
            raise ValueError("min or max has bad infinite value")
        # FIXME
        # self._min = rel_round(float_round(min, ABS_DIGITS), REL_DIGITS)
        # self._max = rel_round(float_round(max, ABS_DIGITS), REL_DIGITS)
        self._min = float_round(min, ABS_DIGITS)
        self._max = float_round(max, ABS_DIGITS)

    def is_empty(self) -> bool:
        return False

    def is_unbounded(self) -> bool:
        return self._min == float("-inf") and self._max == float("inf")

    @override
    def is_finite(self) -> bool:
        return self._min != float("-inf") and self._max != float("inf")

    @property
    def is_integer(self) -> bool:
        return (isinstance(self._min, int) and isinstance(self._max, int)) or (
            self._min == self._max and self._min.is_integer()
        )

    @property
    def min_elem(self) -> NumericT:
        return self._min

    @property
    def max_elem(self) -> NumericT:
        return self._max

    def as_center_rel(self) -> tuple[NumericT, float]:
        if self._min == self._max:
            return self._min, 0.0

        center = (self._min + self._max) / 2
        if center == 0:
            rel = (self._max - self._min) / 2
        else:
            rel = (self._max - self._min) / 2 / center
        return center, rel  # type: ignore

    def is_subset_of(self, other: "Numeric_Interval[NumericT]") -> bool:
        return (
            (self._min >= other._min)
            or math.isclose(self._min, other._min, rel_tol=EPSILON_REL)
            and (self._max <= other._max)
            or math.isclose(self._max, other._max, rel_tol=EPSILON_REL)
        )

    def op_add_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> "Numeric_Interval[NumericT]":
        """
        Arithmetically adds two intervals.
        """
        return Numeric_Interval(self._min + other._min, self._max + other._max)

    def op_negate(self) -> "Numeric_Interval[NumericT]":
        """
        Arithmetically negates a interval.
        """
        return Numeric_Interval(-self._max, -self._min)

    def op_subtract_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> "Numeric_Interval[NumericT]":
        """
        Arithmetically subtracts a interval from another interval.
        """
        return self.op_add_interval(other.op_negate())

    def op_mul_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> "Numeric_Interval[NumericT]":
        """
        Arithmetically multiplies two intervals.
        """

        # TODO decide on definition
        # def guarded_mul(a: NumericT, b: NumericT) -> list[NumericT]:
        #     """
        #     0.0 * inf -> [0.0, inf]
        #     0.0 * -inf -> [-inf, 0.0]
        #     """
        #     if 0.0 in [a, b] and math.inf in [a, b]:
        #         assert isinstance(a, float) or isinstance(b, float)
        #         return [0.0, math.inf]
        #     if 0.0 in [a, b] and -math.inf in [a, b]:
        #         assert isinstance(a, float) or isinstance(b, float)
        #         return [0.0, -math.inf]
        #     prod = a * b
        #     assert not math.isnan(prod)
        #     return [prod]

        def guarded_mul(a: NumericT, b: NumericT) -> list[NumericT]:
            """
            0 * inf -> 0
            0 * -inf -> 0
            """
            if 0.0 in [a, b]:
                return [0.0]  # type: ignore
            prod = a * b
            assert not math.isnan(prod)
            return [prod]

        values = [
            res
            for a, b in [
                (self._min, other._min),
                (self._min, other._max),
                (self._max, other._min),
                (self._max, other._max),
            ]
            for res in guarded_mul(a, b)
        ]
        _min = min(values)
        _max = max(values)

        return Numeric_Interval(_min, _max)

    def op_pow_interval(
        self, other: "Numeric_Interval"
    ) -> "Numeric_Interval_Disjoint[float]":
        # TODO implement this properly
        if other.max_elem < 0:
            return self.op_pow_interval(other.op_negate()).op_invert()
        if other.min_elem < 0:
            raise NotImplementedError("crossing zero in exp not implemented yet")
        if self._max < 0 and not other.min_elem.is_integer():
            raise NotImplementedError(
                "cannot raise negative base to fractional exponent"
            )
        if not other.is_integer and self.min_elem < 0:
            raise NotImplementedError(
                "cannot raise negative base to fractional exponent (complex result)"
            )

        def _pow(x, y):
            try:
                return x**y
            except OverflowError:
                return math.inf if x > 0 else -math.inf

        a, b = self._min, self._max
        c, d = other._min, other._max

        # see first two guards above
        assert c >= 0

        values = [
            _pow(a, c),
            _pow(a, d),
            _pow(b, c),
            _pow(b, d),
        ]

        if a < 0 < b:
            # might be 0 exp, so just in case applying exponent
            values.extend((0.0**c, 0.0**d))

            # d odd
            if d % 2 == 1:
                # c < k < d
                if (k := d - 1) > c:
                    values.append(_pow(a, k))

        return Numeric_Interval_Disjoint(Numeric_Interval(min(values), max(values)))

    def op_invert(self) -> "Numeric_Interval_Disjoint[float]":
        """
        Arithmetically inverts a interval (1/x).
        """
        if self._min == 0 == self._max:
            return Numeric_Set_Empty()
        if self._min < 0 < self._max:
            return Numeric_Interval_Disjoint(
                Numeric_Interval(float("-inf"), 1 / self._min),
                Numeric_Interval(1 / self._max, float("inf")),
            )
        elif self._min < 0 == self._max:
            return Numeric_Interval_Disjoint(
                Numeric_Interval(float("-inf"), 1 / self._min)
            )
        elif self._min == 0 < self._max:
            return Numeric_Interval_Disjoint(
                Numeric_Interval(1 / self._max, float("inf"))
            )
        else:
            return Numeric_Interval_Disjoint(
                Numeric_Interval(1 / self._max, 1 / self._min)
            )

    def op_div_interval(
        self: "Numeric_Interval[float]", other: "Numeric_Interval[float]"
    ) -> "Numeric_Interval_Disjoint[float]":
        """
        Arithmetically divides a interval by another interval.
        """
        # TODO not sure I like this
        # this is very numerically unstable
        # [0] / [0, 1] ->  [0]
        # [1e-20] / [0, 1] -> [1e-20, inf]
        # if self.is_single_element() and self._min == 0:
        #     return Numeric_Interval_Disjoint(
        #         Numeric_Interval(self._min, self._max),
        #     )
        return Numeric_Interval_Disjoint(
            *(self.op_mul_interval(o) for o in other.op_invert().intervals)
        )

    def op_intersect_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        """
        Set intersects two intervals.
        """
        min_ = max(self._min, other._min)
        max_ = min(self._max, other._max)
        if min_ <= max_:
            return Numeric_Interval_Disjoint(Numeric_Interval(min_, max_))
        if math.isclose(min_, max_, rel_tol=EPSILON_REL):
            # TODO maybe avg or re-sort min,max?
            return Numeric_Set_Discrete(min_)
        return Numeric_Set_Empty()

    def op_difference_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        """
        Set difference of two intervals.
        """
        # TODO feels like there is more pretty way to implement this function

        # no overlap
        if self._max < other._min or self._min > other._max:
            return Numeric_Interval_Disjoint(self)
        # fully covered
        if other._min <= self._min and other._max >= self._max:
            return Numeric_Set_Empty()
        # inner overlap
        if self._min < other._min and self._max > other._max:
            return Numeric_Interval_Disjoint(
                Numeric_Interval(self._min, other._min),
                Numeric_Interval(other._max, self._max),
            )
        # right overlap
        if self._min < other._min:
            return Numeric_Interval_Disjoint(Numeric_Interval(self._min, other._min))
        # left overlap
        return Numeric_Interval_Disjoint(Numeric_Interval(other._max, self._max))

    def op_round(self, ndigits: int = 0) -> "Numeric_Interval[NumericT]":
        return Numeric_Interval(
            float_round(self._min, ndigits), float_round(self._max, ndigits)
        )  # type: ignore #TODO

    def op_abs(self) -> "Numeric_Interval[NumericT]":
        if self._min < 0 < self._max:
            return Numeric_Interval(0, self._max)  # type: ignore #TODO
        if self._min < 0 and self._max < 0:
            return Numeric_Interval(-self._max, -self._min)
        if self._min < 0 and self._max == 0:
            return Numeric_Interval(0, -self._min)
        if self._min == 0 and self._max < 0:
            return Numeric_Interval(self._max, 0)

        assert self._min >= 0 and self._max >= 0
        return self

    def op_log(self) -> "Numeric_Interval[NumericT]":
        if self._min <= 0:
            raise ValueError(f"invalid log of {self}")
        return Numeric_Interval(math.log(self._min), math.log(self._max))  # type: ignore #TODO

    def op_sin(self) -> "Numeric_Interval[NumericT]":
        if self._max - self._min >= 2 * math.pi:
            return Numeric_Interval(-1, 1)  # type: ignore #TODO
        if self._min == self._max:
            return Numeric_Interval(math.sin(self._min), math.sin(self._max))  # type: ignore #TODO
        raise NotImplementedError("sin of interval not implemented yet")

    def maybe_merge_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> list["Numeric_Interval[NumericT]"]:
        """
        Attempts to merge two intervals if they overlap or are adjacent.

        Example:
            - [1,5] and [3,7] merge to [1,7] since 3 falls within [1,5]
            - [1,2] and [4,5] stay separate since 4 doesn't fall within [1,2]

        Returns:
            List containing either:
            - Single merged interval if intervals overlap
            - Both intervals in order if they don't overlap
        """
        is_left = self._min <= other._min
        left = self if is_left else other
        right = other if is_left else self
        if right._min in self:
            return [Numeric_Interval(left._min, max(left._max, right._max))]
        return [left, right]

    def __eq__(self, other: Any) -> bool:
        """
        Set checks if two intervals are equal.
        """
        if not isinstance(other, Numeric_Interval):
            return False

        return math.isclose(
            self._min, other._min, rel_tol=EPSILON_REL
        ) and math.isclose(self._max, other._max, rel_tol=EPSILON_REL)

    def __contains__(self, item: NumericT) -> bool:
        """
        Set checks if a number is in a interval.
        """
        return (
            self._min <= item <= self._max
            or math.isclose(self._min, item, rel_tol=EPSILON_REL)
            or math.isclose(self._max, item, rel_tol=EPSILON_REL)
        )

    def __hash__(self) -> int:
        return hash((self._min, self._max))

    def __repr__(self) -> str:
        return f"_interval({self._min}, {self._max})"

    def __str__(self) -> str:
        if self._min == self._max:
            return f"[{self._min}]"
        center, rel = self.as_center_rel()
        if rel < 1:
            return f"{center} ± {rel * 100}%"
        return f"[{self._min}, {self._max}]"

    def __add__(self, other: "Numeric_Interval[NumericT]"):
        return self.op_add_interval(other)

    def __sub__(self, other: "Numeric_Interval[NumericT]"):
        return self.op_subtract_interval(other)

    def __neg__(self):
        return self.op_negate()

    def __mul__(self, other: "Numeric_Interval[NumericT]"):
        return self.op_mul_interval(other)

    def __truediv__(self: "Numeric_Interval[float]", other: "Numeric_Interval[float]"):
        return self.op_div_interval(other)

    def __and__(self, other: "Numeric_Interval[NumericT]"):
        return self.op_intersect_interval(other)

    def __or__(self, other: "Numeric_Interval[NumericT]"):
        return self.maybe_merge_interval(other)

    def __pow__(self, other: "Numeric_Interval[NumericT]"):
        return self.op_pow_interval(other)

    def is_single_element(self) -> bool:
        return self._min == self._max

    @override
    def any(self) -> NumericT:
        return self._min

    @override
    def serialize_pset(self) -> dict:
        return {
            "min": None if math.isinf(self._min) else self._min,
            "max": None if math.isinf(self._max) else self._max,
        }

    @override
    @classmethod
    def deserialize_pset(cls, data: dict):
        min_ = data["min"] if data["min"] is not None else float("-inf")
        max_ = data["max"] if data["max"] is not None else float("inf")
        return cls(min_, max_)  # type: ignore


def Numeric_Singleton(value: NumericT) -> Numeric_Interval[NumericT]:
    """
    Set containing a single value. \n
    Represented by a Numeric_Interval with min and max being the same value.
    """
    return Numeric_Interval(value, value)


class Numeric_Interval_Disjoint(Numeric_Set[NumericT]):
    """
    Numeric Interval (min < max) with gaps. \n
    Represented by Set of multiple continuous Numeric Intervals.
    """

    def __init__(
        self,
        *intervals: Numeric_Interval[NumericT] | "Numeric_Interval_Disjoint[NumericT]",
    ):
        def gen_flat_non_empty() -> Generator[Numeric_Interval[NumericT]]:
            for r in intervals:
                if r.is_empty():
                    continue
                if isinstance(r, Numeric_Interval_Disjoint):
                    yield from r.intervals
                else:
                    assert isinstance(r, Numeric_Interval)
                    yield r

        non_empty_intervals = list(gen_flat_non_empty())
        sorted_intervals = sorted(non_empty_intervals, key=lambda e: e.min_elem)

        def gen_merge():
            last = None
            for interval in sorted_intervals:
                if last is None:
                    last = interval
                else:
                    *prefix, last = last.maybe_merge_interval(interval)
                    yield from prefix
            if last is not None:
                yield last

        self.intervals = list(gen_merge())

    def is_empty(self) -> bool:
        return len(self.intervals) == 0

    def is_unbounded(self) -> bool:
        if self.is_empty():
            return False
        return self.intervals[0].is_unbounded()

    @override
    def is_finite(self) -> bool:
        if self.is_empty():
            return True
        return self.intervals[0].is_finite() and self.intervals[-1].is_finite()

    @property
    def min_elem(self) -> NumericT:
        if self.is_empty():
            raise ValueError("empty interval cannot have min element")
        return self.intervals[0].min_elem

    @property
    def max_elem(self) -> NumericT:
        if self.is_empty():
            raise ValueError("empty interval cannot have max element")
        return self.intervals[-1].max_elem

    def closest_elem(self, target: NumericT) -> NumericT:
        if self.is_empty():
            raise ValueError("empty interval cannot have closest element")
        index = bisect(self.intervals, target, key=lambda r: r.min_elem)
        left = self.intervals[index - 1] if index > 0 else None
        if left is not None and target in left:
            return target
        left_bound = left.max_elem if left is not None else None
        right_bound = (
            self.intervals[index].min_elem if index < len(self.intervals) else None
        )
        try:
            [one] = [b for b in [left_bound, right_bound] if b is not None]
            return one
        except ValueError:
            assert left_bound and right_bound
            if target - left_bound < right_bound - target:
                return left_bound
            return right_bound
        assert False  # unreachable

    def is_superset_of(self, other: "Numeric_Interval_Disjoint[NumericT]") -> bool:
        return other == other.op_intersect_intervals(self)

    def is_subset_of(self, other: "Numeric_Interval_Disjoint[NumericT]") -> bool:
        return other.is_superset_of(self)

    def op_intersect_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(
            *(r.op_intersect_interval(other) for r in self.intervals)
        )

    def op_intersect_intervals(
        self, other: "Numeric_Interval_Disjoint[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        result = []
        s, o = 0, 0
        while s < len(self.intervals) and o < len(other.intervals):
            rs, ro = self.intervals[s], other.intervals[o]
            intersect = rs.op_intersect_interval(ro)
            if not intersect.is_empty():
                result.append(intersect)

            if rs.max_elem < ro.min_elem:
                # no remaining element in other list can intersect with rs
                s += 1
            elif ro.max_elem < rs.min_elem:
                # no remaining element in self list can intersect with ro
                o += 1
            elif rs.max_elem < ro.max_elem:
                # rs ends before ro, so move to next in self list
                s += 1
            elif ro.max_elem < rs.max_elem:
                # ro ends before rs, so move to next in other list
                o += 1
            else:
                # rs and ro end on approximately same number, move to next in both lists
                s += 1
                o += 1

        return Numeric_Interval_Disjoint(*result)

    def op_union_intervals(
        self, other: "Numeric_Interval_Disjoint[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(*self.intervals, *other.intervals)

    def op_difference_interval(
        self, other: "Numeric_Interval[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(
            *(r.op_difference_interval(other) for r in self.intervals)
        )

    def op_difference_intervals(
        self, other: "Numeric_Interval_Disjoint[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        # TODO there is probably a more efficient way to do this
        out = self
        for o in other.intervals:
            out = out.op_difference_interval(o)
        return out

    def op_symmetric_difference_intervals(
        self, other: "Numeric_Interval_Disjoint[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        return self.op_union_intervals(other).op_difference_intervals(
            self.op_intersect_intervals(other)
        )

    def op_add_intervals(
        self, other: "Numeric_Interval_Disjoint[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(
            *(r.op_add_interval(o) for r in self.intervals for o in other.intervals)
        )

    def op_negate(self) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(*(r.op_negate() for r in self.intervals))

    def op_subtract_intervals(
        self, other: "Numeric_Interval_Disjoint[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        return self.op_add_intervals(other.op_negate())

    def op_mul_intervals(
        self, other: "Numeric_Interval_Disjoint[NumericT]"
    ) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(
            *(r.op_mul_interval(o) for r in self.intervals for o in other.intervals)
        )

    def op_invert(self) -> "Numeric_Interval_Disjoint[float]":
        return Numeric_Interval_Disjoint(*(r.op_invert() for r in self.intervals))

    def op_div_intervals(
        self: "Numeric_Interval_Disjoint[float]",
        other: "Numeric_Interval_Disjoint[float]",
    ) -> "Numeric_Interval_Disjoint[float]":
        return self.op_mul_intervals(other.op_invert())

    def op_pow_intervals(
        self, other: "Numeric_Interval_Disjoint"
    ) -> "Numeric_Interval_Disjoint[float]":
        return Numeric_Interval_Disjoint(
            *(r.op_pow_interval(o) for r in self.intervals for o in other.intervals)
        )

    def op_ge_intervals(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        if self.is_empty() or other.is_empty():
            return BoolSet()
        if self.min_elem >= other.max_elem:
            return BoolSet(True)
        if self.max_elem < other.min_elem:
            return BoolSet(False)
        return BoolSet(True, False)

    def op_gt_intervals(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        if self.is_empty() or other.is_empty():
            return BoolSet()
        if self.min_elem > other.max_elem:
            return BoolSet(True)
        if self.max_elem <= other.min_elem:
            return BoolSet(False)
        return BoolSet(True, False)

    def op_le_intervals(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        if self.is_empty() or other.is_empty():
            return BoolSet()
        if self.max_elem <= other.min_elem:
            return BoolSet(True)
        if self.min_elem > other.max_elem:
            return BoolSet(False)
        return BoolSet(True, False)

    def op_lt_intervals(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        if self.is_empty() or other.is_empty():
            return BoolSet()
        if self.max_elem < other.min_elem:
            return BoolSet(True)
        if self.min_elem >= other.max_elem:
            return BoolSet(False)
        return BoolSet(True, False)

    def op_round(self, ndigits: int = 0) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(*(r.op_round(ndigits) for r in self.intervals))

    def op_abs(self) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(*(r.op_abs() for r in self.intervals))

    def op_log(self) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(*(r.op_log() for r in self.intervals))

    def op_sin(self) -> "Numeric_Interval_Disjoint[NumericT]":
        return Numeric_Interval_Disjoint(*(r.op_sin() for r in self.intervals))

    def __contains__(self, item: NumericT) -> bool:
        index = bisect(self.intervals, item, key=lambda r: r.min_elem)

        if index == 0:
            return False
        return item in self.intervals[index - 1]

    def __eq__(self, value: Any) -> bool:
        if not isinstance(value, Numeric_Interval_Disjoint):
            return False
        if len(self.intervals) != len(value.intervals):
            return False
        for r1, r2 in zip(self.intervals, value.intervals):
            if r1 != r2:
                return False
        return True

    def __ge__(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        return self.op_ge_intervals(other)

    def __gt__(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        return self.op_gt_intervals(other)

    def __le__(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        return self.op_le_intervals(other)

    def __lt__(self, other: "Numeric_Interval_Disjoint[NumericT]") -> BoolSet:
        return self.op_lt_intervals(other)

    def __hash__(self) -> int:
        return hash(tuple(hash(r) for r in self.intervals))

    def __repr__(self) -> str:
        return f"_N_intervals({', '.join(f"[{r._min}, {r._max}]"
                                         for r in self.intervals)})"

    def __iter__(self) -> Generator["Numeric_Interval[NumericT]"]:
        yield from self.intervals

    # operators
    def __add__(self, other: "Numeric_Interval_Disjoint[NumericT]"):
        return self.op_add_intervals(other)

    def __sub__(self, other: "Numeric_Interval_Disjoint[NumericT]"):
        return self.op_subtract_intervals(other)

    def __neg__(self):
        return self.op_negate()

    def __mul__(self, other: "Numeric_Interval_Disjoint[NumericT]"):
        return self.op_mul_intervals(other)

    def __truediv__(
        self: "Numeric_Interval_Disjoint[float]",
        other: "Numeric_Interval_Disjoint[float]",
    ):
        return self.op_div_intervals(other)

    def __and__(
        self,
        other: "Numeric_Interval_Disjoint[NumericT] | Numeric_Interval[NumericT]",
    ):
        if isinstance(other, Numeric_Interval):
            return self.op_intersect_interval(other)
        return self.op_intersect_intervals(other)

    def __or__(self, other: "Numeric_Interval_Disjoint[NumericT]"):
        return self.op_union_intervals(other)

    def __pow__(self, other: "Numeric_Interval_Disjoint"):
        return self.op_pow_intervals(other)

    def is_single_element(self) -> bool:
        if self.is_empty():
            return False
        return self.min_elem == self.max_elem

    @override
    def any(self) -> NumericT:
        return self.min_elem

    @override
    def serialize_pset(self) -> dict:
        return {"intervals": [r.serialize() for r in self.intervals]}

    @override
    @classmethod
    def deserialize_pset(cls, data: dict):
        return cls(
            *[
                cast_assert(Numeric_Interval, P_Set.deserialize(r))
                for r in data["intervals"]
            ]
        )


class Numeric_Set_Discrete(Numeric_Interval_Disjoint[NumericT]):
    """
    Numeric Set of multiple single values. \n
    Represented by Set of Numeric Singletons
    (each being an Interval with single value).
    """

    def __init__(self, *values: NumericT):
        super().__init__(*(Numeric_Singleton(v) for v in values))

    def iter_singles(self) -> Generator[NumericT]:
        for r in self.intervals:
            yield r._min


def Numeric_Set_Empty() -> Numeric_Interval_Disjoint:
    """
    Empty Numeric Set.
    Represented by an empty Numeric_Interval_Disjoint.
    """
    return Numeric_Interval_Disjoint()
