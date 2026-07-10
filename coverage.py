"""Deterministic row-marker detection for extraction coverage.

The pre-extraction LLM analysis is supposed to enumerate every extractable row (via
`section_inventory` / `expected_total_rows`), but it silently under-counts on real
documents — leaving the coverage guard with no target, so extraction can drop rows
and still be marked "verified".

Curriculum/standards sources usually carry a regular, machine-detectable row marker
(a benchmark code like `TDL.9514000.01.01`, or numbered/lettered items). Counting
those with a regex is EXACT where the LLM is unreliable. This module derives the
coverage ground-truth from the document itself so the count no longer depends on the
model. Sources without a consistent marker fall back to the LLM's estimate.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class RowAnchor:
    code: str          # verbatim marker text (e.g. "TDL.9514000.01.01")
    offset: int        # character offset of its first occurrence in the markdown


@dataclass(slots=True)
class CoverageDetection:
    anchors: list[RowAnchor] = field(default_factory=list)
    pattern_name: str = ""
    confidence: str = "none"          # "high" | "low" | "none"
    section_inventory: list[str] = field(default_factory=list)

    @property
    def expected_total_rows(self) -> int:
        return len(self.anchors)

    @property
    def has_markers(self) -> bool:
        return self.confidence == "high" and bool(self.anchors)


# Ranked most-specific first. Each entry: (name, compiled regex, min distinct hits to
# accept). Group-1 (or whole match) is the marker text. Anchored to line starts where
# the marker is a list prefix so prose numbers ("in 2019") don't match.
_PATTERNS: list[tuple[str, re.Pattern, int]] = [
    # Segmented/dotted codes: TDL.9514000.01.01, S5.MAT.CST, ERT-1.A.1, BIO.1.1
    (
        "segmented_code",
        re.compile(r"\b([A-Z]{2,}[.\-][A-Z0-9]+(?:[.\-][A-Z0-9]+){1,5})\b"),
        6,
    ),
    # Benchmark items at line start: "1.", "1)", "A.1", "12.3", "a)"
    (
        "line_item",
        re.compile(r"(?m)^\s{0,8}([A-Za-z]?\d+(?:\.\d+)*[.)])\s+\S"),
        6,
    ),
]


def detect_row_anchors(markdown: str) -> CoverageDetection:
    """Detect the densest consistent row marker in the markdown.

    Returns a CoverageDetection. ``has_markers`` is True (confidence "high") only when a
    pattern yields enough DISTINCT markers to be a credible per-row enumeration; otherwise
    confidence is "low"/"none" and callers should fall back to the LLM estimate.
    """
    if not markdown or len(markdown) < 40:
        return CoverageDetection()

    best: CoverageDetection | None = None
    for name, pattern, min_hits in _PATTERNS:
        seen: dict[str, int] = {}
        for match in pattern.finditer(markdown):
            code = match.group(1).strip()
            if code and code not in seen:
                seen[code] = match.start()
        distinct = len(seen)
        if distinct < min_hits:
            continue
        anchors = [RowAnchor(code=code, offset=off) for code, off in seen.items()]
        anchors.sort(key=lambda a: a.offset)
        detection = CoverageDetection(
            anchors=anchors,
            pattern_name=name,
            confidence="high",
            section_inventory=_group_sections(anchors),
        )
        # First (most specific) pattern that qualifies wins.
        best = detection
        break

    return best or CoverageDetection(confidence="none")


def _group_sections(anchors: list[RowAnchor]) -> list[str]:
    """Seed a section_inventory by grouping codes on their common prefix.

    For dotted codes, the prefix up to the last segment names a section; the count of
    codes sharing it is the expected rows for that section. Falls back to a single
    'Detected rows' entry when codes don't share a splittable structure.
    """
    groups: Counter[str] = Counter()
    for anchor in anchors:
        parts = re.split(r"[.\-]", anchor.code)
        prefix = ".".join(parts[:-1]) if len(parts) > 1 else anchor.code
        groups[prefix] += 1
    if len(groups) <= 1:
        return [f"Detected rows: ~{len(anchors)} rows"]
    return [f"{prefix}: ~{count} rows" for prefix, count in sorted(groups.items())]


def extracted_codes_in(text: str, detected: list[RowAnchor]) -> set[str]:
    """Which detected codes appear (verbatim) in a block of extracted output text."""
    return {anchor.code for anchor in detected if anchor.code in text}
