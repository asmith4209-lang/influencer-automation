# Influencer Automation — Setup Guide

## Overview of Components

| Component | Where it runs | Purpose |
|---|---|---|
| File Watcher | ZimaOS (Docker) | Watches Queue, processes thumbnails, uploads to YouTube, archives videos, updates sheet |
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

The watcher needs read/write access to the Google Sheet.

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

The watcher needs permission to upload to YouTube on her behalf.

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

---

## Step 4 — Generate the YouTube Token (One-Time, on Your PC)

Run this once from the project root — a browser will open:

```bash
cd "C:\Users\Adam Smith\Projects\influencer-automation"
pip install google-auth-oauthlib python-dotenv
python auth_youtube.py
```

- Sign in as `asmith4209@gmail.com`
- Select **Emily's channel** (not your personal channel)
- Token saves to `credentials/youtube_token.pkl`

Then copy it to ZimaOS:
```
credentials/youtube_token.pkl  →  /DATA/credentials/youtube_token.pkl
```

---

## Step 5 — Google Apps Script (Bookmarklet Receiver)

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

## Step 6 — Install the Bookmarklet

1. Open `bookmarklet/bookmarklet.min.js` in a text editor
2. Replace `PASTE_YOUR_APPS_SCRIPT_URL_HERE` with the URL from Step 5
3. Copy the entire line (starts with `javascript:`)
4. In Chrome/Safari:
   - Show bookmarks bar (Cmd+Shift+B)
   - Right-click bookmarks bar → Add page (or New bookmark)
   - Name: `📦 Add to Sheet`
   - URL: paste the `javascript:` code
5. Navigate to any Amazon product page and click the bookmark
   - A green banner should appear confirming it was added

---

## Step 7 — ZimaOS Docker Setup

### Folder structure to create on ZimaOS:
```
/media/HDD-Storage/Business/Emily/Influencer-Automation-App/
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
    ├── service_account.json   ← copy here from Step 2
    └── youtube_token.pkl      ← copy here from Step 4
```

### Configure the watcher:
1. Copy `watcher/.env.example` → `watcher/.env`
2. Fill in all values (Spreadsheet ID, Anthropic API key, affiliate tag, etc.)
3. Verify `docker-compose.yml` volume paths match your ZimaOS folder locations

### Deploy:
```bash
# SSH into ZimaOS, navigate to the project folder:
cd /DATA/influencer-automation
git pull
sudo docker compose down && sudo docker compose up -d

# Check logs:
sudo docker logs -f influencer-automation-watcher-1
```

You should see: `Slots Video 1–7 ready. Watching for drops...`

---

## Credentials Folder Summary

```
influencer-automation/
└── credentials/              ← gitignored, never commit this
    ├── service_account.json        ← from Google Cloud Console (Step 2)
    ├── youtube_client_secrets.json ← from Google Cloud Console (Step 3)
    └── youtube_token.pkl           ← auto-created by auth_youtube.py (Step 4)
```

---

## Daily Workflow (After Setup)

**Emily's steps:**
1. Open Amazon product page → click `📦 Add to Sheet` bookmarklet → green tick appears
2. Upload video to Amazon, tag ASIN, save draft (as before)
3. Drop video + ASIN-named thumbnail into an empty `Queue/Video N/` slot
4. *(Watcher runs automatically — processes thumbnail, uploads to YouTube, archives files, updates sheet)*
5. Complete Amazon submission

**No manual upload steps needed** — the Docker watcher handles everything automatically once files are dropped into a slot.

---

## Final Test Checklist

- [ ] **Bookmarklet** — click on an Amazon product page → row appears in sheet
- [ ] **Full watcher test** — drop a test video + ASIN-named thumbnail (e.g. `B0ABCDE123.jpg`) into `Queue/Video 1`
  - Watcher should: process thumbnail → archive files → recreate empty slot → upload to YouTube → update sheet columns I/L/M/N
- [ ] Check YouTube Studio — video should be scheduled for next available 9:30am slot
- [ ] Check sheet — columns L/M/N filled in, column I = "Posted"
