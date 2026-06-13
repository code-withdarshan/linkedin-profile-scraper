"""LinkedIn lead AI verification.

Uses any OpenAI-compatible endpoint (NVIDIA Build, OpenAI, local Ollama).
Default base URL points at NVIDIA Build's `integrate.api.nvidia.com/v1`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

LOGGER = logging.getLogger("linkedin_ai")

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "meta/llama-3.1-8b-instruct"

PROMPT_TEMPLATE = """You analyze a LinkedIn profile against a target role.

TARGET ROLE: {role}

PROFILE:
- Name: {name}
- Headline: {headline}
- Location: {location}

Return ONLY a single JSON object with these fields:

{{
  "match": "yes" | "no" | "maybe",
  "confidence": <integer 0-100>,
  "seniority": "junior" | "mid" | "senior" | "executive" | "unknown",
  "currently_in_role": "yes" | "no" | "maybe",
  "red_flags": ["<short tag>", ...],
  "signals":   ["<short tag>", ...],
  "reason": "<one short sentence>"
}}

GUIDANCE:
- match: does the headline indicate the target role? yes/no/maybe.
- confidence: 0-100, how confident you are in the match decision.
- seniority: infer from title prefix (Sr/Senior/Lead/Head/Director/VP/Chief) AND from
  explicit experience cues. Use "executive" for VP/C-level/Founder, "senior" for Sr/Lead/Head,
  "mid" for plain titles, "junior" for Jr/Intern/Associate/Entry-level. "unknown" if unclear.
- currently_in_role: "yes" if their CURRENT primary role matches the target;
  "no" if they were the target role in the past but moved on (ex-BDE now PM);
  "maybe" if ambiguous.
- red_flags: short tags for concerns. Examples: "recruiter language",
  "multiple roles in headline", "vague title", "MBA student", "open to anything",
  "agency / consultant", "founder of many things".
- signals: short positive tags. Examples: "open to opportunities", "actively building",
  "recently joined", "decision maker", "specialist in target area".
- reason: ONE short sentence justifying the match decision.

Output ONLY the JSON. No prose, no markdown fences.
"""

# Possible canonical values we accept for each enum field.
_VALID = {
    "match": {"yes", "no", "maybe"},
    "seniority": {"junior", "mid", "senior", "executive", "unknown"},
    "currently_in_role": {"yes", "no", "maybe"},
}


def _parse_response(text: str) -> dict:
    """Pull the first JSON object out of a model reply. Models sometimes wrap
    output in prose or code fences, so we hunt for the first `{...}` block."""
    text = (text or "").strip()
    # Strip common ``` fences.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _coerce_enum(value, field: str, fallback: str = "unknown") -> str:
    v = (str(value or "")).lower().strip()
    return v if v in _VALID.get(field, set()) else fallback


def _coerce_taglist(value) -> list[str]:
    """Models sometimes return a string instead of a list — accept either."""
    if isinstance(value, list):
        return [str(x).strip()[:60] for x in value if str(x).strip()][:8]
    if isinstance(value, str):
        parts = [p.strip()[:60] for p in re.split(r"[,;]", value) if p.strip()]
        return parts[:8]
    return []


def _normalize(data: dict) -> dict:
    try:
        conf = max(0, min(100, int(data.get("confidence") or 0)))
    except (TypeError, ValueError):
        conf = 0
    return {
        "match":             _coerce_enum(data.get("match"), "match"),
        "confidence":        conf,
        "seniority":         _coerce_enum(data.get("seniority"), "seniority"),
        "currently_in_role": _coerce_enum(data.get("currently_in_role"), "currently_in_role"),
        "red_flags":         _coerce_taglist(data.get("red_flags")),
        "signals":           _coerce_taglist(data.get("signals")),
        "reason":            str(data.get("reason") or "")[:300],
    }


def _empty_result(match: str = "unknown", reason: str = "") -> dict:
    return {
        "match": match, "confidence": 0,
        "seniority": "unknown", "currently_in_role": "unknown",
        "red_flags": [], "signals": [], "reason": reason,
    }


def verify_role_single(client, model: str, profile: dict, target_role: str) -> dict:
    """Verify one profile. Returns the full multi-field analysis dict."""
    headline = (profile.get("headline") or "").strip()
    name = (profile.get("name") or "").strip()
    if not headline and not name:
        return _empty_result(reason="No name/headline available.")

    msg = PROMPT_TEMPLATE.format(
        role=target_role,
        name=name or "(unknown)",
        headline=headline or "(no headline)",
        location=(profile.get("location") or "").strip() or "(unknown)",
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": msg}],
            temperature=0.1,
            max_tokens=400,  # multi-field output needs more headroom
        )
        text = resp.choices[0].message.content or ""
        return _normalize(_parse_response(text))
    except Exception as e:
        LOGGER.warning("AI call failed for %r: %s", name, e)
        return _empty_result(match="error", reason=str(e)[:200])


def verify_role_batch(profiles: list[dict], target_role: str,
                      api_key: str, base_url: str, model: str,
                      on_progress: Callable[[int, int], None] | None = None) -> list[dict]:
    """Verify a list of profiles. Returns a list aligned with the input order."""
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "openai package not installed. Run `pip install openai` and retry."
        ) from e

    if not api_key:
        raise ValueError("API key is required for AI verification.")
    if not target_role.strip():
        raise ValueError("Target role is required.")

    client = OpenAI(api_key=api_key, base_url=base_url or DEFAULT_BASE_URL)
    results: list[dict] = []
    total = len(profiles)
    for i, p in enumerate(profiles, start=1):
        results.append(verify_role_single(client, model or DEFAULT_MODEL, p, target_role))
        if on_progress:
            try:
                on_progress(i, total)
            except Exception:
                LOGGER.exception("on_progress callback failed")
    return results


def test_connection(api_key: str, base_url: str, model: str) -> tuple[bool, str]:
    """Quick smoke test for the API settings card. Returns (ok, message)."""
    if not api_key:
        return False, "No API key provided."
    try:
        from openai import OpenAI
    except ImportError as e:
        return False, (f"openai library not importable: {e}. "
                       "In dev: run `pip install openai`. "
                       "In the .exe: rebuild with `package.bat`.")
    except Exception as e:
        return False, f"openai import failed ({type(e).__name__}): {e}"
    try:
        client = OpenAI(api_key=api_key, base_url=base_url or DEFAULT_BASE_URL)
        resp = client.chat.completions.create(
            model=model or DEFAULT_MODEL,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=0.0,
            max_tokens=10,
        )
        out = (resp.choices[0].message.content or "").strip()
        return True, f"OK ({model}): {out[:40]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
