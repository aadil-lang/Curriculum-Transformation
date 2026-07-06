from __future__ import annotations

import argparse
import json
from pathlib import Path

from parsers.web_parser import parse_website_reference


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse a website reference into markdown.")
    parser.add_argument("source", help="Path to a .url, .webloc, or URL-bearing .txt file.")
    args = parser.parse_args()

    parsed = parse_website_reference(Path(args.source).expanduser().resolve())
    print(
        json.dumps(
            {
                "source_name": parsed.source_name,
                "source_type": parsed.source_type,
                "source_path": str(parsed.source_path),
                "document_id": parsed.document_id,
                "markdown": parsed.markdown,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
