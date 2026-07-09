#!/usr/bin/env bash
# Assemble the Vibe py-backend deployment package from the flat repo.
#
# The flat repo (one dir up) is the SOURCE OF TRUTH; this copies the runtime
# subset into py-backend/app/ so `uvicorn app.main:app` runs on Vibe with all
# project modules importable as top-level names (sys.path guard in app/main.py).
#
# Re-run whenever the source changes. Safe/idempotent: it wipes py-backend/app
# and rebuilds it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DEST="$HERE/app"

echo "Building py-backend/app from $ROOT ..."
rm -rf "$DEST"
mkdir -p "$DEST"

# Top-level Python modules (all *.py at repo root except main.py, which is the
# CLI entrypoint — the hosted entrypoint is app/main.py, copied separately).
for f in "$ROOT"/*.py; do
  base="$(basename "$f")"
  [ "$base" = "main.py" ] && continue
  cp "$f" "$DEST/$base"
done

# Packages the app imports.
cp -R "$ROOT/parsers" "$DEST/parsers"
cp -R "$ROOT/validation" "$DEST/validation"

# Static UI.
cp -R "$ROOT/ui" "$DEST/ui"

# Runtime data files read at startup.
for data in schema_config.json default_sample_schema.csv default_schema_source.json sample_csv_column_prompt.md; do
  [ -f "$ROOT/$data" ] && cp "$ROOT/$data" "$DEST/$data"
done

# The FastAPI entrypoint (hosted) lives at app/main.py in the flat repo.
cp "$ROOT/app/main.py" "$DEST/main.py"

# Strip caches and any local runtime dirs (config.ensure_directories may create
# these if a module is imported during/after the build; they must not ship).
find "$DEST" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$DEST/output" "$DEST/input_documents" "$DEST/.runtime"

echo "Done. py-backend/app contains:"
ls "$DEST"
