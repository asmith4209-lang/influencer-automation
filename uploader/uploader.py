"""
YouTube Uploader — run on Mac
Scans Archive for videos with no YouTube URL in the sheet,
uploads each one, schedules them one per day, and writes
the URL + scheduled date back to Google Sheets.

Usage:
    python uploader.py            # upload all pending videos
    python uploader.py --dry-run  # preview what would be uploaded
"""

import os
import sys
import pickle
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# --- Config ---
ARCHIVE_PATH = Path(os.getenv("ARCHIVE_PATH", "./archive"))
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "./credentials/service_account.json")
YOUTUBE_CLIENT_SECRETS = os.getenv("YOUTUBE_CLIENT_SECRETS", "./credentials/youtube_client_secrets.json")
YOUTUBE_TOKEN_FILE = os.getenv("YOUTUBE_TOKEN_FILE", "./credentials/youtube_token.pkl")
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCbPCAaC1iXwTCgVjZQnTDMQ")

# What hour to publish at (24h, your local timezone — Mac must be set to MST)
PUBLISH_HOUR = int(os.getenv("PUBLISH_HOUR", "13"))

# YouTube video description template
# Available placeholders: {product}, {amazon_url}
DESCRIPTION_TEMPLATE = os.getenv("DESCRIPTION_TEMPLATE", """\
Check out {product} on Amazon!

{amazon_url}

#amazon #amazonfinds #amazoninfluencer #amazonreview
""")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Sheet column indices (0-based for row array access)
IDX_PRODUCT = 1
IDX_ASIN = 2
IDX_PRODUCT_URL = 3
IDX_YT_URL = 12   # Column M
IDX_YT_DATE = 13  # Column N


# --- Auth ---

def get_youtube_service():
    """OAuth2 flow for YouTube — opens browser on first run, caches token."""
    creds = None
    token_path = Path(YOUTUBE_TOKEN_FILE)

    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing YouTube token...")
            creds.refresh(Request())
        else:
            log.info("Opening browser for YouTube authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(
                YOUTUBE_CLIENT_SECRETS, YOUTUBE_SCOPES
            )
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)
        log.info("YouTube token saved")

    service = build("youtube", "v3", credentials=creds)

    # Sanity check — confirm we're on the right channel before uploading anything
    if YOUTUBE_CHANNEL_ID:
        resp = service.channels().list(part="id,snippet", mine=True).execute()
        channels = resp.get("items", [])
        matched = any(c["id"] == YOUTUBE_CHANNEL_ID for c in channels)
        if not matched:
            ids = [c["id"] for c in channels]
            raise RuntimeError(
                f"Wrong YouTube account. Expected channel {YOUTUBE_CHANNEL_ID}, "
                f"but authorized account has: {ids}. "
                "Delete credentials/youtube_token.pkl and re-authorize with the correct account."
            )
        name = channels[0]["snippet"]["title"] if channels else "unknown"
        log.info(f"Authorized YouTube channel: {name} ({YOUTUBE_CHANNEL_ID})")

    return service


def get_sheets_service():
    """Service account auth for Google Sheets."""
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SHEETS_SCOPES
    )
    return build("sheets", "v4", credentials=creds)


# --- Sheets helpers ---

def current_month_tab() -> str:
    return datetime.now().strftime("%B")


def load_sheet_rows(tab: str) -> list[list[str]]:
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{tab}!A:N"
    ).execute()
    return result.get("values", [])


def find_row_by_asin(rows: list, asin: str) -> tuple[dict | None, int | None]:
    """Return (row_data_dict, 1indexed_row_number) for the matching ASIN."""
    for i, row in enumerate(rows):
        # Pad row to expected length
        padded = row + [""] * (IDX_YT_DATE + 1 - len(row))
        if padded[IDX_ASIN].strip().upper() == asin.upper():
            return {
                "product": padded[IDX_PRODUCT].strip(),
                "asin": padded[IDX_ASIN].strip(),
                "amazon_url": padded[IDX_PRODUCT_URL].strip(),
                "yt_url": padded[IDX_YT_URL].strip(),
                "yt_date": padded[IDX_YT_DATE].strip(),
            }, i + 1  # 1-indexed
    return None, None


def get_latest_scheduled_date(rows: list) -> datetime | None:
    """Find the most future scheduled date already in the sheet."""
    latest = None
    for row in rows:
        padded = row + [""] * (IDX_YT_DATE + 1 - len(row))
        date_str = padded[IDX_YT_DATE].strip()
        if date_str:
            try:
                d = datetime.fromisoformat(date_str).replace(tzinfo=None)
                if latest is None or d > latest:
                    latest = d
            except ValueError:
                pass
    return latest


def write_youtube_result(row_index: int, yt_url: str, scheduled_date: datetime, tab: str):
    """Write YouTube URL, scheduled date, and updated status to the sheet."""
    service = get_sheets_service()
    date_str = scheduled_date.strftime("%Y-%m-%d %H:%M")

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": [
                {"range": f"{tab}!M{row_index}", "values": [[yt_url]]},
                {"range": f"{tab}!N{row_index}", "values": [[date_str]]},
                {"range": f"{tab}!I{row_index}", "values": [["Posted"]]},
            ]
        }
    ).execute()


# --- Archive scanning ---

def get_slot_files(product_dir: Path) -> tuple[Path | None, Path | None]:
    """Return (video_file, image_file) from an archive product folder."""
    video, image = None, None
    for f in product_dir.iterdir():
        if f.is_file():
            if f.suffix.lower() in VIDEO_EXTENSIONS:
                video = f
            elif f.suffix.lower() in IMAGE_EXTENSIONS:
                image = f
    return video, image


def find_pending_videos(tab: str, rows: list) -> list[dict]:
    """
    Scan Archive/CurrentMonth/ for product folders with no YouTube URL.
    Returns list of dicts with all info needed to upload.
    """
    month = datetime.now().strftime("%B")
    month_dir = ARCHIVE_PATH / month
    pending = []

    if not month_dir.exists():
        log.warning(f"No archive folder found for {month}")
        return pending

    for product_dir in sorted(month_dir.iterdir()):
        if not product_dir.is_dir():
            continue

        video_file, image_file = get_slot_files(product_dir)
        if not video_file or not image_file:
            log.warning(f"Skipping {product_dir.name} — missing video or thumbnail")
            continue

        asin = image_file.stem.strip().upper()
        row_data, row_index = find_row_by_asin(rows, asin)

        if not row_data:
            log.warning(f"Skipping {product_dir.name} — ASIN {asin} not in sheet")
            continue

        if row_data["yt_url"]:
            log.info(f"Skipping {product_dir.name} — already uploaded")
            continue

        thumbnail_file = product_dir / "thumbnail_final.jpg"

        pending.append({
            "product_dir": product_dir,
            "video_file": video_file,
            "thumbnail_file": thumbnail_file if thumbnail_file.exists() else None,
            "asin": asin,
            "product": row_data["product"],
            "amazon_url": row_data["amazon_url"],
            "row_index": row_index,
        })

    return pending


# --- YouTube upload ---

def next_publish_datetime(latest: datetime | None) -> datetime:
    """
    Calculate the next available daily publish slot.
    Always at PUBLISH_HOUR, starting from tomorrow if no queue,
    or the day after the latest scheduled video.
    """
    now = datetime.now()
    tomorrow = now.replace(hour=PUBLISH_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=1)

    if latest is None:
        return tomorrow

    candidate = latest.replace(hour=PUBLISH_HOUR, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(tomorrow, candidate)


def upload_video(youtube, video_file: Path, product: str, amazon_url: str, publish_at: datetime) -> str:
    """
    Upload video to YouTube as a scheduled private video.
    Returns the YouTube video URL.
    """
    description = DESCRIPTION_TEMPLATE.format(
        product=product,
        amazon_url=amazon_url or "https://amazon.com"
    )

    # YouTube requires publish time in UTC ISO 8601
    publish_utc = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "snippet": {
            "title": product,
            "description": description,
            "tags": product.split(),
            "categoryId": "26",  # Howto & Style
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_utc,
            "selfDeclaredMadeForKids": False,
        }
    }

    media = MediaFileUpload(
        str(video_file),
        mimetype="video/*",
        resumable=True,
        chunksize=5 * 1024 * 1024  # 5MB chunks
    )

    log.info(f"Uploading: {video_file.name} ({video_file.stat().st_size / 1024 / 1024:.1f} MB)")
    log.info(f"  Title: {product}")
    log.info(f"  Scheduled: {publish_at.strftime('%Y-%m-%d %I:%M %p')}")

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            log.info(f"  Upload progress: {pct}%")

    video_id = response["id"]
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info(f"  Done: {yt_url}")
    return yt_url


def upload_thumbnail(youtube, video_id: str, thumbnail_file: Path):
    """Upload custom thumbnail for a YouTube video."""
    media = MediaFileUpload(
        str(thumbnail_file),
        mimetype="image/jpeg",
        resumable=False
    )
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=media
    ).execute()
    log.info(f"  Thumbnail set: {thumbnail_file.name}")


# --- Main ---

def main():
    dry_run = "--dry-run" in sys.argv

    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is not set in .env")

    if dry_run:
        log.info("=== DRY RUN — no uploads will happen ===")

    tab = current_month_tab()
    log.info(f"Sheet tab: {tab}")

    rows = load_sheet_rows(tab)
    pending = find_pending_videos(tab, rows)

    if not pending:
        log.info("No pending videos to upload.")
        return

    log.info(f"Found {len(pending)} video(s) to upload")

    if dry_run:
        for item in pending:
            log.info(f"  Would upload: {item['product']} ({item['asin']})")
        return

    youtube = get_youtube_service()

    # Start scheduling from after the latest already-scheduled date
    latest_date = get_latest_scheduled_date(rows)
    next_date = next_publish_datetime(latest_date)

    for item in pending:
        log.info(f"\n--- Processing: {item['product']} ---")
        try:
            yt_url = upload_video(
                youtube,
                item["video_file"],
                item["product"],
                item["amazon_url"],
                next_date
            )

            if item["thumbnail_file"]:
                video_id = yt_url.split("v=")[1]
                upload_thumbnail(youtube, video_id, item["thumbnail_file"])
            else:
                log.warning(f"  No thumbnail_final.jpg found — skipping custom thumbnail")

            write_youtube_result(item["row_index"], yt_url, next_date, tab)
            log.info(f"Sheet updated for row {item['row_index']}")

            # Print Creator Connections reminder
            print(f"\n{'='*55}")
            print(f"  REMINDER: Send Creator Connections message")
            print(f"  Product : {item['product']}")
            print(f"  ASIN    : {item['asin']}")
            print(f"  YouTube : {yt_url}")
            print(f"  Goes live: {next_date.strftime('%A %B %d at %I:%M %p')}")
            print(f"{'='*55}\n")

            # Advance to next day slot for subsequent videos
            next_date = next_date + timedelta(days=1)

        except Exception as e:
            log.error(f"Failed to upload {item['product']}: {e}")
            continue

    log.info("All done.")


if __name__ == "__main__":
    main()
