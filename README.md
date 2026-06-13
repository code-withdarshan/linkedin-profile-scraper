# LinkedIn Profile URL Scraper

Automates LinkedIn login, walks the paginated results of a **pre-filtered people-search URL**, and writes profile URLs to a CSV. Ships either as a Python script or a single Windows `.exe` you can hand to anyone.

## Two ways to use this

### A. Run from source (for development)

```powershell
cd "linkedin-profile-screaper"

# 1. Setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

# 2. Run
python app.py
```

Open **http://127.0.0.1:5000** in your browser. Enter your LinkedIn credentials in the app (they're saved to `config.json`).

### B. Build a distributable `.exe` (for sharing)

```powershell
.\.venv\Scripts\Activate.ps1
build.bat
```

This produces **`dist\LinkedInScraper.exe`** — a single ~40 MB file. Send it to anyone running Windows 10+. They:

1. Double-click `LinkedInScraper.exe`
2. On first launch it downloads Chromium (~170 MB, one-time, takes ~30s)
3. Their default browser opens to the app UI automatically
4. They enter their LinkedIn login in the app
5. Paste a LinkedIn search URL and hit Start

Per-user data (credentials, browser session, CSV outputs) is saved to:
```
%LOCALAPPDATA%\LinkedInScraper\
```

## How to use the app

1. **Sign in** — enter your LinkedIn email + password (saved locally)
2. **Build your search on linkedin.com:**
   - Go to People search
   - Click **All filters**
   - Set whatever filters you want (industry, company, location, seniority, school, verifications, etc.)
   - Hit "Show results", copy the URL from the address bar
3. **Paste the URL** into the app
4. (Optional) Tick **Only verified accounts**
5. Click **Start Scraping**
6. A Chromium window opens. Solve any LinkedIn login challenge in that window — the script waits up to 5 min.
7. The app auto-detects how many pages of results exist and walks all of them
8. When done, click **Download CSV**

## Notes

- **LinkedIn Commercial Use Limit**: free accounts have a monthly cap on people-search. If the scrape stops after ~6–8 results, that's the cap. Solutions: wait it out, use Premium, or narrow your filters.
- **Behavioral pacing**: the scraper uses randomized scrolling, mouse jitter, variable delays (5–12s, occasional 15–35s "thinking" pauses), and a 45–120s idle break every 10 pages — this slows the run but materially reduces detection.
- **Terms of Service**: scraping LinkedIn violates their ToS. Use on accounts and data you own.

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask web server |
| `scraper.py` | Playwright scraping logic |
| `launcher.py` | Entry point for the packaged .exe |
| `templates/index.html` | Web UI |
| `build.bat` | One-click build script |
| `requirements.txt` | Python deps |
| `config.example.json` | Template config (CLI mode) |
