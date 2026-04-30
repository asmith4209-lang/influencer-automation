"""
Queue Watcher — ZimaOS Docker Service
Monitors Queue/Video 1-7 slots. When both a video and ASIN-named thumbnail
are detected, looks up the product in Google Sheets, processes the thumbnail,
archives the files, uploads to YouTube, and updates the sheet automatically.
"""

import io
import json
import os
import queue
import threading
import time
import shutil
import logging
from datetime import datetime
from pathlib import Path

import anthropic

from watchdog.observers.polling import PollingObserver
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
_slot_queue = queue.Queue()


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
ASIN_SENTINEL_EXT = ".asin"  # zero-byte marker written when video is pushed without a thumbnail
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
                    "Make it natural and curiosity-driven — like something a real person would click. "
                    "Do NOT use first-person action phrases like 'I tried', 'I tested', 'I wore', 'I slept on', etc. "
                    "Focus on the product itself and what makes it worth watching. "
                    "Good examples: 'The Gold Cross Earrings Worth the Hype?', "
                    "'Amazon Paint Set — Actually Good for Kids?', "
                    "'Honest Take on This Birthstone Necklace'. "
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


AFFILIATE_DISCLOSURE = (
    "---\n\n"
    "This is an affiliate link and if you purchase through this link, I may make a small commission "
    "at no extra cost to you. Thank you for supporting my channel!\n\n"
    "#amazon #amazonfinds #amazoninfluencer #amazonreview"
)


def generate_youtube_description(product_name: str, amazon_url: str) -> str:
    """Use Claude to generate a catchy, price-free YouTube video description."""
    fallback = (
        f"Check out {product_name} on Amazon!\n\n"
        f"{amazon_url}\n\n"
        f"{AFFILIATE_DISCLOSURE}"
    )
    if not ANTHROPIC_API_KEY:
        return fallback

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a short YouTube video description for an Amazon product review of: {product_name}. "
                    "Be friendly, genuine, and conversational — like recommending something to a friend. "
                    "Do NOT mention any prices, costs, or dollar amounts whatsoever. "
                    "Do not use excessive hype, all-caps, or clickbait. Keep it 2-3 sentences. "
                    "After the sentences, add a blank line, then the exact text [LINK], "
                    "then a blank line, then only the word [END]. "
                    "Reply with only the description text, nothing else."
                )
            }]
        )
        desc = response.content[0].text.strip()
        desc = desc.replace("[LINK]", amazon_url or "https://amazon.com")
        desc = desc.replace("[END]", "").rstrip()
        desc = f"{desc}\n\n{AFFILIATE_DISCLOSURE}"
        log.info(f"Generated YouTube description ({len(desc)} chars)")
        return desc
    except Exception as e:
        log.warning(f"Description generation failed, using fallback: {e}")
        return fallback


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
    """
    Return (video_file, image_or_sentinel) found in the slot, or None if missing.
    image_or_sentinel may be a real image (.jpg/.png) or a zero-byte .asin sentinel
    written when a video is pushed without a thumbnail.
    """
    video, image, sentinel = None, None, None
    try:
        for f in slot_dir.iterdir():
            if f.is_file():
                if f.suffix.lower() in VIDEO_EXTENSIONS:
                    video = f
                elif f.suffix.lower() in IMAGE_EXTENSIONS:
                    image = f
                elif f.suffix.lower() == ASIN_SENTINEL_EXT:
                    sentinel = f
    except Exception:
        pass
    return video, image or sentinel


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


# --- Thumbnail helpers ---

def _compress_thumbnail_if_needed(path: Path, max_bytes: int = 2 * 1024 * 1024) -> Path:
    """Re-compress a JPEG thumbnail to under max_bytes (YouTube 2MB limit). Modifies in-place."""
    if path.stat().st_size <= max_bytes:
        return path
    log.info(f"  Thumbnail {path.stat().st_size // 1024}KB > 2MB limit — recompressing...")
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    for quality in [85, 75, 65, 55, 45]:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= max_bytes:
            path.write_bytes(buf.getvalue())
            log.info(f"  Thumbnail recompressed to {buf.tell() // 1024}KB at quality={quality}")
            return path
    # Last resort: resize to 1280x720 and compress
    img = img.resize((1280, 720))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=45)
    path.write_bytes(buf.getvalue())
    log.info(f"  Thumbnail resized+recompressed to {buf.tell() // 1024}KB")
    return path


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
    # Only wait for real files to stabilize; .asin sentinels are zero-byte by design
    files_to_check = [video_file]
    if image_file.suffix.lower() in IMAGE_EXTENSIONS:
        files_to_check.append(image_file)
    if not all(wait_for_stable(f) for f in files_to_check):
        log.warning(f"Files in '{slot_name}' did not stabilize — skipping")
        write_state(slot_name, {"stage": "error", "message": "Files did not stabilize"})
        return

    asin = image_file.stem.strip().upper()
    has_real_thumbnail = image_file.suffix.lower() in IMAGE_EXTENSIONS
    log.info(f"'{slot_name}' → ASIN: {asin}" + ("" if has_real_thumbnail else " (no thumbnail)"))

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

    # Generate YouTube thumbnail before archiving (only if we have a real image)
    if has_real_thumbnail:
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
    else:
        log.info("No thumbnail image — skipping thumbnail generation (sentinel-only slot)")

    # Move all files (including thumbnail_final.jpg) to archive; skip .asin sentinels
    write_state(slot_name, {"stage": "archive", "asin": asin, "product": product_name})
    for f in slot_dir.iterdir():
        if f.suffix.lower() == ASIN_SENTINEL_EXT:
            f.unlink()  # discard zero-byte sentinel
            continue
        shutil.move(str(f), str(archive_dest / f.name))

    log.info(f"Archived '{slot_name}' → Archive/{month}/{safe_name}/")

    # Mark as Queued in sheet
    if row_index:
        update_sheet_row(row_index, video_file.name)

    # Ensure slot is empty and ready for new drops (without deleting the dir)
    for leftover in slot_dir.iterdir():
        try:
            leftover.unlink()
        except Exception:
            pass
    log.info(f"Slot '{slot_name}' ready")

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

        # Generate catchy, price-free description via Claude
        yt_description = generate_youtube_description(product_name, affiliate_url)

        write_state(slot_name, {"stage": "uploading", "asin": asin, "product": product_name, "pct": 0})

        def _progress(pct):
            write_state(slot_name, {"stage": "uploading", "asin": asin, "product": product_name, "pct": pct})

        archived_video = archive_dest / video_file.name
        yt_url = upload_video(youtube, archived_video, yt_title, product_name, affiliate_url, publish_at,
                              description=yt_description, progress_fn=_progress)

        # Upload custom thumbnail — only if we had a real image to work with
        thumb_to_upload = None
        if has_real_thumbnail:
            thumbnail_final = archive_dest / "thumbnail_final.jpg"
            raw_thumb = archive_dest / f"{asin}.jpg"
            if not thumbnail_final.exists():
                raw_candidates = [f for f in archive_dest.iterdir()
                                  if f.suffix.lower() in {".jpg", ".jpeg", ".png"}]
                if raw_candidates:
                    raw_thumb = raw_candidates[0]
                    log.warning(f"thumbnail_final.jpg missing — falling back to {raw_thumb.name}")
            thumb_to_upload = thumbnail_final if thumbnail_final.exists() else (raw_thumb if raw_thumb.exists() else None)
        else:
            log.info("No thumbnail image provided — YouTube will use auto-generated thumbnail")
        if thumb_to_upload:
            thumb_to_upload = _compress_thumbnail_if_needed(thumb_to_upload)
        if thumb_to_upload:
            try:
                video_id = yt_url.split("v=")[1]
                upload_thumbnail(youtube, video_id, thumb_to_upload)
            except Exception as thumb_err:
                log.error(f"Thumbnail upload failed (video still scheduled): {thumb_err}", exc_info=True)
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
            "completed_at": datetime.now().isoformat(),
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
    """Watches Queue/Video N/ slots. Queues slots for serial processing and writes pending state immediately."""

    def __init__(self):
        self._queued = set()   # slots already in queue (pending or processing)
        self._lock = threading.Lock()

    def on_created(self, event):
        self._evaluate(event.src_path)

    def on_modified(self, event):
        self._evaluate(event.src_path)

    def _evaluate(self, path_str: str):
        path = Path(path_str)
        if not path.is_file():
            return
        slot_dir = path.parent
        if slot_dir.parent != QUEUE_PATH:
            return

        with self._lock:
            if slot_dir in self._queued:
                return
            video, image = get_slot_files(slot_dir)
            if not (video and image):
                return
            self._queued.add(slot_dir)

        # Write pending state immediately so dashboard shows all queued slots
        asin = image.stem.strip().upper()
        write_state(slot_dir.name, {"stage": "pending", "video_file": video.name, "asin": asin})
        log.info(f"Slot '{slot_dir.name}' queued (ASIN: {asin})")
        _slot_queue.put(slot_dir)

    def mark_done(self, slot_dir: Path):
        with self._lock:
            self._queued.discard(slot_dir)


def _processing_worker(handler: QueueHandler):
    """Single worker thread — processes queued slots one at a time."""
    while True:
        slot_dir = _slot_queue.get()
        try:
            time.sleep(2)  # brief pause for file write to settle
            process_slot(slot_dir)
        finally:
            handler.mark_done(slot_dir)
            _slot_queue.task_done()


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

    handler = QueueHandler()

    # Start single worker thread for serial slot processing
    worker = threading.Thread(target=_processing_worker, args=(handler,), daemon=True)
    worker.start()

    # Startup scan: pick up any files already sitting in slots
    for i in range(1, 8):
        slot_dir = QUEUE_PATH / f"Video {i}"
        if slot_dir.exists():
            video, image = get_slot_files(slot_dir)
            if video and image:
                asin = image.stem.strip().upper()
                with handler._lock:
                    handler._queued.add(slot_dir)
                write_state(slot_dir.name, {"stage": "pending", "video_file": video.name, "asin": asin})
                log.info(f"Startup: queued existing slot '{slot_dir.name}' (ASIN: {asin})")
                _slot_queue.put(slot_dir)

    observer = PollingObserver(timeout=5)
    observer.schedule(handler, str(QUEUE_PATH), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(10)
            # Periodic rescan in case polling missed a drop
            for i in range(1, 8):
                slot_dir = QUEUE_PATH / f"Video {i}"
                if not slot_dir.exists():
                    continue
                with handler._lock:
                    if slot_dir in handler._queued:
                        continue
                    video, image = get_slot_files(slot_dir)
                    if not (video and image):
                        continue
                    handler._queued.add(slot_dir)
                asin = image.stem.strip().upper()
                write_state(slot_dir.name, {"stage": "pending", "video_file": video.name, "asin": asin})
                log.info(f"Rescan: queued slot '{slot_dir.name}' (ASIN: {asin})")
                _slot_queue.put(slot_dir)
    except KeyboardInterrupt:
        log.info("Shutting down watcher")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
