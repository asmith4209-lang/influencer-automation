"""
Queue Watcher — ZimaOS Docker Service
Monitors Queue/Video 1-7 slots. When both a video and ASIN-named thumbnail
are detected, looks up the product in Google Sheets, processes the thumbnail,
archives the files, uploads to YouTube, and updates the sheet automatically.
"""

import json
import os
import threading
import time
import shutil
import logging
from datetime import datetime
from pathlib import Path

import anthropic

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from thumbnail import process_thumbnail
from youtube import (
    get_youtube_service,
    get_latest_youtube_scheduled_date,
    next_publish_datetime,
    upload_video,
    upload_thumbnail,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# --- Config from environment ---
QUEUE_PATH = Path(os.getenv("QUEUE_PATH", "/queue"))
SHARED_PATH = Path(os.getenv("SHARED_PATH", "/shared"))
ARCHIVE_PATH = Path(os.getenv("ARCHIVE_PATH", "/archive"))
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/credentials/service_account.json")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "")

# --- Shared state helpers ---

_state_lock = threading.Lock()


def _read_state() -> dict:
    state_file = SHARED_PATH / "state.json"
    if state_file.exists():
        try:
            with open(state_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_state_raw(state: dict):
    state_file = SHARED_PATH / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def write_state(slot_name: str, data: dict):
    """Write/merge slot state into /shared/state.json."""
    with _state_lock:
        state = _read_state()
        state.setdefault("slots", {})[slot_name] = data
        _write_state_raw(state)


def _write_cc_banner(product: str, asin: str, yt_url: str, scheduled_str: str):
    """Write Creator Connections reminder to state.json."""
    with _state_lock:
        state = _read_state()
        state["cc_banner"] = {
            "product": product,
            "asin": asin,
            "yt_url": yt_url,
            "scheduled_str": scheduled_str,
            "dismissed": False,
        }
        _write_state_raw(state)


def _update_next_publish(publish_at):
    """Store the human-readable next publish string in watcher state."""
    try:
        pub_str = publish_at.strftime("%a %b %-d at %-I:%M %p")
    except ValueError:
        pub_str = publish_at.strftime("%a %b %d at %I:%M %p").replace(' 0', ' ')
    with _state_lock:
        state = _read_state()
        state.setdefault("watcher", {})["next_publish_str"] = pub_str
        _write_state_raw(state)


def _setup_file_logging():
    """Append log output to /shared/watcher.log for the dashboard."""
    log_path = SHARED_PATH / "watcher.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(log_path))
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(handler)


def _heartbeat_loop():
    """Write watcher liveness timestamp to state.json every 10 s."""
    start_time = datetime.now().isoformat()
    tz_name = os.getenv("TZ", "UTC")
    while True:
        try:
            with _state_lock:
                state = _read_state()
                state.setdefault("watcher", {}).update({
                    "running": True,
                    "start_time": start_time,
                    "last_seen": datetime.now().isoformat(),
                    "timezone": tz_name,
                })
                _write_state_raw(state)
        except Exception as e:
            log.warning(f"Heartbeat write failed: {e}")
        time.sleep(10)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column positions (0-indexed) in the sheet
COL_DATE        = 0
COL_PRODUCT     = 1
COL_ASIN        = 2
COL_AMAZON_URL  = 3   # Column D
COL_STATUS      = 8   # Column I
COL_VIDEO_FILE  = 11  # Column L
COL_YT_URL      = 12  # Column M
COL_YT_DATE     = 13  # Column N


# --- Amazon URL helpers ---

def build_amazon_url(asin: str, fallback_url: str) -> str:
    """
    Return an affiliate-tagged Amazon URL for the given ASIN.
    If AMAZON_AFFILIATE_TAG is set, always use it so the link is guaranteed
    to be Emily's affiliate link. Falls back to whatever the sheet captured.
    """
    if AMAZON_AFFILIATE_TAG:
        url = f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"
        log.info(f"  Affiliate URL: {url}")
        return url
    return fallback_url or f"https://www.amazon.com/dp/{asin}"


# --- YouTube title generation ---

def generate_youtube_title(product_name: str) -> str:
    """Use Claude to generate a YouTube-optimized title for the product video."""
    if not ANTHROPIC_API_KEY:
        return product_name

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Generate a YouTube title for an Amazon product review video about: {product_name}. "
                    "Make it engaging, conversational, and click-worthy. "
                    "Good examples: 'I Tested This Viral Flag Shirt So You Don't Have To', "
                    "'Is This Amazon Flag Shirt Worth It? My Honest Review', "
                    "'This Amazon Find Actually Surprised Me — Honest Review'. "
                    "Keep it under 80 characters. Reply with only the title, no quotes."
                )
            }]
        )
        title = response.content[0].text.strip()
        if len(title) > 100:
            title = title[:97] + "..."
        log.info(f"Generated YouTube title: '{title}'")
        return title
    except Exception as e:
        log.warning(f"Title generation failed, using product name: {e}")
        return product_name


# --- Google Sheets helpers ---

def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def current_month_tab() -> str:
    return datetime.now().strftime("%B")  # "February", "March", etc.


def load_sheet_rows(tab: str) -> list:
    """Load all rows from the given sheet tab."""
    service = get_sheets_service()
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{tab}!A:N"
        ).execute()
        return result.get("values", [])
    except Exception as e:
        log.error(f"Sheet load failed: {e}")
        return []


def lookup_asin(asin: str) -> tuple[str | None, str | None, int | None]:
    """
    Search the current month tab for the given ASIN.
    Returns (product_name, amazon_url, row_number_1indexed) or (None, None, None).
    """
    tab = current_month_tab()
    rows = load_sheet_rows(tab)

    for i, row in enumerate(rows):
        if len(row) > COL_ASIN and row[COL_ASIN].strip().upper() == asin.upper():
            product_name = row[COL_PRODUCT].strip() if len(row) > COL_PRODUCT else asin
            amazon_url = row[COL_AMAZON_URL].strip() if len(row) > COL_AMAZON_URL else ""
            return product_name, amazon_url, i + 1  # 1-indexed

    log.warning(f"ASIN {asin} not found in '{tab}' tab")
    return None, None, None


def update_sheet_row(row_index: int, video_filename: str):
    """Write video filename and status='Queued' to the sheet row."""
    service = get_sheets_service()
    tab = current_month_tab()
    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": f"{tab}!L{row_index}", "values": [[video_filename]]},
                    {"range": f"{tab}!I{row_index}", "values": [["Queued"]]},
                ]
            }
        ).execute()
        log.info(f"Sheet row {row_index} — video: {video_filename}, status: Queued")
    except Exception as e:
        log.error(f"Sheet update failed: {e}")


def write_youtube_result(row_index: int, yt_url: str, scheduled_date):
    """Write YouTube URL, scheduled date, and status='Posted' to the sheet row."""
    service = get_sheets_service()
    tab = current_month_tab()
    date_str = scheduled_date.strftime("%Y-%m-%d %H:%M")
    try:
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
        log.info(f"Sheet row {row_index} — YouTube URL saved, status: Posted")
    except Exception as e:
        log.error(f"YouTube sheet update failed: {e}")


# --- File helpers ---

def get_slot_files(slot_dir: Path) -> tuple[Path | None, Path | None]:
    """Return (video_file, image_file) found in the slot, or None if missing."""
    video, image = None, None
    try:
        for f in slot_dir.iterdir():
            if f.is_file():
                if f.suffix.lower() in VIDEO_EXTENSIONS:
                    video = f
                elif f.suffix.lower() in IMAGE_EXTENSIONS:
                    image = f
    except Exception:
        pass
    return video, image


def wait_for_stable(path: Path, timeout: int = 60) -> bool:
    """Wait until a file's size stops changing (i.e. fully written)."""
    last_size = -1
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            size = path.stat().st_size
            if size == last_size and size > 0:
                return True
            last_size = size
        except FileNotFoundError:
            return False
        time.sleep(2)
    return False


def safe_folder_name(name: str) -> str:
    """Strip characters that are invalid in folder names."""
    return "".join(c for c in name if c.isalnum() or c in " -_()").strip()


# --- Core processing ---

def process_slot(slot_dir: Path):
    """
    Full processing sequence for a ready slot:
    1. Extract ASIN from thumbnail filename
    2. Look up product in Google Sheet
    3. Process thumbnail (Claude AI)
    4. Move to Archive/Month/ProductName/
    5. Recreate empty slot
    6. Upload to YouTube + set thumbnail
    7. Update sheet with YouTube URL and scheduled date
    """
    slot_name = slot_dir.name
    video_file, image_file = get_slot_files(slot_dir)

    if not video_file or not image_file:
        return

    write_state(slot_name, {"stage": "detected", "video_file": video_file.name})
    log.info(f"Slot '{slot_name}' has both files — waiting for stable write...")
    if not wait_for_stable(video_file) or not wait_for_stable(image_file):
        log.warning(f"Files in '{slot_name}' did not stabilize — skipping")
        write_state(slot_name, {"stage": "error", "message": "Files did not stabilize"})
        return

    asin = image_file.stem.strip().upper()
    log.info(f"'{slot_name}' → ASIN: {asin}")

    write_state(slot_name, {"stage": "lookup", "asin": asin})
    product_name, amazon_url, row_index = lookup_asin(asin)
    if not product_name:
        log.error(f"'{slot_name}' blocked — ASIN {asin} not in sheet. Add it and re-drop.")
        write_state(slot_name, {"stage": "error", "asin": asin,
                                "message": f"ASIN {asin} not found in sheet. Add it and re-drop."})
        return

    safe_name = safe_folder_name(product_name)
    month = current_month_tab()
    archive_dest = ARCHIVE_PATH / month / safe_name
    archive_dest.mkdir(parents=True, exist_ok=True)

    # Generate YouTube thumbnail before archiving
    write_state(slot_name, {"stage": "thumbnail", "asin": asin, "product": product_name})
    if ANTHROPIC_API_KEY:
        thumb_result = process_thumbnail(
            image_file=image_file,
            product_name=product_name,
            output_dir=slot_dir,
            api_key=ANTHROPIC_API_KEY
        )
        if thumb_result:
            log.info(f"Thumbnail ready: {thumb_result.name}")
        else:
            log.warning("Thumbnail generation failed — continuing without it")
    else:
        log.warning("ANTHROPIC_API_KEY not set — skipping thumbnail generation")

    # Move all files (including thumbnail_final.jpg) to archive
    write_state(slot_name, {"stage": "archive", "asin": asin, "product": product_name})
    for f in slot_dir.iterdir():
        shutil.move(str(f), str(archive_dest / f.name))

    log.info(f"Archived '{slot_name}' → Archive/{month}/{safe_name}/")

    # Mark as Queued in sheet
    if row_index:
        update_sheet_row(row_index, video_file.name)

    # Recreate empty slot immediately so she can keep dropping files
    slot_dir.rmdir()
    slot_dir.mkdir()
    log.info(f"Slot '{slot_name}' recreated and ready")

    # Upload to YouTube
    try:
        youtube = get_youtube_service()

        # Find next available scheduling slot — query YouTube directly
        latest_date = get_latest_youtube_scheduled_date(youtube)
        publish_at = next_publish_datetime(latest_date)

        # Generate YouTube-optimized title via Claude
        yt_title = generate_youtube_title(product_name)

        # Build guaranteed affiliate URL from ASIN
        affiliate_url = build_amazon_url(asin, amazon_url)

        write_state(slot_name, {"stage": "uploading", "asin": asin, "product": product_name, "pct": 0})

        def _progress(pct):
            write_state(slot_name, {"stage": "uploading", "asin": asin, "product": product_name, "pct": pct})

        archived_video = archive_dest / video_file.name
        yt_url = upload_video(youtube, archived_video, yt_title, product_name, affiliate_url, publish_at,
                              progress_fn=_progress)

        # Upload custom thumbnail — prefer processed version, fall back to raw ASIN image
        thumbnail_final = archive_dest / "thumbnail_final.jpg"
        raw_thumb = archive_dest / f"{asin}.jpg"
        if not thumbnail_final.exists():
            raw_candidates = [f for f in archive_dest.iterdir()
                              if f.suffix.lower() in {".jpg", ".jpeg", ".png"}]
            if raw_candidates:
                raw_thumb = raw_candidates[0]
                log.warning(f"thumbnail_final.jpg missing — falling back to {raw_thumb.name}")

        thumb_to_upload = thumbnail_final if thumbnail_final.exists() else (raw_thumb if raw_thumb.exists() else None)
        if thumb_to_upload:
            try:
                video_id = yt_url.split("v=")[1]
                upload_thumbnail(youtube, video_id, thumb_to_upload)
            except Exception as thumb_err:
                log.error(f"Thumbnail upload failed (video still scheduled): {thumb_err}")
        else:
            log.warning("No thumbnail file available — YouTube will use auto-generated thumbnail")

        # Update sheet with YouTube URL, scheduled date, and Posted status
        if row_index:
            write_youtube_result(row_index, yt_url, publish_at)

        pub_str = publish_at.strftime("%A %B %d at %I:%M %p")
        log.info(f"'{product_name}' scheduled for {pub_str}")

        write_state(slot_name, {
            "stage": "complete",
            "asin": asin,
            "product": product_name,
            "yt_url": yt_url,
            "scheduled": publish_at.isoformat(),
            "scheduled_str": pub_str,
        })
        _write_cc_banner(product_name, asin, yt_url, pub_str)
        _update_next_publish(next_publish_datetime(publish_at))

    except RuntimeError as e:
        log.warning(f"YouTube upload skipped: {e}")
        write_state(slot_name, {"stage": "error", "asin": asin, "product": product_name, "message": str(e)})
    except Exception as e:
        log.error(f"YouTube upload failed for '{product_name}': {e}")
        write_state(slot_name, {"stage": "error", "asin": asin, "product": product_name, "message": str(e)})


# --- Watchdog handler ---

class QueueHandler(FileSystemEventHandler):
    """Watches Queue/Video N/ slots and triggers processing when both files arrive."""

    def __init__(self):
        self._processing = set()

    def on_created(self, event):
        self._evaluate(event.src_path)

    def on_modified(self, event):
        self._evaluate(event.src_path)

    def _evaluate(self, path_str: str):
        path = Path(path_str)

        # Only care about files directly inside a Video N/ slot
        if not path.is_file():
            return
        slot_dir = path.parent
        if slot_dir.parent != QUEUE_PATH:
            return
        if slot_dir in self._processing:
            return

        video, image = get_slot_files(slot_dir)
        if video and image:
            self._processing.add(slot_dir)
            try:
                time.sleep(2)  # brief pause before processing
                process_slot(slot_dir)
            finally:
                self._processing.discard(slot_dir)


# --- Entry point ---

def main():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID environment variable is not set")

    _setup_file_logging()

    # Start heartbeat thread so dashboard can show watcher liveness
    t = threading.Thread(target=_heartbeat_loop, daemon=True)
    t.start()

    log.info(f"Queue path: {QUEUE_PATH}")
    log.info(f"Archive path: {ARCHIVE_PATH}")

    # Ensure all 7 slot folders exist on startup
    QUEUE_PATH.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.mkdir(parents=True, exist_ok=True)
    for i in range(1, 8):
        (QUEUE_PATH / f"Video {i}").mkdir(exist_ok=True)

    log.info("Slots Video 1–7 ready. Watching for drops...")

    observer = Observer()
    observer.schedule(QueueHandler(), str(QUEUE_PATH), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Shutting down watcher")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
