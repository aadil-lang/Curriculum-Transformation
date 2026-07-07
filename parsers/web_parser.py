from __future__ import annotations

import asyncio
import logging
import plistlib
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from parsers.base import ParsedDocument


LOGGER = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://\S+")

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Sec-Fetch headers that make an in-context request look like a real navigation.
# Edge bot-protection (e.g. Akamai on fldoe.org) returns 403 to bare fetches but
# 200 to a same-origin document navigation carried out inside a warmed browser
# context. See _download_documents_in_context.
_NAVIGATION_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}
_DOCUMENT_LINK_SUFFIXES = (".rtf", ".pdf", ".docx", ".doc")
# Non-curriculum documents that commonly sit alongside a real syllabus link and
# must not be mistaken for index entries (mirrors batch_runner's link ranking).
_INDEX_NOISE_KEYWORDS = (
    "assessment",
    "support material",
    "support",
    "resource",
    "record of changes",
    "glossary",
    "schedule",
    "advice",
    "fact sheet",
    "cover sheet",
    "errata",
)


@dataclass(slots=True)
class HarvestedDocument:
    path: Path
    source_url: str
    label: str


def parse_website_reference(path: Path) -> ParsedDocument:
    url = _extract_url(path)
    markdown, metadata = _run_crawl(url)
    return ParsedDocument(
        document_id=path.stem,
        source_path=str(path),
        source_name=path.name,
        source_type="website",
        markdown=markdown,
        metadata=metadata | {"url": url},
    )


def _extract_url(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".webloc":
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        url = payload.get("URL")
        if not url:
            raise ValueError(f"No URL found in webloc file: {path}")
        return url

    content = path.read_text(encoding="utf-8", errors="ignore")
    match = URL_PATTERN.search(content)
    if not match:
        raise ValueError(f"No URL found in website reference file: {path}")
    return match.group(0)


def _run_crawl(url: str) -> tuple[str, dict[str, Any]]:
    return asyncio.run(_crawl_url(url))


async def _crawl_url(url: str) -> tuple[str, dict[str, Any]]:
    from crawl4ai import AsyncWebCrawler

    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url)

    markdown_candidates = [
        getattr(result, "markdown", None),
        getattr(getattr(result, "markdown_v2", None), "raw_markdown", None),
        getattr(getattr(result, "markdown_v2", None), "fit_markdown", None),
        getattr(result, "cleaned_html", None),
    ]
    markdown = next((candidate for candidate in markdown_candidates if candidate), None)
    if not markdown:
        raise RuntimeError(f"Crawl4AI did not return markdown content for {url}")

    metadata = {
        "title": getattr(result, "title", None),
        "status_code": getattr(result, "status_code", None),
        "url": getattr(result, "url", url),
    }
    return str(markdown), metadata


def parse_program_filter(raw: str | None) -> list[str]:
    """Split a user program-filter string into normalized match terms.

    Terms are separated by newlines or commas; blank terms are dropped. An
    empty result means 'no filter' (keep every linked document).
    """
    if not raw:
        return []
    parts = re.split(r"[,\n]+", raw)
    return [term.strip().lower() for term in parts if term.strip()]


def harvest_index_documents(
    index_url: str,
    destination_dir: Path,
    *,
    max_documents: int | None = None,
    filename_for: "Callable[[str, str, int], str] | None" = None,
    program_filter: list[str] | None = None,
) -> list[HarvestedDocument]:
    """Harvest linked documents (.rtf/.pdf/.doc/.docx) from an index page.

    Some sites (e.g. fldoe.org curriculum frameworks) list many programs as
    separate downloadable documents on one landing page, and sit behind edge
    bot-protection that 403s any non-browser request. This drives a single
    Chromium session: navigate the landing page to solve the challenge and hold
    the cookies, extract the document links from the rendered DOM, then download
    each one in that same warmed context. Returns one entry per downloaded file.

    When ``program_filter`` is non-empty, only links whose label or filename
    contains at least one filter term (case-insensitive) are downloaded.
    """
    return asyncio.run(
        _harvest_index_documents(
            index_url,
            destination_dir,
            max_documents=max_documents,
            filename_for=filename_for,
            program_filter=program_filter or [],
        )
    )


def download_protected_file(url: str, destination_path: Path) -> bool:
    """Download a single bot-protected file via a warmed browser context.

    Used as a fallback when a plain HTTP download is rejected (e.g. HTTP 403
    from edge protection). Warms the origin with a navigation to obtain the
    clearance cookie, then fetches the file with navigation Sec-Fetch headers.
    Returns True on success.
    """
    return asyncio.run(_download_protected_file(url, destination_path))


async def _download_protected_file(url: str, destination_path: Path) -> bool:
    from playwright.async_api import async_playwright

    origin = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}/"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=_BROWSER_USER_AGENT)
            page = await context.new_page()
            await page.goto(origin, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)
            response = await context.request.get(url, headers={**_NAVIGATION_HEADERS, "Referer": origin})
            if response.status != 200:
                LOGGER.warning("Browser download of %s failed (HTTP %s).", url, response.status)
                return False
            body = await response.body()
            if not body:
                return False
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_bytes(body)
            return True
        finally:
            await browser.close()


async def _harvest_index_documents(
    index_url: str,
    destination_dir: Path,
    *,
    max_documents: int | None,
    filename_for: "Callable[[str, str, int], str] | None",
    program_filter: list[str],
) -> list[HarvestedDocument]:
    from playwright.async_api import async_playwright

    destination_dir.mkdir(parents=True, exist_ok=True)

    # Link discovery goes through Crawl4AI's stealth browser, which solves the
    # edge bot-protection challenge and returns the fully rendered page. A plain
    # Playwright goto() is fingerprinted and served a 403 with no links.
    markdown, _ = await _crawl_url(index_url)
    ordered_links = _extract_document_links_from_markdown(markdown, index_url)
    if not ordered_links:
        return []

    if program_filter:
        discovered = len(ordered_links)
        ordered_links = [
            (href, label)
            for href, label in ordered_links
            if _matches_program_filter(href, label, program_filter)
        ]
        LOGGER.info(
            "Program filter %s kept %d of %d linked documents on %s.",
            program_filter,
            len(ordered_links),
            discovered,
            index_url,
        )
        if not ordered_links:
            LOGGER.warning(
                "Program filter %s matched none of the %d documents on %s.",
                program_filter,
                discovered,
                index_url,
            )
            return []

    total_found = len(ordered_links)
    if max_documents is not None and total_found > max_documents:
        LOGGER.warning(
            "Index page %s exposed %d documents; capping to %d (web_index_max_documents).",
            index_url,
            total_found,
            max_documents,
        )
        ordered_links = ordered_links[:max_documents]

    harvested: list[HarvestedDocument] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=_BROWSER_USER_AGENT)
            page = await context.new_page()
            # The navigation itself is 403'd, but it still sets the edge
            # clearance cookie that lets the subsequent in-context document
            # requests (with navigation Sec-Fetch headers) return 200.
            await page.goto(index_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)

            for position, (href, label) in enumerate(ordered_links, start=1):
                downloaded = await _download_in_context(
                    context, href, index_url, destination_dir, label, position, filename_for
                )
                if downloaded is not None:
                    harvested.append(downloaded)
        finally:
            await browser.close()

    LOGGER.info(
        "Harvested %d of %d linked documents from index page %s",
        len(harvested),
        total_found,
        index_url,
    )
    return harvested


def _matches_program_filter(url: str, label: str, program_filter: list[str]) -> bool:
    filename = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    haystack = f"{label.lower()} {filename.lower()}"
    return any(term in haystack for term in program_filter)


_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]{0,200})\]\((https?://[^)\s]+)\)")


def _extract_document_links_from_markdown(markdown: str, base_url: str) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, href in _MARKDOWN_LINK_PATTERN.findall(markdown):
        absolute = urllib.parse.urljoin(base_url, href)
        path_only = urllib.parse.urlparse(absolute.lower()).path
        if not path_only.endswith(_DOCUMENT_LINK_SUFFIXES):
            continue
        haystack = f"{absolute.lower()} {label.lower()}"
        if any(keyword in haystack for keyword in _INDEX_NOISE_KEYWORDS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        ordered.append((absolute, label.strip()))
    return ordered


async def _download_in_context(
    context: Any,
    url: str,
    referer: str,
    destination_dir: Path,
    label: str,
    position: int,
    filename_for: "Callable[[str, str, int], str] | None",
) -> HarvestedDocument | None:
    headers = {**_NAVIGATION_HEADERS, "Referer": referer}
    try:
        response = await context.request.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - a single bad link must not abort the harvest
        LOGGER.warning("Failed to fetch harvested document %s: %r", url, exc)
        return None

    if response.status != 200:
        LOGGER.warning("Skipping harvested document %s (HTTP %s)", url, response.status)
        return None

    body = await response.body()
    if not body:
        LOGGER.warning("Skipping harvested document %s (empty body)", url)
        return None

    if filename_for is not None:
        filename = filename_for(url, label, position)
    else:
        filename = Path(urllib.parse.urlparse(url).path).name or f"document_{position}"
    destination_path = destination_dir / filename
    destination_path.write_bytes(body)
    return HarvestedDocument(path=destination_path, source_url=url, label=label.strip())
