import pytest

from sdf_toolkit.core.model import DelayValues, UnalignableDelayError, Values


class TestValuesAdd:
    def test_add_full(self) -> None:
        result = Values(1, 2, 3) + Values(4, 5, 6)
        assert result == Values(5, 7, 9)

    def test_add_partial_none(self) -> None:
        result = Values(1, None, 3) + Values(4, 5, None)
        assert result == Values(5, None, None)

    def test_add_all_none(self) -> None:
        result = Values() + Values()
        assert result == Values()


class TestValuesSub:
    def test_sub_full(self) -> None:
        result = Values(5, 7, 9) - Values(4, 5, 6)
        assert result == Values(1, 2, 3)

    def test_sub_partial_none(self) -> None:
        result = Values(5, None, 9) - Values(4, 5, None)
        assert result == Values(1, None, None)


class TestValuesNeg:
    def test_neg_full(self) -> None:
        result = -Values(1, 2, 3)
        assert result == Values(-1, -2, -3)

    def test_neg_with_none(self) -> None:
        result = -Values(1, None, 3)
        assert result == Values(-1, None, -3)

    def test_neg_all_none(self) -> None:
        result = -Values()
        assert result == Values()


class TestValuesMul:
    def test_mul_scalar(self) -> None:
        result = Values(1, 2, 3) * 2
        assert result == Values(2, 4, 6)

    def test_rmul_scalar(self) -> None:
        result = 2 * Values(1, 2, 3)
        assert result == Values(2, 4, 6)

    def test_mul_with_none(self) -> None:
        result = Values(1, None, 3) * 2
        assert result == Values(2, None, 6)


class TestValuesApproxEq:
    def test_exact_equal(self) -> None:
        assert Values(1, 2, 3).approx_eq(Values(1, 2, 3))

    def test_within_tolerance(self) -> None:
        assert Values(1.0, 2.0, 3.0).approx_eq(Values(1.0 + 1e-10, 2.0, 3.0))

    def test_outside_tolerance(self) -> None:
        assert not Values(1, 2, 3).approx_eq(Values(1.1, 2, 3))

    def test_both_none_equal(self) -> None:
        assert Values().approx_eq(Values())

    def test_one_none_not_equal(self) -> None:
        assert not Values(1, None, 3).approx_eq(Values(1, 2, 3))

    def test_custom_tolerance(self) -> None:
        assert Values(1.0, 2.0, 3.0).approx_eq(Values(1.05, 2.0, 3.0), tolerance=0.1)
        assert not Values(1.0, 2.0, 3.0).approx_eq(
            Values(1.05, 2.0, 3.0), tolerance=0.01
        )


class TestValuesHash:
    def test_hash_equal(self) -> None:
        assert hash(Values(1, 2, 3)) == hash(Values(1, 2, 3))

    def test_in_set(self) -> None:
        s = {Values(1, 2, 3), Values(4, 5, 6)}
        assert Values(1, 2, 3) in s
        assert Values(4, 5, 6) in s
        assert Values(7, 8, 9) not in s


def _dv(*maxima: float) -> DelayValues:
    return DelayValues(rvalues=tuple(Values.uniform(m) for m in maxima))


class TestDelayValuesArithmetic:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            # Equal counts add position by position.
            (_dv(1, 2), _dv(10, 20), _dv(11, 22)),
            # A 1-value delay times every transition, so it broadcasts.
            (_dv(1), _dv(10, 20, 30), _dv(11, 21, 31)),
            (_dv(10, 20), _dv(1), _dv(11, 21)),
            # Mixed 2 and 3 values expand to the 6-transition form.
            (_dv(1, 2), _dv(10, 20, 30), _dv(11, 22, 31, 11, 32, 22)),
        ],
    )
    def test_add(self, a, b, expected) -> None:
        assert a + b == expected

    def test_sub(self) -> None:
        assert _dv(5, 7) - _dv(4, 5) == _dv(1, 2)

    @pytest.mark.parametrize(
        ("a", "b"), [(_dv(1), _dv(*range(12))), (_dv(1, 2), _dv(*range(12)))]
    )
    def test_add_shorter_to_twelve_raises(self, a, b) -> None:
        with pytest.raises(UnalignableDelayError, match="12-value form"):
            a + b

    def test_mul(self) -> None:
        assert _dv(1, 2) * 3 == _dv(3, 6)

    @pytest.mark.parametrize(
        ("a", "b", "equal"),
        [
            (_dv(1, 2), _dv(1 + 1e-12, 2), True),
            (_dv(1, 2), _dv(1.1, 2), False),
            (_dv(1), _dv(1, 1), False),
        ],
    )
    def test_approx_eq(self, a, b, equal) -> None:
        assert a.approx_eq(b) is equal


class TestRoundTrip:
    def test_add_sub_roundtrip(self) -> None:
        a = Values(1.5, 2.7, 3.9)
        b = Values(4.1, 5.3, 6.8)
        result = a + b - b
        assert result.approx_eq(a)

    def test_delay_add_sub_roundtrip(self) -> None:
        a = _dv(1.5, 2.7, 3.9)
        b = _dv(4.1, 5.3, 6.8)
        assert (a + b - b).approx_eq(a)
