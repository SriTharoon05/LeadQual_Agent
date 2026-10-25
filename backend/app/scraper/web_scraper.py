"""
scraper/web_scraper.py
──────────────────────
Company website scraper for the RAG ingestion pipeline.

Adapted from a previous project's research module — LangSearch fallback and the
Django `settings` dependency have been REMOVED. Pure website scrape only:

    requests + BeautifulSoup → normalize URL → strip boilerplate → clean text.

Primary (and only) source: the company's own website. If the site is
unreachable, the URL is missing, or the scrape yields too little usable text,
we simply return whatever we have (possibly empty) and let the caller decide.

Single entry point the pipeline should call:

    from app.scraper.web_scraper import research_company
    result = research_company(company_name, website_url)
    # → {"text": str, "source": "scrape"|"none", "source_url": str, "reachable": bool}
"""

import logging
import re

from bs4 import BeautifulSoup
from app.config import settings
from app.utils.public_web import fetch_public_html

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 8   # seconds — pipeline processes many leads, fail fast
USER_AGENT = (
    "Mozilla/5.0 (compatible; LeadQualification/1.0; "
    "+research-bot for internal lead qualification)"
)
MIN_USEFUL_TEXT_LENGTH = 200   # below this, the scrape is "not useful enough"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _normalize_url(url: str) -> str:
    """Add https:// if the scheme is missing; trim whitespace."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"
    return url


# ── Scrape ────────────────────────────────────────────────────────────────────
def scrape_website(url: str) -> dict:
    """
    Fetch a company website and extract visible text, stripping common
    boilerplate (scripts, styles, nav, footer, noscript).

    Returns:
        {"text": str, "reachable": bool, "source": "scrape", "source_url": str}
    """
    normalized = _normalize_url(url)
    if not normalized:
        return {"text": "", "reachable": False, "source": "scrape", "source_url": ""}

    try:
        body, final_url = fetch_public_html(normalized, USER_AGENT,
            settings.SCRAPE_MAX_HTML_BYTES, timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        logger.info("Company website could not be read (%s)", type(exc).__name__)
        return {"text": "", "reachable": False, "source": "scrape",
                "source_url": normalized}

    # Prefer lxml; fall back to the stdlib parser if lxml isn't installed.
    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        soup = BeautifulSoup(body, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()

    truncated = len(text) > settings.SCRAPE_MAX_TEXT_CHARS
    return {"text": text[:settings.SCRAPE_MAX_TEXT_CHARS], "reachable": True, "source": "scrape",
            "source_url": final_url, "truncated": truncated}


# ── Orchestrator (single entry point) ─────────────────────────────────────────
def research_company(company_name: str, website_url: str) -> dict:
    """
    Scrape one company's website and return its cleaned text.

    (LangSearch fallback removed — website scrape is the only source now.)

    Returns:
        {"text": str, "source": "scrape"|"none", "source_url": str,
         "reachable": bool}
    """
    scrape_result = scrape_website(website_url)

    if scrape_result["reachable"] and \
            len(scrape_result["text"]) >= MIN_USEFUL_TEXT_LENGTH:
        return {
            "text":       scrape_result["text"],
            "source":     "scrape",
            "source_url": scrape_result["source_url"],
            "reachable":  True,
            "truncated": bool(scrape_result.get("truncated")),
        }

    # Reachable but thin, or unreachable — return whatever text we managed to get.
    if scrape_result["text"]:
        logger.info(
            "research_company: thin scrape for %s (%d chars)",
            company_name or website_url, len(scrape_result["text"]),
        )
        return {
            "text":       scrape_result["text"],
            "source":     "scrape",
            "source_url": scrape_result["source_url"],
            "reachable":  scrape_result["reachable"],
        }

    logger.info("research_company: no usable text for %s",
                company_name or website_url)
    return {"text": "", "source": "none",
            "source_url": scrape_result["source_url"], "reachable": False}
