# pages/05_Perfil_Conta.py
# -------------------------------------------------------------
# Perfil / Conta (sem banco fake)
# - Lê plano/datas do st.session_state (definidos no pós-login)
# - Edita nome/e-mail em public.profiles
# - (Opcional) Salva altura/peso em public.user_nutrition se existir
# -------------------------------------------------------------
import datetime as dt
from typing import Optional, Dict, Any

import streamlit as st
from supabase import create_client, Client

st.set_page_config(page_title="Perfil / Conta", page_icon="👤", layout="centered")
st.title("👤 Perfil / Conta")

# -------- Supabase client --------
@st.cache_resource
def _supabase() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])

sb = _supabase()

# -------- Helpers (definidos ANTES do uso) --------
def db_get_profile(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = sb.table("profiles").select("*").eq("id", user_id).single().execute()
        return res.data
    except Exception:
        return None

def db_upsert_profile(user_id: str, email: str, nome: Optional[str] = None) -> Optional[Dict[str, Any]]:
    payload: Dict[str, Any] = {"id": user_id, "email": email}
    if nome is not None:
        payload["nome"] = nome
    try:
        res = sb.table("profiles").upsert(
            payload,
            on_conflict="id",
            returning="representation"
        ).execute()
        return (res.data[0] if isinstance(res.data, list) and res.data else res.data)
    except Exception as e:
        st.warning(f"Não foi possível salvar perfil: {e}")
        return None

def db_get_user_nutrition(user_id: str) -> Optional[Dict[str, Any]]:
    """Opcional: lê altura/peso da tabela user_nutrition (se existir)."""
    try:
        res = sb.table("user_nutrition").select("*").eq("user_id", user_id).single().execute()
        return res.data
    except Exception:
        # tabela pode não existir ainda no seu projeto — tudo bem para MVP
        return None

def db_upsert_user_nutrition(user_id: str, altura_cm: Optional[float], peso_kg: Optional[float]) -> Optional[Dict[str, Any]]:
    """Opcional: salva altura/peso na tabela user_nutrition (se existir)."""
    try:
        payload: Dict[str, Any] = {"user_id": user_id}
        if altura_cm is not None:
            payload["height_cm"] = altura_cm
        if peso_kg is not None:
            payload["weight_kg"] = peso_kg
        res = sb.table("user_nutrition").upsert(
            payload,
            on_conflict="user_id",
            returning="representation"
        ).execute()
        return (res.data[0] if isinstance(res.data, list) and res.data else res.data)
    except Exception:
        st.info("Tabela 'user_nutrition' não encontrada (ok para MVP).")
        return None

# -------- Sessão (dados vindos do login) --------
uid = st.session_state.get("user_id")
email = st.session_state.get("user_email", "-")
plan_id = st.session_state.get("plan_id", "DISCIPULO")          # 'DISCIPULO' | 'FIEL'
plan_name = st.session_state.get("plan_name", "Discípulo (3 meses)")
inicio = st.session_state.get("plan_inicio")                     # 'YYYY-MM-DD' ou None
fim    = st.session_state.get("plan_fim")

if not uid:
    st.warning("Faça login para ver seu perfil.")
    st.stop()

# -------- Carrega dados atuais --------
prof = db_get_profile(uid) or {"email": email, "nome": ""}
nut  = db_get_user_nutrition(uid) or {}

# ==========================
# Status do Plano
# ==========================
st.subheader("Status do Plano")
col1, col2 = st.columns(2)
with col1:
    st.metric("Plano", plan_name)
    st.write(f"**E-mail:** {email}")
with col2:
    if inicio and fim:
        d_hoje   = dt.date.today()
        d_inicio = dt.date.fromisoformat(inicio)
        d_fim    = dt.date.fromisoformat(fim)
        total    = (d_fim - d_inicio).days
        passados = max(0, (min(d_hoje, d_fim) - d_inicio).days)
        restantes= max(0, (d_fim - max(d_hoje, d_inicio)).days)
        pct      = passados/total if total>0 else 0
        st.metric("Dias restantes", restantes)
        st.write("Progresso do plano")
        st.progress(min(1.0, max(0.0, pct)))
    else:
        st.info("Sem assinatura ativa (padrão: Discípulo).")

st.divider()

# ==========================
# Dados Pessoais
# ==========================
st.subheader("Dados Pessoais")
with st.form("form_dados_pessoais"):
    nome_in  = st.text_input("Nome",  value=prof.get("nome")  or "")
    email_in = st.text_input("E-mail", value=prof.get("email") or email)
    colA, colB = st.columns(2)
    with colA:
        altura_in = st.number_input(
            "Altura (cm)", min_value=0.0, max_value=300.0,
            value=float(nut.get("height_cm") or 0.0), step=0.5
        )
    with colB:
        peso_in = st.number_input(
            "Peso (kg)", min_value=0.0, max_value=500.0,
            value=float(nut.get("weight_kg") or 0.0), step=0.1
        )
    submitted = st.form_submit_button("💾 Salvar alterações")

if submitted:
    db_upsert_profile(uid, email_in, nome=nome_in)
    db_upsert_user_nutrition(uid, altura_in or None, peso_in or None)
    st.success("Perfil atualizado.")

st.divider()

# ==========================
# Acesso a Conteúdos
# ==========================
st.subheader("Acesso a Conteúdos")
if plan_id == "FIEL":
    st.success("🔓 Receitas Premium: **Liberado**")
else:
    st.warning("🔒 Receitas Premium: **Bloqueado** no seu plano atual.")
