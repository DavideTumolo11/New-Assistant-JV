"""
User-defined source preferences for JARVIS.

Lets the user say things like "for weather, always use meteo.it" and have
JARVIS remember and reuse that source automatically in future requests,
instead of asking or guessing every time.
"""
import json
import sys
from pathlib import Path
from threading import Lock


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


PREFS_PATH = _base_dir() / "memory" / "source_preferences.json"
_lock = Lock()


def load_source_prefs() -> dict:
    """Returns {topic: url} — e.g. {"weather": "https://meteo.it"}."""
    if not PREFS_PATH.exists():
        return {}
    with _lock:
        try:
            data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[SourcePrefs] Load error: {e}")
            return {}


def save_source_pref(topic: str, url: str) -> None:
    topic = (topic or "").strip().lower()
    url   = (url or "").strip()
    if not topic or not url:
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    prefs = load_source_prefs()
    prefs[topic] = url
    with _lock:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(
            json.dumps(prefs, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"[SourcePrefs] Saved: {topic} -> {url}")


def format_source_prefs_for_prompt(prefs: dict) -> str:
    """Renders saved preferences as a block to inject into the system prompt."""
    if not prefs:
        return ""
    lines = ["[PREFERRED SOURCES — use these automatically, do not ask again]"]
    for topic, url in prefs.items():
        lines.append(f"- For {topic}: use {url} (call read_website with this url)")
    return "\n".join(lines) + "\n\n"