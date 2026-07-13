FROM python:3.12-slim

# --- System tools that pip cannot install ---
# Shared libraries headless Chromium (Playwright/Crawl4AI) needs on a minimal image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Chromium under a fixed, image-owned path (NOT the app's .runtime dir, which is a
# mounted volume at runtime). The app reads PLAYWRIGHT_BROWSERS_PATH, so pin it here.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

# --- Python dependencies (layer cached unless requirements.txt changes) ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install-deps chromium \
    && playwright install chromium

# --- Application code ---
COPY . .

# Ensure runtime dirs exist (also created at startup, but harmless to pre-make).
RUN python main.py bootstrap

# The UI binds 0.0.0.0 so it is reachable from outside the container.
EXPOSE 8765
CMD ["python", "main.py", "ui", "--host", "0.0.0.0", "--port", "8765"]
