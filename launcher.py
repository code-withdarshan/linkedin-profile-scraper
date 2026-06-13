"""Standalone launcher used when the app is packaged as a Windows .exe.

Responsibilities:
- Resolve bundled resource paths (templates) when running under PyInstaller.
- Verify Google Chrome is installed (required for CDP-based scraping).
- Start the Flask server on a free port.
- Open the user's default browser to the UI.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def resource_path(relative: str) -> str:
    """Resolve a path that works in dev AND inside a PyInstaller --onefile bundle."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


def app_data_dir() -> Path:
    """Per-user writable folder for config.json, session cookies, output CSVs.

    When frozen, we can't write next to the .exe (Program Files is read-only on
    standard installs). %LOCALAPPDATA%\\LinkedInScraper is the right place.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    d = base / "LinkedInScraper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_free_port(default: int = 5000) -> int:
    """Try the default port; if taken, ask the OS for any free one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", default))
            return default
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def verify_chrome_installed() -> None:
    """Make sure Google Chrome is on this PC before we even start the UI."""
    from scraper import find_chrome_executable
    try:
        chrome = find_chrome_executable()
        print(f"Using Chrome: {chrome}")
    except FileNotFoundError as e:
        print()
        print("=" * 60)
        print("  Google Chrome is required but was not found.")
        print("=" * 60)
        print("  Install Chrome from:  https://www.google.com/chrome/")
        print("  Then re-launch this app.")
        print()
        input("  Press Enter to close...")
        sys.exit(1)


def main() -> None:
    # Safety net: if this .exe ever gets spawned as a child of itself
    # (e.g. by a misbehaving subprocess call), refuse to run a second time.
    if os.environ.get("LINKEDIN_SCRAPER_CHILD") == "1":
        print("Refusing to re-launch self. Exiting.")
        sys.exit(0)
    os.environ["LINKEDIN_SCRAPER_CHILD"] = "1"

    # Make sure imports inside app.py can find templates and the working dir.
    os.chdir(app_data_dir())  # outputs/, config.json, .chrome_profile land here

    verify_chrome_installed()

    # Import lazily so the heavy stuff isn't pulled in before the browser check.
    from flask import Flask  # noqa: F401  (just to fail fast if missing)
    import app as flask_app

    # Override Flask's template folder to point at the bundled resources.
    flask_app.app.template_folder = resource_path("templates")
    flask_app.app.jinja_loader.searchpath = [resource_path("templates")]

    port = find_free_port(5000)
    url = f"http://127.0.0.1:{port}"

    def open_browser():
        # Small delay so Flask has time to start listening.
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    print("=" * 60)
    print("  LinkedIn Profile Scraper")
    print("=" * 60)
    print(f"  Web UI:      {url}")
    print(f"  Data folder: {app_data_dir()}")
    print()
    print("  Your browser should open automatically. If it doesn't,")
    print(f"  copy this URL and paste it into your browser: {url}")
    print()
    print("  KEEP THIS WINDOW OPEN. Closing it stops the app.")
    print("=" * 60)

    try:
        flask_app.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
