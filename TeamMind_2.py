#! /usr/bin/env python3


import streamlit as st
import subprocess
import requests
import shutil
import glob
import json
import os
import signal
from datetime import datetime

SCREENPIPE_API = "http://localhost:3030"
DEFAULT_NOTION_KEY = os.getenv("NOTION_API_KEY", "")
DEFAULT_DB_ID = os.getenv("NOTION_DATABASE_ID", "")

RELEVANT_APPS = ["chrome", "firefox", "safari", "edge", "brave", "notion", "obsidian", "code", "cursor", "evince", "preview", "acrobat", "terminal"]

st.set_page_config(
    page_title="TeamMind — One-Button Workflow", 
    page_icon="logoTeamMind.png", 
    layout="centered"
)

# --- 1. LECTURE AUTOMATIQUE DE L'IA CONNECTÉE DANS SCREENPIPE ---
def get_screenpipe_connected_ai():
    search_paths = [
        os.path.expanduser("~/.screenpipe/store.bin"),
        os.path.expanduser("~/.local/share/screenpipe/store.bin"),
        os.path.expanduser("~/.config/screenpipe/store.bin"),
        os.path.expanduser("~/Library/Application Support/screenpipe/store.bin"),
    ] + glob.glob(os.path.expanduser("~/.local/share/*screenpipe*/store.bin"))

    for path in search_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    data = json.load(f)
                s = data.get("settings", data)
                
                ai_type = str(s.get("aiProviderType") or s.get("ai_provider_type") or "").lower()
                ai_url = s.get("aiUrl") or s.get("ai_url") or ""
                ai_key = s.get("openaiApiKey") or s.get("openai_api_key") or s.get("aiApiKey") or s.get("anthropicApiKey") or ""
                ai_model = s.get("aiModel") or s.get("ai_model") or "gpt-4o-mini"

                if ai_key or "ollama" in ai_type or "11434" in ai_url or "screenpipe-cloud" in ai_type:
                    provider = "anthropic" if ("anthropic" in ai_url or "claude" in ai_model.lower() or ai_key.startswith("sk-ant")) else "openai_compat"
                    if not ai_url:
                        ai_url = "https://api.anthropic.com/v1" if provider == "anthropic" else "https://api.openai.com/v1"
                    return {
                        "found": True,
                        "source": f"Screenpipe Desktop ({ai_model})",
                        "provider": provider,
                        "url": ai_url.rstrip("/"),
                        "key": ai_key,
                        "model": ai_model
                    }
            except Exception:
                continue

    if shutil.which("claude"):
        return {
            "found": True,
            "source": "Claude CLI (Système)",
            "provider": "claude_cli",
            "model": "claude-cli"
        }

    return {"found": False}

# --- 2. PILOTAGE DE SCREENPIPE ---
def is_screenpipe_running():
    try:
        return requests.get(f"{SCREENPIPE_API}/search?limit=1", timeout=1).status_code == 200
    except Exception:
        return False

def start_screenpipe_low_resource():
    if is_screenpipe_running():
        return True, "Screenpipe tourne déjà (port 3030 actif)."
    
    binary = shutil.which("screenpipe")
    if not binary:
        possible_paths = [
            os.path.expanduser("~/.local/bin/screenpipe"),
            "/usr/local/bin/screenpipe",
            "/Applications/screenpipe.app/Contents/MacOS/screenpipe"
        ]
        binary = next((p for p in possible_paths if os.path.exists(p)), None)

    if binary:
        cmd = [binary, "record", "--fps", "0.2", "--disable-audio"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        st.session_state["sp_pid"] = proc.pid
        return True, f"Screenpipe démarré (PID {proc.pid}, 0.2 FPS, Audio OFF)."
    else:
        subprocess.Popen(["open", "-a", "screenpipe"] if os.uname().sysname == "Darwin" else ["screenpipe"])
        return True, "Application Screenpipe lancée."

def stop_screenpipe_cleanly():
    pid = st.session_state.get("sp_pid")
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    subprocess.run(["pkill", "-f", "screenpipe"], stderr=subprocess.DEVNULL)
    st.session_state["sp_pid"] = None

# --- 3. EXTRACTION & PROMPT ---
def fetch_and_clean_session(limit=60):
    try:
        r = requests.get(f"{SCREENPIPE_API}/search", params={"content_type": "ocr", "limit": limit}, timeout=5)
        items = r.json().get("data", [])
    except Exception as e:
        return f"Erreur lecture Screenpipe : {e}", []

    snippets, apps = [], set()
    for item in items:
        c = item.get("content", {})
        app = c.get("app_name", "App")
        win = c.get("window_name", "")
        txt = c.get("text", "").strip()
        if any(a in app.lower() for a in RELEVANT_APPS) and len(txt) > 40:
            snippets.append(f"[{app} - {win}] : {txt[:400]}")
            apps.add(app)
    unique_snippets = list(dict.fromkeys(snippets))[:25]
    return "\n---\n".join(unique_snippets), list(apps)

def build_prompt(corpus, author, project):
    return f"""Tu es l'Architecte de Mémoire Collective de notre équipe.
Voici les captures OCR de la session de recherche de {author} pour le projet {project} :

{corpus}

Ignore tout le bruit (menus, UI, doublons). Rédige une fiche de synthèse ultra-claire en Markdown pour Notion :
# 🧭 [Titre résumant le sujet exploré]
> 💡 **TL;DR** — **Auteur :** {author} | **Projet :** {project}
> **Conclusion clé :** [1 phrase percutante]

## 📊 Faits, Chiffres & Comparatifs
- [Points clés et données chiffrées relevés à l'écran]

## 🔗 Sources & Outils étudiés
- [Noms des onglets/docs/outils et ce qu'on en retient]

## 🚀 Passation Équipe
- [ ] **À creuser :** [Prochaine étape]
- [ ] **Impasse à éviter :** [Ce qu'il ne faut pas refaire]"""

# --- 4. APPEL IA ---
def call_ai(prompt, ai_config):
    provider = ai_config.get("provider")
    
    if provider == "claude_cli":
        res = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=45)
        return res.stdout.strip() if res.returncode == 0 else f"Erreur Claude CLI : {{res.stderr}}"

    elif provider == "anthropic":
        headers = {"x-api-key": ai_config["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {"model": ai_config.get("model", "claude-3-5-sonnet-latest"), "max_tokens": 1024, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}
        r = requests.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers, timeout=25)
        return r.json()["content"][0]["text"] if r.status_code == 200 else f"Erreur Claude : {{r.text}}"

    elif provider == "openai_compat":
        url = ai_config.get("url", "https://api.openai.com/v1")
        if not url.endswith("/chat/completions"):
            url = f"{{url}}/chat/completions"
        headers = {"Authorization": f"Bearer {{ai_config.get('key', '')}}", "Content-Type": "application/json"}
        payload = {"model": ai_config.get("model", "gpt-4o-mini"), "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
        r = requests.post(url, json=payload, headers=headers, timeout=25)
        return r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else f"Erreur IA ({{r.status_code}}) : {{r.text}}"

    return None

def push_card_to_notion(author, project, summary, apps, notion_key, db_id):
    if not notion_key or not db_id:
        return False
    headers = {"Authorization": f"Bearer {{notion_key}}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    now_str = datetime.now().strftime("%d/%m %H:%M")
    payload = {
        "parent": {"database_id": db_id},
        "properties": {"Name": {"title": [{"text": {"content": f"[{{project}}] Session de {{author}} ({{now_str}})"}}]}},
        "children": [
            {"object": "block", "type": "callout", "callout": {"icon": {"emoji": "🚀"}, "rich_text": [{"type": "text", "text": {"content": f"Auteur : {{author}} | Projet : {{project}} | Apps : {{', '.join(apps[:4])}}}}]}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": summary[:1950]}}]}}
        ]
    }
    return requests.post("https://api.notion.com/v1/pages", json=payload, headers=headers, timeout=10).status_code == 200

# --- 5. INTERFACE UTILISATEUR ---
st.title("🎛️ TeamMind — One-Button Workflow")
st.caption("Track 3 : Work as a Team • Zéro configuration si Screenpipe est déjà connecté")

col1, col2 = st.columns(2)
with col1:
    author = st.text_input("👤 Chercheur", value="Salim")
with col2:
    project = st.selectbox("📂 Projet en cours", ["SkyCab"])

sp_ai = get_screenpipe_connected_ai()

if sp_ai["found"]:
    st.success(f"🔗 **IA détectée automatiquement :** `{{sp_ai['source']}}` (Prêt à l'emploi)")
    active_ai_config = sp_ai
else:
    st.warning("⚠️ Aucune IA détectée dans Screenpipe. Choisis comment générer la synthèse :")
    fallback_mode = st.radio(
        "Moteur de secours :",
        ["📋 Copier-Coller Universel (Gratuit / Sans clé)", "🟣 Clé API Claude", "🟢 Clé API OpenAI"],
        horizontal=True
    )
    if "Claude" in fallback_mode:
        k = st.text_input("Clé API Anthropic (`sk-ant-...`)", type="password")
        active_ai_config = {"found": True, "provider": "anthropic", "key": k, "model": "claude-3-5-sonnet-latest"}
    elif "OpenAI" in fallback_mode:
        k = st.text_input("Clé API OpenAI (`sk-...`)", type="password")
        active_ai_config = {"found": True, "provider": "openai_compat", "url": "https://api.openai.com/v1", "key": k, "model": "gpt-4o-mini"}
    else:
        active_ai_config = {"found": False, "provider": "clipboard"}

st.divider()

running = is_screenpipe_running()
st.markdown(f"**État Screenpipe :** {{'🟢 **ACTIF (Capture 0.2 FPS en cours)**' if running else '⚪ **ÉTEINT (0% CPU/RAM)**'}}")

col_start, col_stop = st.columns(2)

with col_start:
    if st.button("▶️ 1. Démarrer ma journée", use_container_width=True, type="primary", disabled=running):
        ok, msg = start_screenpipe_low_resource()
        st.rerun()

with col_stop:
    if st.button("⏹️ 2. Clôturer & Publier sur Notion", use_container_width=True, disabled=not running):
        with st.status("Clôture de la session en cours...", expanded=True) as status:
            st.write("📥 1/3 Extraction des données Screenpipe (:3030)...")
            corpus, apps = fetch_and_clean_session()
            prompt = build_prompt(corpus, author, project)

            if active_ai_config.get("provider") == "clipboard":
                st.session_state["output_text"] = prompt
                st.session_state["is_raw_prompt"] = True
            else:
                st.write("🧠 2/3 Génération de la synthèse via l'IA connectée...")
                summary = call_ai(prompt, active_ai_config)
                st.session_state["output_text"] = summary
                st.session_state["is_raw_prompt"] = False
                if DEFAULT_NOTION_KEY and DEFAULT_DB_ID:
                    push_card_to_notion(author, project, summary, apps, DEFAULT_NOTION_KEY, DEFAULT_DB_ID)

            st.write("🔌 3/3 Extinction propre de Screenpipe...")
            stop_screenpipe_cleanly()
            status.update(label="✅ Journée clôturée et Screenpipe arrêté !", state="complete")

if st.session_state.get("output_text"):
    if st.session_state.get("is_raw_prompt"):
        st.subheader("📋 Copie ce bloc dans ton Chat IA puis dans Notion :")
    else:
        st.subheader("✨ Fiche de recherche générée (Prête pour Notion) :")
    st.code(st.session_state["output_text"], language="markdown")
