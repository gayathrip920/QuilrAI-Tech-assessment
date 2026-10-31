import random

import pytest

from task3_stream_guardrail.redactor import MAX_HOLD, StreamRedactor, luhn_ok, redact

R = "[REDACTED]"


def stream(chunks):
    r = StreamRedactor()
    return "".join(r.feed(c) for c in chunks) + r.flush()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("mail me at john.smith@example.com today", f"mail me at {R} today"),
        ("a+b_c%d@sub.domain.co.uk.", f"{R}."),
        ("ssn 123-45-6789.", f"ssn {R}."),
        ("ssn 123 45 6789", f"ssn {R}"),
        ("visa 4111111111111111 ok", f"visa {R} ok"),
        ("visa 4111 1111 1111 1111 ok", f"visa {R} ok"),
        ("amex 3782-822463-10005", f"amex {R}"),
        ("all: x@y.io, 123-45-6789, 5500 0000 0000 0004", f"all: {R}, {R}, {R}"),
    ],
)
def test_redacts_each_type(text, expected):
    assert redact(text) == expected
    assert stream([text]) == expected


@pytest.mark.parametrize(
    "text",
    [
        "order 1234567812345678 is not a valid card",  # fails Luhn
        "call 555-123-4567",  # phone number, not SSN
        "invalid ssn 000-12-3456 and 666-12-3456 and 900-12-3456",
        "mixed separators 123-45 6789",
        "date 2026-10-30 and version 1.2.3",
        "user@localhost has no TLD",
        "the @ sign alone",
    ],
)
def test_no_false_positives(text):
    assert redact(text) == text
    assert stream(list(text)) == text


def test_luhn():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")


SAMPLE = (
    "Contact jane.doe@example.com or 123-45-6789; card 4111 1111 1111 1111, "
    "backup ops+billing@corp.example.org and 5500-0000-0000-0004. Thanks!"
)


def test_every_two_way_split_matches_one_shot():
    expected = redact(SAMPLE)
    for i in range(len(SAMPLE) + 1):
        assert stream([SAMPLE[:i], SAMPLE[i:]]) == expected, i


def test_char_by_char_matches_one_shot():
    assert stream(list(SAMPLE)) == redact(SAMPLE)


def test_random_chunkings():
    rng = random.Random(1)
    expected = redact(SAMPLE)
    for _ in range(500):
        chunks, i = [], 0
        while i < len(SAMPLE):
            n = rng.randint(1, 12)
            chunks.append(SAMPLE[i:i + n])
            i += n
        assert stream(chunks) == expected


def test_no_raw_pii_ever_leaves_even_mid_stream():
    r = StreamRedactor()
    out = ""
    for ch in SAMPLE:
        out += r.feed(ch)
        for secret in ["jane.doe@", "123-45-6789", "4111 1111 1111 1111", "5500-0000-0000-0004"]:
            assert secret not in out


def test_plain_prose_flows_out_with_only_a_word_held_back():
    r = StreamRedactor()
    out = r.feed("Hello there, this is a normal sentence and some more wor")
    assert out == "Hello there, this is a normal sentence and some more "
    assert r.buffered == len("wor")


def test_memory_is_bounded_on_huge_unbroken_input():
    r = StreamRedactor()
    out = []
    for _ in range(20_000):  # 1 MB of characters with no spaces
        out.append(r.feed("abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"[:50]))
        assert r.buffered <= MAX_HOLD + 50
    out.append(r.flush())
    assert len("".join(out)) == 20_000 * 48


def test_email_inside_long_blob_still_redacted():
    blob = "x" * 1000 + " " + "secret.person@example.com" + " " + "y" * 1000
    assert stream(list(blob)) == redact(blob)
    assert "secret.person" not in stream(list(blob))


# ---------- performance ----------

def _time_stream(text, chunk=8):
    import time

    r = StreamRedactor()
    t = time.perf_counter()
    out = "".join(r.feed(text[i:i + chunk]) for i in range(0, len(text), chunk)) + r.flush()
    return time.perf_counter() - t, out


PARA = "The fox emailed jane.doe@example.com about card 4111 1111 1111 1111 and SSN 123-45-6789. "


def test_throughput_is_far_above_llm_speed():
    # LLMs stream a few KB per second. 1 MB in 8-character chunks must take well under 10 s
    # (it takes about 0.4 s on a laptop).
    text = PARA * (1_000_000 // len(PARA))
    elapsed, out = _time_stream(text)
    assert elapsed < 10, elapsed
    assert out.count(R) == 3 * (1_000_000 // len(PARA))


def test_cost_grows_linearly_not_quadratically():
    small = PARA * 2_000
    big = PARA * 8_000
    t_small, _ = _time_stream(small)
    t_big, _ = _time_stream(big)
    # 4x the input should cost about 4x the time. Quadratic would be about 16x.
    assert t_big / t_small < 8, (t_small, t_big)
