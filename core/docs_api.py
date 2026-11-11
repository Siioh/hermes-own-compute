"""REST routes for the in-app Help topics.

GET /docs            -> [{topic, title}]
GET /docs/{topic}    -> {topic, title, body_md, links:[{label, url}]}

Area module contract: ``routes()``, ``tools()`` and ``background_tasks()``.
Plumbing types come from the sibling ``interfaces`` module and are imported
lazily, because this package can be loaded under more than one package name.
"""

import re

from . import docs_content

_TOPIC_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


def list_docs(req):
    """All Help topics, in display order."""
    return docs_content.list_topics()


def _not_found():
    from .interfaces import ApiError

    return ApiError(404, "not_found", "No such help topic.")


def get_doc(req):
    """One Help topic; unknown or malformed ids give a 404."""
    params = getattr(req, "path_params", None) or {}
    topic = params.get("topic")
    if not isinstance(topic, str) or not _TOPIC_RE.match(topic):
        raise _not_found()
    entry = docs_content.get_topic(topic)
    if entry is None:
        raise _not_found()
    return entry


def routes():
    from .interfaces import Route

    return [
        Route("GET", "/docs", list_docs, summary="List Help topics"),
        Route("GET", "/docs/{topic}", get_doc, summary="Read one Help topic"),
    ]


def tools():
    return []


def background_tasks():
    return []
