# Influencer Automation — Setup Guide

## Overview of Components

| Component | Where it runs | Purpose |
|---|---|---|
| File Watcher | ZimaOS (Docker) | Watches Queue, archives videos, updates sheet |
| YouTube Uploader | Mac (Python script) | Uploads + schedules videos, writes URL to sheet |
| Bookmarklet | Browser (Chrome/Safari) | One-click product capture → Google Sheet |
| Apps Script | Google Cloud | Receives bookmarklet data, writes to sheet |

---

## Step 1 — Extend the Google Sheet

Add these columns to the right of your existing columns (after Keep/Sell):

| Column L | Column M | Column N |
|---|---|---|
| Video Filename | YouTube URL | YT Scheduled Date |

Do this for every month tab that has data.

---

## Step 2 — Google Cloud Console (Service Account)

The watcher and uploader need read/write access to the Google Sheet.

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. "Influencer Automation")
3. Enable **Google Sheets API**: APIs & Services → Enable APIs → search "Sheets"
4. Create a Service Account:
   - IAM & Admin → Service Accounts → Create
   - Name: `influencer-watcher`
   - Role: leave blank (not needed)
   - Done
5. Create a key:
   - Click the service account → Keys tab → Add Key → JSON
   - Download the file → rename to `service_account.json`
   - Save to: `influencer-automation/credentials/service_account.json`
6. **Share the Google Sheet** with the service account email
   - Open the sheet → Share
   - Paste the service account email (looks like `influencer-watcher@your-project.iam.gserviceaccount.com`)
   - Set to **Editor**

---

## Step 3 — YouTube OAuth Credentials

The uploader needs permission to upload to YouTube on her behalf.

1. In Google Cloud Console, same project:
   - Enable **YouTube Data API v3**: APIs & Services → Enable APIs
2. Create OAuth credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Name: "Influencer Uploader"
   - Download the JSON → rename to `youtube_client_secrets.json`
   - Save to: `influencer-automation/credentials/youtube_client_secrets.json`
3. Add her Google account as a test user:
   - OAuth consent screen → Test users → Add her YouTube account email

> The first time `uploader.py` runs, it will open a browser window asking her
> to authorize YouTube access. After that, the token is saved and it runs silently.

---

## Step 4 — Google Apps Script (Bookmarklet Receiver)

1. Go to [script.google.com](https://script.google.com) → New project
2. Paste the contents of `bookmarklet/apps-script.gs`
3. Replace `YOUR_SPREADSHEET_ID_HERE` with the actual Sheet ID
   - Get it from the sheet URL: `docs.google.com/spreadsheets/d/THIS_PART/edit`
4. Click **Deploy** → New deployment
   - Type: Web app
   - Execute as: **Me**
   - Who has access: **Anyone**
   - Deploy
5. Copy the deployment URL (looks like `https://script.google.com/macros/s/.../exec`)

---

## Step 5 — Install the Bookmarklet

1. Open `bookmarklet/bookmarklet.min.js` in a text editor
2. Replace `PASTE_YOUR_APPS_SCRIPT_URL_HERE` with the URL from Step 4
3. Copy the entire line (starts with `javascript:`)
4. In Chrome/Safari:
   - Show bookmarks bar (Cmd+Shift+B)
   - Right-click bookmarks bar → Add page (or New bookmark)
   - Name: `📦 Add to Sheet`
   - URL: paste the `javascript:` code
5. Navigate to any Amazon product page and click the bookmark
   - A green banner should appear confirming it was added

---

## Step 6 — ZimaOS Docker Setup

### Folder structure to create on ZimaOS:
```
/DATA/
├── Queue/
│   ├── Video 1/
│   ├── Video 2/
│   ├── Video 3/
│   ├── Video 4/
│   ├── Video 5/
│   ├── Video 6/
│   └── Video 7/
├── Archive/
└── credentials/
    └── service_account.json   ← copy here from Step 2
```

### Configure the watcher:
1. Copy `watcher/.env.example` → `watcher/.env`
2. Fill in:
   ```
   SPREADSHEET_ID=your_sheet_id_here
   ```
3. Update `docker-compose.yml` volume paths to match your ZimaOS folder locations:
   ```yaml
   volumes:
     - /DATA/Queue:/queue
     - /DATA/Archive:/archive
     - /DATA/credentials:/credentials:ro
   ```

### Deploy:
```bash
# On ZimaOS, in the project folder:
docker compose up -d --build

# Check logs:
docker compose logs -f watcher
```

---

## Step 7 — Mac Uploader Setup

### Mount ZimaOS Archive on Mac:
- Finder → Go → Connect to Server
- `smb://YOUR_ZIMA_IP/Archive` (or whatever your ZimaOS share is called)
- Note the mount path (e.g. `/Volumes/Archive`)

### Install Python dependencies:
```bash
cd influencer-automation/uploader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configure:
1. Copy `uploader/.env.example` → `uploader/.env`
2. Fill in:
   ```
   SPREADSHEET_ID=your_sheet_id
   ARCHIVE_PATH=/Volumes/Archive        # ZimaOS mount point on Mac
   PUBLISH_HOUR=9                       # 9am publish time
   ```

### Run:
```bash
# Preview what would be uploaded (no API calls):
python uploader.py --dry-run

# Upload and schedule all pending videos:
python uploader.py
```

> First run will open a browser for YouTube OAuth. Authorize and close.
> Every run after that is fully automatic.

---

## Credentials Folder Summary

```
influencer-automation/
└── credentials/              ← gitignored, never commit this
    ├── service_account.json  ← from Google Cloud Console (Step 2)
    ├── youtube_client_secrets.json  ← from Google Cloud Console (Step 3)
    └── youtube_token.pkl     ← auto-created after first YouTube login
```

---

## Daily Workflow (After Setup)

**Emily's steps:**
1. Open Amazon product page → click `📦 Add to Sheet` bookmarklet → green tick appears
2. Upload video to Amazon, tag ASIN, save draft (as before)
3. Drop video + ASIN-named thumbnail into an empty `Queue/Video N/` slot
4. *(Watcher runs automatically — folder archives, sheet updates)*
5. Complete Amazon submission

**Your steps (run once, processes all pending):**
```bash
python uploader.py
```

After each upload:
```
=======================================================
  REMINDER: Send Creator Connections message
  Product : Pizza Oven
  ASIN    : B0FCC6XJV
  YouTube : https://www.youtube.com/watch?v=xxxxx
  Goes live: Wednesday March 04 at 09:00 AM
=======================================================
```
