import re

from bs4 import BeautifulSoup
from markdownify import markdownify as md


_STRIP_TAGS = ["script", "style", "img", "code", "noscript", "svg"]

# Matches an opening fence like ```markdown or ``` (with optional language tag)
_FENCE_OPEN = re.compile(r"^```[a-z]*\s*$", re.MULTILINE)
_FENCE_CLOSE = re.compile(r"^```\s*$", re.MULTILINE)


def strip_agent_chat(text: str) -> str:
    """Remove LLM conversational wrapper from agent-returned content.

    Strips preamble/epilogue prose and ```markdown ... ``` fences so only
    the raw markdown content remains.
    """
    if not text:
        return text

    open_match = _FENCE_OPEN.search(text)
    if not open_match:
        return text.strip()

    inner_start = open_match.end()
    close_match = _FENCE_CLOSE.search(text, inner_start)
    inner_end = close_match.start() if close_match else len(text)
    return text[inner_start:inner_end].strip()


def clean_html_to_markdown(html: str) -> str:
    """Strip noise tags from raw HTML then convert to markdown.

    Removes script, style, img, code, noscript, and svg tags before
    running markdownify, which keeps meaningful structure (headings,
    lists, links) while cutting the token-heavy boilerplate.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    return md(str(soup), heading_style="ATX", strip=["a"])
