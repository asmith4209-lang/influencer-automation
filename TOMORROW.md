# Pick Up Here Tomorrow

## Done ✓

- YouTube channel: `asmith4209@gmail.com`, Channel ID: `UCbPCAaC1iXwTCgVjZQnTDMQ`
- Publish time: 1pm MST (PUBLISH_HOUR=13)
- Google Cloud for Sheets: service account created, `service_account.json` saved
- Sheet shared with service account email
- Spreadsheet ID filled into `watcher/.env`
- YouTube OAuth client secrets: `uploader/credentials/youtube_client_secrets.json` ✓
- Apps Script deployed + bookmarklet wired and working
- **Architecture complete:** YouTube upload is now inside the watcher (no separate uploader on Emily's Mac)
  - `watcher/youtube.py` — upload logic
  - `watcher/watcher.py` — triggers upload automatically on file drop
  - `auth_youtube.py` — one-time OAuth script (run on PC)

---

## First Thing: Fill In Anthropic API Key

Open `watcher/.env` and replace the placeholder:
```
ANTHROPIC_API_KEY=your_new_anthropic_api_key_here
```
Get a key at: https://console.anthropic.com → API Keys → Create Key

---

## Step-by-Step To Finish

### 1. Google Sheet Columns (2 min)
Open the sheet and add these column headers if they're not there yet:
- **Column L** → `Video Filename`
- **Column M** → `YouTube URL`
- **Column N** → `YT Scheduled Date`

### 2. Font File (2 min)
- Download **Montserrat ExtraBold** from Google Fonts
- Save as `influencer-automation/fonts/Montserrat-ExtraBold.ttf`
- (See `fonts/README.txt` for the direct download link)

### 3. Generate YouTube Token on Your PC (5 min)
Run this once from the project root — a browser will open:
```
cd C:\Users\Adam Smith\Projects\influencer-automation
pip install google-auth-oauthlib python-dotenv
python auth_youtube.py
```
- Sign in as `asmith4209@gmail.com`
- Select **Emily's channel** (not your personal channel)
- Token saves to `uploader/credentials/youtube_token.pkl`

### 4. Copy Credentials to ZimaOS
Copy these two files to `/DATA/credentials/` on ZimaOS:
```
uploader/credentials/service_account.json   →   /DATA/credentials/service_account.json
uploader/credentials/youtube_token.pkl      →   /DATA/credentials/youtube_token.pkl
```

### 5. Create Folder Structure on ZimaOS
```
/DATA/Queue/Video 1/
/DATA/Queue/Video 2/
/DATA/Queue/Video 3/
/DATA/Queue/Video 4/
/DATA/Queue/Video 5/
/DATA/Queue/Video 6/
/DATA/Queue/Video 7/
/DATA/Archive/
/DATA/credentials/    ← already exists if you did step 4
```

### 6. Deploy Docker on ZimaOS
SSH into ZimaOS (or use its terminal), navigate to the project, then:
```bash
docker compose up -d
docker compose logs -f watcher
```
You should see: `Slots Video 1–7 ready. Watching for drops...`

### 7. Install Bookmarklet in Emily's Browser
Open `bookmarklet/bookmarklet.min.js` → copy the full `javascript:...` content
→ Drag to Emily's browser bookmarks bar (or manually create a bookmark and paste it as the URL)

---

## Needs Emily

- [ ] **Confirm sheet tab names** — watcher expects `"February"`, `"March"`, etc. (full month name, capitalized). Let me know if the tabs are named differently.
- [ ] **YouTube description** — currently uses: `{product name}\n\n{Amazon URL}\n\n#AmazonFinds ...` — any changes?

---

## Final Test Checklist

- [ ] **Bookmarklet** — click on an Amazon product page → row appears in sheet
- [ ] **Full watcher test** — drop a test video + ASIN-named thumbnail (e.g. `B0ABCDE123.jpg`) into `Queue/Video 1`
  - Watcher should: process thumbnail → archive files → recreate empty slot → upload to YouTube → update sheet columns I/L/M/N
- [ ] Check YouTube Studio — video should be scheduled for next available 1pm slot
- [ ] Check sheet — columns L/M/N filled in, column I = "Posted"
