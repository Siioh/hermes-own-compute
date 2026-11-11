"""Local, pattern-based secret redaction for log and error text.

Pure standard library: compiled regexes with no nested quantifiers, so cost is
linear in the input. Secret parts are replaced with ``[redacted]``; names and
surrounding text are kept. Redacting twice gives the same result.
"""

from __future__ import annotations

import re
from typing import Callable

__all__ = ["redact_text", "redact_tokens", "PATTERNS", "TOKEN_PATTERNS", "REDACTED",
           "HOST_PATTERN_SOURCES"]

REDACTED = "[redacted]"

_NB = r"(?<![A-Za-z0-9_])"
_VENDOR = re.compile(
    _NB + r"(?:"
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"
    r"|gh[pos]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|xox[abposr]-[A-Za-z0-9\-]{10,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|AIza[A-Za-z0-9_\-]{35}"
    r"|glpat-[A-Za-z0-9_\-]{20,}"
    r")")

_PEM = re.compile(
    r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----(?:[^-]|-(?!----END))*"
    r"-----END [A-Z ]{0,30}PRIVATE KEY-----")

_AUTH_HEADER = re.compile(
    r"(\bAuthorization[ \t]*[:=][ \t]*(?:Bearer|Basic)[ \t]+)[^\s\"',;]+", re.I)
_BEARER = re.compile(r"(\bBearer[ \t]+)[A-Za-z0-9._~+/=\-]{8,}", re.I)

_URL_USERINFO = re.compile(
    r"(\b[A-Za-z][A-Za-z0-9+.\-]{0,30}://[^\s:/@]{1,200}:)[^\s@/]{1,500}(@)")

_WORD = r"[A-Za-z0-9_.\-]{0,64}"
_SECRET_WORD = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD)"
_NAME = _WORD + _SECRET_WORD + _WORD
# NAME=value, NAME: value, "NAME": "value", --name=value
_ASSIGN = re.compile(
    r"(?<![A-Za-z0-9_.\-])([\"']?)(" + _NAME + r")\1([ \t]*[:=][ \t]*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;&]+)", re.I)
# --name value
_FLAG = re.compile(
    r"(?<![\w\-])(--" + _NAME + r")([ \t]+)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|(?!-)[^\s\"']+)", re.I)

_PLURAL_OK = ("tokens", "keys")  # e.g. max_tokens=512, num_keys=3: counts, not secrets


def _mask_value(value: str) -> str:
    if value[:1] in "\"'" and len(value) >= 2:
        return value[0] + REDACTED + value[0]
    return REDACTED


def _assign(m: re.Match) -> str:
    if m.group(2).lower().endswith(_PLURAL_OK):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{_mask_value(m.group(4))}"


def _flag(m: re.Match) -> str:
    if m.group(1).lower().endswith(_PLURAL_OK):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}{_mask_value(m.group(3))}"


# (name, regex, replacement) in application order.
PATTERNS: list[tuple[str, re.Pattern, str | Callable[[re.Match], str]]] = [
    ("vendor-key", _VENDOR, REDACTED),
    ("pem-private-key", _PEM, REDACTED),
    ("authorization-header", _AUTH_HEADER, r"\1" + REDACTED),
    ("bearer-token", _BEARER, r"\1" + REDACTED),
    ("url-password", _URL_USERINFO, r"\1" + REDACTED + r"\2"),
    ("name-value", _ASSIGN, _assign),
    ("cli-flag", _FLAG, _flag),
]

# Secret-shaped token regexes for ``ctx.register_redaction_patterns``. The host needs >= 2 literal
# leading characters and no top-level alternation, so each vendor form is its own pattern.
HOST_PATTERN_SOURCES: tuple[str, ...] = (
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}",
    r"gh[pos]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"hf_[A-Za-z0-9]{20,}",
    r"xox[abposr]-[A-Za-z0-9\-]{10,}",
    r"AKIA[A-Z0-9]{16}",
    r"ASIA[A-Z0-9]{16}",
    r"AIza[A-Za-z0-9_\-]{35}",
    r"glpat-[A-Za-z0-9_\-]{20,}",
)


#: The patterns that match a secret by its own shape (vendor keys, private keys, bearer and
#: Authorization values, passwords in URLs), not by a nearby name: they do not hide ordinary
#: words, so they are also applied to environment output (see :func:`redact_tokens`).
TOKEN_PATTERNS = [p for p in PATTERNS if p[0] not in ("name-value", "cli-flag")]


def redact_tokens(text: str) -> str:
    """Mask only secret-shaped tokens in ``text`` (no guessing from names such as ``secret_set``)."""
    out = text or ""
    for _name, rx, repl in TOKEN_PATTERNS:
        out = rx.sub(repl, out)
    return out


def redact_text(text: str) -> str:
    """Mask secret-looking substrings in ``text``."""
    out = text or ""
    for _name, rx, repl in PATTERNS:
        out = rx.sub(repl, out)
    return out
