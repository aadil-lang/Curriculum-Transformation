from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from multiprocessing import cpu_count
from typing import Literal


ROOT_DIR = Path(__file__).resolve().parent
INPUT_DIR = ROOT_DIR / "input_documents"
OUTPUT_DIR = ROOT_DIR / "output"
RUNTIME_DIR = ROOT_DIR / ".runtime"
PLAYWRIGHT_BROWSERS_DIR = RUNTIME_DIR / "playwright-browsers"
STATE_PATH = OUTPUT_DIR / "processing_state.json"
MANUAL_REVIEW_PATH = OUTPUT_DIR / "manual_review.json"
FINAL_CSV_PATH = OUTPUT_DIR / "final_extracted_data.csv"
LOG_PATH = OUTPUT_DIR / "agent.log"
MONITOR_STATUS_PATH = OUTPUT_DIR / "monitor_status.json"
DEFAULT_SCHEMA_CONFIG_PATH = ROOT_DIR / "schema_config.json"
DOT_ENV_PATH = ROOT_DIR / ".env"
DIRECT_PROVIDER_EXECUTION_MODE = "direct_provider"
CODEX_CHAT_ASSISTED_EXECUTION_MODE = "codex_chat_assisted"
SUPPORTED_EXECUTION_MODES = {
    DIRECT_PROVIDER_EXECUTION_MODE,
    CODEX_CHAT_ASSISTED_EXECUTION_MODE,
}


def load_dotenv(path: Path = DOT_ENV_PATH, override: bool = False) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip()
        if override:
            os.environ[normalized_key] = normalized_value
        else:
            os.environ.setdefault(normalized_key, normalized_value)


load_dotenv()
os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(RUNTIME_DIR))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PLAYWRIGHT_BROWSERS_DIR))


# Cross-cloud failover targets for a stage, as "provider|model,provider|model".
# Each Claude model is named differently per cloud (Vertex: anthropic.claude-opus-4-6;
# Azure Sweden: claude-opus-4-6), so provider AND model are specified per target.
DEFAULT_EXTRACTOR_FALLBACKS = "@swedencentral-anthropic|claude-opus-4-6"
DEFAULT_CRITIC_FALLBACKS = "@swedencentral-anthropic|claude-opus-4-6"


def parse_provider_fallbacks(raw: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "|" not in entry:
            continue
        provider, model = entry.split("|", 1)
        provider, model = provider.strip(), model.strip()
        if provider and model:
            targets.append((provider, model))
    return targets


@dataclass(slots=True)
class Settings:
    extractor_model: str = field(default_factory=lambda: os.getenv("EXTRACTOR_MODEL", "gemini-3.5-flash"))
    extractor_provider: str = field(default_factory=lambda: os.getenv("EXTRACTOR_PROVIDER", "gemini"))
    critic_provider: str = field(default_factory=lambda: os.getenv("CRITIC_PROVIDER", "openai").lower())
    critic_model: str = field(default_factory=lambda: os.getenv("CRITIC_MODEL", "gpt-5.5"))
    execution_mode: Literal["direct_provider", "codex_chat_assisted"] = field(
        default_factory=lambda: os.getenv("EXECUTION_MODE", DIRECT_PROVIDER_EXECUTION_MODE).strip().lower()
    )
    portkey_extractor_provider: str = field(
        default_factory=lambda: os.getenv("PORTKEY_EXTRACTOR_PROVIDER", "@google-ai")
    )
    portkey_critic_provider: str = field(
        default_factory=lambda: os.getenv("PORTKEY_CRITIC_PROVIDER", "")
    )
    extractor_fallbacks: list[tuple[str, str]] = field(
        default_factory=lambda: parse_provider_fallbacks(
            os.getenv("EXTRACTOR_FALLBACKS", DEFAULT_EXTRACTOR_FALLBACKS)
        )
    )
    critic_fallbacks: list[tuple[str, str]] = field(
        default_factory=lambda: parse_provider_fallbacks(
            os.getenv("CRITIC_FALLBACKS", DEFAULT_CRITIC_FALLBACKS)
        )
    )
    watch_interval_seconds: float = field(default_factory=lambda: float(os.getenv("WATCH_INTERVAL_SECONDS", "10")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("MAX_RETRIES", "2")))
    extraction_max_workers: int = field(
        default_factory=lambda: max(1, int(os.getenv("EXTRACTION_MAX_WORKERS", str(min(8, max(1, cpu_count()))))))
    )
    batch_max_workers: int = field(
        default_factory=lambda: max(1, int(os.getenv("BATCH_MAX_WORKERS", str(min(4, max(1, cpu_count()))))))
    )
    row_max_workers: int = field(
        default_factory=lambda: max(1, int(os.getenv("ROW_MAX_WORKERS", "6")))
    )
    llm_max_concurrency: int = field(
        default_factory=lambda: max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "12")))
    )
    extraction_max_chars_per_chunk: int = field(
        default_factory=lambda: max(4000, int(os.getenv("EXTRACTION_MAX_CHARS_PER_CHUNK", "45000")))
    )
    enable_region_targeting: bool = field(
        default_factory=lambda: os.getenv("ENABLE_REGION_TARGETING", "1").strip().lower() not in {"0", "false", "no", ""}
    )
    schema_config_path: Path = field(
        default_factory=lambda: Path(os.getenv("SCHEMA_CONFIG_PATH", str(DEFAULT_SCHEMA_CONFIG_PATH)))
    )
    portkey_api_key: str | None = field(default_factory=lambda: os.getenv("PORTKEY_API_KEY"))
    gemini_api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    google_sheets_sync_enabled: bool = field(
        default_factory=lambda: os.getenv("GOOGLE_SHEETS_SYNC_ENABLED", "1").strip().lower() not in {"0", "false", "no", ""}
    )
    google_sheets_spreadsheet_id: str | None = field(default_factory=lambda: os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID"))
    google_sheets_sheet_name: str = field(default_factory=lambda: os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Sheet1"))
    google_service_account_json: str | None = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
    google_service_account_json_path: str | None = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH"))
    google_oauth_client_secret_path: str | None = field(default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_PATH"))
    google_oauth_token_path: str = field(
        default_factory=lambda: os.getenv("GOOGLE_OAUTH_TOKEN_PATH", str(RUNTIME_DIR / "google_oauth_token.json"))
    )

    def __post_init__(self) -> None:
        if self.execution_mode not in SUPPORTED_EXECUTION_MODES:
            supported = ", ".join(sorted(SUPPORTED_EXECUTION_MODES))
            raise ValueError(
                f"Unsupported EXECUTION_MODE '{self.execution_mode}'. Supported values: {supported}."
            )

    @property
    def uses_codex_chat_assisted_execution(self) -> bool:
        return self.execution_mode == CODEX_CHAT_ASSISTED_EXECUTION_MODE


@dataclass(slots=True)
class RuntimePaths:
    input_dir: Path
    output_dir: Path
    runtime_dir: Path
    analysis_dir: Path
    review_dir: Path
    state_path: Path
    manual_review_path: Path
    final_csv_path: Path
    csv_finalization_status_path: Path
    sheet_sync_status_path: Path
    log_path: Path
    monitor_status_path: Path
    playwright_browsers_dir: Path


def ensure_directories() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PLAYWRIGHT_BROWSERS_DIR.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    ensure_directories()
    return Settings()


def refresh_environment() -> None:
    load_dotenv(override=True)


def get_runtime_paths(root_dir: Path | None = None) -> RuntimePaths:
    if root_dir is None or root_dir.resolve() == ROOT_DIR:
        ensure_directories()
        return RuntimePaths(
            input_dir=INPUT_DIR,
            output_dir=OUTPUT_DIR,
            runtime_dir=RUNTIME_DIR,
            analysis_dir=OUTPUT_DIR / "analysis_plans",
            review_dir=OUTPUT_DIR / "transformation_reviews",
            state_path=STATE_PATH,
            manual_review_path=MANUAL_REVIEW_PATH,
            final_csv_path=FINAL_CSV_PATH,
            csv_finalization_status_path=OUTPUT_DIR / "csv_finalization_status.json",
            sheet_sync_status_path=OUTPUT_DIR / "google_sheets_sync_status.json",
            log_path=LOG_PATH,
            monitor_status_path=MONITOR_STATUS_PATH,
            playwright_browsers_dir=PLAYWRIGHT_BROWSERS_DIR,
        )

    resolved_root = root_dir.resolve()
    input_dir = resolved_root / "input_documents"
    output_dir = resolved_root / "output"
    runtime_dir = resolved_root / ".runtime"
    analysis_dir = output_dir / "analysis_plans"
    review_dir = output_dir / "transformation_reviews"
    playwright_browsers_dir = runtime_dir / "playwright-browsers"

    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    playwright_browsers_dir.mkdir(parents=True, exist_ok=True)

    return RuntimePaths(
        input_dir=input_dir,
        output_dir=output_dir,
        runtime_dir=runtime_dir,
        analysis_dir=analysis_dir,
        review_dir=review_dir,
        state_path=output_dir / "processing_state.json",
        manual_review_path=output_dir / "manual_review.json",
        final_csv_path=output_dir / "final_extracted_data.csv",
        csv_finalization_status_path=output_dir / "csv_finalization_status.json",
        sheet_sync_status_path=output_dir / "google_sheets_sync_status.json",
        log_path=output_dir / "agent.log",
        monitor_status_path=output_dir / "monitor_status.json",
        playwright_browsers_dir=playwright_browsers_dir,
    )


def setup_logging() -> None:
    ensure_directories()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
