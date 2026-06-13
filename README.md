# LinkedIn Profile Scraper

A simple desktop app that collects LinkedIn profile URLs from a search and saves them to a CSV file you can open in Excel.

You build a search on LinkedIn (with whatever filters you want — location, company, industry, school, etc.), paste the URL into the app, and it walks every page of results and exports the profile links.

---

## Table of contents

- [What it does](#what-it-does)
- [Who this is for](#who-this-is-for)
- [Quick start (non-technical users)](#quick-start-non-technical-users)
- [How to use the app](#how-to-use-the-app)
- [Run from source (developers)](#run-from-source-developers)
- [Build your own `.exe`](#build-your-own-exe)
- [How it works](#how-it-works)
- [Project files](#project-files)
- [Troubleshooting](#troubleshooting)
- [Important notes & limits](#important-notes--limits)
- [Legal / Terms of Service](#legal--terms-of-service)

---

## What it does

1. You sign in to LinkedIn through the app (credentials stay on your PC).
2. You paste a LinkedIn **People search** URL — anything from `linkedin.com/search/results/people/...` after applying filters.
3. The app opens a real Chromium browser, logs in, and clicks through every page of results.
4. It collects all the profile URLs it sees.
5. You click **Download CSV** and open the file in Excel / Google Sheets.

It uses human-like timing (random delays, scrolling, mouse jitter, occasional long pauses) to avoid getting your account flagged.

---

## Who this is for

- **Recruiters, sales, marketers** who want a list of profiles from a specific filter set.
- **Researchers** who need to enumerate people matching certain criteria.
- **Developers** who want a working Playwright + Flask scraping example.

You do **not** need to know how to code to use the packaged `.exe`. You only need Python knowledge if you want to run the source or modify it.

---

## Quick start (non-technical users)

If someone gave you a file called `LinkedInScraper.exe`:

1. Make sure **Google Chrome** is installed. ([Download here](https://www.google.com/chrome/) if not.)
2. **Double-click `LinkedInScraper.exe`.**
3. A black console window appears and your browser opens automatically to the app. If it doesn't open, copy the address shown in the console (looks like `http://127.0.0.1:5000`) into your browser.
4. The first time you run it, Chromium downloads in the background (~170 MB, takes about 30 seconds). This only happens once.
5. Follow the **How to use the app** steps below.

> **"Windows protected your PC" warning?** Click **More info → Run anyway**. The app isn't code-signed (signing costs ~$200/year), so Windows shows this warning for any unsigned program. It's safe.

Your data — login, browser session, and CSV downloads — is stored at:
```
C:\Users\YOURNAME\AppData\Local\LinkedInScraper\
```

---

## How to use the app

### 1. Sign in
- In the top card of the app, enter your LinkedIn **email** and **password**.
- Click **Save**. Credentials stay on your computer in a local config file — they're only sent to `linkedin.com` itself.

### 2. Build your LinkedIn search
- Open [linkedin.com](https://www.linkedin.com) in another tab.
- Click the search bar → choose **People**.
- Click **All filters** on the right.
- Tick whatever you want: **location, current company, industry, seniority, school, language, "open to work", verifications**, etc.
- Click **Show results**.
- **Copy the URL** from the address bar at the top of your browser.

### 3. Paste & start
- Paste that URL into the **LinkedIn People-Search URL** field in the app.
- (Optional) Tick **Only verified accounts** to keep only profiles with LinkedIn's blue checkmark.
- Click **Start Scraping**.

### 4. Solve any login challenge
- A Chromium window opens and the app logs in.
- If LinkedIn asks for a CAPTCHA, SMS code, or email verification — solve it **in that Chromium window**. The app waits up to 5 minutes.

### 5. Wait
- The progress bar shows **Page X of Y**.
- A full 100-page scrape takes **20–40 minutes**. This is deliberate — going faster gets your account flagged.
- **Don't close the black console window** while it's running. Closing it stops the app.

### 6. Download
- When it finishes, click **Download CSV**.
- Open it in Excel or Google Sheets.

---

## Run from source (developers)

Requires Python 3.10+ and Git.

```powershell
git clone <this-repo>
cd linkedin-profile-scraper

# 1. Create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt
python -m playwright install chromium

# 3. Run the web app
python app.py
```

Open **http://127.0.0.1:5000** in your browser. Enter your LinkedIn credentials in the UI — they're saved to `config.json` in the project root.

### Tech stack
- **Flask** — local web UI (`app.py`, `templates/index.html`)
- **Playwright (Chromium)** — browser automation (`scraper.py`)
- **SQLite** — stores scraped URLs and run history (`database.py`)
- **OpenAI** (optional) — used by `ai.py` for any AI-assisted features
- **PyInstaller** — builds the standalone `.exe`

---

## Build your own `.exe`

To produce a single-file Windows executable you can share with anyone:

```powershell
.\.venv\Scripts\Activate.ps1
build.bat
```

Output: **`dist\LinkedInScraper.exe`** (~40 MB).

The first time a recipient runs it, the `.exe` downloads Chromium into `%LOCALAPPDATA%\LinkedInScraper\` (~170 MB, one-time). Subsequent launches are instant.

---

## How it works

1. **Flask app** (`app.py`) serves a small web UI on `http://127.0.0.1:5000`.
2. When you click **Start Scraping**, it spawns `scraper.py` in a background thread.
3. **Playwright** launches a real Chromium browser, signs into LinkedIn, and navigates to your search URL.
4. For each page it:
   - Scrolls naturally to load lazy content.
   - Adds random mouse jitter and 5–12 second pauses between actions.
   - Occasionally takes a 15–35 second "thinking" pause.
   - Every 10 pages, takes a 45–120 second idle break.
5. It extracts every profile link from the results and writes them to SQLite + a CSV file.
6. Progress is streamed to the UI in real time.

---

## Project files

| File | Purpose |
|---|---|
| `app.py` | Flask web server and HTTP routes |
| `scraper.py` | Playwright scraping logic with anti-detection pacing |
| `database.py` | SQLite storage for runs and scraped URLs |
| `ai.py` | Optional OpenAI integration |
| `launcher.py` | Entry point used when packaged into the `.exe` |
| `templates/index.html` | Web UI |
| `build.bat` | One-click build script (runs PyInstaller) |
| `package.bat` | Packages build output for distribution |
| `LinkedInScraper.spec` | PyInstaller configuration |
| `requirements.txt` | Python dependencies |
| `config.json` | Your saved credentials (created on first save) |
| `config.example.json` | Template config for CLI/scripted mode |
| `USER_GUIDE.txt` | Plain-text guide shipped with the `.exe` |

---

## Troubleshooting

**"Windows protected your PC" on first launch**
Click **More info → Run anyway**. Appears because the `.exe` isn't code-signed.

**Browser doesn't open automatically**
Copy the URL shown in the console window (e.g. `http://127.0.0.1:5000`) into your browser manually.

**"Address already in use"**
Another copy of the app is already running. Close it (or close any process using port 5000) and try again.

**Scrape returns 0 results**
- Confirm the URL is a **People** search (`/search/results/people/...`), not Jobs, Posts, or Companies.
- Try a less restrictive filter to confirm the app itself is working.

**Login fails / stops at 6–8 results**
- Double-check email and password in the app.
- LinkedIn may be asking for an SMS / email verification code — watch the Chromium window and complete the challenge there.
- If you're on a **free LinkedIn account**, you've likely hit the monthly commercial-use cap on people-search. Wait until next month or use Premium.

**Scrape is very slow**
That's intentional. Each page has 5–12 second delays, plus longer pauses every 10 pages. Going faster gets your account flagged.

---

## Important notes & limits

- **LinkedIn's Commercial Use Limit** — Free accounts can only run a small number of people-searches per month. If the scrape stops abruptly after a handful of results, that's the cap. Options: wait it out, upgrade to Premium, or narrow your filters so you don't burn through the quota.
- **Behavioral pacing** — Randomized delays, scrolling, mouse jitter, and idle breaks slow each run but materially reduce detection risk. Don't disable them.
- **Account safety** — Use accounts you're willing to risk. Aggressive use can trigger temporary restrictions or bans.

---

## Legal / Terms of Service

Scraping LinkedIn violates [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Use this tool only:

- on accounts you own,
- against data you have a right to collect,
- in jurisdictions where doing so is lawful,
- at your own risk.

This project is provided for educational and personal-research purposes. The authors are not responsible for account suspensions, data misuse, or legal consequences arising from your use of it.
