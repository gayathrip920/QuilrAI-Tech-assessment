"""Streaming PII redactor.

The problem: an email or card number can be split across two chunks, like
"call me at jo" + "hn@example.com". If we redact each chunk on its own we
miss it. If we wait for the whole response we kill time to first token.

The approach: keep a small buffer and only hold back the part at the end
that could still grow into a match.

  * Emails never contain whitespace, so only the trailing run of
    email-safe characters can still become an email.
  * Card numbers and SSNs are only digits, spaces and dashes, so only the
    trailing run of those (starting at a digit) can still become one.

In normal prose that tail is just the last partial word, so text flows out
almost immediately. The tail is capped at MAX_HOLD characters, so memory
stays bounded no matter how long the response is.
"""
import re

REDACTED = "[REDACTED]"

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}(?![A-Za-z0-9\-])"
)
# 123-45-6789 or 123 45 6789 (same separator twice). Skips ranges that are never issued.
SSN_RE = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}(?![\d-])")
# 13 to 19 digits, optionally grouped with spaces or dashes. Luhn checked below.
CARD_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")

EMAIL_CHARS = re.compile(r"[A-Za-z0-9._%+\-@]")
NUMBER_CHARS = re.compile(r"[0-9 \-]")

MAX_EMAIL_LEN = 254
MAX_NUMBER_LEN = 19 + 18  # 19 digits plus a separator between each pair
MAX_HOLD = MAX_EMAIL_LEN


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_pii(text: str) -> list[tuple[int, int]]:
    """Return sorted, non-overlapping (start, end) spans to redact."""
    spans = [m.span() for m in EMAIL_RE.finditer(text)]
    spans += [m.span() for m in SSN_RE.finditer(text)]
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if luhn_ok(digits):
            spans.append(m.span())
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _trailing_run(text: str, char_re: re.Pattern, limit: int) -> int:
    """Length of the trailing run of characters matching char_re (capped)."""
    n = 0
    for ch in reversed(text):
        if n >= limit or not char_re.match(ch):
            break
        n += 1
    return n


def _holdback(buf: str) -> int:
    email_tail = _trailing_run(buf, EMAIL_CHARS, MAX_EMAIL_LEN)
    num_tail = _trailing_run(buf, NUMBER_CHARS, MAX_NUMBER_LEN)
    # A number match must start with a digit, so drop leading spaces/dashes.
    tail = buf[len(buf) - num_tail:]
    num_tail = len(tail.lstrip(" -"))
    return max(email_tail, num_tail)


def redact(text: str) -> str:
    """Redact a complete string in one go."""
    out, pos = [], 0
    for s, e in find_pii(text):
        out.append(text[pos:s])
        out.append(REDACTED)
        pos = e
    out.append(text[pos:])
    return "".join(out)


class StreamRedactor:
    """Feed text chunks in, get redacted text out as early as it is safe."""

    def __init__(self) -> None:
        self._buf = ""

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        cut = len(self._buf) - _holdback(self._buf)
        # Never hold more than MAX_HOLD characters, even for a long blob with no spaces.
        cut = max(cut, len(self._buf) - MAX_HOLD)
        if cut <= 0:
            return ""
        return self._emit(cut)

    def flush(self) -> str:
        """End of stream: everything left is final, so redact and emit it."""
        return self._emit(len(self._buf))

    def _emit(self, cut: int) -> str:
        buf = self._buf
        out, pos = [], 0
        # Search the whole buffer so boundary checks see the next character,
        # but only act on matches that sit fully before the cut.
        for s, e in find_pii(buf):
            if s >= cut:
                break
            if e > cut:
                # Match crosses the cut (only possible after the MAX_HOLD cap).
                # Move the cut back so the match stays buffered and is redacted later.
                cut = s
                break
            out.append(buf[pos:s])
            out.append(REDACTED)
            pos = e
        out.append(buf[pos:cut])
        self._buf = buf[cut:]
        return "".join(out)
