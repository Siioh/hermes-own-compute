"""Static checks for desktop/plugin.js plus a run of the node smoke harness.

Reads W/desktop/plugin.js (never writes inside the repository). The node
harness (tests/js) renders the plugin against a stubbed SDK and the REST
fixtures; that test is skipped only when node is not on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = ROOT / "desktop" / "plugin.js"
SDK_EXPORTS = ROOT / "tests" / "js" / "stubs" / "sdk-exports.json"

ALLOWED_SPECIFIERS = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}

# Built by concatenation so this file does not itself contain the words it forbids.
FORBIDDEN_TEXT = [
    "AGENTS" + ".md",
    "CLAU" + "DE" + ".md",
    "ws" + ".py",
    "sub" + "agent",
    "Cla" + "ude",
    "Anthro" + "pic",
    "Co-auth" + "ored-by",
]


# ---------------------------------------------------------------- JS scrubbing


class _Scanner:
    """Minimal JavaScript scanner: drops comments, optionally blanks string/regex contents."""

    _REGEX_AFTER_WORDS = {"return", "typeof", "case", "delete", "void", "throw", "in", "of", "new", "else", "do"}

    def __init__(self, src: str, blank_strings: bool) -> None:
        self.src = src
        self.blank = blank_strings
        self.n = len(src)
        self.prev = ""  # last significant character
        self.word = ""  # last identifier when prev is an identifier character
        self.strings: list[str] = []  # string and template literal contents, in order

    def _fill(self, text: str) -> str:
        if not self.blank:
            return text

        return "".join(c if c == "\n" else " " for c in text)

    def _regex_allowed(self) -> bool:
        if self.prev == "":
            return True

        if self.prev in "(,=:[!&|?{};+-*%<>~^":
            return True

        return self.word in self._REGEX_AFTER_WORDS

    def scan(self, i: int = 0, stop_at_brace: bool = False) -> tuple[int, str]:
        src, n = self.src, self.n
        out: list[str] = []
        depth = 0

        while i < n:
            c = src[i]

            if src.startswith("//", i):
                j = src.find("\n", i)
                i = n if j < 0 else j
                continue

            if src.startswith("/*", i):
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                out.append("".join("\n" if ch == "\n" else " " for ch in src[i:j]))
                i = j
                continue

            if c in "'\"":
                j = i + 1

                while j < n and src[j] != c and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1

                self.strings.append(src[i + 1 : j])
                out.append(c + self._fill(src[i + 1 : j]) + (c if j < n and src[j] == c else ""))
                i = j + 1
                self.prev, self.word = c, ""
                continue

            if c == "`":
                out.append("`")
                i += 1
                chunk: list[str] = []

                while i < n and src[i] != "`":
                    if src[i] == "\\":
                        out.append(self._fill(src[i : i + 2]))
                        chunk.append(src[i : i + 2])
                        i += 2
                    elif src.startswith("${", i):
                        out.append("${")
                        self.strings.append("".join(chunk))
                        chunk = []
                        i, inner = self.scan(i + 2, stop_at_brace=True)
                        out.append(inner + "}")
                    else:
                        out.append(self._fill(src[i]))
                        chunk.append(src[i])
                        i += 1

                self.strings.append("".join(chunk))

                out.append("`")
                i += 1
                self.prev, self.word = "`", ""
                continue

            if c == "/" and self._regex_allowed():
                j, in_class = i + 1, False

                while j < n and src[j] != "\n":
                    if src[j] == "\\":
                        j += 2
                        continue

                    if src[j] == "[":
                        in_class = True
                    elif src[j] == "]":
                        in_class = False
                    elif src[j] == "/" and not in_class:
                        break

                    j += 1

                if j < n and src[j] == "/":
                    k = j + 1

                    while k < n and src[k].isalpha():
                        k += 1

                    out.append("/" + self._fill(src[i + 1 : j]) + src[j:k])
                    i = k
                    self.prev, self.word = "/", ""
                    continue

            if stop_at_brace:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if depth == 0:
                        return i + 1, "".join(out)

                    depth -= 1

            out.append(c)

            if c.isalnum() or c in "_$":
                self.word = (self.word if (self.prev.isalnum() or self.prev in "_$") else "") + c
                self.prev = c
            elif not c.isspace():
                self.prev, self.word = c, ""

            i += 1

        return i, "".join(out)


def string_literals(src: str) -> list[str]:
    """Contents of every string and template literal in ``src`` (comments excluded)."""
    scanner = _Scanner(src, False)
    scanner.scan()

    return scanner.strings


def scrub(src: str, *, blank_strings: bool) -> str:
    """Comments removed; string/template/regex contents blanked when requested."""
    return _Scanner(src, blank_strings).scan()[1]


_JSX_PATTERNS = [
    re.compile(r"</[A-Za-z>]"),  # closing tag / fragment close
    re.compile(r"<>"),  # fragment open
    re.compile(r"(?<![\w$)\]])\s*/>"),  # self-closing tag end
    re.compile(r"(?:(?<=[=(,:?&|{\[;>])|(?<=\breturn)|(?<=\n)|(?<=\A))\s*<[A-Za-z][\w.:-]*(?=[\s/>])"),  # opening tag
]


def jsx_hits(code: str) -> list[str]:
    """Heuristic JSX detector working on string-blanked code."""
    hits = []

    for pattern in _JSX_PATTERNS:
        for match in pattern.finditer(code):
            line = code.count("\n", 0, match.start()) + 1
            hits.append(f"line {line}: {match.group(0).strip()!r}")

    return hits


def _read_plugin() -> str:
    assert PLUGIN_JS.is_file(), f"{PLUGIN_JS} is missing"

    return PLUGIN_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def raw() -> str:
    return _read_plugin()


@pytest.fixture(scope="module")
def code(raw: str) -> str:
    """Comments removed, string contents blanked."""
    return scrub(raw, blank_strings=True)


@pytest.fixture(scope="module")
def text(raw: str) -> str:
    """Comments removed, string contents kept."""
    return scrub(raw, blank_strings=False)


# ---------------------------------------------------------------- scanner self-checks


def test_scanner_blanks_strings_and_drops_comments():
    src = "const a = 'setInterval(1)' // setTimeout(\n/* fetch( */ const b = `x ${setInterval(f)} y` + /re[/]x/.test(s)\n"
    code = scrub(src, blank_strings=True)

    assert "fetch" not in code
    assert "setTimeout" not in code
    assert code.count("setInterval") == 1  # only the template expression
    assert "[/]" not in code  # regex literal contents are blanked
    assert ".test(s)" in code


def test_jsx_heuristic_flags_jsx_not_comparisons():
    assert jsx_hits(scrub("const a = <div className='x'>hi</div>\n", blank_strings=True))
    assert jsx_hits(scrub("return (\n  <Button onClick={f} />\n)\n", blank_strings=True))
    assert jsx_hits(scrub("const f = () => <>x</>\n", blank_strings=True))
    assert not jsx_hits(scrub("if (a < b && c > d) { x = i<n ? 1 : 2 }\nfor (let i = 0; i<n; i++) {}\n", blank_strings=True))
    assert not jsx_hits(scrub("const s = '<div>' + \"</div>\" + `<b>${x}</b>`\n", blank_strings=True))


# ---------------------------------------------------------------- plugin.js checks


def _import_specifiers(text: str) -> list[tuple[str, str]]:
    found = []
    patterns = [
        r"\bimport\s+[^'\";]*?\bfrom\s*(['\"])([^'\"]+)\1",
        r"\bimport\s*(['\"])([^'\"]+)\1",
        r"\bexport\s+(?:\*|\{[^}]*\})(?:\s+as\s+[\w$]+)?\s*from\s*(['\"])([^'\"]+)\1",
        r"\brequire\s*\(\s*(['\"])([^'\"]+)\1\s*\)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.S):
            found.append((match.group(0).split()[0], match.group(2)))

    return found


def test_imports_use_only_the_three_allowed_specifiers(text: str):
    specs = _import_specifiers(text)

    assert specs, "plugin.js imports nothing; expected @hermes/plugin-sdk, react, react/jsx-runtime"
    assert {s for _, s in specs} <= ALLOWED_SPECIFIERS, sorted({s for _, s in specs} - ALLOWED_SPECIFIERS)
    assert {s for _, s in specs} >= {"@hermes/plugin-sdk", "react/jsx-runtime"}


def test_sdk_named_imports_exist_in_the_sdk(text: str):
    exports = set(json.loads(SDK_EXPORTS.read_text(encoding="utf-8")))
    imported: set[str] = set()

    for match in re.finditer(r"\bimport\s+([^'\";]*?)\bfrom\s*(['\"])@hermes/plugin-sdk\2", text, re.S):
        clause = match.group(1)
        braces = re.search(r"\{([^}]*)\}", clause, re.S)

        for part in (braces.group(1) if braces else "").split(","):
            part = part.strip()

            if part and not part.startswith("type "):
                imported.add(part.split(" as ")[0].strip())

    assert imported, "expected named imports from @hermes/plugin-sdk"
    assert imported <= exports, f"not exported by the SDK: {sorted(imported - exports)}"


def test_no_jsx_syntax(code: str):
    hits = jsx_hits(code)

    assert not hits, "JSX syntax found (use jsx()/jsxs() calls): " + "; ".join(hits[:5])


def test_no_global_timers(code: str):
    assert not re.search(r"\b(?:window|globalThis|self)\s*\.\s*(?:setInterval|setTimeout)\b", code)
    assert not re.search(r"(?<![\w$.])(?:setInterval|setTimeout)\s*\(", code), "use ctx.setInterval / ctx.setTimeout"
    assert re.search(r"\bctx\s*\.\s*setInterval\s*\(", code), "the chip refresh must use ctx.setInterval"


def test_no_color_literals(text: str):
    hex_colour = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b")
    functional = re.compile(r"(?<![\w$])(?:rgba?|hsla?|hwb|oklch|oklab|lab|lch)\s*\(", re.I)

    assert not hex_colour.findall(text), hex_colour.findall(text)[:5]
    assert not functional.findall(text), functional.findall(text)[:5]


def test_no_desktop_bridge_or_network(raw: str, code: str):
    assert "hermesDesktop" not in raw
    assert not re.search(r"(?<![\w$.])fetch\s*\(", code)
    assert "XMLHttpRequest" not in code
    assert not re.search(r"(?<![\w$.])import\s*\(", code), "dynamic import() is not allowed"


def test_chat_card_directive_is_registered(code: str, text: str):
    assert re.search(r"\bTRANSCRIPT_DIRECTIVE_AREA\b", code), "TRANSCRIPT_DIRECTIVE_AREA is not used"
    assert re.search(r"""(['"])cc-env\1""", text), "directive name 'cc-env' is not registered"
    assert re.search(r"""(['"])cc-env-card\1""", text), "local id 'cc-env-card' is not registered"


def test_no_same_origin_sandbox_token(raw: str):
    assert "allow-same-origin" not in raw


def test_default_enabled_false(code: str):
    assert re.search(r"\bdefaultEnabled\s*:\s*false\b", code)


def test_no_internal_or_tooling_words(raw: str):
    lowered = raw.lower()
    found = [word for word in FORBIDDEN_TEXT if word.lower() in lowered]

    assert not found, f"forbidden words in plugin.js: {found}"


# ---------------------------------------------------------------- node smoke harness


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_node_smoke_harness_passes():
    node = shutil.which("node")

    assert node

    env = dict(os.environ)
    env.pop("CC_PLUGIN_PATH", None)

    proc = subprocess.run(
        [node, "--import", "./tests/js/register-stubs.mjs", "./tests/js/smoke.mjs"],
        cwd=ROOT,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )

    assert proc.returncode == 0, f"smoke failed (exit {proc.returncode})\n{proc.stderr}\n{proc.stdout}"
    assert "SMOKE OK" in proc.stdout, proc.stdout


# ---------------------------------------------------------------- wording

_PERSONAL = re.compile(r"\b(?:[Ww]e|[Oo]ur|[Uu]s|I)\b|\b(?:[Ww]e|I)(?:'m|'ve|'ll|'d|'re)\b")
# Allowlist: command-line flags and code-ish tokens that merely look like pronouns.
_ALLOWED_TOKENS = re.compile(r"(?<![\w-])-{1,2}[A-Za-z][\w-]*")


def _user_facing(strings):
    """Strings that read as sentences or labels (a letter and a space), flags removed."""
    for text in strings:
        text = _ALLOWED_TOKENS.sub(" ", text)

        if re.search(r"[A-Za-z]{2}", text) and " " in text.strip():
            yield text


def test_scanner_collects_string_literals():
    src = "const a = 'one two', b = \"three\" + `four ${x} five` // 'ignored'\n"

    assert string_literals(src) == ["one two", "three", "four ", " five"]


def test_user_facing_text_has_no_we_i_our_us(raw: str):
    hits = [t for t in _user_facing(string_literals(raw)) if _PERSONAL.search(t)]

    assert not hits, f"first-person wording in plugin.js strings: {hits[:5]}"


def test_pronoun_scan_catches_what_it_should():
    assert _PERSONAL.search("We check DNS") and _PERSONAL.search("tell us why") and _PERSONAL.search("I think")
    assert _PERSONAL.search("our host") and _PERSONAL.search("we're done")
    assert not _PERSONAL.search("Use the host")
    assert not _PERSONAL.search(_ALLOWED_TOKENS.sub(" ", "podman exec -i x"))


def test_no_single_os_wording_in_plugin_strings(raw: str):
    allowed = "Windows Terminal or Command Prompt on Windows"
    text = " ".join(t.replace(allowed, "") for t in string_literals(raw))

    assert not re.search(r"Windows|\bPC\b|PowerShell", text)
