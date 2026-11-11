"""The system prompt section tells the chat model about every card the page can draw."""

from __future__ import annotations

import re
from pathlib import Path

from cloud_compute_core import chat_context

PLUGIN_JS = (Path(__file__).resolve().parents[1] / "desktop" / "plugin.js").read_text(encoding="utf-8")


def test_the_prompt_lists_every_card_kind_with_its_id_shape():
    text = chat_context.SYSTEM_PROMPT_SECTION
    for line in ('::cc-env{id="i-', '::cc-job{id="j-', '::cc-agent{id="a-', '::cc-copy{id="x-', '::cc-host{id='):
        assert line in text, line


def test_every_card_the_prompt_names_is_a_registered_directive():
    named = set(re.findall(r"::cc-([a-z]+)\{", chat_context.SYSTEM_PROMPT_SECTION))
    registered = set(re.findall(r"directive: 'cc-([a-z]+)'", PLUGIN_JS)) | {"env"}
    assert named == registered


def test_the_prompt_still_fits_its_size_limit():
    assert len(chat_context.SYSTEM_PROMPT_SECTION) <= 1500
