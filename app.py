"""Tiny Flask UI for the LinkedIn scraper.

Run:  python app.py
Open: http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_file

import ai
import database as db
from scraper import list_chrome_profiles, save_to_csv, scrape_profiles

app = Flask(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
LOGGER = logging.getLogger("linkedin_ui")

CONFIG_PATH = Path("config.json")
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# Spin up the SQLite history database next to the working dir.
db.init_db()


def make_csv_name(label: str | None) -> str:
    """Produce a unique, filesystem-safe CSV name from an optional user label.

    Format: {sanitized_label}_{YYYY-MM-DD_HH-MM-SS}.csv
    If no label: profiles_{timestamp}.csv
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (label or "").strip()).strip("_")[:40]
    return f"{safe}_{ts}.csv" if safe else f"profiles_{ts}.csv"

# Track a single in-flight job so the UI can poll for status.
JOB_LOCK = threading.Lock()
JOB_STATE: dict = {
    "status": "idle",
    "urls": [],
    "error": None,
    "csv_path": None,
    "current_page": 0,
    "total_pages": None,
}


def load_ai_settings() -> dict:
    """Read AI section of config.json. Returns defaults if not configured."""
    if not CONFIG_PATH.exists():
        return {"api_key": "", "base_url": ai.DEFAULT_BASE_URL, "model": ai.DEFAULT_MODEL}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except json.JSONDecodeError:
        cfg = {}
    a = cfg.get("ai") or {}
    return {
        "api_key": a.get("api_key", "") or "",
        "base_url": a.get("base_url") or ai.DEFAULT_BASE_URL,
        "model": a.get("model") or ai.DEFAULT_MODEL,
    }


def save_ai_settings(settings: dict) -> None:
    existing = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except json.JSONDecodeError:
            existing = {}
    existing["ai"] = {
        "api_key": settings.get("api_key", ""),
        "base_url": settings.get("base_url") or ai.DEFAULT_BASE_URL,
        "model": settings.get("model") or ai.DEFAULT_MODEL,
    }
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)


def load_credentials() -> tuple[str, str]:
    """Read email/password from config.json so they aren't typed into the browser."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            "config.json not found. Copy config.example.json to config.json "
            "and fill in your LinkedIn email and password."
        )
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("email") or not cfg.get("password"):
        raise ValueError("config.json must contain 'email' and 'password'.")
    return cfg["email"], cfg["password"]


def validate_search_url(form: dict) -> str:
    raw = (form.get("raw_url") or "").strip()
    if not raw:
        raise ValueError("A LinkedIn search URL is required.")
    if "linkedin.com/search/results/people" not in raw:
        raise ValueError("URL must be a LinkedIn people-search URL "
                         "(linkedin.com/search/results/people/...).")
    return raw


def _make_progress(run_id: int):
    def cb(page_num: int, total: int | None, count: int) -> None:
        with JOB_LOCK:
            JOB_STATE["current_page"] = page_num
            JOB_STATE["total_pages"] = total
            JOB_STATE["count"] = count
            JOB_STATE["run_id"] = run_id
        db.update_run_progress(run_id, total_pages=total, profile_count=count)
    return cb


def _run_ai_verification(profiles: list[dict], profile_ids: list[int],
                         target_role: str, ai_cfg: dict) -> list[dict]:
    """Run AI role-verification on each profile, update DB, attach result to dict.
    Live progress is mirrored into JOB_STATE so the UI can show it."""
    if not target_role or not ai_cfg.get("api_key"):
        return profiles

    def on_ai_progress(done: int, total: int):
        with JOB_LOCK:
            JOB_STATE["ai_done"] = done
            JOB_STATE["ai_total"] = total
            JOB_STATE["status_text"] = f"AI verifying ({done}/{total})..."

    with JOB_LOCK:
        JOB_STATE["status_text"] = "Starting AI verification..."
        JOB_STATE["ai_done"] = 0
        JOB_STATE["ai_total"] = len(profiles)

    try:
        results = ai.verify_role_batch(
            profiles, target_role,
            api_key=ai_cfg["api_key"],
            base_url=ai_cfg["base_url"],
            model=ai_cfg["model"],
            on_progress=on_ai_progress,
        )
    except Exception as e:
        LOGGER.exception("AI verification failed")
        with JOB_LOCK:
            JOB_STATE["ai_error"] = str(e)
        return profiles

    for i, res in enumerate(results):
        if i < len(profile_ids):
            db.update_profile_ai(profile_ids[i], res)
        # Mirror every AI field onto the in-memory profile so the post-scrape
        # CSV and the UI's live results show the full analysis.
        profiles[i]["ai_match"] = res.get("match")
        profiles[i]["ai_confidence"] = res.get("confidence")
        profiles[i]["ai_reason"] = res.get("reason")
        profiles[i]["ai_seniority"] = res.get("seniority")
        profiles[i]["ai_currently_in_role"] = res.get("currently_in_role")
        profiles[i]["ai_red_flags"] = res.get("red_flags") or []
        profiles[i]["ai_signals"] = res.get("signals") or []
    return profiles


def run_job(cfg: dict, csv_path: Path, run_id: int,
            target_role: str, ai_cfg: dict) -> None:
    """Scrape -> persist -> (optional) AI verify -> CSV -> finish."""
    global JOB_STATE
    csv_filename = csv_path.name
    try:
        profiles = scrape_profiles(cfg, on_progress=_make_progress(run_id))
        profile_ids = db.add_profiles(run_id, profiles)

        if target_role and ai_cfg.get("api_key"):
            profiles = _run_ai_verification(profiles, profile_ids, target_role, ai_cfg)
            # Sort: yes -> maybe -> error -> no -> unknown.
            # Within a bucket, prefer "currently_in_role=yes", then highest confidence.
            _match_order = {"yes": 0, "maybe": 1, "error": 2, "no": 3}
            _curr_order  = {"yes": 0, "maybe": 1, "unknown": 2, "no": 3}
            profiles.sort(key=lambda p: (
                _match_order.get((p.get("ai_match") or "").lower(), 4),
                _curr_order.get((p.get("ai_currently_in_role") or "").lower(), 4),
                -(p.get("ai_confidence") or 0),
            ))

        save_to_csv(profiles, csv_path)
        db.finish_run(run_id, status="done", csv_filename=csv_filename,
                      profile_count=len(profiles))
        with JOB_LOCK:
            JOB_STATE.update({
                "status": "done", "urls": [p["url"] for p in profiles],
                "profiles": profiles, "error": None,
                "csv_path": str(csv_path), "count": len(profiles),
            })
    except Exception as e:
        LOGGER.exception("Scrape job failed")
        db.finish_run(run_id, status="error", error=str(e))
        with JOB_LOCK:
            JOB_STATE.update({"status": "error", "urls": [], "error": str(e),
                              "csv_path": None})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ai-settings", methods=["GET", "POST"])
def ai_settings():
    if request.method == "GET":
        s = load_ai_settings()
        # Don't echo the full key back to the browser; just say if it's set.
        return jsonify({
            "ok": True,
            "configured": bool(s.get("api_key")),
            "base_url": s.get("base_url"),
            "model": s.get("model"),
            "default_base_url": ai.DEFAULT_BASE_URL,
            "default_model": ai.DEFAULT_MODEL,
            "suggested_models": [
                "meta/llama-3.1-8b-instruct",
                "meta/llama-3.3-70b-instruct",
                "mistralai/mistral-7b-instruct-v0.3",
                "nvidia/llama-3.1-nemotron-70b-instruct",
            ],
        })
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    base_url = (body.get("base_url") or "").strip() or ai.DEFAULT_BASE_URL
    model = (body.get("model") or "").strip() or ai.DEFAULT_MODEL
    if not api_key:
        return jsonify({"ok": False, "error": "API key is required."}), 400
    save_ai_settings({"api_key": api_key, "base_url": base_url, "model": model})
    return jsonify({"ok": True})


@app.route("/ai-test", methods=["POST"])
def ai_test():
    """Smoke-test the saved API settings by sending a tiny chat completion."""
    s = load_ai_settings()
    if not s.get("api_key"):
        return jsonify({"ok": False, "error": "No API key saved yet."}), 400
    ok, msg = ai.test_connection(s["api_key"], s["base_url"], s["model"])
    return jsonify({"ok": ok, "message": msg})


@app.route("/chrome-profiles")
def chrome_profiles():
    """Return a list of installed Chrome profiles so the UI can show a picker."""
    try:
        return jsonify({"ok": True, "profiles": list_chrome_profiles()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "profiles": []})


@app.route("/chrome-running")
def chrome_running():
    """Report whether any chrome.exe processes are alive."""
    import subprocess
    if not sys_is_windows():
        return jsonify({"ok": True, "running": False, "count": 0})
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        lines = [l for l in (out.stdout or "").splitlines() if l.strip()
                 and "chrome.exe" in l.lower()]
        return jsonify({"ok": True, "running": len(lines) > 0, "count": len(lines)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "running": False, "count": 0})


@app.route("/chrome-kill", methods=["POST"])
def chrome_kill():
    """Force-close all chrome.exe processes (and background helpers)."""
    import subprocess
    if not sys_is_windows():
        return jsonify({"ok": False, "error": "Only supported on Windows."}), 400
    try:
        # /F = force, /T = also kill child processes (helpers, tray icons, extensions).
        subprocess.run(["taskkill", "/F", "/T", "/IM", "chrome.exe"],
                       capture_output=True, text=True, timeout=10)
        # Brief pause so the OS releases the user-data lock.
        import time as _t
        _t.sleep(0.6)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def sys_is_windows() -> bool:
    import sys as _s
    return _s.platform == "win32"


@app.route("/credentials", methods=["GET", "POST"])
def credentials():
    """Read or write LinkedIn credentials. Lets the distributed .exe ask
    the user for their login from the web UI on first run."""
    if request.method == "GET":
        try:
            email, _ = load_credentials()
            return jsonify({"ok": True, "configured": True, "email": email})
        except (FileNotFoundError, ValueError):
            return jsonify({"ok": True, "configured": False, "email": ""})
    # POST
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password required."}), 400
    existing = {}
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except json.JSONDecodeError:
            existing = {}
    existing["email"] = email
    existing["password"] = password
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    return jsonify({"ok": True})


@app.route("/scrape", methods=["POST"])
def start_scrape():
    global JOB_STATE
    with JOB_LOCK:
        if JOB_STATE["status"] == "running":
            return jsonify({"ok": False, "error": "A scrape is already running."}), 409

    form = request.get_json(silent=True) or {}
    try:
        search_url = validate_search_url(form)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    label = (form.get("label") or "").strip()
    verified_only = bool(form.get("verified_only"))
    chrome_profile = (form.get("chrome_profile") or "").strip() or None
    target_role = (form.get("target_role") or "").strip()
    try:
        max_pages = int(form.get("max_pages") or 15)
    except (TypeError, ValueError):
        max_pages = 15
    max_pages = max(1, min(max_pages, 100))

    # Credentials are only required in dedicated-profile mode.
    email, password = "", ""
    if not chrome_profile:
        try:
            email, password = load_credentials()
        except (FileNotFoundError, ValueError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    ai_cfg = load_ai_settings()
    if target_role and not ai_cfg.get("api_key"):
        return jsonify({"ok": False,
                        "error": "Target role set, but no AI API key saved. "
                                 "Open AI Settings and add your key first."}), 400

    csv_path = OUTPUT_DIR / make_csv_name(label)
    run_id = db.create_run(label or None, search_url, verified_only,
                           target_role=target_role or None)

    cfg = {
        "email": email,
        "password": password,
        "search_url": search_url,
        "headless": False,
        "min_delay_seconds": 5.0,
        "max_delay_seconds": 12.0,
        "user_data_dir": ".chrome_profile",
        "verified_only": verified_only,
        "chrome_profile": chrome_profile,
        "max_pages": max_pages,
    }

    with JOB_LOCK:
        JOB_STATE = {
            "status": "running", "urls": [], "error": None, "csv_path": None,
            "current_page": 0, "total_pages": None, "count": 0,
            "run_id": run_id, "target_role": target_role or None,
            "ai_done": 0, "ai_total": 0,
        }

    threading.Thread(
        target=run_job, args=(cfg, csv_path, run_id, target_role, ai_cfg),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "search_url": search_url,
                    "run_id": run_id, "csv_filename": csv_path.name})


@app.route("/status")
def status():
    with JOB_LOCK:
        return jsonify(JOB_STATE)


@app.route("/download")
def download():
    with JOB_LOCK:
        path = JOB_STATE.get("csv_path")
    if not path or not Path(path).exists():
        return "No CSV available yet.", 404
    return send_file(Path(path).resolve(), as_attachment=True)


# ---------------- History API ----------------

@app.route("/history")
def history_list():
    return jsonify({"ok": True, "runs": db.list_runs()})


@app.route("/history/<int:run_id>")
def history_get(run_id: int):
    run = db.get_run(run_id)
    if not run:
        return jsonify({"ok": False, "error": "Run not found"}), 404
    profiles = db.get_run_profiles(run_id)
    return jsonify({"ok": True, "run": run, "profiles": profiles})


@app.route("/history/<int:run_id>/download")
def history_download(run_id: int):
    run = db.get_run(run_id)
    if not run:
        return "Run not found.", 404
    fname = run.get("csv_filename")
    if fname:
        p = OUTPUT_DIR / fname
        if p.exists():
            return send_file(p.resolve(), as_attachment=True)
    # CSV file is missing — regenerate on the fly from the DB rows.
    import csv as _csv, io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow([
        "profile_url", "name", "headline", "location", "verified",
        "ai_match", "ai_confidence", "ai_seniority", "currently_in_role",
        "red_flags", "signals", "ai_reason",
    ])
    for p in db.get_run_profiles(run_id):
        w.writerow([
            p.get("url", ""), p.get("name") or "", p.get("headline") or "",
            p.get("location") or "", "1" if p.get("verified") else "0",
            p.get("ai_match") or "", p.get("ai_confidence") or "",
            p.get("ai_seniority") or "",
            p.get("ai_currently_in_role") or "",
            " | ".join(p.get("ai_red_flags") or []),
            " | ".join(p.get("ai_signals") or []),
            p.get("ai_reason") or "",
        ])
    from flask import Response
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="run_{run_id}.csv"'},
    )


@app.route("/history/<int:run_id>", methods=["DELETE"])
def history_delete(run_id: int):
    run = db.get_run(run_id)
    if not run:
        return jsonify({"ok": False, "error": "Run not found"}), 404
    # Best-effort delete of the CSV file too.
    fname = run.get("csv_filename")
    if fname:
        p = OUTPUT_DIR / fname
        try:
            p.unlink(missing_ok=True)
        except Exception:
            LOGGER.exception("Failed to delete CSV %s", p)
    db.delete_run(run_id)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
