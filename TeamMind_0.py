#! /usr/bin/env python3

import streamlit as st
import subprocess
import requests
import shutil
import os
import signal
from datetime import datetime

# --- CONFIGURATION (À remplir ou passer en variables d'environnement) ---
SCREENPIPE_API = "http://localhost:3030"
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "secret_TON_TOKEN_NOTION")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "TON_DATABASE_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-TA_CLE_OPENAI")  # ou Anthropic

RELEVANT_APPS = ["chrome", "firefox", "safari", "edge", "notion", "obsidian", "code", "cursor", "evince", "preview", "acrobat"]

st.set_page_config(page_title="TeamMind — One-Button Workflow", page_icon="🎛️", layout="centered")

# --- FONCTIONS DE PILOTAGE DE SCREENPIPE ---
def is_screenpipe_running():
    """Vérifie si le démon Screenpipe répond déjà sur le port 3030."""
    try:
        r = requests.get(f"{SCREENPIPE_API}/health", timeout=1)
        return r.status_code == 200
    except Exception:
        # Fallback sur /search si /health n'est pas exposé
        try:
            r = requests.get(f"{SCREENPIPE_API}/search?limit=1", timeout=1)
            return r.status_code == 200
        except Exception:
            return False

def start_screenpipe_low_resource():
    """Démarre Screenpipe avec les paramètres low-resource (0.2 FPS, sans audio)."""
    if is_screenpipe_running():
        return True, "Screenpipe tourne déjà en arrière-plan (port 3030 actif)."
    
    # Cherche le binaire CLI ou le binaire embarqué par l'app Desktop
    binary = shutil.which("screenpipe")
    if not binary:
        # Chemins habituels du binaire embarqué par l'app Desktop (Linux / macOS)
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
        return True, f"Screenpipe démarré en mode léger (PID {proc.pid}, 0.2 FPS, Audio OFF)."
    else:
        # Fallback : lance l'application Desktop si seul le bundle GUI est trouvé
        subprocess.Popen(["open", "-a", "screenpipe"] if os.uname().sysname == "Darwin" else ["screenpipe-app"])
        return True, "App Screenpipe Desktop lancée."

def stop_screenpipe_cleanly():
    """Arrête proprement Screenpipe pour libérer CPU et RAM en fin de journée."""
    pid = st.session_state.get("sp_pid")
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    # Sécurité supplémentaire : coupe tout processus screenpipe résiduel
    subprocess.run(["pkill", "-f", "screenpipe"], stderr=subprocess.DEVNULL)
    st.session_state["sp_pid"] = None

# --- FONCTIONS DE DISTILLATION (PROMPT 1) & PUSH NOTION ---
def fetch_and_clean_session(limit=60):
    r = requests.get(f"{SCREENPIPE_API}/search", params={"content_type": "ocr", "limit": limit}, timeout=5)
    items = r.json().get("data", [])
    snippets, apps = [], set()
    for item in items:
        c = item.get("content", {})
        app = c.get("app_name", "")
        win = c.get("window_name", "")
        txt = c.get("text", "").strip()
        if any(a in app.lower() for a in RELEVANT_APPS) and len(txt) > 40:
            snippets.append(f"[{app} - {win}] : {txt[:400]}")
            apps.add(app)
    # Déduplication simple en gardant l'ordre
    unique_snippets = list(dict.fromkeys(snippets))[:25]
    return "\n---\n".join(unique_snippets), list(apps)

def run_prompt_1_llm(corpus, author, status_choice):
    prompt = f"""Tu es l'Architecte de Mémoire Collective de notre équipe.
Voici les captures OCR de la session de travail de {author} :
{corpus}

Ignore tout le bruit (menus, UI, doublons). Rédige une synthèse structurée et directement exploitable par un nouveau membre de l'équipe :
1. CONCLUSION CLÉ (en 1 phrase percutante)
2. FAITS & CHIFFRES RELEVÉS (liste à puces dense et factuelle)
3. SOURCES & OUTILS ÉTUDIÉS (noms des sites/docs et ce qu'on en tire)
4. PASSATION ÉQUIPE : 1 piste à creuser + 1 impasse à éviter absolument."""

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }
    res = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=20)
    return res.json()["choices"][0]["message"]["content"]

def push_card_to_notion(author, status_choice, summary, apps):
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    now_str = datetime.now().strftime("%d/%m %H:%M")
    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": f"[{status_choice}] Session de {author} ({now_str})"}}]}
        },
        "children": [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "icon": {"emoji": "💡"},
                    "rich_text": [{"type": "text", "text": {"content": f"Auteur : {author} | Statut : {status_choice} | Apps : {', '.join(apps[:4])}"}}]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": summary[:1950]}}]
                }
            }
        ]
    }
    r = requests.post("https://api.notion.com/v1/pages", json=payload, headers=headers, timeout=10)
    return r.status_code == 200, r.text

# --- INTERFACE UTILISATEUR STREAMLIT ---
st.title("🎛️ TeamMind — One-Button Workflow")
st.caption("Track 3 : Work as a Team • Capture locale légère $\\rightarrow$ Distillation IA $\\rightarrow$ Notion")

col_info1, col_info2 = st.columns(2)
with col_info1:
    author = st.text_input("👤 Chercheur actif", value="Salim")
with col_info2:
    status_choice = st.selectbox("🏷️ Verdict de la session", ["🟢 Piste Validée", "🟡 À creuser", "🔴 Impasse (À éviter)"])

st.divider()

# Indicateur d'état en temps réel
running = is_screenpipe_running()
st.markdown(f"**État du capteur Screenpipe :** {'🟢 **ACTIF (Capture 0.2 FPS en cours)**' if running else '⚪ **ÉTEINT (0% CPU/RAM)**'}")

col_start, col_stop = st.columns(2)

with col_start:
    if st.button("▶️ 1. Démarrer ma journée", use_container_width=True, type="primary", disabled=running):
        ok, msg = start_screenpipe_low_resource()
        st.success(msg)
        st.rerun()

with col_stop:
    if st.button("⏹️ 2. Clôturer & Publier sur Notion", use_container_width=True, disabled=not running):
        with st.status("Clôture de la session en cours...", expanded=True) as status:
            st.write("📥 1/4 Extraction et filtrage des données Screenpipe (:3030)...")
            corpus, apps = fetch_and_clean_session()
            
            st.write("🧠 2/4 Exécution du Prompt 1 (Distillation & Synthèse d'équipe)...")
            summary = run_prompt_1_llm(corpus, author, status_choice)
            
            st.write("📤 3/4 Publication de la fiche structurée dans Notion...")
            ok_notion, resp_notion = push_card_to_notion(author, status_choice, summary, apps)
            
            st.write("🔌 4/4 Extinction propre de Screenpipe (libération RAM/CPU)...")
            stop_screenpipe_cleanly()
            
            status.update(label="✅ Session publiée sur Notion et Screenpipe arrêté proprement !", state="complete")
        
        st.subheader("Aperçu de la fiche envoyée à l'équipe :")
        st.info(summary)