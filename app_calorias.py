# app_calorias_v2.py
# Mini app em Streamlit (Versão 2)
# - Calcula BMR/TDEE/Água
# - Seleciona objetivo (cut/manutenção/bulk) com ajuste automático de calorias
# - Macros por g/kg ou por %
# - Exporta PDF simples com o plano diário (usa reportlab se disponível)

import io
from datetime import datetime, date
from typing import Optional, Dict, Any

import streamlit as st
from supabase import create_client, Client

# --- Config da página (faça cedo) ---
st.set_page_config(page_title="Mini App • Calorias & Macros (v2)", page_icon="🔥", layout="centered")

# --- Verificação silenciosa de config (sem print no UI) ---
import logging
import streamlit as st

# Configura logging só no servidor (não aparece pro usuário)
logger = logging.getLogger("caloria")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

def assert_required_secrets():
    required = ["SUPABASE_URL", "SUPABASE_ANON_KEY"]  # ajuste se tiver mais
    missing = [k for k in required if not st.secrets.get(k)]
    if missing:
        # Mensagem amigável para você (em produção só verá se acessar o app logado como admin)
        st.error("Configuração do servidor ausente. Contate o suporte.")
        logger.error("Secrets ausentes: %s", ", ".join(missing))
        st.stop()
    else:
        logger.info("Secrets verificados com sucesso.")

assert_required_secrets()

from pathlib import Path
ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_PATH = ASSETS_DIR / "logo.png"

# --- Splash simples e estável (1x por sessão, sem rerun) ---
import time
import streamlit as st

def show_splash_once():
    # Reaparecer com ?fresh=1
    q = dict(st.query_params)
    if "fresh" in q:
        st.session_state.pop("_splash_done", None)

    if st.session_state.get("_splash_done"):
        return

    ph = st.empty()
    with ph.container():
        st.markdown(
            "<div style='height:100vh;display:flex;flex-direction:column;"
            "align-items:center;justify-content:center;'>",
            unsafe_allow_html=True
        )
        try:
            # use o caminho já definido no topo:
            st.image(str(LOGO_PATH), width=140)
        except Exception:
            st.markdown("<div style='font-size:1.4rem;'>CalorIA</div>", unsafe_allow_html=True)

        st.markdown(
            "<div style='margin-top:12px;font-size:1.05rem;opacity:.75;'>CalorIA</div></div>",
            unsafe_allow_html=True
        )

    time.sleep(1.0)                 # Mostra 1s
    ph.empty()                       # Some com o splash
    st.session_state["_splash_done"] = True  # Marca como exibido
    return                            # segue o app normalmente

# >>> CHAME AQUI, ANTES DE QUALQUER OUTRA UI <<<
show_splash_once()

# Tentativa de importar reportlab (para exportar PDF)
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

@st.cache_resource
def get_supabase_client() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_ANON_KEY"]
    return create_client(url, key)

supabase = get_supabase_client()

def storage_public_url(bucket: str, path: str | None) -> str | None:
    """Retorna a URL pública (string) ou None, independente do formato do SDK."""
    if not path:
        return None
    try:
        res = supabase.storage.from_(bucket).get_public_url(path)
        # v2 geralmente retorna dict {"data": {"publicUrl": "..."}}
        if isinstance(res, dict):
            data = res.get("data") or {}
            # cobre variações de chave
            for k in ("publicUrl", "public_url", "publicURL", "signedUrl", "signedURL", "signed_url"):
                if data.get(k):
                    return data[k]
        # fallback: algumas versões retornam str
        if isinstance(res, str):
            return res
    except Exception:
        pass
    return None

def local_img_path(basename: str, exts=(".jpg", ".jpeg", ".png")) -> str | None:
    """Fallback local (apenas funciona no ambiente local, não no Cloud)."""
    for ext in exts:
        p = ASSETS_DIR / f"{basename}{ext}"
        if p.exists():
            return str(p)
    return None

def _show_image(url: str | None, caption: str | None = None):
    """Blinda o st.image e loga o tipo/valor quando está inválido."""
    if isinstance(url, str) and url:
        try:
            st.image(url, caption=caption, use_container_width=True)
            return
        except Exception as e:
            st.warning(f"Falha ao renderizar imagem (valor={repr(url)[:120]}).")
    else:
        st.info("DEBUG: URL da imagem inválida → " + repr(url))

# === Onboarding (wizard) - helpers ===
import math
from datetime import date
import pandas as pd
import streamlit as st

def _fator_atividade(txt: str) -> float:
    return {
        "Sedentário (pouco ou nenhum exercício)": 1.2,
        "Leve (1–3x/semana)": 1.375,
        "Moderado (3–5x/semana)": 1.55,
        "Alto (6–7x/semana)": 1.725,
        "Atleta/Extremo (2x/dia)": 1.9,
    }.get(txt, 1.2)

def _bmr_mifflin(kg: float, cm: float, anos: int, sex: str) -> float:
    s = 5 if sex == "Masculino" else -161
    return (10*kg) + (6.25*cm) - (5*anos) + s

def _tdee(kg, cm, anos, sex, atividade_txt):
    return _bmr_mifflin(kg, cm, anos, sex) * _fator_atividade(atividade_txt)

def _idade_from_dob(dob: date) -> int:
    if not dob:
        return 30
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

def _semanas_para_alvo(peso_atual, peso_meta, objetivo):
    # aproximações conservadoras de ritmo
    if objetivo == "Emagrecer":
        perda_por_sem = 0.5
        delta = max(peso_atual - peso_meta, 0.0)
        return 0 if delta <= 0 else math.ceil(delta / perda_por_sem)
    elif objetivo == "Ganhar massa":
        ganho_por_sem = 0.25
        delta = max(peso_meta - peso_atual, 0.0)
        return 0 if delta <= 0 else math.ceil(delta / ganho_por_sem)
    return 0

# === Onboarding (wizard) ===
def render_onboarding(uid: str, profile: dict):
    st.markdown("### 👋 Boas-vindas ao calorIA")
    if "ob_step" not in st.session_state:
        st.session_state.ob_step = 1
    step = st.session_state.ob_step

    # estado temporário (defaults do profile se existirem)
    full_name = st.session_state.get("ob_name", profile.get("full_name", ""))
    email = profile.get("email", "")
    dob = st.session_state.get("ob_dob") or (profile.get("dob") and date.fromisoformat(profile["dob"]))
    sex = st.session_state.get("ob_sex", profile.get("sex", "Masculino"))
    height_cm = st.session_state.get("ob_h", float(profile.get("height_cm") or 170))
    weight_kg = st.session_state.get("ob_w", float(profile.get("weight_kg") or 75))
    atividade = st.session_state.get("ob_act", "Moderado (3–5x/semana)")
    goal = st.session_state.get("ob_goal", profile.get("goal") or "Emagrecer")
    target_weight_kg = st.session_state.get("ob_target", float(profile.get("target_weight_kg") or max(weight_kg-5, 50)))
    obstacles = st.session_state.get("ob_obs", profile.get("obstacles") or "")

    if step == 1:
        st.subheader("Por que o calorIA é diferente?")
        st.markdown(
            "**Dietas genéricas**: restrições rígidas, sem contexto.\n\n"
            "**Com o calorIA**: plano ajustável, registro simples e revisão semanal guiada por dados (peso, fotos, medidas, diário, jejum)."
        )

    elif step == 2:
        st.subheader("Seus dados básicos")
        full_name = st.text_input("Nome completo", value=full_name)
        col1, col2 = st.columns(2)
        with col1:
            sex = st.selectbox("Sexo", ["Masculino","Feminino"], index=0 if sex=="Masculino" else 1)
            dob = st.date_input("Data de nascimento", value=dob or date(1995,1,1))
        with col2:
            height_cm = st.number_input("Altura (cm)", min_value=120.0, max_value=230.0, step=0.5, value=float(height_cm))
            weight_kg = st.number_input("Peso atual (kg)", min_value=30.0, max_value=300.0, step=0.1, value=float(weight_kg))
        atividade = st.selectbox(
            "Nível de atividade",
            ["Sedentário (pouco ou nenhum exercício)","Leve (1–3x/semana)","Moderado (3–5x/semana)",
             "Alto (6–7x/semana)","Atleta/Extremo (2x/dia)"],
            index=["Sedentário (pouco ou nenhum exercício)","Leve (1–3x/semana)","Moderado (3–5x/semana)",
                   "Alto (6–7x/semana)","Atleta/Extremo (2x/dia)"].index(atividade)
        )

    elif step == 3:
        st.subheader("Seu objetivo e meta")
        goal = st.selectbox("Objetivo principal", ["Emagrecer","Ganhar massa","Manutenção"],
                            index=["Emagrecer","Ganhar massa","Manutenção"].index(goal))
        target_weight_kg = st.number_input("Meta de peso (kg)",
                                           min_value=30.0, max_value=300.0, step=0.1, value=float(target_weight_kg))
        st.caption("Pode ajustar depois – a meta serve para estimar tempo e orientar o plano.")

    elif step == 4:
        st.subheader("Estimativas iniciais")
        idade = _idade_from_dob(dob or date(1995,1,1))
        bmr = _bmr_mifflin(weight_kg, height_cm, idade, sex)
        tdee_val = _tdee(weight_kg, height_cm, idade, sex, atividade)
        ajuste = {"Emagrecer": -20, "Ganhar massa": 15, "Manutenção": 0}[goal]
        kcal_alvo = tdee_val * (1 + ajuste/100.0)
        agua_l = weight_kg * 35.0 / 1000.0

        st.metric("BMR", f"{bmr:,.0f} kcal/d")
        c1,c2,c3 = st.columns(3)
        c1.metric("TDEE", f"{tdee_val:,.0f} kcal/d")
        c2.metric("Alvo inicial", f"{kcal_alvo:,.0f} kcal/d")
        c3.metric("Água", f"{agua_l:,.2f} L/d")

        semanas = _semanas_para_alvo(weight_kg, target_weight_kg, goal)
        if semanas > 0:
            st.write(f"⏳ Estimativa até a meta: **~{semanas} semanas**.")
            # mini série para gráfico (linear, apenas indicativo)
            if goal == "Emagrecer":
                passo = (weight_kg - target_weight_kg)/max(semanas,1)
                serie = [weight_kg - i*passo for i in range(semanas+1)]
            elif goal == "Ganhar massa":
                passo = (target_weight_kg - weight_kg)/max(semanas,1)
                serie = [weight_kg + i*passo for i in range(semanas+1)]
            else:
                serie = [weight_kg]*(semanas+1)
            df = pd.DataFrame({"Semana": list(range(len(serie))), "Peso (kg)": serie})
            st.line_chart(df, x="Semana", y="Peso (kg)", use_container_width=True)
        else:
            st.info("Você já está na meta — foco em **manter** com constância e follow ups semanais.")

    elif step == 5:
        st.subheader("O que esperar + benefícios")
        if goal == "Emagrecer":
            st.markdown("**Possíveis sintomas:** menos disposição, fome em alguns dias, queda de performance.")
            st.markdown("**Como o app ajuda:** déficit progressivo, hidratação, jejum (opcional), revisão semanal e feedback por dados.")
        elif goal == "Ganhar massa":
            st.markdown("**Possíveis sintomas:** sonolência após refeições, ganho lento na balança, sensação de estufamento.")
            st.markdown("**Como o app ajuda:** divisão de macros, diário e acompanhamento de força/peso.")
        else:
            st.markdown("**Manutenção:** constância e ajustes finos conforme rotina.")

    elif step == 6:
        st.subheader("Curtiu o app?")
        st.caption("Quando publicarmos nas lojas, você verá botões aqui para avaliar 🙂")
        st.button("Deixar para depois", key="rate_later")

    elif step == 7:
        st.subheader("Planos")
        colA, colB = st.columns(2)
        with colA:
            st.markdown("**Mensal**\n\n• Acesso completo\n• Cancelamento simples")
        with colB:
            st.markdown("**Anual**\n\n• Acesso completo\n• **Desconto** equivalente a X meses")
        st.caption("Informativo (MVP). Cobrança não ativada.")

    elif step == 8:
        st.subheader("O que te impede de chegar lá?")
        obstacles = st.text_area("Compartilhe seus obstáculos (tempo, rotina, ansiedade etc.) — isso guia os ajustes.",
                                 value=obstacles, height=100)
        st.info("Você tem potencial. Vamos construir isso em **pequenos passos** — toda semana um pouco melhor.")

    st.divider()
    col_prev, col_next = st.columns(2)
    with col_prev:
        if step > 1 and st.button("← Voltar"):
            st.session_state.ob_step -= 1
            st.rerun()
    with col_next:
        if step < 8:
            if st.button("Próximo →"):
                # salva parciais no estado
                st.session_state.ob_name = full_name
                st.session_state.ob_dob = dob
                st.session_state.ob_sex = sex
                st.session_state.ob_h = height_cm
                st.session_state.ob_w = weight_kg
                st.session_state.ob_act = atividade
                st.session_state.ob_goal = goal
                st.session_state.ob_target = target_weight_kg
                st.session_state.ob_obs = obstacles
                st.session_state.ob_step += 1
                st.rerun()
        else:
            if st.button("Concluir ✅"):
                try:
                    # grava no Supabase
                    supabase.table("profiles").update({
                        "full_name": full_name or None,
                        "dob": str(dob) if dob else None,
                        "sex": sex,
                        "height_cm": float(height_cm) if height_cm else None,
                        "weight_kg": float(weight_kg) if weight_kg else None,
                        "goal": goal,
                        "target_weight_kg": float(target_weight_kg) if target_weight_kg else None,
                        "obstacles": obstacles.strip() or None,
                        "onboarding_done": True,
                    }).eq("id", uid).execute()
                    st.success("Onboarding concluído! Redirecionando…")
                    st.session_state.ob_step = 1
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro ao salvar: {e}")

from streamlit_cookies_manager import EncryptedCookieManager
import time

# Cookies (criptografados pelo lib; ainda assim, use com cautela em PCs compartilhados)
cookies = EncryptedCookieManager(
    prefix="caloria_app_",  # evita conflito com outros apps
    password=st.secrets["COOKIES_PASSWORD"],  # <-- usar a secret aqui
)
if not cookies.ready():
    st.stop()  # aguarda cookies estarem prontos (1º render)

# Restaura sessão se já tem tokens salvos e nenhuma sessão ativa
if not st.session_state.get("sb_session"):
    rt = cookies.get("sb_refresh_token")
    at = cookies.get("sb_access_token")
    if rt and at:
        try:
            # Restaura sessão com tokens salvos
            supabase.auth.set_session(access_token=at, refresh_token=rt)
            sess = supabase.auth.get_session()
            if sess and sess.user:
                st.session_state["sb_session"] = sess
                st.session_state["user_id"] = sess.user.id
                st.session_state["user_email"] = sess.user.email
                # (re)hidrata plano
                from datetime import date
                def _db_get_active_subscription(user_id: str):
                    today = date.today().isoformat()
                    res = (
                        supabase.table("subscriptions")
                        .select("*, plan:plan_id(id, nome, duracao_dias)")
                        .eq("user_id", user_id)
                        .eq("status", "active")
                        .lte("inicio", today)
                        .gt("fim", today)
                        .order("fim", desc=True)
                        .limit(1)
                        .execute()
                    )
                    return res.data[0] if res.data else None
                sub = _db_get_active_subscription(sess.user.id)
                if sub:
                    st.session_state["plan_id"]     = sub["plan"]["id"]
                    st.session_state["plan_name"]   = sub["plan"]["nome"]
                    st.session_state["plan_inicio"] = sub["inicio"]
                    st.session_state["plan_fim"]    = sub["fim"]
                else:
                    st.session_state["plan_id"]     = "DISCIPULO"
                    st.session_state["plan_name"]   = "Discípulo (3 meses)"
                    st.session_state["plan_inicio"] = None
                    st.session_state["plan_fim"]    = None
                # também restaura e-mail salvo pro form
                saved_email = cookies.get("saved_email")
                if saved_email:
                    st.session_state["saved_email"] = saved_email
        except Exception:
            # tokens inválidos/expirados → limpa
            cookies.pop("sb_refresh_token")
            cookies.pop("sb_access_token")
            cookies.pop("saved_email")
            cookies.save()

from pathlib import Path

ASSETS_DIR = Path(__file__).parent / "assets"

def storage_public_url(bucket: str, path: str | None) -> str | None:
    if not path:
        return None
    try:
        res = supabase.storage.from_(bucket).get_public_url(path)
        # supabase-py v2 retorna dict {"data": {"publicUrl": "..."}}
        if isinstance(res, dict):
            data = res.get("data") or {}
            return data.get("publicUrl") or data.get("public_url")
        # (em versões antigas podia vir str direto)
        if isinstance(res, str):
            return res
    except Exception:
        pass
    return None

def signed_url(bucket: str, path: str, expires_sec: int = 3600) -> str | None:
    """Para buckets privados: gera URL temporária."""
    try:
        res = supabase.storage.from_(bucket).create_signed_url(path, expires_sec)
        # supabase-py v2 retorna dict {"data": {"signedUrl": "..."}}
        if isinstance(res, dict):
            data = res.get("data") or {}
            return data.get("signedUrl") or data.get("signedURL") or data.get("signed_url")
        if isinstance(res, str):
            return res
    except Exception:
        pass
    return None

import json, re, requests

def ai_detect_foods_from_image_openrouter(image_url: str) -> list[dict]:
    """
    Chama a API do OpenRouter para identificar alimentos e estimar gramas.
    Retorna: [{"food":"frango grelhado","grams":150,"confidence":0.8}, ...]
    """
    api_key = st.secrets.get("OPENROUTER_API_KEY")
    model = st.secrets.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    if not api_key:
        return []

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://seu-dominio-ou-localhost",  # opcional, mas recomendado
        "X-Title": "CalorIA - Foto Refeição",
        "Content-Type": "application/json",
    }

    system_prompt = (
        "Você é um assistente de nutrição. Dada uma foto de refeição, retorne JSON com "
        "lista de itens no formato: {\"items\":[{\"food\":\"nome\",\"grams\":int,\"confidence\":0-1}]}."
        "Nomes simples (pt-BR). Estime gramas inteiras e confiança (0..1)."
    )
    user_text = (
        "Identifique os principais alimentos visíveis, estime gramas (inteiro) e confiança. "
        "Responda APENAS em JSON válido com a chave 'items'."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                # Muitos modelos aceitam este formato multimodal:
                {"type": "image_url", "image_url": {"url": image_url}}
            ]}
        ],
        "temperature": 0.2,
        # Alguns modelos exigem este campo para JSON:
        # "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers, json=payload, timeout=45
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        # Alguns modelos devolvem JSON puro; outros devolvem texto que contém JSON
        try:
            parsed = json.loads(content)
        except Exception:
            # tenta extrair JSON com regex
            match = re.search(r"\{.*\}", content, flags=re.S)
            parsed = json.loads(match.group(0)) if match else {}

        items = parsed.get("items") or []
        out = []
        for it in items:
            food = str(it.get("food") or "").strip()
            grams = float(it.get("grams") or 0)
            conf  = float(it.get("confidence") or 0)
            if food:
                out.append({"food": food, "grams": max(0.0, grams), "confidence": max(0.0, min(conf, 1.0))})
        return out
    except Exception as e:
        # st.warning(f"Falha IA: {e}")
        return []

# Helpers Supabase ---------------------
def db_get_profile(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
        return res.data
    except Exception:
        return None

def db_upsert_profile(user_id: str, email: str, extra: Dict[str, Any] | None = None) -> Optional[Dict[str, Any]]:
    payload = {"id": user_id, "email": email}
    if extra:
        payload.update(extra)
    try:
        res = supabase.table("profiles").upsert(
            payload,
            on_conflict="id",
            returning="representation"
        ).execute()
        return (res.data[0] if isinstance(res.data, list) and res.data else res.data)
    except Exception as e:
        st.warning(f"Não foi possível upsert profile: {e}")
        return None

def db_get_active_subscription(user_id: str) -> Optional[Dict[str, Any]]:
    """Retorna a assinatura ativa hoje (se houver)."""
    today = date.today().isoformat()
    try:
        res = (
            supabase.table("subscriptions")
            .select("*, plan:plan_id(id, nome, duracao_dias)")  # FK para plans_catalog
            .eq("user_id", user_id)
            .eq("status", "active")
            .lte("inicio", today)
            .gt("fim", today)
            .order("fim", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        st.warning(f"Não foi possível ler assinatura: {e}")
        return None

# --- Helpers para imagens no Storage ---
def storage_public_url(bucket: str, path: str | None) -> str | None:
    if not path:
        return None
    try:
        res = supabase.storage.from_(bucket).get_public_url(path)
        # supabase-py v2 retorna dict {"data": {"publicUrl": "..."}}
        if isinstance(res, dict):
            data = res.get("data") or {}
            return data.get("publicUrl") or data.get("public_url")
        # (em versões antigas podia vir str direto)
        if isinstance(res, str):
            return res
    except Exception:
        pass
    return None

def signed_url(bucket: str, path: str, expires_sec: int = 3600) -> str | None:
    """Para buckets privados: gera URL temporária."""
    try:
        res = supabase.storage.from_(bucket).create_signed_url(path, expires_sec)
        # supabase-py v2 retorna dict {"data": {"signedUrl": "..."}}
        if isinstance(res, dict):
            data = res.get("data") or {}
            return data.get("signedUrl") or data.get("signedURL") or data.get("signed_url")
        if isinstance(res, str):
            return res
    except Exception:
        pass
    return None

def storage_try_extensions(bucket: str, basename: str, exts=(".jpeg", ".jpg", ".png")) -> str | None:
    """Tenta basename + extensão no bucket e retorna a 1ª URL pública encontrada."""
    for ext in exts:
        url = storage_public_url(bucket, f"{basename}{ext}")
        if url:
            return url
    return None

    ASSETS_DIR = Path(__file__).parent / "assets"

def local_img_path(basename: str, exts=(".jpg", ".jpeg", ".png")) -> str | None:
    """Fallback local para assets/"""
    for ext in exts:
        p = ASSETS_DIR / f"{basename}{ext}"
        if p.exists():
            return str(p)
    return None

import os

def storage_try_extensions_safe(bucket: str, basename: str, exts=(".jpg", ".jpeg", ".png")) -> str | None:
    # Suporta subpastas: ex.: "medidas/measure_female"
    folder, name = os.path.split(basename)
    name = name or basename  # se não houver pasta
    try:
        items = supabase.storage.from_(bucket).list(folder or "")
        names = {it.get("name") for it in items or []}
        for ext in exts:
            candidate = f"{name}{ext}"
            if candidate in names:
                path = f"{folder + '/' if folder else ''}{candidate}"
                return storage_public_url(bucket, path)
    except Exception:
        pass
    return None

from supabase import create_client
import streamlit as st

sb = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])

def public_url(bucket: str, path: str | None) -> str | None:
    if not path:
        return None
    return sb.storage.from_(bucket).get_public_url(path)

def signed_url(bucket: str, path: str, expires_sec: int = 3600) -> str | None:
    """Para buckets privados: gera URL temporária."""
    try:
        res = sb.storage.from_(bucket).create_signed_url(path, expires_sec)
        return res.get("signedURL") or res.get("signedUrl") or res.get("signed_url")
    except Exception:
        return None

# Estado base --------------------------
if "sb_session" not in st.session_state:
    st.session_state["sb_session"] = None

# Sidebar Login ------------------------
st.sidebar.header("🔐 Acesso")
auth_mode = st.sidebar.radio("Autenticação", ["Entrar", "Criar conta"], horizontal=True)

session = st.session_state.get("sb_session")
if session:
    uid = session.user.id
    try:
        resp = supabase.table("profiles").select("*").eq("id", uid).single().execute()
        profile = resp.data or {}
    except Exception:
        profile = {}

    if not profile.get("onboarding_done"):
        render_onboarding(uid, profile)
        st.stop()

# === Inputs (UMA vez só) ===
default_email = st.session_state.get("saved_email", "")
email_auth = st.sidebar.text_input("E-mail", value=default_email)
password_auth = st.sidebar.text_input("Senha", type="password")
remember = st.sidebar.checkbox("Lembrar meu login", value=True)

colA, colB = st.sidebar.columns(2)

if auth_mode == "Criar conta":
    if colA.button("Criar conta"):
        try:
            supabase.auth.sign_up({"email": email_auth, "password": password_auth})
            st.sidebar.success("Conta criada! Verifique seu e-mail (se exigido) e depois faça login.")
        except Exception as e:
            st.sidebar.error(f"Erro ao criar conta: {e}")
else:
    if colA.button("Entrar"):
        try:
            res = supabase.auth.sign_in_with_password({"email": email_auth, "password": password_auth})
            st.session_state["sb_session"] = res.session  # pode ser None se exigir confirmação por e-mail
            if st.session_state["sb_session"]:
                st.sidebar.success("Login realizado!")
                st.session_state["user_id"] = res.session.user.id
                st.session_state["user_email"] = res.session.user.email

                # salva perfil uma vez
                db_upsert_profile(st.session_state["user_id"], st.session_state["user_email"])

                # salva tokens/e-mail nos cookies (se marcado)
                if remember and res.session:
                    cookies["sb_refresh_token"] = res.session.refresh_token
                    cookies["sb_access_token"]  = res.session.access_token
                    cookies["saved_email"]      = email_auth
                    cookies.save()

                # hidrata plano
                sub = db_get_active_subscription(st.session_state["user_id"])
                if sub:
                    st.session_state["plan_id"]     = sub["plan"]["id"]
                    st.session_state["plan_name"]   = sub["plan"]["nome"]
                    st.session_state["plan_inicio"] = sub["inicio"]
                    st.session_state["plan_fim"]    = sub["fim"]
                else:
                    st.session_state["plan_id"]     = "DISCIPULO"
                    st.session_state["plan_name"]   = "Discípulo (3 meses)"
                    st.session_state["plan_inicio"] = None
                    st.session_state["plan_fim"]    = None
            else:
                st.sidebar.info("Login pendente (confirme o e-mail, se exigido).")
        except Exception as e:
            st.sidebar.error(f"Erro ao entrar: {e}")

if colB.button("Sair"):
    try:
        supabase.auth.sign_out()
    except Exception:
        pass
    for k in ["sb_session","user_id","user_email","plan_id","plan_name","plan_inicio","plan_fim"]:
        st.session_state.pop(k, None)
    cookies.pop("sb_refresh_token"); cookies.pop("sb_access_token")
    # cookies.pop("saved_email")  # deixe comentado se quiser manter o e-mail preenchido
    cookies.save()
    rt = cookies.get("sb_refresh_token")
    at = cookies.get("sb_access_token")
    st.sidebar.info("Sessão encerrada.")

    cookie_pwd = st.secrets.get("COOKIES_PASSWORD", "dev-cookie-pass-change-me")
    cookies = EncryptedCookieManager(prefix="caloria_app_", password=cookie_pwd)

# Rehidratar só se já há sb_session mas ainda não populou user_id
if st.session_state.get("sb_session") and "user_id" not in st.session_state:
    st.session_state["user_id"] = st.session_state["sb_session"].user.id
    st.session_state["user_email"] = st.session_state["sb_session"].user.email
    db_upsert_profile(st.session_state["user_id"], st.session_state["user_email"])
    sub = db_get_active_subscription(st.session_state["user_id"])
    if sub:
        st.session_state["plan_id"]     = sub["plan"]["id"]
        st.session_state["plan_name"]   = sub["plan"]["nome"]
        st.session_state["plan_inicio"] = sub["inicio"]
        st.session_state["plan_fim"]    = sub["fim"]
    else:
        st.session_state["plan_id"]     = "DISCIPULO"
        st.session_state["plan_name"]   = "Discípulo (3 meses)"
        st.session_state["plan_inicio"] = None
        st.session_state["plan_fim"]    = None

# Cabeçalho da página principal ----------------------------------------------
st.title("🔥 Calorias & Macros (MVP v2)")
st.caption("MVP educativo para estimativas — não substitui avaliação clínica individualizada.")

aba_plano, aba_follow, aba_dash, aba_diario = st.tabs(
    ["📊 Plano diário", "📝 Follow up", "📈 Dashboard", "📒 Diário alimentar"]
)

with aba_diario:
    # ===== JEJUM =====
    st.divider()
    st.subheader("⏳ Jejum intermitente")

    session = st.session_state.get("sb_session")
    if session:
        uid = session.user.id

        fasting_on = st.checkbox("Ativar jejum intermitente")

        if fasting_on:
            colj1, colj2 = st.columns(2)
            with colj1:
                start_time = st.time_input("Início do jejum")
            with colj2:
                end_time = st.time_input("Fim do jejum (opcional)", value=None)

            if st.button("Salvar jejum"):
                import datetime as dt

                today = dt.date.today()
                start_dt = dt.datetime.combine(today, start_time)
                end_dt = dt.datetime.combine(today, end_time) if end_time else None
                try:
                    supabase.table("fasting_log").insert(
                        {
                            "user_id": uid,
                            "start_time": start_dt.isoformat(),
                            "end_time": end_dt.isoformat() if end_dt else None,
                        }
                    ).execute()
                    st.success("Jejum salvo!")
                except Exception as e:
                    st.error(f"Erro ao salvar jejum: {e}")

            st.markdown("### Histórico de jejuns")
            try:
                resp = (
                    supabase.table("fasting_log")
                    .select("*")
                    .eq("user_id", uid)
                    .order("start_time", desc=True)
                    .limit(10)
                    .execute()
                )
                rows = resp.data or []
                if rows:
                    import pandas as pd

                    df = pd.DataFrame(rows)
                    df["start_time"] = pd.to_datetime(df["start_time"])
                    df["end_time"] = pd.to_datetime(df["end_time"])
                    df["duração (h)"] = (
                        (df["end_time"] - df["start_time"]).dt.total_seconds() / 3600
                    ).round(1)
                    st.dataframe(
                        df[["start_time", "end_time", "duração (h)"]],
                        use_container_width=True,
                    )
                else:
                    st.caption("Nenhum jejum registrado ainda.")
            except Exception as e:
                st.warning(f"Não foi possível carregar os jejuns: {e}")

            with st.expander("Protocolos comuns de jejum"):
                st.markdown(
                    """
                - **16/8** → jejum de 16h, janela de alimentação 8h (mais popular)  
                - **14/10** → mais flexível, bom para iniciantes  
                - **20/4 (Warrior Diet)** → jejum de 20h, alimentação em 4h  
                - **24h (1–2x por semana)** → usado em contextos avançados  

                🔑 *Dicas:*  
                - Mantenha hidratação adequada durante o jejum (água, café, chá sem açúcar).  
                - Evite exageros na janela de alimentação.  
                - Sempre ajuste ao seu contexto de treino/objetivo.  
                """
                )
    else:
        st.info("Faça login para registrar seu jejum.")

    # ===== DIÁRIO ALIMENTAR =====
    st.subheader("📒 Diário alimentar")

    session = st.session_state.get("sb_session")
    if not session:
        st.info("Faça login para registrar e visualizar seu diário.")
    else:
        uid = session.user.id

        # Seleção de data
        col_d1, col_d2 = st.columns([1, 2])
        with col_d1:
            ref_date = st.date_input("Data", value=datetime.today().date())
        with col_d2:
            st.caption("Atalho por dia da semana")
            dia_semana = st.radio(
                "Dias",
                ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"],
                horizontal=True,
                label_visibility="collapsed",
            )
            if st.button("Ir para último registro deste dia da semana"):
                try:
                    map_pg = {
                        "Segunda": 1,
                        "Terça": 2,
                        "Quarta": 3,
                        "Quinta": 4,
                        "Sexta": 5,
                        "Sábado": 6,
                        "Domingo": 7,
                    }
                    resp_last = supabase.rpc(
                        "exec_sql",
                        {
                            "sql": f"""
                            select ref_date
                              from public.food_diary
                             where user_id = '{uid}'
                               and extract(isodow from ref_date) = {map_pg[dia_semana]}
                          order by ref_date desc
                             limit 1
                        """
                        },
                    ).execute()
                except Exception:
                    pass

        st.divider()

        # ===== Alimento rápido (offline) =====
        st.markdown("#### ⚡ Adicionar alimento rápido (offline)")
        _LOCAL_DB = {
            # kcal, p, c, f por 100 g (aprox. cozidos / uso comum BR)
            "frango grelhado":   {"kcal":165, "p":31.0, "c":0.0,  "f":3.6},
            "arroz branco":      {"kcal":130, "p":2.7,  "c":28.0, "f":0.3},
            "arroz integral":    {"kcal":111, "p":2.6,  "c":23.0, "f":0.9},
            "feijão cozido":     {"kcal":95,  "p":6.0,  "c":17.0, "f":0.5},
            "batata doce coz.":  {"kcal":86,  "p":1.6,  "c":20.0, "f":0.1},
            "ovo cozido":        {"kcal":155, "p":13.0, "c":1.1,  "f":11.0},
            "aveia (flocos)":    {"kcal":389, "p":16.9, "c":66.0, "f":6.9},
            "abacate":           {"kcal":160, "p":2.0,  "c":9.0,  "f":15.0},
            "banana prata":      {"kcal":89,  "p":1.1,  "c":23.0, "f":0.3},
            "pão francês":       {"kcal":270, "p":9.0,  "c":57.0, "f":3.0},
        }

        colq1, colq2, colq3 = st.columns([2,1,1])
        with colq1:
            food_q = st.selectbox("Alimento", sorted(_LOCAL_DB.keys()))
        with colq2:
            grams_q = st.number_input("Gramas", min_value=0.0, step=5.0, value=100.0)
        with colq3:
            meal_q = st.selectbox("Refeição", ["Café da manhã","Almoço","Jantar","Lanche","Pré-treino","Pós-treino","Outra"], index=1)

        if st.button("➕ Adicionar alimento rápido"):
            info = _LOCAL_DB.get(food_q)
            factor = grams_q / 100.0
            kcal_q = info["kcal"] * factor
            p_q = info["p"] * factor
            c_q = info["c"] * factor
            f_q = info["f"] * factor
            try:
                supabase.table("food_diary").insert({
                    "user_id": uid,
                    "ref_date": str(ref_date),
                    "meal_type": meal_q,
                    "description": food_q,
                    "qty_g": float(grams_q),
                    "kcal": float(kcal_q),
                    "protein_g": float(p_q),
                    "carbs_g": float(c_q),
                    "fat_g": float(f_q),
                    "photo_path": None,  # se quiser, pode reaproveitar a foto de prato
                }).execute()
                st.success(f"{food_q} adicionado!")
            except Exception as e:
                st.error(f"Erro ao salvar: {e}")

        # ====== IA: Analisar foto do prato (beta) ======
        if st.secrets.get("ENABLE_AI", "false").lower() == "true" and st.secrets.get("OPENROUTER_API_KEY"):
            st.markdown("#### 🤖 Analisar foto do prato (beta)")

            # Padrão: revisão (auto desativado)
            auto_mode = st.checkbox("Analisar e salvar automaticamente (sem revisão)", value=False)

            # 1) entrada de imagem: câmera OU upload
            cam_pic = st.camera_input("Tirar foto do prato (opcional)")
            ai_file = st.file_uploader(
                "…ou enviar foto da galeria",
                type=["jpg", "jpeg", "png"],
                accept_multiple_files=False,
                key="ai_meal_photo",
            )

            # Prioridade: câmera > upload
            img_src_file = cam_pic if cam_pic is not None else ai_file

            # Função interna: processa IA e salva (auto) ou exibe editor (revisão)
            def _process_and_save(img_url: str, ai_path: str, ref_date, uid, auto: bool):
                with st.spinner("Analisando imagem com IA..."):
                    items = ai_detect_foods_from_image_openrouter(img_url)

                if not items:
                    st.warning("Não consegui identificar nada com confiança suficiente. Tente outra foto/ângulo/luz.")
                    return

                enriched = []
                for it in items:
                    per100 = lookup_macros_per_100g(it["food"])
                    grams = it["grams"]
                    conf = it["confidence"]
                    if per100:
                        mac = scale_macros(per100, grams)
                        enriched.append(
                            {
                                "Alimento": it["food"],
                                "Gramas": round(grams, 0),
                                "Kcal": round(mac["kcal"], 0),
                                "Prot (g)": round(mac["p"], 1),
                                "Carb (g)": round(mac["c"], 1),
                                "Gord (g)": round(mac["f"], 1),
                                "Confiança": round(conf, 2),
                            }
                        )
                    else:
                        enriched.append(
                            {
                                "Alimento": it["food"],
                                "Gramas": round(grams, 0),
                                "Kcal": None,
                                "Prot (g)": None,
                                "Carb (g)": None,
                                "Gord (g)": None,
                                "Confiança": round(conf, 2),
                            }
                        )

                import pandas as pd
                df_ai = pd.DataFrame(enriched)

                if auto:
                    # === AUTO: salva direto no diário ===
                    try:
                        rows_to_insert = []
                        for _, r in df_ai.iterrows():
                            rows_to_insert.append(
                                {
                                    "user_id": uid,
                                    "ref_date": str(ref_date),
                                    "meal_type": "IA (auto)",
                                    "description": r["Alimento"],
                                    "qty_g": float(r["Gramas"]) if pd.notnull(r["Gramas"]) else None,
                                    "kcal": float(r["Kcal"]) if pd.notnull(r["Kcal"]) else None,
                                    "protein_g": float(r["Prot (g)"]) if pd.notnull(r["Prot (g)"]) else None,
                                    "carbs_g": float(r["Carb (g)"]) if pd.notnull(r["Carb (g)"]) else None,
                                    "fat_g": float(r["Gord (g)"]) if pd.notnull(r["Gord (g)"]) else None,
                                    "photo_path": ai_path,
                                }
                            )
                        if rows_to_insert:
                            supabase.table("food_diary").insert(rows_to_insert).execute()
                            tot_k = float((df_ai["Kcal"].fillna(0)).sum())
                            tot_p = float((df_ai["Prot (g)"].fillna(0)).sum())
                            tot_c = float((df_ai["Carb (g)"].fillna(0)).sum())
                            tot_f = float((df_ai["Gord (g)"].fillna(0)).sum())
                            st.success(f"Itens adicionados automaticamente: {len(rows_to_insert)}")
                            st.caption(
                                f"Totais estimados — Kcal {tot_k:.0f} • P {tot_p:.0f} g • C {tot_c:.0f} g • G {tot_f:.0f} g"
                            )
                    except Exception as e:
                        st.error(f"Erro ao salvar (auto): {e}")
                    return

                # === REVISÃO: editor + botão salvar ===
                st.markdown("**Revise/ajuste antes de salvar:**")
                edited = st.data_editor(
                    df_ai,
                    use_container_width=True,
                    num_rows="dynamic",
                    key="ai_meal_editor",
                    column_config={
                        "Alimento": st.column_config.TextColumn(width="medium"),
                        "Gramas": st.column_config.NumberColumn(min_value=0, step=5),
                        "Kcal": st.column_config.NumberColumn(step=5),
                        "Prot (g)": st.column_config.NumberColumn(step=0.5),
                        "Carb (g)": st.column_config.NumberColumn(step=0.5),
                        "Gord (g)": st.column_config.NumberColumn(step=0.5),
                        "Confiança": st.column_config.NumberColumn(min_value=0, max_value=1, step=0.01, disabled=True),
                    },
                )

                tot_k = float((edited["Kcal"].fillna(0)).sum())
                tot_p = float((edited["Prot (g)"].fillna(0)).sum())
                tot_c = float((edited["Carb (g)"].fillna(0)).sum())
                tot_f = float((edited["Gord (g)"].fillna(0)).sum())
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Kcal (estim.)", f"{tot_k:,.0f}")
                c2.metric("Prot (g)", f"{tot_p:,.0f}")
                c3.metric("Carb (g)", f"{tot_c:,.0f}")
                c4.metric("Gord (g)", f"{tot_f:,.0f}")

                if st.button("✅ Adicionar itens ao diário (esta data)"):
                    try:
                        rows_to_insert = []
                        for _, r in edited.iterrows():
                            rows_to_insert.append(
                                {
                                    "user_id": uid,
                                    "ref_date": str(ref_date),
                                    "meal_type": "IA (estimativa)",
                                    "description": r["Alimento"],
                                    "qty_g": float(r["Gramas"]) if pd.notnull(r["Gramas"]) else None,
                                    "kcal": float(r["Kcal"]) if pd.notnull(r["Kcal"]) else None,
                                    "protein_g": float(r["Prot (g)"]) if pd.notnull(r["Prot (g)"]) else None,
                                    "carbs_g": float(r["Carb (g)"]) if pd.notnull(r["Carb (g)"]) else None,
                                    "fat_g": float(r["Gord (g)"]) if pd.notnull(r["Gord (g)"]) else None,
                                    "photo_path": ai_path,
                                }
                            )
                        if rows_to_insert:
                            supabase.table("food_diary").insert(rows_to_insert).execute()
                            st.success("Itens adicionados ao diário! Role a página para ver a listagem do dia.")
                    except Exception as e:
                        st.error(f"Erro ao salvar no diário: {e}")

            # 2) se houver imagem (câmera ou upload), sobe pro Storage e processa
            if img_src_file is not None:
                import io
                import datetime as _dt

                # nome seguro e caminho
                y_m = _dt.datetime.now().strftime("%Y-%m")
                d_hms = _dt.datetime.now().strftime("%d-%H%M%S")
                has_name = hasattr(img_src_file, "name") and img_src_file.name
                safe_name = (img_src_file.name if has_name else "camera.jpg").replace(" ", "_").lower()
                ai_path = f"{uid}/ai-meals/{y_m}/{d_hms}-{safe_name}"

                # bytes: camera_input usa getvalue(); uploader tem .read() (mas Streamlit normaliza .getvalue())
                try:
                    file_bytes = img_src_file.getvalue() if hasattr(img_src_file, "getvalue") else img_src_file.read()
                except Exception:
                    file_bytes = None

                img_url = None
                try:
                    supabase.storage.from_("progress-photos").upload(
                        path=ai_path,
                        file=io.BytesIO(file_bytes),
                        file_options={"contentType": "image/jpeg", "upsert": False},
                    )
                    signed = supabase.storage.from_("progress-photos").create_signed_url(ai_path, 3600)
                    img_url = signed.get("signedURL") or signed.get("signed_url")
                except Exception as e:
                    st.error(f"Falha ao subir/assinar a imagem: {e}")

                if img_url:
                    # AUTO -> processa imediatamente; REVISÃO -> pede clique
                    if auto_mode:
                        _process_and_save(img_url, ai_path, ref_date, uid, auto=True)
                    else:
                        if st.button("Analisar com IA"):
                            _process_and_save(img_url, ai_path, ref_date, uid, auto=False)
        else:
            st.caption("IA de foto desativada (sem custos). Para ativar, defina ENABLE_AI='true' e informe OPENROUTER_API_KEY em secrets.toml.")

        # ===== FORMULÁRIO =====
        with st.form("food_form"):
            c_top1, c_top2 = st.columns(2)
            with c_top1:
                meal_type = st.selectbox(
                    "Refeição",
                    [
                        "Café da manhã",
                        "Almoço",
                        "Jantar",
                        "Lanche",
                        "Pré-treino",
                        "Pós-treino",
                        "Outra",
                    ],
                    index=1,
                )
            with c_top2:
                qty_g = st.number_input(
                    "Quantidade (g) — opcional", min_value=0.0, step=1.0, value=0.0
                )

            description = st.text_area(
                "O que você comeu?",
                placeholder="Ex.: 150g frango, 120g arroz, salada...",
            )

            c_mac1, c_mac2, c_mac3, c_kcal = st.columns(4)
            with c_mac1:
                protein_g = st.number_input(
                    "Proteína (g)", min_value=0.0, step=1.0, value=0.0
                )
            with c_mac2:
                carbs_g = st.number_input(
                    "Carboidratos (g)", min_value=0.0, step=1.0, value=0.0
                )
            with c_mac3:
                fat_g = st.number_input(
                    "Gorduras (g)", min_value=0.0, step=0.5, value=0.0
                )
            with c_kcal:
                kcal = st.number_input(
                    "Kcal (opcional)",
                    min_value=0.0,
                    step=1.0,
                    value=0.0,
                    help="Se deixar 0, calculo automático: 4p + 4c + 9g",
                )

            # FOTO do prato (agora dentro do form)
            photo_file = st.file_uploader(
                "Foto do prato (opcional)",
                type=["png", "jpg", "jpeg"],
                accept_multiple_files=False,
                key="meal_photo",
            )

            add_meal = st.form_submit_button("➕ Adicionar refeição")

        # ===== SALVAR =====
        if add_meal:
            kcal_val = (
                float(kcal)
                if kcal and kcal > 0
                else (protein_g * 4 + carbs_g * 4 + fat_g * 9)
            )
            photo_path = None
            try:
                if photo_file is not None:
                    import datetime as _dt

                    y_m = _dt.datetime.now().strftime("%Y-%m")
                    d_hms = _dt.datetime.now().strftime("%d-%H%M%S")
                    safe_name = photo_file.name.replace(" ", "_").lower()
                    photo_path = f"{uid}/meals/{y_m}/{d_hms}-{safe_name}"
                    supabase.storage.from_("progress-photos").upload(
                        path=photo_path,
                        file=photo_file,
                        file_options={
                            "contentType": photo_file.type or "image/jpeg",
                            "upsert": False,
                        },
                    )
            except Exception as e:
                st.warning(f"Falha ao subir a foto do prato: {e}")

            try:
                supabase.table("food_diary").insert(
                    {
                        "user_id": uid,
                        "ref_date": str(ref_date),
                        "meal_type": meal_type,
                        "description": description.strip() or None,
                        "qty_g": float(qty_g) if qty_g else None,
                        "kcal": float(kcal_val) if kcal_val else None,
                        "protein_g": float(protein_g) if protein_g else None,
                        "carbs_g": float(carbs_g) if carbs_g else None,
                        "fat_g": float(fat_g) if fat_g else None,
                        "photo_path": photo_path,
                    }
                ).execute()
                st.success("Refeição adicionada!")
            except Exception as e:
                st.error(f"Erro ao salvar refeição: {e}")

        # ===== LISTAGEM =====
        try:
            resp = (
                supabase.table("food_diary")
                .select("*")
                .eq("user_id", uid)
                .eq("ref_date", str(ref_date))
                .order("created_at", desc=False)
                .execute()
            )
            rows = resp.data or []
        except Exception as e:
            rows = []
            st.error(f"Erro ao carregar diário: {e}")

        if not rows:
            st.caption("Nenhuma refeição registrada para esta data.")
        else:
            import pandas as pd

            df = pd.DataFrame(rows)

            # Totais
            total_kcal = float(df["kcal"].fillna(0).sum())
            total_p = float(df["protein_g"].fillna(0).sum())
            total_c = float(df["carbs_g"].fillna(0).sum())
            total_f = float(df["fat_g"].fillna(0).sum())

            st.markdown("### Total do dia")
            c_tot1, c_tot2, c_tot3, c_tot4 = st.columns(4)
            c_tot1.metric("Kcal", f"{total_kcal:,.0f}")
            c_tot2.metric("Proteína", f"{total_p:,.0f} g")
            c_tot3.metric("Carbo", f"{total_c:,.0f} g")
            c_tot4.metric("Gordura", f"{total_f:,.0f} g")

            # Progresso vs metas
            kcal_meta = st.session_state.get("kcal_alvo")
            p_meta = st.session_state.get("prot_g")
            c_meta = st.session_state.get("carb_g")
            f_meta = st.session_state.get("gord_g")
            if all(v is not None for v in [kcal_meta, p_meta, c_meta, f_meta]):
                st.markdown("#### Progresso vs meta do dia")
                st.progress(
                    min(total_kcal / kcal_meta, 1.0),
                    text=f"Kcal: {int(total_kcal)}/{int(kcal_meta)}",
                )
                st.progress(
                    min(total_p / p_meta, 1.0),
                    text=f"Proteína: {int(total_p)}/{int(p_meta)} g",
                )
                st.progress(
                    min(total_c / c_meta, 1.0),
                    text=f"Carbo: {int(total_c)}/{int(c_meta)} g",
                )
                st.progress(
                    min(total_f / f_meta, 1.0),
                    text=f"Gordura: {int(total_f)}/{int(f_meta)} g",
                )

            st.markdown("### Refeições")
            show_df = df[
                [
                    "created_at",
                    "meal_type",
                    "description",
                    "qty_g",
                    "kcal",
                    "protein_g",
                    "carbs_g",
                    "fat_g",
                ]
            ].copy()
            show_df.rename(
                columns={
                    "created_at": "Quando",
                    "meal_type": "Refeição",
                    "description": "Descrição",
                    "qty_g": "Qtd (g)",
                    "kcal": "Kcal",
                    "protein_g": "Prot (g)",
                    "carbs_g": "Carb (g)",
                    "fat_g": "Gord (g)",
                },
                inplace=True,
            )
            st.dataframe(show_df, use_container_width=True)

            # Fotos
            st.markdown("#### Fotos das refeições do dia")
            thumbs = [r for r in rows if r.get("photo_path")]
            if not thumbs:
                st.caption("Nenhuma foto enviada hoje.")
            else:
                cols = st.columns(3)
                for i, r in enumerate(thumbs):
                    try:
                        signed = supabase.storage.from_(
                            "progress-photos"
                        ).create_signed_url(r["photo_path"], 3600)
                        url = signed.get("signedURL") or signed.get("signed_url")
                        if url:
                            with cols[i % 3]:
                                _show_image(url)
                                st.caption(f"{r['meal_type']} — {r['created_at'][:16]}")

                    except Exception as e:
                        st.warning(
                            f"Não foi possível exibir a foto de {r.get('meal_type','?')}: {e}"
                        )

            # Deletar
            with st.expander("Apagar alguma refeição?"):
                ids = [
                    (
                        r["id"],
                        f'{r["meal_type"]} - {r.get("description","")} ({r["created_at"][:16]})',
                    )
                    for r in rows
                ]
                if ids:
                    sel = st.selectbox(
                        "Selecione para apagar", ids, format_func=lambda x: x[1]
                    )
                    if st.button("🗑️ Apagar selecionado"):
                        try:
                            supabase.table("food_diary").delete().eq(
                                "id", sel[0]
                            ).execute()
                            st.success(
                                "Apagado. Atualize a página para ver a lista atualizada."
                            )
                        except Exception as e:
                            st.error(f"Erro ao apagar: {e}")

with aba_dash:
    st.subheader("Evolução do peso corporal")

    session = st.session_state.get("sb_session")
    if not session:
        st.info("Faça login para visualizar seu dashboard.")
    else:
        uid = session.user.id

        try:
            # pega os followups com peso
            resp = (
                supabase.table("followups")
                .select("ref_date, weight_kg")
                .eq("user_id", uid)
                .not_.is_("weight_kg", "null")
                .order("ref_date", desc=False)
                .execute()
            )

            rows = resp.data or []
            if not rows:
                st.caption(
                    "Ainda não há pesos registrados. Preencha o peso no Follow up."
                )
            else:
                import pandas as pd

                df = pd.DataFrame(rows)
                # converte datas e filtra válidos
                df["ref_date"] = pd.to_datetime(df["ref_date"]).dt.date
                df = df.dropna(subset=["weight_kg"])
                df = df.sort_values("ref_date")

                # métricas rápidas
                atual = float(df["weight_kg"].iloc[-1])
                primeiro = float(df["weight_kg"].iloc[0])
                delta = atual - primeiro
                m1, m2, m3 = st.columns(3)
                m1.metric("Peso atual", f"{atual:,.1f} kg")
                m2.metric("Peso inicial", f"{primeiro:,.1f} kg")
                m3.metric("Variação", f"{delta:+.1f} kg")

                st.markdown("### Gráfico")
                # gráfico simples
                st.line_chart(data=df.set_index("ref_date")["weight_kg"], height=300)

                # tabela opcional
                with st.expander("Ver dados"):
                    st.dataframe(
                        df.rename(
                            columns={"ref_date": "Data", "weight_kg": "Peso (kg)"}
                        ),
                        use_container_width=True,
                    )
        except Exception as e:
            st.error(f"Erro ao carregar dashboard: {e}")

with aba_follow:
    st.subheader("Check‑in semanal")

    session = st.session_state.get("sb_session")
    if not session:
        st.info("Faça login para registrar e visualizar seus follow ups.")
    else:
        uid = session.user.id

        with st.form("follow_form"):
            col0 = st.columns(2)
            with col0[0]:
                ref_date = st.date_input(
                    "Data do check‑in", value=datetime.today().date()
                )
                weight_kg = st.number_input(
                    "Peso corporal da semana (kg)",
                    min_value=30.0,
                    max_value=300.0,
                    step=0.1,
                )
            with col0[1]:
                st.caption("0 = pior / 10 = excelente")

            # notas 0–10
            c1, c2 = st.columns(2)

            with c1:
                sleep = st.slider("Sono", 0, 10, 7)
                bowel = st.slider("Intestino", 0, 10, 7)
                hunger = st.slider("Fome", 0, 10, 5)
                motivation = st.slider("Motivação", 0, 10, 7)

            with c2:
                stress = st.slider("Estresse", 0, 10, 4)
                anxiety = st.slider("Ansiedade", 0, 10, 4)
                adherence = st.slider("Adesão / Constância", 0, 10, 7)

            st.markdown("**Comentários (opcional)**")
            notes_sleep = st.text_area("Sono — observações", height=70)
            notes_bowel = st.text_area("Intestino — observações", height=70)
            notes_hunger = st.text_area("Fome — observações", height=70)
            notes_motivation = st.text_area("Motivação — observações", height=70)
            notes_stress = st.text_area("Estresse — observações", height=70)
            notes_anxiety = st.text_area("Ansiedade — observações", height=70)
            notes_adherence = st.text_area("Adesão — observações", height=70)

            submitted = st.form_submit_button("Salvar follow up")

        if submitted:
            try:
                payload = {
                    "user_id": uid,
                    "ref_date": str(ref_date),
                    "weight_kg": float(weight_kg) if weight_kg else None,
                    "sleep": sleep,
                    "bowel": bowel,
                    "hunger": hunger,
                    "motivation": motivation,
                    "stress": stress,
                    "anxiety": anxiety,
                    "adherence": adherence,
                    "notes_sleep": notes_sleep.strip() or None,
                    "notes_bowel": notes_bowel.strip() or None,
                    "notes_hunger": notes_hunger.strip() or None,
                    "notes_motivation": notes_motivation.strip() or None,
                    "notes_stress": notes_stress.strip() or None,
                    "notes_anxiety": notes_anxiety.strip() or None,
                    "notes_adherence": notes_adherence.strip() or None,
                }
                supabase.table("followups").insert(payload).execute()
                st.success("Follow up salvo com sucesso!")
            except Exception as e:
                st.error(f"Erro ao salvar follow up: {e}")

        st.divider()
        st.subheader("Seus últimos follow ups")
        try:
            resp = (
                supabase.table("followups")
                .select("*")
                .eq("user_id", uid)
                .order("ref_date", desc=True)
                .limit(20)
                .execute()
            )

            rows = resp.data or []
            if not rows:
                st.caption("Ainda não há registros.")
            else:
                # Mostrar em tabela legível
                import pandas as pd

                df = pd.DataFrame(rows)
                # ordenar colunas
                cols_order = [
                    "ref_date",
                    "weight_kg",
                    "sleep",
                    "bowel",
                    "hunger",
                    "motivation",
                    "stress",
                    "anxiety",
                    "adherence",
                    "notes_sleep",
                    "notes_bowel",
                    "notes_hunger",
                    "notes_motivation",
                    "notes_stress",
                    "notes_anxiety",
                    "notes_adherence",
                    "created_at",
                    "id",
                ]
                df = df[[c for c in cols_order if c in df.columns]]
                st.dataframe(df, use_container_width=True)
        except Exception as e:
            st.warning(f"Não foi possível listar: {e}")

        # ===== MEDIDAS =====
        st.divider()
        st.subheader("📏 Medidas corporais")
        
        with st.expander("Orientações e exemplos", expanded=True):
            st.markdown(
                "**Use fita métrica apertando levemente na pele, nas posições indicadas na imagem.**\n\n"
                "Repita o processo **semanalmente** ou a cada **15 dias** para comparar."
            )
        
            sexo_ref = st.radio("Ver exemplo para:", ["Masculino", "Feminino"], horizontal=True)
        
            if sexo_ref == "Masculino":
                img = storage_public_url("guides", "measure_male.jpg") or local_img_path("measure_male")
            else:
                img = storage_public_url("guides", "measure_female.jpeg") or local_img_path("measure_female")
        
            _show_image(img)
        
        # === Formulário de medidas (fora do else, sempre visível) ===
        with st.form("measure_form"):
            colA, colB, colC = st.columns(3)
            with colA:
                m_date = st.date_input("Data da medição", value=datetime.today().date())
                chest_cm = st.number_input("Peito/Tórax (cm)", min_value=0.0, step=0.1)
                arm_cm = st.number_input("Braço (cm)", min_value=0.0, step=0.1)
            with colB:
                waist_cm = st.number_input("Cintura (cm)", min_value=0.0, step=0.1)
                abdomen_cm = st.number_input("Abdômen (cm)", min_value=0.0, step=0.1)
                hip_cm = st.number_input("Quadril (cm)", min_value=0.0, step=0.1)
            with colC:
                thigh_cm = st.number_input("Coxa (cm)", min_value=0.0, step=0.1)
                calf_cm = st.number_input("Panturrilha (cm)", min_value=0.0, step=0.1)
                st.caption("Padronize o lado (ex.: sempre o direito).")
        
            save_meas = st.form_submit_button("💾 Salvar medidas")
        
        if save_meas:
            try:
                supabase.table("measurements").insert(
                    {
                        "user_id": uid,
                        "ref_date": str(m_date),
                        "chest_cm": chest_cm or None,
                        "arm_cm": arm_cm or None,
                        "waist_cm": waist_cm or None,
                        "abdomen_cm": abdomen_cm or None,
                        "hip_cm": hip_cm or None,
                        "thigh_cm": thigh_cm or None,
                        "calf_cm": calf_cm or None,
                    }
                ).execute()
                st.success("Medidas salvas!")
            except Exception as e:
                st.error(f"Erro ao salvar medidas: {e}")
        
        # === Listagem + deltas ===
        st.markdown("### Suas últimas medidas")
        try:
            resp = (
                supabase.table("measurements")
                .select("*")
                .eq("user_id", uid)
                .order("ref_date", desc=True)
                .limit(12)
                .execute()
            )
            ms = resp.data or []
            if not ms:
                st.caption("Ainda não há medições registradas.")
            else:
                import pandas as pd
                dfm = pd.DataFrame(ms)
                dfm["ref_date"] = pd.to_datetime(dfm["ref_date"]).dt.date
                dfm = dfm.sort_values("ref_date")
                for col in ["chest_cm","arm_cm","waist_cm","abdomen_cm","hip_cm","thigh_cm","calf_cm"]:
                    if col in dfm.columns:
                        dfm[f"Δ {col.replace('_cm','')}"] = dfm[col].diff().round(1)
                dfm = dfm.sort_values("ref_date", ascending=False)
                cols_show = [c for c in [
                    "ref_date","chest_cm","arm_cm","waist_cm","abdomen_cm","hip_cm","thigh_cm","calf_cm",
                    "Δ chest","Δ arm","Δ waist","Δ abdomen","Δ hip","Δ thigh","Δ calf"
                ] if c in dfm.columns]
                st.dataframe(dfm[cols_show], use_container_width=True)
        except Exception as e:
            st.warning(f"Não foi possível listar medidas: {e}")

        # ... insert no Supabase e listagem da tabela aqui ...

        # -------- ORIENTAÇÕES + EXEMPLOS (fica DENTRO da aba_follow) --------
        from pathlib import Path

        ASSETS_DIR = Path(__file__).parent / "assets"

        def img_path(basename: str):
            for ext in (".jpg", ".jpeg", ".png"):
                p = ASSETS_DIR / f"{basename}{ext}"
                if p.exists():
                    return str(p)
            return None

        female_img = img_path("example_female")
        male_img = img_path("example_male")

        st.divider()
        st.subheader("📸 Fotos de progresso (1x/mês)")
        
        with st.expander("Orientações e exemplos"):
            st.markdown(
                "**Tente tirar as fotos sempre no mesmo local, com a mesma iluminação e roupa, para melhor comparação.**\n\n"
                "Atualize as fotos e medidas a cada **15 ou 30 dias**."
            )
        
            sexo_exemplo = st.radio("Ver exemplo para:", ["Feminino", "Masculino"], horizontal=True)
        
            if sexo_exemplo == "Feminino":
                img = storage_public_url("guides", "example_female.jpeg") or local_img_path("example_female")
            else:
                img = storage_public_url("guides", "example_male.jpeg")   or local_img_path("example_male")
        
            _show_image(img, caption="Exemplo: frente • perfil • costas")

        # -------- Upload e listagem de fotos (também DENTRO da aba_follow) --------
        files = st.file_uploader(
            "Envie suas fotos (PNG/JPG/JPEG)",
            type=["png", "jpg", "jpeg"],
            accept_multiple_files=True,
        )
        if files:
            import datetime as _dt

            for f in files:
                try:
                    y_m = _dt.datetime.now().strftime("%Y-%m")
                    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = f"{uid}/{y_m}/{ts}-{f.name}".replace(" ", "_").lower()
                    supabase.storage.from_("progress-photos").upload(
                        path=path,
                        file=f,
                        file_options={
                            "contentType": f.type or "image/jpeg",
                            "upsert": False,
                        },
                    )
                    st.success(f"Enviado: {f.name}")
                except Exception as e:
                    st.error(f"Falha ao enviar {f.name}: {e}")

        st.markdown("### Suas fotos")
        try:
            root_items = supabase.storage.from_("progress-photos").list(path=uid)
            if not root_items:
                st.caption("Ainda não há fotos enviadas.")
            else:
                for folder in sorted(root_items, key=lambda x: x.get("name", "")):
                    month_path = f"{uid}/{folder['name']}"
                    month_items = (
                        supabase.storage.from_("progress-photos").list(path=month_path)
                        or []
                    )
                    if not month_items:
                        continue
                    st.markdown(f"**{folder['name']}**")
                    cols = st.columns(3)
                    for i, item in enumerate(
                        sorted(month_items, key=lambda x: x.get("name", ""))
                    ):
                        full_path = f"{month_path}/{item['name']}"
                        signed = supabase.storage.from_(
                            "progress-photos"
                        ).create_signed_url(full_path, 3600)
                        url = signed.get("signedURL") or signed.get("signed_url")
                        if url:
                            with cols[i % 3]:
                                _show_image(url)
                                st.caption(item["name"])

        except Exception as e:
            st.warning(f"Não foi possível listar as fotos: {e}")

with aba_plano:
    with st.form("dados_basicos"):
        st.subheader("1) Dados básicos")
        col1, col2 = st.columns(2)

        with col1:
            peso = st.number_input(
                "Peso (kg)", min_value=30.0, max_value=300.0, step=0.1, value=75.0
            )
            altura = st.number_input(
                "Altura (cm)", min_value=120.0, max_value=230.0, step=0.5, value=175.0
            )
            idade = st.number_input(
                "Idade (anos)", min_value=14, max_value=100, step=1, value=30
            )

        with col2:
            sexo = st.selectbox("Sexo", ["Masculino", "Feminino"])
            atividade = st.selectbox(
                "Nível de atividade",
                [
                    "Sedentário (pouco ou nenhum exercício)",
                    "Leve (1–3x/semana)",
                    "Moderado (3–5x/semana)",
                    "Alto (6–7x/semana)",
                    "Atleta/Extremo (2x/dia)",
                ],
            )
            email = st.text_input("E-mail (opcional, protótipo)")

        st.subheader("2) Objetivo calórico")
        objetivo = st.selectbox(
            "Selecione o objetivo",
            ["Cut (déficit)", "Manutenção", "Bulk (superávit)"],
            index=1,
        )
        ajuste_padrao = {"Cut (déficit)": -20, "Manutenção": 0, "Bulk (superávit)": 15}[
            objetivo
        ]
        ajuste_percent = st.slider(
            "Ajuste calórico (%)",
            min_value=-40,
            max_value=40,
            value=ajuste_padrao,
            step=1,
            help="Percentual aplicado sobre as calorias de manutenção (TDEE).",
        )

        st.subheader("3) Definição de Macros")
        metodo_macros = st.radio("Como definir?", ["Por g/kg", "Por %"], index=0)
        if metodo_macros == "Por g/kg":
            colp, colf = st.columns(2)
            with colp:
                p_gkg = st.number_input(
                    "Proteína (g/kg)", min_value=0.5, max_value=3.0, value=2.0, step=0.1
                )
            with colf:
                f_gkg = st.number_input(
                    "Gordura (g/kg)", min_value=0.2, max_value=2.0, value=0.8, step=0.05
                )
            c_gkg = None
            p_pct = c_pct = f_pct = None
        else:
            colp, colc, colf = st.columns(3)
            with colp:
                p_pct = st.number_input(
                    "Proteína (%)", min_value=0, max_value=100, value=30, step=1
                )
            with colc:
                c_pct = st.number_input(
                    "Carboidratos (%)", min_value=0, max_value=100, value=40, step=1
                )
            with colf:
                f_pct = st.number_input(
                    "Gorduras (%)", min_value=0, max_value=100, value=30, step=1
                )
            p_gkg = f_gkg = c_gkg = None

        calcular = st.form_submit_button("Calcular")

    # --- Cálculos e Exibição (ficam DENTRO da aba_plano) ---
    if calcular:
        avisos = []

        bmr = bmr_mifflin(peso, altura, idade, sexo)
        tdee_val = tdee(peso, altura, idade, sexo, atividade)
        kcal_alvo = tdee_val * (1 + ajuste_percent / 100.0)
        agua = agua_diaria_ml(peso)

        if metodo_macros == "Por %":
            g_p, g_c, g_f, (pN, cN, fN) = kcal_to_macros_grams(
                kcal_alvo, p_pct, c_pct, f_pct
            )
            if abs((p_pct + c_pct + f_pct) - 100) > 0.01:
                st.info(
                    f"As porcentagens somavam {p_pct + c_pct + f_pct:.1f}%. Normalizadas para 100% → Prot {pN:.1f}%, Carbo {cN:.1f}%, Gord {fN:.1f}%."
                )
            prot_g, carb_g, gord_g = g_p, g_c, g_f
        else:
            prot_g, carb_g, gord_g, kcal_rest = grams_from_gkg(
                peso, p_gkg, f_gkg, kcal_alvo
            )
            if kcal_rest < 0:
                avisos.append(
                    "Com as metas de proteína/gordura por kg escolhidas, as calorias alvo ficaram insuficientes para carboidratos (carbo zerado). Ajuste metas ou calorias."
                )

        # Salva metas na sessão para o Diário usar as barras de progresso
        st.session_state["kcal_alvo"] = float(kcal_alvo)
        st.session_state["prot_g"] = float(prot_g)
        st.session_state["carb_g"] = float(carb_g)
        st.session_state["gord_g"] = float(gord_g)

        st.subheader("Resultados")
        m1, m2, m3 = st.columns(3)
        m1.metric(
            "BMR (Mifflin-St Jeor)", f"{bmr:,.0f} kcal/d", help="Taxa metabólica basal"
        )
        m2.metric(
            "TDEE (Manutenção)", f"{tdee_val:,.0f} kcal/d", help="Gasto total estimado"
        )
        m3.metric("Água diária", f"{agua/1000:,.2f} L/d")

        st.write(
            f"**Objetivo:** {objetivo}  |  **Ajuste aplicado:** {ajuste_percent}%  → **Alvo:** **{kcal_alvo:,.0f} kcal/dia**"
        )

        kcal_p = prot_g * 4
        kcal_c = carb_g * 4
        kcal_f = gord_g * 9

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Proteína", f"{prot_g:,.0f} g", help=f"{kcal_p:,.0f} kcal")
        with c2:
            st.metric("Carboidratos", f"{carb_g:,.0f} g", help=f"{kcal_c:,.0f} kcal")
        with c3:
            st.metric("Gorduras", f"{gord_g:,.0f} g", help=f"{kcal_f:,.0f} kcal")

        if avisos:
            for a in avisos:
                st.warning(a)

        st.divider()
        st.write("**Exportar**")
        if REPORTLAB_AVAILABLE:
            resumo = {
                "peso": peso,
                "altura": altura,
                "idade": idade,
                "sexo": sexo,
                "atividade": atividade,
                "bmr": round(bmr),
                "tdee": round(tdee_val),
                "kcal_alvo": round(kcal_alvo),
                "objetivo": objetivo,
                "ajuste_percent": ajuste_percent,
                "g_prot": round(prot_g),
                "g_carb": round(carb_g),
                "g_gord": round(gord_g),
                "kcal_prot": round(kcal_p),
                "kcal_carb": round(kcal_c),
                "kcal_gord": round(kcal_f),
                "agua_l": round(agua / 1000, 2),
                "avisos": avisos,
            }
            pdf_bytes = gerar_pdf_bytes(resumo)
            nome_pdf = f"Plano_Diario_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
            st.download_button(
                label="📄 Baixar PDF do Plano Diário",
                data=pdf_bytes,
                file_name=nome_pdf,
                mime="application/pdf",
            )
        else:
            st.info(
                "Para exportar PDF, instale a biblioteca **reportlab** e reinicie o app:\n\n`python -m pip install reportlab`"
            )

        # === [BOTÃO: SALVAR NO SUPABASE] ===
        if st.session_state.get("sb_session") is None:
            st.info("Faça login para salvar seu plano no Supabase.")
        else:
            if st.button("💾 Salvar plano no Supabase"):
                try:
                    uid = st.session_state["sb_session"].user.id
                    insert_data = {
                        "user_id": uid,
                        "weight_kg": float(peso),
                        "height_cm": float(altura),
                        "age_years": int(idade),
                        "sex": sexo,
                        "activity": atividade,
                        "bmr_kcal": float(bmr),
                        "tdee_kcal": float(tdee_val),
                        "target_kcal": float(kcal_alvo),
                        "protein_g": float(prot_g),
                        "carbs_g": float(carb_g),
                        "fats_g": float(gord_g),
                        "water_l": float(agua / 1000.0),
                    }
                    supabase.table("plans").insert(insert_data).execute()
                    st.success("Plano salvo com sucesso!")
                except Exception as e:
                    st.error(f"Erro ao salvar plano: {e}")

        # === [LISTAR PLANOS SALVOS] ===
        if st.session_state.get("sb_session"):
            try:
                uid = st.session_state["sb_session"].user.id
                rows = (
                    supabase.table("plans")
                    .select("*")
                    .eq("user_id", uid)
                    .order("created_at", desc=True)
                    .limit(10)
                    .execute()
                )
                if rows.data:
                    st.write("**Seus últimos planos:**")
                    for r in rows.data:
                        st.write(
                            f"- {r['created_at']}: {r['target_kcal']} kcal | P {r['protein_g']}g • C {r['carbs_g']}g • G {r['fats_g']}g | Água {r['water_l']} L"
                        )
                else:
                    st.caption("Você ainda não tem planos salvos.")
            except Exception as e:
                st.warning(f"Não foi possível listar planos: {e}")
    else:
        st.info(
            "Preencha os dados e clique em **Calcular** para ver resultados e liberar a exportação em PDF."
        )

















