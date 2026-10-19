from __future__ import annotations

from app.base62 import ALPHABET, ID_OFFSET, code_for_id, decode, encode


def test_known_values():
    assert encode(0) == "0"
    assert encode(61) == "z"
    assert encode(62) == "10"
    assert decode("10") == 62
    assert decode(encode(123456789)) == 123456789


def test_roundtrip_is_bijective():
    for n in [0, 1, 60, 61, 62, 12345, ID_OFFSET, 2**31, 10**12]:
        assert decode(encode(n)) == n


def test_alphabet_is_62_unique_chars():
    assert len(ALPHABET) == 62
    assert len(set(ALPHABET)) == 62
    assert ALPHABET.isalnum()


def test_codes_from_ids_are_short_and_unique():
    codes = {code_for_id(i) for i in range(1, 5000)}
    assert len(codes) == 4999
    assert min(len(c) for c in codes) >= 3  # ID_OFFSET 保证不会有 1~2 位码
    assert max(len(c) for c in codes) <= 4


def test_decode_rejects_invalid_characters():
    import pytest

    with pytest.raises(ValueError):
        decode("ab$c")