import platform as _platform
import subprocess as _subprocess

# ── Force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────────────
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from ui import JarvisUI
from core.stt import WhisperSTT
from core.tts import create_tts_player
from core.llm_client import (
    call_llm, ensure_ollama_running, warmup_model, check_model_available,
)
from core.mic_recorder import record_utterance
from core.boot_sound import play_boot_sequence
from memory.config_manager import load_api_keys, is_local_configured
from memory.source_preferences import (
    load_source_prefs, save_source_pref, format_source_prefs_for_prompt,
)

from actions.open_app import open_app
from actions.web_search import web_search as web_search_action
from actions.weather_report import weather_action
from actions.browser_control import browser_control


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR    = get_base_dir()
PROMPT_PATH = BASE_DIR / "core" / "prompt.txt"

# Words Whisper often mis-hears — biases transcription toward these when it's unsure.
WHISPER_HINT = "Jarvis, Cagliari, blocco note, Chrome, Spotify, WhatsApp."

# Browser used for automated page reading/control. Edge is not set up for
# Playwright automation on this machine, so we pin this to Chrome by default.
READ_BROWSER = "chrome"


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise and direct."
        )


def _time_context() -> str:
    now = datetime.now()
    return (
        f"[CURRENT DATE & TIME]\n"
        f"Right now it is: {now.strftime('%A, %d %B %Y — %H:%M')} (24-hour clock, local time).\n"
        f"Use this exact value if asked what time or date it is. Never guess or invent a time.\n\n"
    )


# ── Tools — Phase 2 ───────────────────────────────────────────────────────────
# Described in the OpenAI/Ollama function-calling format.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": (
                "Opens any application on the computer. "
                "Use this whenever the user asks to open, launch, or start any app, "
                "website, or program."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Exact name of the application (e.g. 'Chrome', 'Spotify')",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Searches the web. Use for ANY question about current facts, events, "
                "prices, or topics — always prefer this over guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query or topic"},
                    "mode":  {"type": "string", "description": "search | news | research | price | compare"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather_report",
            "description": "Quick weather report for a city — use only if no preferred weather source is set.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_website",
            "description": (
                "Opens a specific website and immediately reads its text content, in one step. "
                "Use this when the user names a particular site and wants its content, "
                "or when a preferred source is set for the topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The website address to open (add https:// if missing)",
                    },
                    "what_to_find": {
                        "type": "string",
                        "description": "Briefly, what information the user is looking for on this page",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_control",
            "description": (
                "Controls the browser tab by tab: open a new tab and search Google for something, "
                "go to a specific URL, read the text of the currently open tab, close the current tab, "
                "or close the browser entirely. Use this for requests like 'search Google for X', "
                "'open a new tab and look up X', 'what does this page say', 'close this tab', "
                "'close the browser'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "go_to | search | get_text | close_tab | close | close_all",
                    },
                    "url": {
                        "type": "string",
                        "description": "Website address, required for action=go_to",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search terms, required for action=search",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_source_preference",
            "description": (
                "Saves the user's preferred website for a given topic, so it is used automatically "
                "in future requests on that topic. Call this when the user says things like "
                "'for weather always use X' or 'use Y as your source for news'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Short topic name, lowercase (e.g. 'weather', 'news', 'sport')",
                    },
                    "url": {
                        "type": "string",
                        "description": "The website address to remember for this topic",
                    },
                },
                "required": ["topic", "url"],
            },
        },
    },
]

_VALID_TOOL_NAMES = {
    "open_app", "web_search", "weather_report",
    "read_website", "browser_control", "save_source_preference",
}

# Catches a tool call the model wrote as visible text instead of using the
# real tool-calling mechanism, e.g.: {"name": "web_search", "parameters": {...}}
_FAKE_TOOL_RE = re.compile(
    r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"parameters"\s*:\s*(\{.*?\})\s*\}',
    re.DOTALL,
)


def _extract_fake_tool_call(text: str):
    """If the model wrote a tool call as plain text, parse it out. Returns (name, args) or None."""
    m = _FAKE_TOOL_RE.search(text)
    if not m:
        return None
    name = m.group(1)
    if name not in _VALID_TOOL_NAMES:
        return None
    try:
        args = json.loads(m.group(2))
    except Exception:
        return None
    return name, args


class JarvisLocal:
    """
    Local core loop: listen (Whisper) → think (Ollama, with tools) → speak (Kokoro).
    Phase 2: 6 tools connected (open_app, web_search, weather_report,
    read_website, browser_control, save_source_preference).
    """

    def __init__(self, ui: JarvisUI):
        self.ui = ui
        self._config = load_api_keys()
        self._history: list[dict] = []
        self._busy = False
        self._stt = None
        self._tts = None
        self.ui.on_text_command = self._on_text_command
        self.ui.on_interrupt    = self._do_interrupt

    def _on_text_command(self, text: str):
        # ui.py already echoes "You: ..." to the log when text is typed —
        # do NOT log it again here, only process it.
        threading.Thread(target=self._handle_input, args=(text,), daemon=True).start()

    def _do_interrupt(self):
        """Called from the UI thread when the user presses INTERRUPT or ESC."""
        if self._tts:
            try:
                self._tts.stop()
            except Exception as e:
                self.ui.write_log(f"ERR: Interrupt failed — {e}")
        self.ui.write_log("SYS: Interrupted.")

    def _speak(self, text: str) -> None:
        if self.ui.muted:
            return
        self.ui.start_speaking()
        try:
            self._tts.speak(text)
        except Exception as e:
            self.ui.write_log(f"ERR: TTS failed — {e}")
        self.ui.stop_speaking()

    def _read_website(self, url: str) -> str:
        """Opens a URL in Chrome (automation-ready) and reads its text content."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        open_result = browser_control({"action": "go_to", "url": url, "browser": READ_BROWSER})
        if not str(open_result).lower().startswith("opened"):
            return f"Could not open {url}: {open_result}"
        text_result = browser_control({"action": "get_text", "browser": READ_BROWSER})
        return str(text_result)[:4000] if text_result else "The page loaded but had no readable text."

    def _browser_control(self, args: dict) -> str:
        """Runs a browser_control action, always pinned to Chrome for automation reliability."""
        action = (args.get("action") or "").strip()
        payload = {"action": action, "browser": READ_BROWSER}
        if args.get("url"):
            payload["url"] = args["url"]
        if args.get("query"):
            payload["query"] = args["query"]
        result = browser_control(payload)
        if action == "get_text" and result:
            return str(result)[:4000]
        return str(result)

    def _execute_tool(self, name: str, args: dict) -> str:
        """Runs the actual Python function behind a tool call. Returns a text result."""
        self.ui.write_log(f"SYS: uso {name}...")
        try:
            if name == "open_app":
                result = open_app(parameters=args, response=None, player=self.ui)
                return result or f"Opened {args.get('app_name')}."

            if name == "web_search":
                result = web_search_action(parameters=args, player=self.ui)
                return result or "No results."

            if name == "weather_report":
                result = weather_action(parameters=args, player=self.ui)
                return result or "Weather unavailable."

            if name == "read_website":
                return self._read_website(args.get("url", ""))

            if name == "browser_control":
                return self._browser_control(args)

            if name == "save_source_preference":
                topic = args.get("topic", "")
                url   = args.get("url", "")
                save_source_pref(topic, url)
                return f"Saved: for '{topic}', always use {url}."

            return f"Unknown tool: {name}"
        except Exception as e:
            self.ui.write_log(f"ERR: {name} failed — {e}")
            return f"Tool '{name}' failed: {e}"

    def _ask_llm(self, user_text: str) -> str:
        prefs_block = format_source_prefs_for_prompt(load_source_prefs())
        system_prompt = _time_context() + prefs_block + _load_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._history[-10:])
        messages.append({"role": "user", "content": user_text})

        result = call_llm(messages, tools=TOOLS)

        tool_calls = result.get("tool_calls") or []

        # Safety net: the model sometimes writes a tool call as plain text
        # instead of using the real mechanism. Catch it and run it for real.
        if not tool_calls:
            fake = _extract_fake_tool_call(result.get("content", ""))
            if fake:
                fn_name, fn_args = fake
                tool_calls = [{
                    "id": "fake-0",
                    "function": {"name": fn_name, "arguments": fn_args},
                }]

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": result.get("content", ""),
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = tc["function"].get("arguments", {}) or {}
                tool_result = self._execute_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "name": fn_name,
                    "content": str(tool_result),
                })
            result = call_llm(messages)

        reply = (result.get("content") or "").strip() or "I'm not sure how to respond to that."

        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": reply})
        return reply

    def _handle_input(self, text: str):
        if self._busy or not text.strip():
            return
        self._busy = True
        try:
            self.ui.set_state("THINKING")
            reply = self._ask_llm(text)
            self.ui.write_log(f"{self.ui.assistant_name}: {reply}")
            self._speak(reply)
        except Exception as e:
            self.ui.write_log(f"ERR: {e}")
        finally:
            self._busy = False

    def _voice_loop(self):
        while True:
            if self.ui.muted or self._busy:
                time.sleep(0.2)
                continue
            self.ui.set_state("LISTENING")
            audio = record_utterance(should_stop=lambda: self.ui.muted)
            if audio.size < 1600:
                continue
            if self.ui.muted:
                continue
            self.ui.set_state("THINKING")
            try:
                text = self._stt.transcribe(audio).strip()
            except Exception as e:
                self.ui.write_log(f"ERR: Transcription failed — {e}")
                continue
            if not text:
                if not self.ui.muted:
                    self.ui.set_state("LISTENING")
                continue
            # Voice input has no other place logging it — log it here, once.
            self.ui.write_log(f"You: {text}")
            self._handle_input(text)

    def run(self):
        if not is_local_configured():
            self.ui.write_log(
                "ERR: No local engine configured. "
                "This build requires Ollama — reconfigure via the setup screen."
            )
            return

        self.ui.write_log("SYS: Checking Ollama...")
        if not ensure_ollama_running():
            self.ui.write_log("ERR: Could not reach Ollama. Install it from https://ollama.com and restart.")
            return

        stt_language = self._config.get("stt_language", "it")
        self.ui.write_log(f"SYS: Loading speech recognition (Whisper, large-v3, lang={stt_language})...")
        try:
            self._stt = WhisperSTT(
                model_name="large-v3",
                language=stt_language,
                initial_prompt=WHISPER_HINT,
            )
        except Exception as e:
            self.ui.write_log(f"ERR: Whisper failed to load — {e}")
            return

        self.ui.write_log("SYS: Loading voice synthesis...")
        try:
            self._tts = create_tts_player(self._config)
        except Exception as e:
            self.ui.write_log(f"ERR: TTS failed to load — {e}")
            return

        self.ui.write_log("SYS: Warming up language model...")
        warmup_model(_time_context() + _load_system_prompt())
        check_model_available(log=self.ui.write_log)

        self.ui.write_log(f"SYS: {self.ui.assistant_name} online (local).")
        self.ui.set_state("LISTENING")

        try:
            play_boot_sequence()
        except Exception as e:
            self.ui.write_log(f"ERR: Boot sound failed — {e}")

        now_str = datetime.now().strftime("%H:%M")
        greeting = f"Sono le {now_str}. Sistemi operativi. Jarvis a sua disposizione, signore."
        self.ui.write_log(f"{self.ui.assistant_name}: {greeting}")
        self._speak(greeting)

        self._voice_loop()


def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLocal(ui)
        threading.Thread(target=jarvis.run, daemon=True).start()

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()


if __name__ == "__main__":
    main()