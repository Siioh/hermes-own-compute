"""Tests for cloud_compute_core.redact (fake values only, built by concatenation)."""

from __future__ import annotations

import time

import pytest

from cloud_compute_core.redact import PATTERNS, redact_text

R = "[redacted]"
TAIL = "AbCdEfGhIjKlMnOpQrStUvWx0123"  # 28 chars


@pytest.mark.parametrize("secret", [
    "sk-" + TAIL,
    "sk-ant-" + TAIL,
    "sk-proj-" + TAIL,
    "ghp_" + TAIL,
    "gho_" + TAIL,
    "ghs_" + TAIL,
    "github_pat_" + TAIL,
    "hf_" + TAIL,
    "xoxb-" + "1234567890-abcdef",
    "xoxp-" + "1234567890-abcdef",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "ASIA" + "ABCDEFGHIJKLMNOP",
    "AIza" + "A" * 35,
    "glpat-" + TAIL,
])
def test_vendor_keys(secret):
    assert redact_text(f"using {secret} now") == f"using {R} now"


def test_authorization_and_bearer():
    assert redact_text("Authorization: Bearer abc.def-123") == f"Authorization: Bearer {R}"
    assert redact_text("authorization: Basic dXNlcjpwYXNz") == f"authorization: Basic {R}"
    assert redact_text("curl -H 'x' Bearer abcdefgh12345") == f"curl -H 'x' Bearer {R}"


def test_pem_block():
    pem = ("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc/+=\nline2\n"
           "-----END " + "RSA PRIVATE KEY-----")
    assert redact_text(f"before\n{pem}\nafter") == f"before\n{R}\nafter"


@pytest.mark.parametrize("src,want", [
    ("API_KEY=hunter2", f"API_KEY={R}"),
    ("db_password: hunter2 ok", f"db_password: {R} ok"),
    ('{"client_secret": "hunter2", "n": 1}', f'{{"client_secret": "{R}", "n": 1}}'),
    ("--api-token hunter2 --x", f"--api-token {R} --x"),
    ("--password=hunter2", f"--password={R}"),
    ("PWD=/home/u", f"PWD={R}"),
    ("passwd='a b'", f"passwd='{R}'"),
])
def test_name_value_forms(src, want):
    assert redact_text(src) == want


def test_url_userinfo():
    assert redact_text("clone https://bob:hunter2@example.com/r.git") == \
        f"clone https://bob:{R}@example.com/r.git"


@pytest.mark.parametrize("line", [
    "Cloning into /home/user/proj/src/main.py",
    "HEAD is now at " + "a1b2c3d4e5f6" * 3 + "a1b2c3d4",  # 40 hex
    "container " + "0123456789abcdef" * 4 + " started",     # 64 hex
    "retries=3 timeout=30 host=x1carbon user=bob",
    "max_tokens=512 num_keys=3",
    "https://example.com:8080/path?x=1",
    "ssh://bob@example.com/repo",
    "step 12/40 loss=0.0231 lr=3e-4",
    "task-abcdefghijklmnopqrstuvwxyz keyboard layout",
    "password:",
    "2026-11-08T10:00:00Z INFO job 42 done in 3.5s",
])
def test_ordinary_lines_unchanged(line):
    assert redact_text(line) == line


def test_idempotent():
    src = ("Authorization: Bearer abcdefgh1234 API_KEY=hunter2 \"token\": \"zz\" "
           "https://u:pw@h/x --secret s3 sk-" + TAIL)
    once = redact_text(src)
    assert R in once and "hunter2" not in once and "pw@" not in once
    assert redact_text(once) == once


def test_empty_and_none():
    assert redact_text("") == ""
    assert redact_text(None) == ""


def test_pattern_list_nonempty():
    assert len(PATTERNS) >= 7


def _inputs(n):
    chunk = ("INFO step done path=/a/b/c sha=" + "ab12" * 10 + " API_KEY=hunter2 "
             "-----BEGIN x " + "a" * 30 + " token:\n")
    return [(chunk * (n // len(chunk) + 1))[:n], "a" * n, "-" * n]


def _best_time(n, repeats=3):
    """Fastest of a few runs: robust against a busy machine."""
    best = float("inf")
    for _ in range(repeats):
        t = time.perf_counter()
        for data in _inputs(n):
            redact_text(data)
        best = min(best, time.perf_counter() - t)
    return best


def test_large_input_is_fast():
    # Generous absolute bound: only a pathological regex gets near it (it would not finish).
    assert _best_time(1_000_000, repeats=1) < 5.0


def test_redaction_time_grows_linearly():
    """Catastrophic backtracking is quadratic or worse: doubling the input must stay near 2x."""
    _best_time(100_000, repeats=1)              # warm-up (regex caches, allocator)
    small = _best_time(400_000)
    big = _best_time(800_000)
    # Linear is ~2x, quadratic ~4x: 3x (plus a little timer slack) separates them.
    assert big < 3 * small + 0.03, f"2x input took {big / small:.1f}x as long"


def test_redact_tokens_masks_secret_shapes_but_not_ordinary_words():
    from cloud_compute_core.redact import redact_tokens

    key = "sk-" + "Zz9Yy8Xx7Ww6Vv5Uu4"          # made-up, vendor-shaped
    assert redact_tokens(f"secret_set=yes api_key_id=7 {key}") == \
        "secret_set=yes api_key_id=7 [redacted]"
    assert redact_tokens("Authorization: Bearer abcdefgh12345678") == \
        "Authorization: Bearer [redacted]"
