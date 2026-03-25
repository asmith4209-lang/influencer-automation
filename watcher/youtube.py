"""
YouTube upload helpers for the watcher service.

On first use: run auth_youtube.py on your PC to generate youtube_token.pkl,
then copy it to /DATA/credentials/youtube_token.pkl on ZimaOS.
Token refreshes automatically after that.
"""

import os
import pickle
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

PUBLISH_HOUR    = int(os.getenv("PUBLISH_HOUR", "9"))
PUBLISH_MINUTE  = int(os.getenv("PUBLISH_MINUTE", "30"))
CHANNEL_ID      = os.getenv("YOUTUBE_CHANNEL_ID", "UCbPCAaC1iXwTCgVjZQnTDMQ")
TOKEN_FILE      = os.getenv("YOUTUBE_TOKEN_FILE", "/credentials/youtube_token.pkl")

DESCRIPTION_TEMPLATE = os.getenv("DESCRIPTION_TEMPLATE", """\
Check out {product} on Amazon!

{amazon_url}

---

This is an affiliate link and if you purchase through this link, I may make a small commission at no extra cost to you. Thank you for supporting my channel!

#amazon #amazonfinds #amazoninfluencer #amazonreview
""")

IDX_YT_DATE = 13  # Column N (0-indexed in row arrays)


def get_youtube_service():
    """Load cached OAuth token. Refreshes automatically. Never opens a browser."""
    token_path = Path(TOKEN_FILE)

    if not token_path.exists():
        raise RuntimeError(
            f"YouTube token not found at {TOKEN_FILE}. "
            "Run auth_youtube.py on your PC first, then copy "
            "youtube_token.pkl to /DATA/credentials/ on ZimaOS."
        )

    with open(token_path, "rb") as f:
        creds = pickle.load(f)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("Refreshing YouTube token...")
            creds.refresh(Request())
            with open(token_path, "wb") as f:
                pickle.dump(creds, f)
            log.info("YouTube token refreshed and saved.")
        else:
            raise RuntimeError(
                "YouTube token is invalid and cannot be refreshed. "
                "Re-run auth_youtube.py on your PC to generate a new token, "
                "then copy it to /DATA/credentials/youtube_token.pkl."
            )

    service = build("youtube", "v3", credentials=creds)

    if CHANNEL_ID:
        resp = service.channels().list(part="id,snippet", mine=True).execute()
        channels = resp.get("items", [])
        matched = any(c["id"] == CHANNEL_ID for c in channels)
        if not matched:
            ids = [c["id"] for c in channels]
            raise RuntimeError(
                f"Wrong YouTube account. Expected channel {CHANNEL_ID}, "
                f"but authorized account has: {ids}."
            )
        name = channels[0]["snippet"]["title"] if channels else "unknown"
        log.info(f"YouTube channel verified: {name} ({CHANNEL_ID})")

    return service


def get_latest_youtube_scheduled_date(youtube) -> datetime | None:
    """
    Query the YouTube API directly to find the latest future scheduled publish date.
    This catches videos scheduled via YouTube Studio that aren't in the sheet yet.
    """
    try:
        # Get the channel's uploads playlist ID
        ch_resp = youtube.channels().list(
            part="contentDetails",
            mine=True
        ).execute()

        if not ch_resp.get("items"):
            return None

        uploads_playlist_id = (
            ch_resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        )

        # Collect recent video IDs from the uploads playlist (up to 200)
        video_ids = []
        next_page_token = None

        while len(video_ids) < 200:
            pl_resp = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="snippet",
                maxResults=50,
                pageToken=next_page_token
            ).execute()

            for item in pl_resp.get("items", []):
                video_ids.append(item["snippet"]["resourceId"]["videoId"])

            next_page_token = pl_resp.get("nextPageToken")
            if not next_page_token:
                break

        if not video_ids:
            return None

        # Fetch status for those videos in batches of 50
        now_utc = datetime.now(timezone.utc)
        latest = None

        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            v_resp = youtube.videos().list(
                part="status",
                id=",".join(batch)
            ).execute()

            for video in v_resp.get("items", []):
                publish_at = video.get("status", {}).get("publishAt")
                if publish_at:
                    pub_dt = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
                    if pub_dt > now_utc:
                        if latest is None or pub_dt > latest:
                            latest = pub_dt

        if latest:
            # Return as naive local time for consistency with next_publish_datetime
            return latest.astimezone().replace(tzinfo=None)
        return None

    except Exception as e:
        log.warning(f"Could not fetch YouTube scheduled dates: {e}")
        return None


def next_publish_datetime(latest: datetime | None) -> datetime:
    """Calculate next available daily publish slot at PUBLISH_HOUR:PUBLISH_MINUTE."""
    now = datetime.now()
    tomorrow = now.replace(
        hour=PUBLISH_HOUR, minute=PUBLISH_MINUTE, second=0, microsecond=0
    ) + timedelta(days=1)

    if latest is None:
        return tomorrow

    candidate = latest.replace(
        hour=PUBLISH_HOUR, minute=PUBLISH_MINUTE, second=0, microsecond=0
    ) + timedelta(days=1)
    return max(tomorrow, candidate)


def upload_video(
    youtube, video_file: Path, title: str, product: str, amazon_url: str, publish_at: datetime,
    description: str | None = None,
    progress_fn=None
) -> str:
    """Upload video to YouTube as a scheduled private video. Returns YouTube URL."""
    if description is None:
        description = DESCRIPTION_TEMPLATE.format(
            product=product,
            amazon_url=amazon_url or "https://amazon.com"
        )

    publish_utc = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "snippet": {
            "title": title,
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
        chunksize=5 * 1024 * 1024
    )

    log.info(f"  Uploading: {video_file.name} ({video_file.stat().st_size / 1024 / 1024:.1f} MB)")
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
            if progress_fn:
                progress_fn(pct)

    video_id = response["id"]
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info(f"  Uploaded: {yt_url}")
    return yt_url


def upload_thumbnail(youtube, video_id: str, thumbnail_file: Path):
    """Set a custom thumbnail on a YouTube video."""
    if not thumbnail_file.exists():
        raise FileNotFoundError(f"Thumbnail file not found: {thumbnail_file}")
    size_kb = thumbnail_file.stat().st_size // 1024
    if size_kb == 0:
        raise ValueError(f"Thumbnail file is empty: {thumbnail_file}")
    log.info(f"  Uploading thumbnail: {thumbnail_file.name} ({size_kb} KB)")

    ext = thumbnail_file.suffix.lower()
    mimetype = "image/png" if ext == ".png" else "image/jpeg"

    media = MediaFileUpload(
        str(thumbnail_file),
        mimetype=mimetype,
        resumable=False
    )
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=media
    ).execute()
    log.info(f"  Thumbnail set: {thumbnail_file.name}")
