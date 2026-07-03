"""Free-text GitHub PR-reference extraction for the Adjudicator (live auto-close wiring).

This is a small, **pure**, network-free helper. The Adjudicator runs it over a
candidate's message text on the happy path; when it finds a GitHub Pull Request
reference it stamps the written ``Obligation`` with ``artifact_type=GITHUB_PR`` and
the normalized ``artifact_ref`` so the Verifier (loop.verifier) and the Action
auto-close beat can ground the loop in real GitHub state (Req 4, 8).

It detects BOTH the shorthand and the canonical-URL forms a human would actually
type in Slack and normalizes either to the single ``owner/repo#number`` shape that
:meth:`loop.verifier.verifier.PrRef.parse` already understands:

    shorthand:  owner/repo#123
    URL:        https://github.com/owner/repo/pull/123
                (scheme optional; trailing path/query like /files?w=1 is ignored)

GitHub owner/repo names are restricted to alphanumerics plus ``-``, ``_`` and ``.``
(the ``[\\w.-]`` class, where ``\\w`` already covers alnum + underscore). The
extractor is robust to surrounding words/punctuation, returns the **first** PR
reference in document order, and returns ``None`` when the text has no PR ref.
"""

from __future__ import annotations

import re
from typing import Optional

# Canonical PR URL: optional scheme, github.com host, owner/repo, /pull/<number>.
# Anything after the number (a trailing path like /files, or a ?query) is ignored.
_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
    re.IGNORECASE,
)

# Shorthand reference: owner/repo#number. owner and repo cannot contain '/' or '#'
# (the [\w.-] class enforces this), so the captures stay tight against surrounding
# text. Requires the '#<digits>' tail, which is what distinguishes it from a path.
_SHORTHAND_RE = re.compile(r"([\w.-]+)/([\w.-]+)#(\d+)")


def _normalize(owner: str, repo: str, number: str) -> str:
    """Return the canonical ``owner/repo#number`` form PrRef.parse understands."""
    return f"{owner}/{repo}#{int(number)}"


def extract_pr_ref(text: str) -> Optional[str]:
    """Extract the first GitHub PR reference from free text, normalized.

    Detects both the shorthand (``owner/repo#123``) and URL
    (``https://github.com/owner/repo/pull/123``) forms and returns a normalized
    ``"owner/repo#number"`` string. When both forms appear, the one occurring
    earliest in the text wins (first match in document order). Returns ``None`` when
    no PR reference is present.
    """
    if not text:
        return None

    url_match = _URL_RE.search(text)
    shorthand_match = _SHORTHAND_RE.search(text)

    # Pick whichever real match starts earliest in the text (first in document
    # order). A None match is treated as "infinitely far away" so the other wins.
    candidates: list[tuple[int, re.Match[str]]] = []
    if url_match is not None:
        candidates.append((url_match.start(), url_match))
    if shorthand_match is not None:
        candidates.append((shorthand_match.start(), shorthand_match))
    if not candidates:
        return None

    _, best = min(candidates, key=lambda pair: pair[0])
    owner, repo, number = best.group(1), best.group(2), best.group(3)
    return _normalize(owner, repo, number)


__all__ = ["extract_pr_ref"]
