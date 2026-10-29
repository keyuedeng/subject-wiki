"""Reading a structured answer while it streams in. Pure.

Structured outputs arrive as JSON text in schema order, so the answer's markdown can be
shown before the JSON is complete: `partial_string` decodes as much of one string field
as has arrived, stopping before an escape sequence that's cut off.
"""

import re

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
            "t": "\t"}  # fmt: skip
# A citation marker still being written at the end of the text: "[", "[S", "[S1", "[S1,".
_TRAILING_MARKER = re.compile(r"\[\s*(?:S\d*(?:\s*[,;]\s*S?\d*)*)?\s*$")


def partial_string(buffer: str, key: str) -> str | None:
    """The decoded value of string field `key` so far, or None if it hasn't started."""
    m = re.search('"' + re.escape(key) + r'"\s*:\s*"', buffer)
    if m is None:
        return None
    out: list[str] = []
    i, n = m.end(), len(buffer)
    while i < n:
        c = buffer[i]
        if c == '"':
            break
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        e = buffer[i + 1]
        if e != "u":
            out.append(_ESCAPES.get(e, e))
            i += 2
            continue
        code = _hex4(buffer, i + 2)
        if code is None:
            break
        if 0xD800 <= code < 0xDC00:  # high surrogate: needs its low half
            low = _hex4(buffer, i + 8) if buffer[i + 6 : i + 8] == "\\u" else None
            if low is None:
                break
            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
            i += 12
            continue
        out.append(chr(code))
        i += 6
    return "".join(out)


def _hex4(buffer: str, start: int) -> int | None:
    digits = buffer[start : start + 4]
    if len(digits) < 4:
        return None
    try:
        return int(digits, 16)
    except ValueError:
        return None


def trim_partial_marker(text: str) -> str:
    """Drop a citation marker that's only half written, so "[S" never flashes up."""
    return _TRAILING_MARKER.sub("", text)
