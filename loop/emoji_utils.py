"""Strip emoji/pictographs from model-generated user-facing text.

Loop's surfaces are deliberately emoji-free (a professional-tone product
decision), but the LLM drafters and answerers still reach for emoji on their
own. This is the single guarantee that no emoji reaches a rendered card, a
drafted nudge, or a message sent as the user — applied at the text boundary so
it holds whatever the model returns.

Typographic symbols that are part of the clean design — arrows (``→ ⇄ ↔``),
the middle dot ``·``, em/en dashes, the ellipsis — are intentionally *kept*;
only pictographic emoji ranges are removed.
"""

from __future__ import annotations

import re

# Pictographic / emoji ranges only. Deliberately excludes the arrows block
# (U+2190–U+21FF) and general punctuation like ·, —, … so the design's
# typographic symbols survive.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # symbols & pictographs, emoticons, transport, supplemental
    "\U00002600-\U000026FF"  # miscellaneous symbols (☀ ☔ ⚡ …)
    "\U00002700-\U000027BF"  # dingbats (✂ ✅ ✈ ✉ ✊ ✋ ✨ ✔ ✖ …)
    "\U00002B00-\U00002BFF"  # misc symbols & arrows pictographs (⬆ ⭐ …)
    "\U00002139"             # information source
    "\U000023E9-\U000023FA"  # media controls (⏩ ⏰ ⏳ …)
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero-width joiner (emoji sequences)
    "\U000020E3"             # combining enclosing keycap
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    """Return ``text`` with emoji/pictographs removed and whitespace tidied.

    Safe on ``None``/empty. Collapses the runs of spaces an emoji removal can
    leave behind (e.g. ``"thanks 🙏 !"`` → ``"thanks !"`` → ``"thanks!"`` is not
    attempted; spacing is only collapsed, not re-punctuated) and trims the ends.
    """
    if not text:
        return text
    cleaned = _EMOJI.sub("", text)
    # An emoji sat between spaces leaves a double space; collapse those runs.
    cleaned = re.sub(r"[ \t ]{2,}", " ", cleaned)
    # Drop a leftover space before common punctuation ("thanks !" -> "thanks!").
    cleaned = re.sub(r" +([,.!?;:])", r"\1", cleaned)
    return cleaned.strip()
