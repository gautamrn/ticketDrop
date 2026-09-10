"""DOM reduction for the self-heal prompt.

ARCHITECTURE.md §7 step 1. A checkout page is ~800 KB of which almost all is
script, inline style, and framework bookkeeping. Stripping it to structure plus
the attributes a selector can actually key on is both a cost control and a
latency control: the heal stage has a ~1 s budget and the model cannot read
800 KB inside it.

What survives is deliberately the attribute set a durable selector is built
from — id, class, role, aria-*, data-testid, type — and the text, because text
is how a human names the control we are asking about.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment

STRIPPED_TAGS = ("script", "style", "svg")

ALLOWED_ATTRS = frozenset({"id", "class", "role", "data-testid", "type"})
ALLOWED_PREFIXES = ("aria-",)

_WHITESPACE = re.compile(r"\s+")


def clean_html(html: str, *, max_bytes: int | None = None) -> str:
    """Strip a page down to selectable structure.

    Inline event handlers go with everything else: `on*` attributes are both
    the largest attribute payload on a typical vendor page and the one thing in
    the DOM that could be misread as a control's identity.
    """
    soup = BeautifulSoup(html or "", "lxml")

    for tag_name in STRIPPED_TAGS:
        for element in soup.find_all(tag_name):
            element.decompose()

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for element in soup.find_all(True):
        element.attrs = {
            name: value for name, value in element.attrs.items() if _keep_attribute(name.lower())
        }

    return _collapse(str(soup), max_bytes)


def _keep_attribute(name: str) -> bool:
    if name.startswith("on"):
        return False
    return name in ALLOWED_ATTRS or name.startswith(ALLOWED_PREFIXES)


def _collapse(markup: str, max_bytes: int | None) -> str:
    collapsed = _WHITESPACE.sub(" ", markup).strip()
    if max_bytes is None or len(collapsed.encode("utf-8")) <= max_bytes:
        return collapsed
    # Truncation can split a tag. That is acceptable: the model is asked for a
    # selector, not for valid HTML, and a hard ceiling is the only thing that
    # bounds the cost of a pathological page.
    return collapsed.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
