"""Base62 编解码：把自增主键压成短码。

为什么不用随机串：随机串需要「生成 -> 查重 -> 重试」，QPS 高时和 DB 往返次数成正比。
自增 ID 做双射编码天生无冲突、长度可控，且靠 UNIQUE 约束兜底自定义别名。
"""

from __future__ import annotations

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(ALPHABET)
_INDEX = {c: i for i, c in enumerate(ALPHABET)}

# 让短码至少 3 位：id=100000 -> "q0U"，避免出现 1 位码被扫号遍历
ID_OFFSET = 100_000


def encode(number: int) -> str:
    if number < 0:
        raise ValueError("number must be >= 0")
    if number == 0:
        return ALPHABET[0]
    out = []
    while number:
        number, rem = divmod(number, _BASE)
        out.append(ALPHABET[rem])
    return "".join(reversed(out))


def decode(code: str) -> int:
    number = 0
    for ch in code:
        if ch not in _INDEX:
            raise ValueError(f"invalid base62 character: {ch!r}")
        number = number * _BASE + _INDEX[ch]
    return number


def code_for_id(row_id: int) -> str:
    return encode(row_id + ID_OFFSET)