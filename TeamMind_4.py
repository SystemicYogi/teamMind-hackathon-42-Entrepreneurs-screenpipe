#! /usr/bin/env python3
"""TeamMind - One-Button Workflow (Screenpipe x 42 hackathon, Track 3: Work as a Team)."""

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

SCREENPIPE_API = "http://localhost:3030"
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
    for pattern in ("~/.local/share/*screenpipe*", "~/.config/*screenpipe*",
                    "~/Library/Application Support/*screenpipe*"):
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

    if shutil.which("claude"):
        return {"found": True, "source": "Claude CLI (system)", "provider": "claude_cli", "model": "claude-cli"}, diag

    return {"found": False}, diag


# --------------------------------------------------------------------------
# 2. SCREENPIPE CONTROL (AppImage, desktop app, CLI)
# --------------------------------------------------------------------------
def is_screenpipe_running():
    try:
        return requests.get(f"{SCREENPIPE_API}/search?limit=1", timeout=1).status_code == 200
    except Exception:
        return False


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

    for _ in range(40):  # wait up to ~40 s for the API to come up
        if is_screenpipe_running():
            return True, f"🚀 Screenpipe started via `{os.path.basename(path)}` (PID {proc.pid})."
        time.sleep(1)
    return False, ("⚠️ Screenpipe was launched but the API on port 3030 is not responding yet. "
                   "Check that recording is enabled in the Screenpipe app.")


def stop_screenpipe_cleanly():
    pid = st.session_state.get("sp_pid")
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    # Exact process name only: never match our own Streamlit process by path
    subprocess.run(["pkill", "-x", "screenpipe"], stderr=subprocess.DEVNULL)
    st.session_state["sp_pid"] = None


# --------------------------------------------------------------------------
# 3. EXTRACTION & PROMPT
# --------------------------------------------------------------------------
def fetch_and_clean_session(limit=60):
    try:
        r = requests.get(f"{SCREENPIPE_API}/search", params={"content_type": "ocr", "limit": limit}, timeout=5)
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
            res = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=90)
            return res.stdout.strip() if res.returncode == 0 else f"Claude CLI error: {res.stderr}"

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
head_logo, head_title = st.columns([1, 4])
with head_logo:
    if logo_path:
        st.image(str(logo_path), width=110)
with head_title:
    st.title("🎛️ TeamMind - One-Button Workflow")
    st.caption("Track 3: Work as a Team • Zero setup if Screenpipe is already connected")

col1, col2 = st.columns(2)
with col1:
    author = st.text_input("👤 Researcher", value="Salim")
with col2:
    project = st.selectbox("📂 Current project", ["SkyCab"])

sp_ai, ai_diag = get_screenpipe_connected_ai()

if sp_ai["found"]:
    st.success(f"🔗 **AI detected automatically:** `{sp_ai['source']}` (ready to use)")
    active_ai_config = sp_ai
else:
    st.warning("⚠️ No AI detected in Screenpipe. Choose how to generate the summary:")
    fallback_mode = st.radio(
        "Fallback engine:",
        ["📋 Universal Copy-Paste (free / no key)", "🟣 Claude API key", "🟢 OpenAI API key"],
        horizontal=True,
    )
    if "Claude" in fallback_mode:
        k = st.text_input("Anthropic API key (`sk-ant-...`)", type="password")
        active_ai_config = {"found": True, "provider": "anthropic", "key": k, "model": DEFAULT_CLAUDE_MODEL}
    elif "OpenAI" in fallback_mode:
        k = st.text_input("OpenAI API key (`sk-...`)", type="password")
        active_ai_config = {"found": True, "provider": "openai_compat", "url": "https://api.openai.com/v1",
                            "key": k, "model": "gpt-4o-mini"}
    else:
        active_ai_config = {"found": False, "provider": "clipboard"}

with st.expander("🔍 Diagnostics (detection details)"):
    sp_path, sp_kind = find_screenpipe_executable()
    st.write(f"**Script folder:** `{SCRIPT_DIR}`")
    st.write(f"**Current folder:** `{Path.cwd()}`")
    st.write(f"**Logo:** `{logo_path}`")
    st.write(f"**Screenpipe executable:** `{sp_path}` ({sp_kind})")
    st.write(f"**Screenpipe API (port 3030):** {'reachable' if is_screenpipe_running() else 'not reachable'}")
    st.write(f"**AI presets found in settings:** {ai_diag['presets']}")
    st.write(f"**Top-level settings keys:** {ai_diag['keys']}")
    st.write("**Settings files checked:**")
    for f in ai_diag["files"]:
        st.write(f"- `{f['path']}` - exists: {f['exists']}, parsed as JSON: {f['parsed']}")

st.divider()

running = is_screenpipe_running()
st.markdown(f"**Screenpipe status:** {'🟢 **ACTIVE (capture running)**' if running else '⚪ **OFF (0% CPU/RAM)**'}")

col_start, col_stop = st.columns(2)

with col_start:
    if st.button("▶️ 1. Start my day", use_container_width=True, type="primary", disabled=running):
        with st.spinner("Starting Screenpipe..."):
            ok, msg = start_screenpipe_low_resource()
        st.session_state["start_msg"] = (ok, msg)
        st.rerun()

with col_stop:
    if st.button("⏹️ 2. Wrap up & publish to Notion", use_container_width=True, disabled=not running):
        with st.status("Wrapping up the session...", expanded=True) as status:
            st.write("📥 1/3 Extracting Screenpipe data (:3030)...")
            corpus, apps = fetch_and_clean_session()
            prompt = build_prompt(corpus, author, project)

            if active_ai_config.get("provider") == "clipboard":
                st.session_state["output_text"] = prompt
                st.session_state["is_raw_prompt"] = True
            else:
                st.write("🧠 2/3 Generating the summary with the connected AI...")
                summary = call_ai(prompt, active_ai_config) or "No response from the AI."
                st.session_state["output_text"] = summary
                st.session_state["is_raw_prompt"] = False
                if DEFAULT_NOTION_KEY and DEFAULT_DB_ID:
                    pushed = push_card_to_notion(author, project, summary, apps, DEFAULT_NOTION_KEY, DEFAULT_DB_ID)
                    st.session_state["notion_status"] = pushed
                else:
                    st.session_state["notion_status"] = None

            st.write("🔌 3/3 Shutting Screenpipe down cleanly...")
            stop_screenpipe_cleanly()
            status.update(label="✅ Session wrapped up and Screenpipe stopped!", state="complete")

if "start_msg" in st.session_state:
    ok, msg = st.session_state.pop("start_msg")
    (st.success if ok else st.error)(msg)

if st.session_state.get("notion_status") is True:
    st.success("📤 Card published to Notion.")
elif st.session_state.get("notion_status") is False:
    st.error("Notion push failed (check NOTION_API_KEY, NOTION_DATABASE_ID and that the database is shared with your integration).")

if st.session_state.get("output_text"):
    if st.session_state.get("is_raw_prompt"):
        st.subheader("📋 Copy this block into your AI chat, then into Notion:")
    else:
        st.subheader("✨ Generated research card (ready for Notion):")
    st.code(st.session_state["output_text"], language="markdown")
