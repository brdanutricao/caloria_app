# pages/06_Receitas.py
# -------------------------------------------------------------
# Receitas (via Supabase) com gating por plano (Discípulo x Fiel)
# - Lê plano do st.session_state (setado no pós-login)
# - Busca receitas em public.recipes
# - Imagens públicas do bucket 'recipes' no Storage
# -------------------------------------------------------------
from typing import List, Dict, Any, Optional

import streamlit as st
from supabase import create_client, Client

# Config da página cedo
st.set_page_config(page_title="Receitas", page_icon="🍽️", layout="wide")
st.title("🍽️ Receitas")

# -------- Supabase client --------
@st.cache_resource
def _supabase() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_ANON_KEY"])

sb = _supabase()

# -------- Helpers DB --------
def db_list_recipes(search: str = "", categorias: Optional[list] = None) -> List[Dict[str, Any]]:
    q = sb.table("recipes").select("*")
    if search:
        q = q.ilike("titulo", f"%{search}%")  # busca pelo título
        # Se quiser buscar também por ingredientes (jsonb), faça no app ou crie indice/FTS depois.
    if categorias:
        q = q.in_("categoria", categorias)
    res = q.order("created_at", desc=True).execute()
    return res.data or []

def recipe_image_public_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    # bucket 'recipes' deve ser público
    return sb.storage.from_("recipes").get_public_url(path)

# -------- Sessão / Plano --------
plan_id   = st.session_state.get("plan_id", "DISCIPULO")           # 'DISCIPULO' | 'FIEL'
plan_name = st.session_state.get("plan_name", "Discípulo (3 meses)")
is_fiel   = (plan_id == "FIEL")

# Aviso de acesso
st.caption(
    f"Plano atual: **{plan_name}** — "
    + ("Acesso completo liberado." if is_fiel else "Acesso parcial: 5 receitas grátis desbloqueadas.")
)

# -------- Filtros --------
with st.container(border=True):
    cols = st.columns([2, 1, 1, 1])
    with cols[0]:
        q = st.text_input("Buscar por título (ex.: frango, aveia, salada…)", placeholder="Ex.: frango, aveia, salada…")
    with cols[1]:
        # Carregar categorias distintas do próprio dataset (pode otimizar depois com view/enum)
        all_rows = db_list_recipes()  # leve para poucos itens; se tiver muitas receitas, faça uma view de categorias
        cats = sorted({r["categoria"] for r in all_rows}) if all_rows else []
        cat_sel = st.multiselect("Categoria", options=cats, default=[])
    with cols[2]:
        only_quick = st.toggle("Até 15 min", value=False)
    with cols[3]:
        sort_opt = st.selectbox("Ordenar por", ["Relevância", "Menor kcal", "Maior proteína"])

# -------- Consulta --------
rows = db_list_recipes(search=q, categorias=cat_sel if cat_sel else None)

# Filtro local (tempo)
if only_quick:
    rows = [r for r in rows if (r.get("tempo_min") or 0) <= 15]

# Ordenação
if sort_opt == "Menor kcal":
    rows = sorted(rows, key=lambda r: r.get("kcal", 0))
elif sort_opt == "Maior proteína":
    rows = sorted(rows, key=lambda r: float(r.get("proteina_g") or 0), reverse=True)

# -------- Gating por plano --------
if is_fiel:
    visiveis = rows
    bloqueadas = []
else:
    visiveis = [r for r in rows if r.get("degustacao_gratis")]
    bloqueadas = [r for r in rows if not r.get("degustacao_gratis")]

# -------- UI helpers --------
def card_receita(r: Dict[str, Any], locked: bool = False):
    box = st.container(border=True)
    with box:
        cols = st.columns([1, 2])
        with cols[0]:
            url = recipe_image_public_url(r.get("imagem_url"))
            if url:
                try:
                    st.image(url, use_container_width=True)
                except Exception:
                    st.empty()
            else:
                st.empty()
        with cols[1]:
            titulo = ("🔒 " if locked else "") + str(r.get("titulo", ""))
            st.markdown(f"### {titulo}")
            # linha de resumo
            tempo_min = r.get("tempo_min") or 0
            porcoes   = r.get("porcoes") or 1
            st.write(f"**Categoria:** {r.get('categoria','-')}  •  **{tempo_min} min**  •  **{porcoes} porção(ões)**")
            # macros
            kcal = r.get("kcal") or 0
            P = r.get("proteina_g") or 0
            C = r.get("carbo_g") or 0
            G = r.get("gordura_g") or 0
            st.write(f"**Kcal:** {kcal}  |  **P:** {P} g  •  **C:** {C} g  •  **G:** {G} g")

            if locked:
                st.info("Esta receita é Premium. Faça upgrade para o Plano Fiel para desbloquear.")
            else:
                with st.expander("Ver ingredientes e modo de preparo"):
                    st.markdown("**Ingredientes**")
                    for ing in (r.get("ingredientes") or []):
                        st.write(f"- {ing}")
                    st.markdown("**Preparo**")
                    for i, step in enumerate((r.get("preparo") or []), start=1):
                        st.write(f"{i}. {step}")

# -------- Render --------
if not rows:
    st.info("Nenhuma receita encontrada com esses filtros.")
else:
    cols = st.columns(2)
    for i, r in enumerate(visiveis):
        with cols[i % 2]:
            card_receita(r, locked=False)

    if bloqueadas:
        st.subheader("🔒 Receitas Premium (bloqueadas no seu plano)")
        cols2 = st.columns(2)
        for i, r in enumerate(bloqueadas):
            with cols2[i % 2]:
                card_receita(r, locked=True)

st.divider()
st.caption(
    "Banco real • As imagens são servidas do Storage. "
    "Para desempenho com muitos itens: use paginação e uma view de categorias."
)
