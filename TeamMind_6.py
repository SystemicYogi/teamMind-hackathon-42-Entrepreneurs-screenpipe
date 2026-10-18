#! /usr/bin/env python3
"""TeamMind - One-Button Workflow (Screenpipe x 42 hackathon, Track 3: Work as a Team)."""

import base64
import getpass
import glob
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

DEFAULT_NOTION_KEY = os.getenv("NOTION_API_KEY", "")
DEFAULT_DB_ID = os.getenv("NOTION_DATABASE_ID", "")
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"

RELEVANT_APPS = ["chrome", "firefox", "safari", "edge", "brave", "notion", "obsidian",
                 "code", "cursor", "evince", "preview", "acrobat", "terminal"]

SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()


# --------------------------------------------------------------------------
# LOGO (searched next to the script first, then in the current directory)
# --------------------------------------------------------------------------
def find_logo():
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    for folder in (SCRIPT_DIR, Path.cwd()):
        try:
            files = sorted(folder.iterdir())
        except OSError:
            continue
        for f in files:
            if f.suffix.lower() in exts and "logo" in f.name.lower():
                return f
    return None


logo_path = find_logo()
page_icon = "🧠"
if logo_path:
    try:
        from PIL import Image
        page_icon = Image.open(logo_path)
    except Exception:
        page_icon = "🧠"

st.set_page_config(page_title="TeamMind - One-Button Workflow", page_icon=page_icon, layout="centered")


# --------------------------------------------------------------------------
# 1. DETECT THE AI CONNECTED TO SCREENPIPE
# --------------------------------------------------------------------------
def _settings_files():
    roots = [
        HOME / ".screenpipe",
        HOME / ".local/share/screenpipe",
        HOME / ".config/screenpipe",
        HOME / "Library/Application Support/screenpipe",
    ]
    # "*screenpi*" matches both "screenpipe" and the Tauri app folder "screenpi.pe"
    for pattern in ("~/.local/share/*screenpi*", "~/.config/*screenpi*",
                    "~/Library/Application Support/*screenpi*", "~/.screenpipe*"):
        roots += [Path(p) for p in glob.glob(os.path.expanduser(pattern))]
    names = ("store.bin", "store.json", "settings.json")
    seen, files = set(), []
    for r in roots:
        for n in names:
            p = r / n
            if str(p) not in seen:
                seen.add(str(p))
                files.append(p)
    return files


def _normalize_ai(url, key, model, provider_hint, source):
    url, key, model = (url or "").strip(), (key or "").strip(), (model or "").strip()
    hint = (provider_hint or "").lower()
    is_local = "ollama" in hint or "11434" in url or "localhost" in url or "127.0.0.1" in url
    if not (key or is_local):
        return None
    if "ollama" in hint and not url:
        url = "http://localhost:11434/v1"
    is_claude = "anthropic" in url or "anthropic" in hint or "claude" in model.lower() or key.startswith("sk-ant")
    provider = "anthropic" if is_claude else "openai_compat"
    if not url:
        url = "https://api.anthropic.com/v1" if is_claude else "https://api.openai.com/v1"
    model = model or (DEFAULT_CLAUDE_MODEL if is_claude else "gpt-4o-mini")
    return {"found": True, "source": f"{source} ({model})", "provider": provider,
            "url": url.rstrip("/"), "key": key, "model": model}


def find_claude_cli():
    path = shutil.which("claude")
    if path:
        return path
    for p in (HOME / ".local/bin/claude", HOME / ".claude/local/claude",
              HOME / ".npm-global/bin/claude", Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return None


def get_screenpipe_connected_ai():
    """Returns (ai_config, diagnostics)."""
    diag = {"files": [], "keys": [], "presets": 0}

    for path in _settings_files():
        entry = {"path": str(path), "exists": path.exists(), "parsed": False}
        diag["files"].append(entry)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        entry["parsed"] = True
        s = data.get("settings", data) if isinstance(data, dict) else {}
        if not isinstance(s, dict):
            continue
        diag["keys"] = sorted(s.keys())[:40]

        # Recent Screenpipe versions: list of AI presets, one of them being the default
        presets = s.get("aiPresets") or s.get("ai_presets") or []
        diag["presets"] = len(presets) if isinstance(presets, list) else 0
        if isinstance(presets, list) and presets:
            ordered = sorted(presets, key=lambda p: not (isinstance(p, dict) and p.get("defaultPreset")))
            for p in ordered:
                if not isinstance(p, dict):
                    continue
                prov = str(p.get("provider", "")).lower()
                agent = str(p.get("acpAgent") or "").lower()
                is_claude = any("claude" in x or "anthropic" in x for x in (prov, agent, str(p.get("model", "")).lower()))
                if is_claude and not (p.get("apiKey") or p.get("api_key")):
                    cli = find_claude_cli()
                    if cli:
                        return {"found": True, "source": f"Claude via CLI (Screenpipe preset '{p.get('id')}')",
                                "provider": "claude_cli", "cli": cli, "model": "claude-cli"}, diag
                    diag.setdefault("unusable", []).append(
                        f"{p.get('id')}: Claude preset without API key, and the `claude` CLI was not found")
                    continue
                if prov == "screenpipe-cloud":
                    user = s.get("user") if isinstance(s.get("user"), dict) else {}
                    token = user.get("token")
                    if token:
                        return {"found": True, "source": f"Screenpipe Cloud ({p.get('model') or 'auto'})",
                                "provider": "openai_compat",
                                "url": (p.get("url") or "https://api.screenpipe.com/v1").rstrip("/"),
                                "key": token, "model": p.get("model") or "auto"}, diag
                    diag.setdefault("unusable", []).append(f"{p.get('id')}: no user token in settings")
                    continue
                if prov.startswith("openai-chatgpt") and not (p.get("apiKey") or p.get("api_key")):
                    diag.setdefault("unusable", []).append(
                        f"{p.get('id')}: connected through account login (credentials kept in Screenpipe's vault)")
                    continue
                cfg = _normalize_ai(p.get("url"), p.get("apiKey") or p.get("api_key"), p.get("model"),
                                    p.get("provider"), "Screenpipe preset")
                if cfg:
                    return cfg, diag

        # Legacy flat settings
        cfg = _normalize_ai(
            s.get("aiUrl") or s.get("ai_url"),
            s.get("openaiApiKey") or s.get("openai_api_key") or s.get("aiApiKey") or s.get("anthropicApiKey"),
            s.get("aiModel") or s.get("ai_model"),
            s.get("aiProviderType") or s.get("ai_provider_type"),
            "Screenpipe settings",
        )
        if cfg:
            return cfg, diag

    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        cfg = _normalize_ai("", env_key, "", "anthropic", "ANTHROPIC_API_KEY env var")
        if cfg:
            return cfg, diag

    cli = find_claude_cli()
    if cli:
        return {"found": True, "source": "Claude CLI (system)", "provider": "claude_cli",
                "cli": cli, "model": "claude-cli"}, diag

    return {"found": False}, diag


# --------------------------------------------------------------------------
# 2. SCREENPIPE CONTROL (AppImage, desktop app, CLI)
# --------------------------------------------------------------------------
def read_store_settings():
    """Settings dict from Screenpipe's store file (empty dict if unreadable)."""
    for path in _settings_files():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        s = data.get("settings", data) if isinstance(data, dict) else {}
        if isinstance(s, dict) and s:
            return s
    return {}


def api_port():
    return str(os.getenv("SCREENPIPE_PORT") or read_store_settings().get("port") or 3030)


def api_base():
    return f"http://localhost:{api_port()}"


def api_headers():
    token = (os.getenv("SCREENPIPE_API_KEY") or st.session_state.get("sp_token", "")
             or read_store_settings().get("apiKey") or "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def probe_screenpipe():
    """Returns (is_up, detail). Any HTTP answer below 500 means the server is up
    (a 401/403 means it is up but wants an auth token)."""
    last = "no connection"
    for path in ("/health", "/search?limit=1"):
        try:
            r = requests.get(f"{api_base()}{path}", headers=api_headers(), timeout=2)
            last = f"GET {path} -> HTTP {r.status_code}"
            if r.status_code < 500:
                return True, last
        except Exception as e:
            last = f"GET {path} -> {type(e).__name__}"
    return False, last


def is_screenpipe_running():
    return probe_screenpipe()[0]


def find_screenpipe_executable():
    """Returns (path, kind) with kind in {'appimage', 'app', 'cli'} or (None, None)."""
    search_dirs = [SCRIPT_DIR, Path.cwd(), HOME / "Desktop", HOME / "Downloads", HOME,
                   HOME / "Applications", HOME / ".local/bin", Path("/opt")]
    seen = set()
    for d in search_dirs:
        if str(d) in seen or not d.is_dir():
            continue
        seen.add(str(d))
        try:
            for f in sorted(d.iterdir()):
                if f.is_file() and f.name.lower().endswith(".appimage") and "screenpipe" in f.name.lower():
                    return str(f.resolve()), "appimage"
        except OSError:
            continue

    mac_app = "/Applications/screenpipe.app/Contents/MacOS/screenpipe"
    if os.path.exists(mac_app):
        return mac_app, "app"

    binary = shutil.which("screenpipe")
    if binary:
        return binary, "cli"
    for p in (HOME / ".local/bin/screenpipe", Path("/usr/local/bin/screenpipe")):
        if p.exists():
            return str(p), "cli"
    return None, None


def start_screenpipe_low_resource():
    if is_screenpipe_running():
        return True, "Screenpipe is already running (port 3030 active)."

    path, kind = find_screenpipe_executable()
    if not path:
        return False, ("❌ Screenpipe not found. Looked for a *screenpipe*.AppImage in the script folder, "
                       "current folder, Desktop, Downloads and home, then for the `screenpipe` binary in PATH.")

    env = dict(os.environ)
    if kind == "appimage":
        os.chmod(path, 0o755)
        env["APPIMAGE_EXTRACT_AND_RUN"] = "1"  # works even without FUSE
        # The AppImage is the desktop app: it records on its own, no CLI arguments.
        cmd = [path]
    elif kind == "app":
        cmd = [path]
    else:
        cmd = [path, "record", "--fps", "0.2", "--disable-audio"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, env=env)
        st.session_state["sp_pid"] = proc.pid
    except Exception as e:
        return False, f"❌ Failed to launch Screenpipe: {e}"

    detail = "no connection"
    for _ in range(90):  # wait up to ~90 s for the API to come up
        up, detail = probe_screenpipe()
        if up:
            return True, f"🚀 Screenpipe started via `{os.path.basename(path)}` (PID {proc.pid}). {detail}"
        time.sleep(1)
    return False, (f"⚠️ Screenpipe was launched but its API on port {api_port()} did not answer "
                   f"(last probe: {detail}). The app may use another port "
                   f"(set SCREENPIPE_PORT) or require a token (set SCREENPIPE_API_KEY).")


def stop_screenpipe_cleanly():
    pid = st.session_state.get("sp_pid")
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    names = ("screenpipe-app", "screenpipe")  # exact names only: never match our own Streamlit process
    for sig in ("-TERM", "-KILL"):
        for n in names:
            subprocess.run(["pkill", sig, "-x", n], stderr=subprocess.DEVNULL)
        for _ in range(6):
            if not is_screenpipe_running():
                break
            time.sleep(0.5)
        if not is_screenpipe_running():
            break
    st.session_state["sp_pid"] = None


# --------------------------------------------------------------------------
# 3. EXTRACTION & PROMPT
# --------------------------------------------------------------------------
def fetch_and_clean_session(limit=60):
    try:
        r = requests.get(f"{api_base()}/search", params={"content_type": "ocr", "limit": limit},
                         headers=api_headers(), timeout=10)
        if r.status_code != 200:
            return f"Screenpipe returned HTTP {r.status_code}: {r.text[:200]}", []
        items = r.json().get("data", [])
    except Exception as e:
        return f"Error reading Screenpipe: {e}", []

    snippets, apps = [], set()
    for item in items:
        c = item.get("content", {})
        app = c.get("app_name", "App")
        win = c.get("window_name", "")
        txt = c.get("text", "").strip()
        if any(a in app.lower() for a in RELEVANT_APPS) and len(txt) > 40:
            snippets.append(f"[{app} - {win}]: {txt[:400]}")
            apps.add(app)
    unique_snippets = list(dict.fromkeys(snippets))[:25]
    return "\n---\n".join(unique_snippets), list(apps)


def build_prompt(corpus, author, project):
    return f"""You are our team's Collective Memory Architect.
Below are OCR captures from {author}'s research session on the project {project}:

{corpus}

Ignore all noise (menus, UI elements, duplicates). Write an ultra-clear summary card in Markdown for Notion:
# 🧭 [Title summarizing the explored topic]
> 💡 **TL;DR** - **Author:** {author} | **Project:** {project}
> **Key takeaway:** [1 punchy sentence]

## 📊 Facts, Figures & Comparisons
- [Key points and numerical data seen on screen]

## 🔗 Sources & Tools Reviewed
- [Names of tabs/docs/tools and what we take away from each]

## 🚀 Team Handover
- [ ] **To dig into:** [Next step]
- [ ] **Dead end to avoid:** [What should not be redone]"""


# --------------------------------------------------------------------------
# 4. AI CALL + NOTION
# --------------------------------------------------------------------------
def call_ai(prompt, ai_config):
    provider = ai_config.get("provider")
    try:
        if provider == "claude_cli":
            res = subprocess.run([ai_config.get("cli") or "claude", "-p"], input=prompt,
                                 capture_output=True, text=True, timeout=120)
            return res.stdout.strip() if res.returncode == 0 else (
                f"Claude CLI error (exit code {res.returncode}): {(res.stderr or res.stdout).strip() or 'no output'}")

        if provider == "anthropic":
            headers = {"x-api-key": ai_config["key"], "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            payload = {"model": ai_config.get("model", DEFAULT_CLAUDE_MODEL), "max_tokens": 1024,
                       "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}
            r = requests.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers, timeout=60)
            return r.json()["content"][0]["text"] if r.status_code == 200 else f"Claude error: {r.text}"

        if provider == "openai_compat":
            url = ai_config.get("url", "https://api.openai.com/v1")
            if not url.endswith("/chat/completions"):
                url = f"{url}/chat/completions"
            headers = {"Authorization": f"Bearer {ai_config.get('key', '')}", "Content-Type": "application/json"}
            payload = {"model": ai_config.get("model", "gpt-4o-mini"),
                       "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
            r = requests.post(url, json=payload, headers=headers, timeout=60)
            return r.json()["choices"][0]["message"]["content"] if r.status_code == 200 \
                else f"AI error ({r.status_code}): {r.text}"
    except Exception as e:
        return f"AI call failed: {e}"
    return None


def push_card_to_notion(author, project, summary, apps, notion_key, db_id):
    if not notion_key or not db_id:
        return False
    headers = {"Authorization": f"Bearer {notion_key}", "Content-Type": "application/json",
               "Notion-Version": "2022-06-28"}
    now_str = datetime.now().strftime("%d/%m %H:%M")
    chunks = [summary[i:i + 1900] for i in range(0, len(summary), 1900)][:90]  # Notion limit: 2000 chars/block
    children = [{"object": "block", "type": "callout", "callout": {
        "icon": {"emoji": "🚀"},
        "rich_text": [{"type": "text", "text": {
            "content": f"Author: {author} | Project: {project} | Apps: {', '.join(apps[:4])}"}}]}}]
    children += [{"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": c}}]}} for c in chunks]
    payload = {
        "parent": {"database_id": db_id},
        "properties": {"Name": {"title": [{"text": {"content": f"[{project}] {author}'s session ({now_str})"}}]}},
        "children": children,
    }
    try:
        return requests.post("https://api.notion.com/v1/pages", json=payload, headers=headers,
                             timeout=15).status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# 5. USER INTERFACE
# --------------------------------------------------------------------------
AUTHOR = os.getenv("TEAMMIND_AUTHOR") or getpass.getuser()
PROJECT = "SkyCab"


def logo_html(path):
    if not path:
        return "<div style='font-size:96px'>🧠</div>"
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix.lower()]
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"<img src='data:{mime};base64,{b64}' style='width:260px;max-width:70%;'>"


st.markdown(
    "<div style='text-align:center'>"
    f"{logo_html(logo_path)}"
    "<h1 style='margin:0.3rem 0 0 0;'>TeamMind</h1>"
    "<p style='opacity:0.7;margin:0 0 1.5rem 0;'>One-Button Workflow · Track 3: Work as a Team</p>"
    "</div>",
    unsafe_allow_html=True,
)

# AI detection (runs on every refresh, so it picks up Screenpipe once it is started)
sp_ai, ai_diag = get_screenpipe_connected_ai()
ai_messages = []
if sp_ai["found"]:
    active_ai_config = sp_ai
    ai_messages.append(("success", f"🔗 AI detected: {sp_ai['source']}"))
else:
    active_ai_config = {"found": False, "provider": "clipboard"}
    note = " ".join(f"({u})" for u in ai_diag.get("unusable", []))
    ai_messages.append(("info", "ℹ️ No usable AI found: copy-paste mode. You will get a prompt to paste into "
                                f"Screenpipe's chat. {note}".strip()))

running = is_screenpipe_running()
col_start, col_stop = st.columns(2)
start_clicked = col_start.button("▶️ 1. Start my day", use_container_width=True, type="primary", disabled=running)
stop_clicked = col_stop.button("⏹️ 2. Wrap up & publish to Notion", use_container_width=True, disabled=not running)

if start_clicked:
    with st.spinner("Starting Screenpipe..."):
        ok, msg = start_screenpipe_low_resource()
    st.session_state["messages"] = [("success" if ok else "error", msg)]
    st.session_state["output_text"] = None
    st.rerun()

if stop_clicked:
    msgs = []
    with st.spinner("Wrapping up the session..."):
        corpus, apps = fetch_and_clean_session()
        if corpus.startswith(("Error reading", "Screenpipe returned")):
            msgs.append(("error", f"{corpus} (Screenpipe left running)"))
        else:
            st.session_state["output_text"] = None
            st.session_state["is_raw_prompt"] = False
            if not corpus.strip():
                msgs.append(("info", "Nothing relevant was captured during this session."))
            else:
                prompt = build_prompt(corpus, AUTHOR, PROJECT)
                if active_ai_config.get("provider") == "clipboard":
                    st.session_state["output_text"] = prompt
                    st.session_state["is_raw_prompt"] = True
                    msgs.append(("info", "Paste this prompt into your AI chat, then the result into Notion."))
                else:
                    summary = call_ai(prompt, active_ai_config) or "AI error: empty response."
                    if summary.startswith(("Claude CLI error", "Claude error", "AI error", "AI call failed")):
                        msgs.append(("error", summary))
                    else:
                        st.session_state["output_text"] = summary
                        msgs.append(("success", "✨ Research card generated."))
                        if DEFAULT_NOTION_KEY and DEFAULT_DB_ID:
                            if push_card_to_notion(AUTHOR, PROJECT, summary, apps, DEFAULT_NOTION_KEY, DEFAULT_DB_ID):
                                msgs.append(("success", "📤 Card published to Notion."))
                            else:
                                msgs.append(("warning", "Notion push failed: check NOTION_API_KEY, NOTION_DATABASE_ID "
                                                        "and that the database is shared with your integration."))
                        else:
                            msgs.append(("info", "Notion not configured: set NOTION_API_KEY and NOTION_DATABASE_ID."))
            stop_screenpipe_cleanly()
            msgs.append(("success", "✅ Session wrapped up and Screenpipe stopped."))
    st.session_state["messages"] = msgs
    st.rerun()

# Messages: green = ok, blue = info, orange = warning, red = error
for level, text in ai_messages + st.session_state.get("messages", []):
    getattr(st, level)(text)

if st.session_state.get("output_text"):
    st.code(st.session_state["output_text"], language="markdown")
