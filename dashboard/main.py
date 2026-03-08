"""
Dashboard Backend — FastAPI service for the Influencer Automation dashboard.
Serves the single-page frontend and provides REST API endpoints.
"""

import json
import os
import pickle
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()

# --- Paths ---
QUEUE_PATH = Path(os.getenv("QUEUE_PATH", "/queue"))
SHARED_PATH = Path(os.getenv("SHARED_PATH", "/shared"))
STAGING_PATH = SHARED_PATH / "staging"
STATE_FILE = SHARED_PATH / "state.json"
LOG_FILE = SHARED_PATH / "watcher.log"
WATCHER_ENV = Path("/app/watcher.env")

STAGING_PATH.mkdir(parents=True, exist_ok=True)

# Sheet column constants (0-indexed)
COL_DATE = 0
COL_PRODUCT = 1
COL_ASIN = 2
COL_STATUS = 8   # Column I
COL_VIDEO_FILE = 11  # Column L
COL_YT_URL = 12  # Column M
COL_YT_DATE = 13  # Column N

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


# --- Helpers ---

def load_env() -> dict:
    if WATCHER_ENV.exists():
        return dotenv_values(WATCHER_ENV)
    return {}


def get_sheets_service(env: dict):
    sa_file = env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/credentials/service_account.json")
    creds = service_account.Credentials.from_service_account_file(sa_file, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def current_month_tab() -> str:
    return datetime.now().strftime("%B")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_slot_dirs() -> list[Path]:
    return sorted(d for i in range(1, 8) if (d := QUEUE_PATH / f"Video {i}").exists())


def slot_is_free(slot_dir: Path) -> bool:
    try:
        return not any(f.is_file() for f in slot_dir.iterdir())
    except Exception:
        return False


def slot_file_presence(slot_dir: Path) -> tuple[bool, bool]:
    video_exts = {".mp4", ".mov", ".avi", ".m4v"}
    image_exts = {".jpg", ".jpeg", ".png"}
    has_video = False
    has_image = False
    try:
        for f in slot_dir.iterdir():
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in video_exts:
                has_video = True
            elif ext in image_exts:
                has_image = True
    except Exception:
        pass
    return has_video, has_image


def get_staging_batches() -> list[dict]:
    if not STAGING_PATH.exists():
        return []
    batches = []
    for batch_dir in sorted(STAGING_PATH.iterdir()):
        if not batch_dir.is_dir():
            continue
        meta_file = batch_dir / "meta.json"
        if meta_file.exists():
            try:
                with open(meta_file) as f:
                    batches.append(json.load(f))
            except Exception:
                pass
    return batches


def compute_next_publish_str(env: dict, state: dict) -> str:
    # Prefer value the watcher wrote after its last upload
    watcher_str = state.get("watcher", {}).get("next_publish_str")
    if watcher_str:
        return watcher_str
    try:
        tz = ZoneInfo("America/Denver")
        hour = int(env.get("PUBLISH_HOUR", 9))
        minute = int(env.get("PUBLISH_MINUTE", 30))
        now = datetime.now(tz)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.strftime("%a %b %-d at %-I:%M %p")
    except Exception:
        return "—"


# --- API Endpoints ---

@app.get("/api/state")
def get_state():
    env = load_env()
    spreadsheet_id = env.get("SPREADSHEET_ID")
    state = load_state()

    # Slot states
    slots_state = state.get("slots", {})
    slots = []
    free_count = 0
    for slot_dir in get_slot_dirs():
        slot_name = slot_dir.name
        is_free = slot_is_free(slot_dir)
        if is_free:
            free_count += 1
        slot_data = dict(slots_state.get(slot_name, {"stage": "empty"}))
        slot_data["slot"] = slot_name
        slot_data["free"] = is_free
        slots.append(slot_data)

    # Staging
    staging = get_staging_batches()

    # Sheet data: pending products + month count
    pending = []
    month_count = 0
    if spreadsheet_id:
        try:
            service = get_sheets_service(env)
            tab = current_month_tab()
            result = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=f"{tab}!A:N"
            ).execute()
            rows = result.get("values", [])
            for row in rows[1:]:
                if len(row) <= COL_ASIN:
                    continue
                asin = row[COL_ASIN].strip() if len(row) > COL_ASIN else ""
                if not asin:
                    continue
                month_count += 1
                yt_url = row[COL_YT_URL].strip() if len(row) > COL_YT_URL else ""
                if asin and not yt_url:
                    pending.append({
                        "product": row[COL_PRODUCT].strip() if len(row) > COL_PRODUCT else "",
                        "asin": asin,
                        "date": row[COL_DATE].strip() if len(row) > COL_DATE else "",
                    })
        except Exception:
            pass

    return {
        "slots": slots,
        "staging": staging,
        "pending": pending,
        "free_slots": free_count,
        "month_count": month_count,
        "watcher": state.get("watcher", {}),
        "cc_banner": state.get("cc_banner"),
        "next_publish_str": compute_next_publish_str(env, state),
    }


@app.get("/api/activity")
def get_activity():
    env = load_env()
    spreadsheet_id = env.get("SPREADSHEET_ID")
    tab = current_month_tab()
    if not spreadsheet_id:
        return {"rows": [], "month": tab}
    try:
        service = get_sheets_service(env)
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{tab}!A:N"
        ).execute()
        rows = []
        for row in result.get("values", [])[1:]:
            if len(row) <= COL_ASIN or not row[COL_ASIN].strip():
                continue
            rows.append({
                "date": row[COL_DATE].strip() if len(row) > COL_DATE else "",
                "product": row[COL_PRODUCT].strip() if len(row) > COL_PRODUCT else "",
                "asin": row[COL_ASIN].strip(),
                "status": row[COL_STATUS].strip() if len(row) > COL_STATUS else "",
                "yt_url": row[COL_YT_URL].strip() if len(row) > COL_YT_URL else "",
                "yt_date": row[COL_YT_DATE].strip() if len(row) > COL_YT_DATE else "",
            })
        return {"rows": rows, "month": tab}
    except Exception as e:
        return {"rows": [], "month": tab, "error": str(e)}


@app.post("/api/stage")
async def stage_files(video: UploadFile | None = File(None), image: UploadFile | None = File(None)):
    if not video and not image:
        raise HTTPException(status_code=400, detail="Upload at least one file: video or image")

    batch_id = str(uuid.uuid4())[:8]
    batch_dir = STAGING_PATH / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    if video:
        # Stream video to disk in 1 MB chunks to avoid loading into memory
        video_path = batch_dir / video.filename
        with open(video_path, "wb") as f:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

    if image:
        image_path = batch_dir / image.filename
        with open(image_path, "wb") as f:
            f.write(await image.read())

    inferred_asin = ""
    if image:
        stem = Path(image.filename).stem.strip().upper()
        if stem and stem.replace("-", "").replace("_", "").isalnum():
            inferred_asin = stem

    meta = {
        "batch_id": batch_id,
        "video_filename": video.filename if video else "",
        "image_filename": image.filename if image else "",
        "asin": inferred_asin,
        "created": datetime.now().isoformat(),
    }
    with open(batch_dir / "meta.json", "w") as f:
        json.dump(meta, f)
    return meta


@app.get("/api/stage/{batch_id}/image")
def get_stage_image(batch_id: str):
    batch_dir = STAGING_PATH / batch_id
    meta_file = batch_dir / "meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404)
    with open(meta_file) as f:
        meta = json.load(f)
    image_filename = meta.get("image_filename", "")
    if not image_filename:
        raise HTTPException(status_code=404)
    img_path = batch_dir / image_filename
    if not img_path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(img_path))


@app.patch("/api/stage/{batch_id}")
async def update_stage(batch_id: str, request: Request):
    body = await request.json()
    batch_dir = STAGING_PATH / batch_id
    meta_file = batch_dir / "meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="Batch not found")

    with open(meta_file) as f:
        meta = json.load(f)

    if "asin" in body:
        meta["asin"] = body["asin"].strip().upper()
    if "video_filename" in body and body["video_filename"] != meta.get("video_filename", ""):
        old_path = batch_dir / meta["video_filename"]
        new_path = batch_dir / body["video_filename"]
        if old_path.exists():
            old_path.rename(new_path)
        meta["video_filename"] = body["video_filename"]

    with open(meta_file, "w") as f:
        json.dump(meta, f)
    return meta


@app.delete("/api/stage/{batch_id}")
def delete_stage(batch_id: str):
    batch_dir = STAGING_PATH / batch_id
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    return {"ok": True}


@app.post("/api/push")
def push_all():
    batches = get_staging_batches()
    if not batches:
        raise HTTPException(status_code=400, detail="No staged batches")

    normalized = []
    for b in batches:
        has_video = bool(b.get("video_filename"))
        has_image = bool(b.get("image_filename"))
        if not has_video and not has_image:
            raise HTTPException(
                status_code=400,
                detail=f"Batch {b['batch_id']} has no files to push"
            )
        if has_image and not b.get("asin"):
            raise HTTPException(
                status_code=400,
                detail=f"Batch {b['batch_id']} has an image but is missing an ASIN"
            )
        normalized.append((b, has_video, has_image))

    # Most constrained first: full pairs, then single-file batches.
    normalized.sort(key=lambda item: (item[1] and item[2], item[1], item[2]), reverse=True)

    slots = get_slot_dirs()
    slot_state = {slot: slot_file_presence(slot) for slot in slots}
    assignments: list[tuple[dict, Path]] = []

    for batch, needs_video, needs_image in normalized:
        chosen = None
        for slot in slots:
            has_video, has_image = slot_state[slot]
            if needs_video and has_video:
                continue
            if needs_image and has_image:
                continue
            chosen = slot
            break

        if chosen is None:
            requested = []
            if needs_video:
                requested.append("video")
            if needs_image:
                requested.append("image")
            wanted = " + ".join(requested)
            raise HTTPException(
                status_code=400,
                detail=f"No available slot for batch {batch['batch_id']} ({wanted})"
            )

        has_video, has_image = slot_state[chosen]
        slot_state[chosen] = (has_video or needs_video, has_image or needs_image)
        assignments.append((batch, chosen))

    pushed = []
    for batch, slot in assignments:
        batch_dir = STAGING_PATH / batch["batch_id"]
        asin = batch.get("asin", "")

        if batch.get("image_filename"):
            img_ext = Path(batch["image_filename"]).suffix.lower()
            img_src = batch_dir / batch["image_filename"]
            shutil.move(str(img_src), str(slot / f"{asin}{img_ext}"))

        if batch.get("video_filename"):
            vid_src = batch_dir / batch["video_filename"]
            shutil.move(str(vid_src), str(slot / batch["video_filename"]))

        shutil.rmtree(batch_dir, ignore_errors=True)
        pushed.append({
            "slot": slot.name,
            "asin": asin,
            "video": batch.get("video_filename", ""),
            "image": batch.get("image_filename", ""),
        })

    return {"pushed": pushed}


@app.get("/api/logs")
def get_logs():
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return {"lines": [ln.rstrip() for ln in lines[-100:]]}
    except Exception:
        return {"lines": []}


@app.get("/api/health")
def get_health():
    env = load_env()
    anthropic_ok = bool(env.get("ANTHROPIC_API_KEY"))

    sa_file = env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "/credentials/service_account.json")
    sheets_ok = Path(sa_file).exists()

    token_file = env.get("YOUTUBE_TOKEN_FILE", "/credentials/youtube_token.pkl")
    token_path = Path(token_file)
    token_exists = token_path.exists()
    token_valid = False
    if token_exists:
        try:
            with open(token_path, "rb") as f:
                creds = pickle.load(f)
            token_valid = creds.valid or (creds.expired and bool(creds.refresh_token))
        except Exception:
            pass

    return {
        "anthropic": anthropic_ok,
        "sheets": sheets_ok,
        "youtube_api": token_exists,
        "youtube_token": token_valid,
    }


@app.get("/api/settings")
def get_settings():
    env = load_env()
    return {
        "publish_hour": env.get("PUBLISH_HOUR", "9"),
        "publish_minute": env.get("PUBLISH_MINUTE", "30"),
        "amazon_affiliate_tag": env.get("AMAZON_AFFILIATE_TAG", ""),
        "spreadsheet_id": env.get("SPREADSHEET_ID", ""),
    }


@app.post("/api/settings")
async def save_settings(request: Request):
    body = await request.json()
    if not WATCHER_ENV.exists():
        raise HTTPException(status_code=404, detail="watcher.env not found")

    with open(WATCHER_ENV) as f:
        lines = f.readlines()

    updates = {
        "PUBLISH_HOUR": str(body.get("publish_hour", "")).strip() or None,
        "PUBLISH_MINUTE": str(body.get("publish_minute", "")).strip() or None,
        "AMAZON_AFFILIATE_TAG": body.get("amazon_affiliate_tag"),
        "SPREADSHEET_ID": body.get("spreadsheet_id"),
    }

    updated = set()
    new_lines = []
    for line in lines:
        key = line.split("=")[0].strip()
        if key in updates and updates[key] is not None:
            new_lines.append(f"{key}={updates[key]}\n")
            updated.add(key)
        else:
            new_lines.append(line)

    for key, val in updates.items():
        if key not in updated and val is not None:
            new_lines.append(f"{key}={val}\n")

    try:
        with open(WATCHER_ENV, "w") as f:
            f.writelines(new_lines)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write settings: {e}")

    return {"ok": True}


@app.delete("/api/slots/{slot_name}/stage")
def dismiss_slot(slot_name: str):
    state = load_state()
    slots = state.get("slots", {})
    if slot_name in slots:
        slots[slot_name] = {"stage": "empty"}
        state["slots"] = slots
        save_state(state)
    return {"ok": True}


@app.post("/api/dismiss")
def dismiss_banner():
    state = load_state()
    if state.get("cc_banner"):
        state["cc_banner"]["dismissed"] = True
    save_state(state)
    return {"ok": True}


# Static files — must be mounted AFTER all API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")
