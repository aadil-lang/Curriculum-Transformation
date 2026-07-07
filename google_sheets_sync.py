from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import RuntimePaths, Settings


LOGGER = logging.getLogger(__name__)
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


@dataclass(slots=True)
class GoogleSheetsSyncResult:
    status: str
    message: str
    csv_path: str
    spreadsheet_id: str
    sheet_name: str
    row_count: int
    synced_at_utc: str
    csv_fingerprint: str


@dataclass(slots=True)
class GoogleOAuthLoginResult:
    status: str
    message: str
    client_secret_path: str
    token_path: str
    authorized_at_utc: str


def sync_csv_to_configured_google_sheet(
    csv_path: Path,
    settings: Settings,
    runtime_paths: RuntimePaths,
) -> GoogleSheetsSyncResult:
    csv_path = csv_path.expanduser().resolve()
    spreadsheet_id = (settings.google_sheets_spreadsheet_id or "").strip()
    sheet_name = _derive_sheet_name(csv_path, settings)
    csv_fingerprint = _fingerprint(csv_path) if csv_path.exists() else ""
    synced_at_utc = datetime.now(timezone.utc).isoformat()

    if not settings.google_sheets_sync_enabled:
        result = GoogleSheetsSyncResult(
            status="disabled",
            message="Google Sheets sync is disabled by configuration.",
            csv_path=str(csv_path),
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            row_count=0,
            synced_at_utc=synced_at_utc,
            csv_fingerprint=csv_fingerprint,
        )
        _write_status(runtime_paths.sheet_sync_status_path, result)
        return result

    if not csv_path.exists():
        result = GoogleSheetsSyncResult(
            status="missing_csv",
            message="CSV file does not exist; nothing to sync.",
            csv_path=str(csv_path),
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            row_count=0,
            synced_at_utc=synced_at_utc,
            csv_fingerprint="",
        )
        _write_status(runtime_paths.sheet_sync_status_path, result)
        return result

    if not spreadsheet_id:
        result = GoogleSheetsSyncResult(
            status="not_configured",
            message="Set GOOGLE_SHEETS_SPREADSHEET_ID to enable Google Sheets sync.",
            csv_path=str(csv_path),
            spreadsheet_id="",
            sheet_name=sheet_name,
            row_count=0,
            synced_at_utc=synced_at_utc,
            csv_fingerprint=csv_fingerprint,
        )
        _write_status(runtime_paths.sheet_sync_status_path, result)
        return result

    credentials = _load_google_credentials(settings)
    if credentials is None:
        result = GoogleSheetsSyncResult(
            status="not_configured",
            message=(
                "Configure either service account credentials or OAuth user login for Google Sheets sync."
            ),
            csv_path=str(csv_path),
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            row_count=0,
            synced_at_utc=synced_at_utc,
            csv_fingerprint=csv_fingerprint,
        )
        _write_status(runtime_paths.sheet_sync_status_path, result)
        return result

    previous = _read_status(runtime_paths.sheet_sync_status_path)
    if (
        previous.get("status") == "synced"
        and previous.get("csv_path") == str(csv_path)
        and previous.get("spreadsheet_id") == spreadsheet_id
        and previous.get("sheet_name") == sheet_name
        and previous.get("csv_fingerprint") == csv_fingerprint
    ):
        result = GoogleSheetsSyncResult(
            status="up_to_date",
            message="Google Sheet already matches the latest CSV contents.",
            csv_path=str(csv_path),
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            row_count=int(previous.get("row_count", 0)),
            synced_at_utc=synced_at_utc,
            csv_fingerprint=csv_fingerprint,
        )
        _write_status(runtime_paths.sheet_sync_status_path, result)
        return result

    rows = _read_csv_rows(csv_path)
    row_count = max(len(rows) - 1, 0) if rows else 0

    try:
        service = _build_sheets_service(credentials)
        _ensure_sheet_exists(service, spreadsheet_id, sheet_name)
        sheet_range = _format_sheet_range(sheet_name)
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=sheet_range,
        ).execute()
        if rows:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_range}!A1",
                valueInputOption="RAW",
                body={"values": rows},
            ).execute()
        result = GoogleSheetsSyncResult(
            status="synced",
            message="Finalized CSV synced to Google Sheets.",
            csv_path=str(csv_path),
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            row_count=row_count,
            synced_at_utc=synced_at_utc,
            csv_fingerprint=csv_fingerprint,
        )
        _write_status(runtime_paths.sheet_sync_status_path, result)
        return result
    except Exception as exc:
        LOGGER.exception("Google Sheets sync failed for %s", csv_path)
        result = GoogleSheetsSyncResult(
            status="failed",
            message=f"Google Sheets sync failed: {exc}",
            csv_path=str(csv_path),
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            row_count=row_count,
            synced_at_utc=synced_at_utc,
            csv_fingerprint=csv_fingerprint,
        )
        _write_status(runtime_paths.sheet_sync_status_path, result)
        return result


def authorize_google_sheets_user(
    settings: Settings,
    *,
    client_secret_path: str | None = None,
    open_browser: bool = True,
    port: int = 8787,
) -> GoogleOAuthLoginResult:
    authorized_at_utc = datetime.now(timezone.utc).isoformat()
    raw_client_secret = client_secret_path or settings.google_oauth_client_secret_path or ""
    secret_path = Path(raw_client_secret).expanduser().resolve()
    token_path = _get_oauth_token_path(settings)

    if not secret_path.exists():
        return GoogleOAuthLoginResult(
            status="missing_client_secret",
            message="OAuth client secret file not found. Set GOOGLE_OAUTH_CLIENT_SECRET_PATH or pass --client-secret.",
            client_secret_path=str(secret_path),
            token_path=str(token_path),
            authorized_at_utc=authorized_at_utc,
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception as exc:
        return GoogleOAuthLoginResult(
            status="missing_dependency",
            message=f"OAuth login requires google-auth-oauthlib: {exc}",
            client_secret_path=str(secret_path),
            token_path=str(token_path),
            authorized_at_utc=authorized_at_utc,
        )

    token_path.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), [GOOGLE_SHEETS_SCOPE])
    credentials = flow.run_local_server(
        port=port,
        open_browser=open_browser,
        authorization_prompt_message="Open this URL in your browser to authorize Google Sheets access: {url}",
        success_message="Google Sheets authorization complete. You can close this window and return to the app.",
    )
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return GoogleOAuthLoginResult(
        status="authorized",
        message="OAuth user credentials saved for Google Sheets sync.",
        client_secret_path=str(secret_path),
        token_path=str(token_path),
        authorized_at_utc=authorized_at_utc,
    )


def _read_csv_rows(csv_path: Path) -> list[list[str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        return [list(row) for row in csv.reader(handle)]


def _fingerprint(csv_path: Path) -> str:
    return hashlib.sha256(csv_path.read_bytes()).hexdigest()


def _read_status(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_status(path: Path, result: GoogleSheetsSyncResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def _load_service_account_credentials(settings: Settings):
    try:
        from google.oauth2 import service_account
    except Exception:
        return None

    raw_json = (settings.google_service_account_json or "").strip()
    json_path = (settings.google_service_account_json_path or "").strip()
    if raw_json:
        info = json.loads(raw_json)
        return service_account.Credentials.from_service_account_info(info, scopes=[GOOGLE_SHEETS_SCOPE])
    if json_path:
        path = Path(json_path).expanduser().resolve()
        if path.exists():
            return service_account.Credentials.from_service_account_file(str(path), scopes=[GOOGLE_SHEETS_SCOPE])
    return None


def _load_google_credentials(settings: Settings):
    service_account_credentials = _load_service_account_credentials(settings)
    if service_account_credentials is not None:
        return service_account_credentials
    return _load_oauth_user_credentials(settings)


def _load_oauth_user_credentials(settings: Settings):
    token_path = _get_oauth_token_path(settings)
    if not token_path.exists():
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except Exception:
        return None

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), [GOOGLE_SHEETS_SCOPE])
    except Exception:
        return None

    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
            return credentials
        except Exception:
            return None
    return None


def _get_oauth_token_path(settings: Settings) -> Path:
    return Path(settings.google_oauth_token_path).expanduser().resolve()


def _build_sheets_service(credentials):
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _derive_sheet_name(csv_path: Path, settings: Settings) -> str:
    raw_name = csv_path.stem.strip()
    if not raw_name:
        raw_name = (settings.google_sheets_sheet_name or "Sheet1").strip() or "Sheet1"
    return _sanitize_sheet_name(raw_name)


def _sanitize_sheet_name(raw_name: str) -> str:
    sanitized = re.sub(r"[\\/*?\[\]:]", "_", raw_name).strip()
    sanitized = re.sub(r"\s+", " ", sanitized)
    return sanitized[:100] or "Sheet1"


def _format_sheet_range(sheet_name: str) -> str:
    escaped_sheet_name = sheet_name.replace("'", "''")
    return f"'{escaped_sheet_name}'"


def _ensure_sheet_exists(service, spreadsheet_id: str, sheet_name: str) -> None:
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties.title",
    ).execute()
    titles = {
        sheet.get("properties", {}).get("title", "")
        for sheet in metadata.get("sheets", [])
    }
    if sheet_name in titles:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": sheet_name,
                        }
                    }
                }
            ]
        },
    ).execute()
