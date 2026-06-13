"""LinkedIn people-search profile URL scraper.

Reads a config.json with credentials + a pre-filtered LinkedIn people-search URL,
logs in (reusing a persistent session when possible), iterates the result pages,
extracts unique profile URLs, and writes them to a CSV.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LOGGER = logging.getLogger("linkedin_scraper")

PROFILE_HREF_RE = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+")


def chrome_user_data_root() -> Path | None:
    """Return Chrome's User Data folder for the current OS, if it exists."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            p = Path(local) / "Google" / "Chrome" / "User Data"
            if p.exists():
                return p
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        if p.exists():
            return p
    else:
        p = Path.home() / ".config" / "google-chrome"
        if p.exists():
            return p
    return None


def list_chrome_profiles() -> list[dict]:
    """List the user's Chrome profiles by reading `Local State`.

    Returns a list of {dir, name} dicts — `dir` is the profile directory name
    (e.g. "Default", "Profile 1") to pass to --profile-directory, and `name`
    is the friendly name shown in Chrome's profile menu.
    """
    root = chrome_user_data_root()
    if not root:
        return []
    state_path = root / "Local State"
    if not state_path.exists():
        return []
    try:
        with state_path.open("r", encoding="utf-8", errors="ignore") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    cache = (state.get("profile") or {}).get("info_cache") or {}
    out = []
    for dir_name, info in cache.items():
        out.append({
            "dir": dir_name,
            "name": info.get("name") or dir_name,
            "user": info.get("user_name") or "",
        })
    # Sort with "Default" first, then alphabetically.
    out.sort(key=lambda p: (p["dir"] != "Default", p["name"].lower()))
    return out


def find_chrome_executable() -> Path:
    """Locate the user's installed Google Chrome on Windows / macOS / Linux."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            Path(pf) / "Google/Chrome/Application/chrome.exe",
            Path(pf86) / "Google/Chrome/Application/chrome.exe",
            Path(local) / "Google/Chrome/Application/chrome.exe",
        ]
        # Authoritative path from the Windows registry, if present.
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                        candidates.append(Path(value))
                except OSError:
                    continue
        except ImportError:
            pass
    elif sys.platform == "darwin":
        candidates += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            from shutil import which
            p = which(name)
            if p:
                candidates.append(Path(p))

    for c in candidates:
        if c and c.exists():
            return c

    raise FileNotFoundError(
        "Google Chrome was not found on this PC. "
        "Install it from https://www.google.com/chrome/ and try again."
    )


def clone_chrome_profile_session(source_profile_dir: str, dest_user_data: Path) -> None:
    """Copy LinkedIn session files from a real Chrome profile into our private folder.

    This is what lets the user keep their real Chrome open while we scrape:
    we don't touch the real profile, we just copy out the cookies + the master
    encryption key (in `Local State`) so our private Chrome can decrypt them.
    Files copied:
      - Local State                       (DPAPI-wrapped key for cookie decryption)
      - <Profile>/Cookies                 (legacy cookies DB)
      - <Profile>/Network/Cookies         (current cookies DB, Chrome 96+)
      - <Profile>/Preferences             (some session bits live here)
    Login Data is intentionally NOT copied — we don't want saved passwords.
    """
    src_root = chrome_user_data_root()
    if not src_root:
        raise RuntimeError("Could not locate your Chrome User Data folder.")
    src_profile = src_root / source_profile_dir
    if not src_profile.exists():
        raise RuntimeError(
            f"Chrome profile {source_profile_dir!r} doesn't exist in {src_root}"
        )

    dest_user_data.mkdir(parents=True, exist_ok=True)
    dest_profile = dest_user_data / "Default"
    (dest_profile / "Network").mkdir(parents=True, exist_ok=True)

    pairs = [
        (src_root / "Local State", dest_user_data / "Local State"),
        (src_profile / "Cookies", dest_profile / "Cookies"),
        (src_profile / "Cookies-journal", dest_profile / "Cookies-journal"),
        (src_profile / "Network" / "Cookies", dest_profile / "Network" / "Cookies"),
        (src_profile / "Network" / "Cookies-journal", dest_profile / "Network" / "Cookies-journal"),
        (src_profile / "Preferences", dest_profile / "Preferences"),
    ]
    copied = 0
    last_err: Exception | None = None
    for src, dst in pairs:
        if not src.exists():
            continue
        try:
            shutil.copy2(src, dst)
            copied += 1
        except (OSError, PermissionError) as e:
            last_err = e
            LOGGER.warning("Skipped %s: %s", src.name, e)

    if copied == 0:
        raise RuntimeError(
            f"Could not copy any session files from profile {source_profile_dir!r}. "
            f"Last error: {last_err}"
        )
    LOGGER.info("Cloned %d session files from profile %r", copied, source_profile_dir)


def _wait_for_cdp(cdp_url: str, timeout: float = 20.0) -> None:
    """Block until Chrome's DevTools endpoint responds, or raise on timeout."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception as e:
            last_err = e
        time.sleep(0.4)
    raise RuntimeError(
        f"Chrome did not open its remote-debugging port in time. Last error: {last_err}"
    )


def launch_chrome_with_cdp(profile_dir: Path, port: int = 9222,
                           real_profile: str | None = None) -> subprocess.Popen:
    """Launch a private Chrome instance with a remote-debugging port.

    If `real_profile` is given (e.g. "Default", "Profile 1"), we first CLONE
    its cookies + master key into our dedicated `profile_dir` so the LinkedIn
    session is available — but we still launch our private user-data-dir.
    This means the user's real Chrome can stay open the entire time.
    """
    chrome = find_chrome_executable()
    profile_dir.mkdir(parents=True, exist_ok=True)

    if real_profile:
        # Refresh the cloned cookies each run so the session stays current.
        try:
            clone_chrome_profile_session(real_profile, profile_dir)
        except Exception as e:
            raise RuntimeError(
                f"Couldn't read your '{real_profile}' Chrome profile: {e}\n"
                f"If the file is locked, close ONLY that profile's Chrome window "
                f"and try again — other Chrome windows can stay open."
            )
        profile_arg = ["--profile-directory=Default"]
        LOGGER.info("Using cloned session from real profile %r", real_profile)
    else:
        profile_arg = []
        LOGGER.info("Using dedicated Chrome profile at %s", profile_dir)

    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        *profile_arg,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "https://www.linkedin.com/feed/",
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        _wait_for_cdp(f"http://127.0.0.1:{port}")
    except RuntimeError:
        try: proc.terminate()
        except Exception: pass
        raise RuntimeError(
            "Chrome did not start the remote-debugging port in time. "
            "Check that Google Chrome is installed and not blocked by antivirus."
        )
    return proc


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    required = ["email", "password", "search_url", "output_csv"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ValueError(f"Missing required config keys: {missing}")
    cfg.setdefault("headless", False)
    cfg.setdefault("min_delay_seconds", 2.0)
    cfg.setdefault("max_delay_seconds", 5.0)
    cfg.setdefault("user_data_dir", ".linkedin_session")
    cfg.setdefault("verified_only", False)
    return cfg


def jittered_sleep(min_s: float, max_s: float, thinking_chance: float = 0.12) -> None:
    """Sleep a randomized amount, with an occasional longer 'thinking' pause.

    Humans don't click at uniform intervals. ~12% of the time we add a long
    pause (15-35s) to simulate the user reading a profile, switching tabs,
    or just being distracted.
    """
    delay = random.uniform(min_s, max_s)
    if random.random() < thinking_chance:
        thinking = random.uniform(15.0, 35.0)
        LOGGER.debug("Thinking pause: %.1fs", thinking)
        delay += thinking
    LOGGER.debug("Sleeping %.2fs", delay)
    time.sleep(delay)


def random_mouse_jitter(page: Page) -> None:
    """Move the mouse to a random spot, sometimes in a small curve.

    Real users move the mouse between actions. Bots don't. A few small moves
    between page loads make the session look much more human in LinkedIn's
    behavioral telemetry.
    """
    try:
        viewport = page.viewport_size or {"width": 1366, "height": 850}
        for _ in range(random.randint(1, 3)):
            x = random.randint(50, viewport["width"] - 50)
            y = random.randint(80, viewport["height"] - 80)
            # steps > 1 makes Playwright interpolate the path instead of teleporting.
            page.mouse.move(x, y, steps=random.randint(8, 20))
            page.wait_for_timeout(random.randint(80, 260))
    except Exception:
        # Mouse moves are nice-to-have; never let them break the scrape.
        pass


def is_logged_in(page: Page) -> bool:
    # The global nav bar is only rendered when authenticated.
    try:
        page.wait_for_selector("nav.global-nav, a[href*='/feed']", timeout=5000)
        return True
    except PlaywrightTimeoutError:
        return False


# How long we'll wait for the user to finish logging in / solving captchas / 2FA.
# Set generous so slow connections and SMS-code waits don't time out.
MANUAL_LOGIN_TIMEOUT_SECONDS = 30 * 60   # 30 minutes
LOGIN_POLL_INTERVAL_SECONDS = 3


def _wait_for_manual_login(page: Page, timeout: float) -> bool:
    """Poll until LinkedIn's feed loads, or the timeout expires.

    Returns True if we detect a logged-in state, False on timeout.
    Robust against page navigations mid-poll (LinkedIn redirects a lot during
    captcha / 2FA flows).
    """
    deadline = time.time() + timeout
    remaining_announced = -1
    while time.time() < deadline:
        try:
            if is_logged_in(page):
                return True
        except Exception:
            # Page is mid-navigation — ignore and retry.
            pass
        remaining_min = int((deadline - time.time()) / 60)
        # Announce remaining time only when the minute changes, to avoid log spam.
        if remaining_min != remaining_announced and remaining_min % 5 == 0:
            LOGGER.info("Still waiting for sign-in (%d minutes left)...", remaining_min)
            remaining_announced = remaining_min
        time.sleep(LOGIN_POLL_INTERVAL_SECONDS)
    return False


def login(page: Page, email: str, password: str) -> None:
    """Log in to LinkedIn. Pauses up to 30 minutes for manual interventions.

    The Chrome window is left open the entire time so the user can solve
    captchas, type SMS / email codes, or sign in by hand without rush.
    """
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    if is_logged_in(page):
        LOGGER.info("Reusing existing session — already logged in.")
        return

    # If we have email+password, try to auto-fill the form. If anything goes
    # wrong (different field names, A/B test variant, etc.), we silently fall
    # back to letting the user sign in manually.
    if email and password:
        LOGGER.info("Attempting auto-login...")
        try:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            page.fill("input#username", email)
            page.fill("input#password", password)
            page.click("button[type=submit]")
            try:
                page.wait_for_url(
                    re.compile(r"linkedin\.com/(feed|checkpoint|in/)"),
                    timeout=30000,
                )
            except PlaywrightTimeoutError:
                pass
        except Exception as e:
            LOGGER.warning("Auto-login failed (%s) — falling back to manual.", e)

    # If we're still not in, wait patiently for the user to complete sign-in
    # in the open Chrome window. This covers captchas, 2FA, SMS codes,
    # password challenges — anything LinkedIn throws at us.
    if not is_logged_in(page):
        LOGGER.warning(
            "==============================================================\n"
            "  Sign-in needed. Please complete the login in the Chrome\n"
            "  window that just opened. The scraper will wait up to %d\n"
            "  minutes — take your time. DO NOT close the Chrome window.\n"
            "==============================================================",
            MANUAL_LOGIN_TIMEOUT_SECONDS // 60,
        )
        if not _wait_for_manual_login(page, MANUAL_LOGIN_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"Login timeout after {MANUAL_LOGIN_TIMEOUT_SECONDS // 60} minutes. "
                "Re-run the scrape when you're ready."
            )
    LOGGER.info("Logged in successfully.")


def build_page_url(search_url: str, page_number: int) -> str:
    parsed = urlparse(search_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page_number)
    return urlunparse(parsed._replace(query=urlencode(query)))


def scroll_to_load_results(page: Page) -> None:
    """Human-ish scroll: varied distances, occasional reverse, irregular pauses.

    A uniform wheel-down loop is a strong bot signal. Real users scroll in
    bursts, sometimes overshoot and scroll back up to re-read something, and
    pause at uneven intervals. We mimic that here while still ensuring every
    card lazy-loads.
    """
    last_height = 0
    stable_passes = 0
    for i in range(50):
        # Most of the time scroll down by a varied amount.
        # ~12% of the time scroll up a little (re-reading behavior).
        if random.random() < 0.12 and i > 2:
            delta = -random.randint(200, 600)
        else:
            delta = random.randint(500, 1400)
        page.mouse.wheel(0, delta)

        # Irregular pause — short most of the time, occasionally longer.
        if random.random() < 0.15:
            page.wait_for_timeout(random.randint(900, 1800))
        else:
            page.wait_for_timeout(random.randint(280, 700))

        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            stable_passes += 1
            if stable_passes >= 4:
                break
        else:
            stable_passes = 0
            last_height = height

    # Drift back to the top in 2-3 steps (not a teleport) so the verification
    # badge re-mounts and the next page-nav feels natural.
    for _ in range(random.randint(2, 3)):
        page.mouse.wheel(0, -random.randint(1500, 3500))
        page.wait_for_timeout(random.randint(150, 350))
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_timeout(random.randint(250, 500))


# Robust extraction: LinkedIn rotates CSS class names to break scrapers, so we
# don't rely on container class names. We find every /in/ profile link, walk
# up to a card-like ancestor, then read name/headline/location from the card's
# visible text using positional heuristics that survive markup churn.
EXTRACT_CARDS_JS = r"""
() => {
  const verifiedSignal = (root) => {
    if (!root || !root.querySelector) return false;
    if (root.querySelector('[data-test-icon*="verified" i]') ||
        root.querySelector('svg[aria-label*="verified" i]') ||
        root.querySelector('[aria-label*="verification" i]') ||
        root.querySelector('use[href*="verified" i]') ||
        root.querySelector('li-icon[type*="verified" i]')) return true;
    for (const u of root.querySelectorAll('use')) {
      const h = u.getAttribute('xlink:href') || u.getAttribute('href') || '';
      if (h.toLowerCase().includes('verified')) return true;
    }
    return false;
  };

  // Strip out LinkedIn's UI noise around the actual profile content.
  const NOISE = new Set([
    'Connect', 'Message', 'Follow', 'Pending', 'View profile', 'More',
    'Status is online', 'Status is offline', 'Status is reachable',
    'Visible to anyone on or off LinkedIn',
    '1st', '2nd', '3rd', '3rd+', '· 1st', '· 2nd', '· 3rd', '· 3rd+',
  ]);
  const cleanLines = (txt) => (txt || '')
    .split('\n')
    .map(s => s.trim())
    .filter(s => s && !NOISE.has(s) && !/^· (1st|2nd|3rd\+?)$/i.test(s))
    // Deduplicate consecutive identical lines (name often appears twice).
    .filter((s, i, arr) => i === 0 || s !== arr[i - 1]);

  const mainCandidates = [
    document.querySelector('main'),
    document.querySelector('div.search-results-container'),
    document.querySelector('[role="main"]'),
  ].filter(Boolean);
  const scope = mainCandidates[0] || document;

  const anchors = Array.from(scope.querySelectorAll('a[href*="/in/"]'));
  const byUrl = new Map();

  for (const a of anchors) {
    const href = a.href || '';
    if (!href.includes('/in/')) continue;
    const card = a.closest(
      'li, article, ' +
      'div[data-chameleon-result-urn], ' +
      'div.entity-result, ' +
      'div.reusable-search__result-container'
    ) || a.parentElement;
    if (!card) continue;

    const verified = verifiedSignal(card);
    const lines = cleanLines(card.innerText);

    // Best-effort name: the visible text inside the /in/ link, falling back
    // to the first line of the card.
    let name = (a.innerText || '').trim().split('\n')[0] || '';
    if (!name && lines.length) name = lines[0];
    // Remove " · 2nd" / connection-degree suffix that sometimes follows the name.
    name = name.replace(/\s*[·•]\s*(1st|2nd|3rd\+?).*$/i, '').trim();

    // After the name line, the next non-noise lines are usually headline,
    // then location, then "Current: ..." etc.
    const idxName = lines.findIndex(l => l === name);
    const after = idxName >= 0 ? lines.slice(idxName + 1) : lines;
    const headline = after[0] || '';
    const location = after[1] || '';

    const payload = { url: href, verified, name, headline, location };
    if (!byUrl.has(href)) {
      byUrl.set(href, payload);
    } else {
      // Keep the richer record (prefer one with headline filled).
      const existing = byUrl.get(href);
      if (!existing.headline && payload.headline) byUrl.set(href, payload);
      if (payload.verified) existing.verified = true;
    }
  }
  return Array.from(byUrl.values());
}
"""


def detect_search_limit(page: Page) -> str | None:
    """Return a short message if LinkedIn is showing a search-limit warning."""
    try:
        text = (page.evaluate("document.body.innerText || ''") or "").lower()
    except Exception:
        return None
    markers = [
        "you've reached the monthly limit",
        "commercial use limit",
        "search limit",
        "try again next month",
    ]
    for m in markers:
        if m in text:
            return m
    return None


def extract_profile_cards(page: Page) -> list[dict]:
    """Return [{url, verified, name, headline, location}] for each result card."""
    raw = page.evaluate(EXTRACT_CARDS_JS)
    seen: set[str] = set()
    cards: list[dict] = []
    for item in raw or []:
        href = item.get("url") or ""
        match = PROFILE_HREF_RE.match(href)
        if not match:
            continue
        clean = match.group(0).split("?")[0].split("#")[0].rstrip("/")
        if clean in seen:
            continue
        seen.add(clean)
        cards.append({
            "url": clean,
            "verified": bool(item.get("verified")),
            "name": (item.get("name") or "").strip(),
            "headline": (item.get("headline") or "").strip(),
            "location": (item.get("location") or "").strip(),
        })
    return cards


def click_next_page(page: Page) -> bool:
    """Click LinkedIn's 'Next' pagination button. Returns False if no enabled Next.

    Using the Next button (instead of `?page=N` URL navigation) is more reliable
    because LinkedIn sometimes hides the page-count UI on later pages, which
    breaks `detect_total_pages`. Next stays clickable until truly the last page.
    """
    selectors = [
        'button[aria-label="Next"]:not([disabled])',
        'button.artdeco-pagination__button--next:not([disabled])',
        'button[aria-label*="Next" i]:not([disabled])',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            btn.scroll_into_view_if_needed(timeout=2000)
            btn.click(timeout=3000)
            # Wait for results to start loading — either URL changes or the
            # spinner appears. domcontentloaded fires fast on SPA navigation.
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(random.randint(600, 1200))
            return True
        except Exception:
            continue
    return False


def detect_total_pages(page: Page) -> int | None:
    """Read LinkedIn's pagination control to find the last page number.

    Returns the highest page index it can find, or None if no pagination
    is rendered (which usually means there's only one page of results).
    LinkedIn caps people-search at 100 pages even when there are more results.
    """
    js = r"""
    () => {
      const candidates = [];
      // Page indicator buttons (e.g. <button aria-label="Page 7">7</button>)
      document.querySelectorAll('button[aria-label^="Page "]').forEach(b => {
        const m = b.getAttribute('aria-label').match(/Page\s+(\d+)/i);
        if (m) candidates.push(parseInt(m[1], 10));
      });
      // Numbered pagination list items
      document.querySelectorAll('li.artdeco-pagination__indicator button').forEach(b => {
        const n = parseInt((b.textContent || '').trim(), 10);
        if (!isNaN(n)) candidates.push(n);
      });
      // Generic "page N of M" text
      const txt = document.body.innerText || '';
      const m = txt.match(/Page\s+\d+\s+of\s+(\d+)/i);
      if (m) candidates.push(parseInt(m[1], 10));
      return candidates.length ? Math.max(...candidates) : null;
    }
    """
    try:
        result = page.evaluate(js)
        if isinstance(result, (int, float)) and result > 0:
            return min(int(result), 100)  # LinkedIn's own hard cap
    except Exception:
        pass
    return None


DEFAULT_MAX_PAGES = 15     # if the caller doesn't set max_pages, use this
ABSOLUTE_MAX_PAGES = 100   # LinkedIn's own ceiling, never go past this


def scrape_search(page: Page, search_url: str,
                  min_delay: float, max_delay: float,
                  verified_only: bool = False,
                  on_progress=None,
                  max_pages: int = DEFAULT_MAX_PAGES) -> list[dict]:
    # Clamp to sane bounds.
    max_pages = max(1, min(int(max_pages or DEFAULT_MAX_PAGES), ABSOLUTE_MAX_PAGES))
    # Target at least max_pages before stopping early on missing-Next.
    MIN_TARGET_PAGES = max_pages
    HARD_MAX_PAGES = max_pages
    collected: list[dict] = []
    seen: set[str] = set()
    total_pages: int | None = None

    # Initial navigation only — after this we drive with the Next button.
    LOGGER.info("Loading search page 1: %s", search_url)
    try:
        page.goto(build_page_url(search_url, 1), wait_until="domcontentloaded", timeout=45000)
    except PlaywrightTimeoutError:
        LOGGER.error("Initial search page didn't load. Aborting.")
        return collected

    page_num = 1
    while page_num <= HARD_MAX_PAGES:
        LOGGER.info("Processing page %d%s", page_num,
                    f" of ~{total_pages}" if total_pages else "")
        scroll_to_load_results(page)

        if total_pages is None:
            total_pages = detect_total_pages(page)
            if total_pages:
                LOGGER.info("Detected at least %d total result pages.", total_pages)
            else:
                LOGGER.info("Page count not visible — will navigate via Next button.")

        cards = extract_profile_cards(page)
        if verified_only:
            kept = [c for c in cards if c["verified"]]
            LOGGER.info("Page %d: %d cards, %d verified.", page_num, len(cards), len(kept))
        else:
            kept = cards
            LOGGER.info("Page %d: %d cards.", page_num, len(cards))

        new_cards = [c for c in kept if c["url"] not in seen]
        if not cards:
            limit_msg = detect_search_limit(page)
            if limit_msg:
                LOGGER.warning(
                    "LinkedIn search limit hit on page %d (%r). "
                    "Free accounts have a monthly commercial-use cap; "
                    "wait it out or use a Premium/Sales Navigator account.",
                    page_num, limit_msg,
                )
            else:
                LOGGER.info("No cards found on page %d — assuming end of results.", page_num)
            break
        if not new_cards and not verified_only:
            LOGGER.info("No new URLs on page %d — assuming end of results.", page_num)
            break

        for c in new_cards:
            seen.add(c["url"])
            collected.append(c)

        if on_progress:
            try:
                on_progress(page_num, total_pages, len(collected))
            except Exception:
                LOGGER.exception("on_progress callback failed")

        # Behavioral pause + jitter between pages.
        random_mouse_jitter(page)
        if page_num > 1 and page_num % 10 == 0:
            idle = random.uniform(45.0, 120.0)
            LOGGER.info("Long idle break after %d pages: %.1fs", page_num - 1, idle)
            time.sleep(idle)
        else:
            jittered_sleep(min_delay, max_delay)

        # ---- Advance to the next page ----
        next_num = page_num + 1
        advanced = False

        # Primary: click LinkedIn's Next button.
        if click_next_page(page):
            advanced = True
        else:
            # Fallback: try the URL ?page=N param. LinkedIn sometimes hides the
            # Next button on later pages even when more results exist.
            LOGGER.info("Next button unavailable; falling back to URL nav.")
            try:
                page.goto(build_page_url(search_url, next_num),
                          wait_until="domcontentloaded", timeout=30000)
                advanced = True
            except PlaywrightTimeoutError:
                advanced = False

        if not advanced:
            if page_num < MIN_TARGET_PAGES:
                LOGGER.warning(
                    "Couldn't reach page %d (target minimum is %d). "
                    "LinkedIn may have truly ended results, or the Commercial-Use "
                    "limit was hit.", next_num, MIN_TARGET_PAGES,
                )
            else:
                LOGGER.info("No further pages available after page %d.", page_num)
            break

        page_num = next_num

    return collected


def save_to_csv(profiles: list, output_path: Path) -> None:
    """Write profiles to CSV. Accepts list[dict] (preferred) or list[str] for
    backward compatibility."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "profile_url", "name", "headline", "location", "verified",
        "ai_match", "ai_confidence",
        "ai_seniority", "currently_in_role",
        "red_flags", "signals", "ai_reason",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for p in profiles:
            if isinstance(p, str):
                writer.writerow([p] + [""] * (len(cols) - 1))
                continue
            writer.writerow([
                p.get("url", ""),
                p.get("name", ""),
                p.get("headline", ""),
                p.get("location", ""),
                "1" if p.get("verified") else "0",
                p.get("ai_match", ""),
                p.get("ai_confidence", ""),
                p.get("ai_seniority", ""),
                p.get("ai_currently_in_role", ""),
                " | ".join(p.get("ai_red_flags") or []) if isinstance(p.get("ai_red_flags"), list) else (p.get("ai_red_flags") or ""),
                " | ".join(p.get("ai_signals") or []) if isinstance(p.get("ai_signals"), list) else (p.get("ai_signals") or ""),
                p.get("ai_reason", ""),
            ])
    LOGGER.info("Wrote %d profiles to %s", len(profiles), output_path)


def scrape_profiles(cfg: dict, on_progress=None) -> list[dict]:
    """Run the full scrape using a config dict. Returns the list of profile dicts.

    Required keys: email, password, search_url.
    Optional keys: headless, min_delay_seconds, max_delay_seconds, user_data_dir,
                   verified_only.
    `on_progress(page_num, total_pages, count)` is called after each page if provided.
    """
    cfg.setdefault("min_delay_seconds", 5.0)
    cfg.setdefault("max_delay_seconds", 12.0)
    cfg.setdefault("user_data_dir", ".chrome_profile")
    cfg.setdefault("verified_only", False)
    cfg.setdefault("cdp_port", 9222)
    cfg.setdefault("chrome_profile", None)  # name like "Default" or "Profile 1"
    cfg.setdefault("max_pages", DEFAULT_MAX_PAGES)

    profile_dir = Path(cfg["user_data_dir"]).resolve()
    chrome_proc = launch_chrome_with_cdp(
        profile_dir, port=cfg["cdp_port"], real_profile=cfg["chrome_profile"],
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cfg['cdp_port']}")
            # The user's Chrome already has a default context; reuse it so cookies
            # / login state from previous runs are available.
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            if cfg["chrome_profile"]:
                # Real-profile mode: cloned cookies SHOULD log us in instantly.
                # If they're stale (logged out / re-login required), give the
                # user the same patient manual-sign-in window as the other path.
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                if is_logged_in(page):
                    LOGGER.info("Using existing LinkedIn session from Chrome profile %r",
                                cfg["chrome_profile"])
                else:
                    LOGGER.warning(
                        "Cloned cookies from profile %r aren't signed in to LinkedIn.",
                        cfg["chrome_profile"],
                    )
                    # Empty creds -> login() skips auto-fill and waits manually.
                    login(page, "", "")
            else:
                login(page, cfg["email"], cfg["password"])

            jittered_sleep(cfg["min_delay_seconds"], cfg["max_delay_seconds"])
            return scrape_search(
                page,
                cfg["search_url"],
                cfg["min_delay_seconds"],
                cfg["max_delay_seconds"],
                verified_only=cfg["verified_only"],
                on_progress=on_progress,
                max_pages=cfg["max_pages"],
            )
    finally:
        # Close Chrome cleanly so the profile dir releases its lock.
        try:
            chrome_proc.terminate()
            chrome_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            chrome_proc.kill()
        except Exception:
            pass


def run(config_path: Path) -> int:
    cfg = load_config(config_path)
    try:
        urls = scrape_profiles(cfg)
        save_to_csv(urls, Path(cfg["output_csv"]))
    except Exception:
        LOGGER.exception("Scraping failed.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LinkedIn profile URL scraper.")
    parser.add_argument("--config", "-c", default="config.json",
                        help="Path to JSON config file (default: config.json)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    return run(Path(args.config))


if __name__ == "__main__":
    sys.exit(main())
