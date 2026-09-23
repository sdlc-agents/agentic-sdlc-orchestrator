from __future__ import annotations

import pytest

from app.shortcode import RESERVED, decode, encode, generate, is_valid


def test_generate_respects_length():
    assert len(generate(7)) == 7
    assert len(generate(12)) == 12


def test_generated_codes_are_valid_and_unique_enough():
    codes = {generate(7) for _ in range(2000)}
    assert len(codes) == 2000
    assert all(is_valid(code) for code in codes)


@pytest.mark.parametrize("number", [0, 1, 61, 62, 3843, 987654321])
def test_base62_roundtrip(number):
    assert decode(encode(number)) == number


def test_encode_rejects_negative():
    with pytest.raises(ValueError):
        encode(-1)


@pytest.mark.parametrize("code", sorted(RESERVED))
def test_reserved_paths_are_not_valid_codes(code):
    assert is_valid(code) is False


@pytest.mark.parametrize("code", ["abc", "a" * 17, "has space", "sym!bol"])
def test_malformed_codes_rejected(code):
    assert is_valid(code) is False
