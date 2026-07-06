from __future__ import annotations

import asyncio
import plistlib
import re
from pathlib import Path
from typing import Any

from parsers.base import ParsedDocument


URL_PATTERN = re.compile(r"https?://\S+")


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
