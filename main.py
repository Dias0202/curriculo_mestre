import os
import io
import re
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.request import HTTPXRequest
from telegram.constants import ParseMode
from fpdf import FPDF
from supabase import create_client, Client
import docx

from scraper_vagas import extrair_vaga_linkedin_prod

# =========================================================
# CONFIGURAÇÃO DE ESTADOS E LOGGING
# =========================================================
ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN = range(3)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "").strip()
SUPABASE_URL    = os.getenv("SUPABASE_URL",    "").strip()
SUPABASE_KEY    = os.getenv("SUPABASE_KEY",    "").strip()

if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("ERRO CRÍTICO: Variáveis de ambiente ausentes.")
    raise SystemExit(1)

llm_client: genai.Client = genai.Client(api_key=GEMINI_API_KEY)
db_client:  Client       = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# SERVIDOR WEB (KEEP-ALIVE para Render free tier)
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ATS Bot Operacional")
    def log_message(self, *args): pass

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# =========================================================
# UTILITÁRIOS
# =========================================================
_SUBS = {
    "\u2022": "-", "\u2013": "-", "\u2014": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u00b7": "-", "\u2026": "...",
}

def sanitize(text: str) -> str:
    if not text: return ""
    text = str(text).replace("\t", " ")
    for char, rep in _SUBS.items():
        text = text.replace(char, rep)
    return text.encode("latin-1", "ignore").decode("latin-1")

def safe_string(val) -> str:
    if isinstance(val, dict):  return " - ".join(str(v) for v in val.values() if v)
    if isinstance(val, list):  return ", ".join(str(v) for v in val if v)
    if val is None:            return ""
    return str(val).strip()

def escape_md(text: str) -> str:
    """Escapa caracteres especiais para MarkdownV2 do Telegram."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))

def extrair_texto_de_arquivo(file_bytes: bytearray, filename: str) -> str:
    ext = filename.lower()
    texto = ""
    if ext.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            texto = "\n".join(
                p.extract_text(extraction_mode="layout") or "" for p in reader.pages
            )
        except Exception as e:
            logger.error(f"Erro PDF: {e}")
    elif ext.endswith(".docx"):
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            texto = "\n".join(p.text for p in doc.paragraphs)
        except Exception as e:
            logger.error(f"Erro DOCX: {e}")
    else:
        for enc in ["utf-8", "latin-1", "cp1252"]:
            try:
                texto = file_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not texto:
            texto = file_bytes.decode("utf-8", errors="ignore")
    return texto.replace("\x00", " ").strip()

# =========================================================
# AUDITORIA DE PERFIL
# =========================================================
def auditar_perfil(parsed_json: dict) -> str:
    gaps = []
    for ed in parsed_json.get("education", []):
        grau = ed.get("grau", "Formação")
        inst = ed.get("instituicao", "instituição")
        if not ed.get("ano_inicio"):
            gaps.append(f"- Ano de início do {grau} em {inst} não informado.")
        if not ed.get("ano_fim"):
            gaps.append(f"- Ano de conclusão do {grau} em {inst} não informado.")
        if not ed.get("curso") or len(str(ed.get("curso", "")).strip()) < 2:
            gaps.append(f"- Área de estudo do {grau} em {inst} está ausente.")
    for exp in parsed_json.get("experiences", []):
        cargo = exp.get("cargo", "Cargo")
        empresa = exp.get("empresa", "Empresa")
        if not exp.get("data_inicio"):
            gaps.append(f"- Data de início em {cargo} ({empresa}) ausente.")
        if not exp.get("data_fim"):
            gaps.append(f"- Data de término em {cargo} ({empresa}) ausente.")
    if gaps:
        return (
            "✅ Dados processados. Detectei informações estruturais ausentes:\n\n"
            + "\n".join(gaps)
            + "\n\nEnvie os dados faltantes em sua próxima mensagem para eu atualizar o perfil."
        )
    return "✅ Perfil integrado com sucesso. Envie a URL ou descrição de uma vaga para gerar o currículo."

# =========================================================
# BANCO DE DADOS — SUPABASE
# =========================================================
def get_user_by_telegram(telegram_id: int):
    r = db_client.table("users").select("*").eq("telegram_id", str(telegram_id)).execute()
    return r.data[0] if r.data else None

def save_user_base(telegram_id: int, dados: dict):
    dados["telegram_id"] = str(telegram_id)
    user = get_user_by_telegram(telegram_id)
    if user:
        db_client.table("users").update(dados).eq("id", user["id"]).execute()
    else:
        db_client.table("users").insert(dados).execute()

def fetch_full_user_profile(user_uuid: str) -> str:
    if not user_uuid: return ""
    profile = {}
    exp_res = db_client.table("experiences").select("*").eq("user_id", user_uuid).execute()
    experiences = exp_res.data or []
    for exp in experiences:
        bull = db_client.table("experience_bullets").select("*").eq("experience_id", exp["id"]).execute()
        bullets = bull.data or []
        exp["responsabilidades"] = [b["texto"] for b in bullets if b["tipo"] == "responsabilidade"]
        exp["conquistas"]        = [b["texto"] for b in bullets if b["tipo"] == "conquista"]
    profile["experiences"]    = experiences
    profile["education"]      = db_client.table("education").select("*").eq("user_id", user_uuid).execute().data or []
    profile["skills"]         = db_client.table("skills").select("*").eq("user_id", user_uuid).execute().data or []
    profile["certifications"] = db_client.table("certifications").select("*").eq("user_id", user_uuid).execute().data or []
    profile["projects"]       = db_client.table("projects").select("*").eq("user_id", user_uuid).execute().data or []
    profile["languages"]      = db_client.table("languages").select("*").eq("user_id", user_uuid).execute().data or []
    return json.dumps(profile, ensure_ascii=False, indent=2)

def save_parsed_history_to_db(user_uuid: str, parsed_json: dict):
    if not user_uuid: return
    for table in ["experiences", "education", "skills", "certifications", "projects", "languages"]:
        db_client.table(table).delete().eq("user_id", user_uuid).execute()

    for exp in parsed_json.get("experiences", []):
        res = db_client.table("experiences").insert({
            "user_id":          user_uuid,
            "cargo":            exp.get("cargo"),
            "empresa":          exp.get("empresa"),
            "localizacao":      exp.get("localizacao"),
            "data_inicio":      exp.get("data_inicio"),
            "data_fim":         exp.get("data_fim"),
            "descricao_empresa": exp.get("descricao_empresa"),
        }).execute()
        if res.data:
            exp_id  = res.data[0]["id"]
            bullets = []
            for r in exp.get("responsabilidades", []):
                if r: bullets.append({"experience_id": exp_id, "tipo": "responsabilidade", "texto": r})
            for c in exp.get("conquistas", []):
                if c: bullets.append({"experience_id": exp_id, "tipo": "conquista", "texto": c})
            if bullets:
                db_client.table("experience_bullets").insert(bullets).execute()

    edu_list  = [{"user_id": user_uuid, "grau": e.get("grau"), "curso": e.get("curso"), "instituicao": e.get("instituicao"), "ano_inicio": e.get("ano_inicio"), "ano_fim": e.get("ano_fim")} for e in parsed_json.get("education", [])]
    if edu_list:  db_client.table("education").insert(edu_list).execute()

    skill_list = [{"user_id": user_uuid, "nome": s.get("nome"), "categoria": s.get("categoria"), "nivel": s.get("nivel")} for s in parsed_json.get("skills", [])]
    if skill_list: db_client.table("skills").insert(skill_list).execute()

    cert_list  = [{"user_id": user_uuid, "nome": c.get("nome"), "emissor": c.get("emissor"), "ano": c.get("ano")} for c in parsed_json.get("certifications", [])]
    if cert_list:  db_client.table("certifications").insert(cert_list).execute()

    proj_list  = [{"user_id": user_uuid, "nome": p.get("nome"), "descricao": p.get("descricao")} for p in parsed_json.get("projects", [])]
    if proj_list:  db_client.table("projects").insert(proj_list).execute()

    lang_list  = [{"user_id": user_uuid, "idioma": l.get("idioma"), "nivel": l.get("nivel")} for l in parsed_json.get("languages", [])]
    if lang_list:  db_client.table("languages").insert(lang_list).execute()

# =========================================================
# GERADOR DE PDF — ESTILO HARVARD
# =========================================================
class CurriculoHarvard(FPDF):
    def __init__(self):
        super().__init__()
        self.set_margins(15, 15, 15)
        self.add_page()
        self.set_auto_page_break(True, margin=15)

    def cabecalho_candidato(self, d: dict):
        self.set_font("helvetica", "B", 18)
        self.multi_cell(0, 8, sanitize(safe_string(d.get("nome", "Candidato")).upper()), align="C", new_x="LMARGIN", new_y="NEXT")
        titulo = safe_string(d.get("titulo", ""))
        if titulo:
            self.set_font("helvetica", "B", 12)
            self.multi_cell(0, 6, sanitize(titulo), align="C", new_x="LMARGIN", new_y="NEXT")
        partes = [safe_string(d.get(k)) for k in ["localizacao", "telefone", "email", "linkedin", "github", "portfolio"] if d.get(k)]
        if partes:
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 5, sanitize(" | ".join(partes)), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def secao(self, titulo: str):
        if not titulo: return
        self.set_font("helvetica", "B", 12)
        self.cell(0, 8, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def item_experiencia(self, exp: dict):
        if not isinstance(exp, dict): return
        self.set_font("helvetica", "B", 11)
        cargo   = safe_string(exp.get("cargo", ""))
        empresa = safe_string(exp.get("empresa", ""))
        header  = f"{cargo} - {empresa}" if cargo and empresa else cargo or empresa
        self.multi_cell(0, 6, sanitize(header), new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "I", 10)
        loc    = safe_string(exp.get("localizacao", ""))
        dt_in  = safe_string(exp.get("data_inicio", ""))
        dt_fim = safe_string(exp.get("data_fim", ""))
        periodo    = f"{dt_in} - {dt_fim}" if dt_in and dt_fim else dt_in or dt_fim
        sub_header = f"{loc} | {periodo}" if loc and periodo else loc or periodo
        if sub_header:
            self.multi_cell(0, 5, sanitize(sub_header), new_x="LMARGIN", new_y="NEXT")
        desc_emp = safe_string(exp.get("descricao_empresa", ""))
        if desc_emp:
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 5, sanitize(desc_emp), new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "", 10)
        for r in exp.get("responsabilidades", []):
            r_str = safe_string(r)
            if r_str: self.multi_cell(0, 5, sanitize(f"- {r_str}"), new_x="LMARGIN", new_y="NEXT")
        for c in exp.get("conquistas", []):
            c_str = safe_string(c)
            if c_str: self.multi_cell(0, 5, sanitize(f"- {c_str}"), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def lista_duas_colunas(self, itens: list):
        self.set_font("helvetica", "", 10)
        col_w   = (self.w - self.l_margin - self.r_margin) / 2.0
        x_left  = self.l_margin
        x_right = self.l_margin + col_w
        limpos  = [safe_string(i) for i in itens if safe_string(i)]
        for i in range(0, len(limpos), 2):
            y_start = self.get_y()
            self.set_xy(x_left, y_start)
            self.multi_cell(col_w - 5, 5, sanitize(f"- {limpos[i]}"))
            y1 = self.get_y()
            y2 = y_start
            if i + 1 < len(limpos):
                self.set_xy(x_right, y_start)
                self.multi_cell(col_w - 5, 5, sanitize(f"- {limpos[i+1]}"))
                y2 = self.get_y()
            self.set_y(max(y1, y2))
            self.set_x(self.l_margin)
        self.ln(3)


def gerar_pdf(dados: dict) -> io.BytesIO:
    if not isinstance(dados, dict): dados = {}
    pdf = CurriculoHarvard()
    cab = dados.get("cabecalhos", {})

    pdf.cabecalho_candidato(dados.get("identificacao", {}))

    if dados.get("resumo"):
        pdf.secao(safe_string(cab.get("resumo", "Professional Summary")))
        pdf.set_font("helvetica", "", 10)
        pdf.multi_cell(0, 5, sanitize(safe_string(dados["resumo"])), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    if dados.get("competencias"):
        pdf.secao(safe_string(cab.get("competencias", "Core Competencies")))
        pdf.lista_duas_colunas(dados["competencias"])

    if dados.get("experiencias"):
        pdf.secao(safe_string(cab.get("experiencias", "Professional Experience")))
        for item in dados["experiencias"]:
            if isinstance(item, dict): pdf.item_experiencia(item)

    if dados.get("educacao"):
        pdf.secao(safe_string(cab.get("educacao", "Education")))
        for item in dados["educacao"]:
            if not isinstance(item, dict): continue
            pdf.set_font("helvetica", "B", 11)
            grau   = safe_string(item.get("grau", ""))
            curso  = safe_string(item.get("curso", ""))
            inst   = safe_string(item.get("instituicao", ""))
            header = (f"{grau} em {curso} - {inst}" if grau and curso and inst
                      else f"{grau} - {inst}" if grau and inst else grau or inst)
            pdf.multi_cell(0, 6, sanitize(header), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("helvetica", "I", 10)
            dt_in  = safe_string(item.get("ano_inicio", ""))
            dt_fim = safe_string(item.get("ano_fim", ""))
            periodo = f"{dt_in} - {dt_fim}" if dt_in and dt_fim else dt_in or dt_fim
            if periodo: pdf.multi_cell(0, 5, sanitize(periodo), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    if dados.get("projetos"):
        pdf.secao(safe_string(cab.get("projetos", "Projects")))
        for p in dados["projetos"]:
            if not isinstance(p, dict): continue
            pdf.set_font("helvetica", "B", 11)
            pdf.multi_cell(0, 6, sanitize(safe_string(p.get("nome", ""))), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("helvetica", "", 10)
            pdf.multi_cell(0, 5, sanitize(safe_string(p.get("descricao", ""))), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    if dados.get("certificacoes"):
        pdf.secao(safe_string(cab.get("certificacoes", "Certifications")))
        pdf.lista_duas_colunas(dados["certificacoes"])

    if dados.get("idiomas"):
        pdf.secao(safe_string(cab.get("idiomas", "Languages")))
        pdf.lista_duas_colunas(dados["idiomas"])

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# LÓGICA LLM — JSON FORÇADO (elimina JSONDecodeError)
# =========================================================
_JSON_CONFIG = genai.types.GenerateContentConfig(
    temperature=0.0,
    response_mime_type="application/json",
)
_GEN_CONFIG = genai.types.GenerateContentConfig(
    temperature=0.1,
    response_mime_type="application/json",
)

def _llm_json(prompt: str, config=None) -> dict:
    """Chama o LLM e retorna JSON limpo. Usa response_mime_type para garantir JSON puro."""
    cfg = config or _JSON_CONFIG
    resp = llm_client.models.generate_content(
        model="gemma-3-27b-it",
        contents=prompt,
        config=cfg,
    )
    text = resp.text.strip()
    # Fallback: remove possíveis crases remanescentes
    text = re.sub(r"^```json|^```|```$", "", text, flags=re.IGNORECASE).strip()
    return json.loads(text)


def classificar_intencao_e_idioma_llm(texto: str) -> dict:
    prompt = (
        "Analise o texto e classifique a intenção. Retorne APENAS JSON com as chaves 'intencao' e 'idioma'.\n"
        "Regras para 'intencao':\n"
        "- 'VAGA': descrição de emprego, requisitos de vaga, job description.\n"
        "- 'HISTORICO': currículo, experiências profissionais, formação acadêmica, habilidades pessoais.\n"
        "- 'EDICAO': solicitação para remover, corrigir ou atualizar um dado específico do perfil "
        "(ex: 'remova minha experiência na empresa X', 'corrija meu telefone').\n"
        "- 'PERFIL': usuário pedindo para ver seu perfil salvo (ex: '/meuperfil', 'mostrar meu perfil').\n"
        "- 'IRRELEVANTE': saudação, conversa fora de contexto profissional.\n"
        "Regras para 'idioma': idioma da vaga ou do texto.\n\n"
        f"TEXTO:\n{texto[:1500]}"
    )
    try:
        return _llm_json(prompt)
    except Exception:
        return {"intencao": "VAGA", "idioma": "Portugues"}


def extrair_e_mesclar_historico(perfil_atual_str: str, nova_entrada: str) -> dict:
    with open("prompt_parser.md", "r", encoding="utf-8") as f:
        template = f.read()
    prompt = template.replace("{perfil_atual}", perfil_atual_str).replace("{nova_entrada}", nova_entrada)
    return _llm_json(prompt, _GEN_CONFIG)


def gerar_curriculo_json(hist: str, vaga: str, perfil: dict, idioma: str) -> dict:
    with open("prompt_generator.md", "r", encoding="utf-8") as f:
        template = f.read()
    prompt = (
        template
        .replace("{idioma_detectado}", safe_string(idioma))
        .replace("{nome}",       safe_string(perfil.get("nome")))
        .replace("{telefone}",   safe_string(perfil.get("telefone")))
        .replace("{email}",      safe_string(perfil.get("email")))
        .replace("{linkedin}",   safe_string(perfil.get("linkedin")))
        .replace("{github}",     safe_string(perfil.get("github")))
        .replace("{portfolio}",  safe_string(perfil.get("portfolio")))
        .replace("{historico}",  safe_string(hist))
        .replace("{vaga}",       safe_string(vaga))
    )
    return _llm_json(prompt, _GEN_CONFIG)

# =========================================================
# FORMATADOR DE PERFIL — /meuperfil
# =========================================================
def formatar_perfil_markdown(usuario: dict, perfil_str: str) -> str:
    """Formata o perfil salvo em texto legível para o Telegram (Markdown simples)."""
    try:
        p = json.loads(perfil_str) if perfil_str else {}
    except Exception:
        p = {}

    linhas = [
        f"👤 *{safe_string(usuario.get('nome', 'Candidato'))}*",
        f"📧 {safe_string(usuario.get('email', ''))}  |  📱 {safe_string(usuario.get('telefone', ''))}",
        f"🔗 {safe_string(usuario.get('linkedin', ''))}",
        "",
    ]

    # Experiências
    exps = p.get("experiences", [])
    if exps:
        linhas.append("*💼 EXPERIÊNCIAS*")
        for e in exps:
            cargo   = safe_string(e.get("cargo", ""))
            empresa = safe_string(e.get("empresa", ""))
            dt_in   = safe_string(e.get("data_inicio", ""))
            dt_fim  = safe_string(e.get("data_fim", ""))
            periodo = f"{dt_in} → {dt_fim}" if dt_in else dt_fim
            linhas.append(f"• *{cargo}* | {empresa}")
            if periodo: linhas.append(f"  _{periodo}_")
        linhas.append("")

    # Formação
    edus = p.get("education", [])
    if edus:
        linhas.append("*🎓 FORMAÇÃO*")
        for e in edus:
            grau  = safe_string(e.get("grau", ""))
            curso = safe_string(e.get("curso", ""))
            inst  = safe_string(e.get("instituicao", ""))
            anos  = f"{e.get('ano_inicio','')} – {e.get('ano_fim','')}"
            linhas.append(f"• {grau} em {curso}" if curso else f"• {grau}")
            linhas.append(f"  _{inst} | {anos}_")
        linhas.append("")

    # Skills
    skills = p.get("skills", [])
    if skills:
        hard = [s["nome"] for s in skills if s.get("categoria", "").lower().startswith("hard")]
        soft = [s["nome"] for s in skills if s.get("categoria", "").lower().startswith("soft")]
        linhas.append("*🛠 COMPETÊNCIAS*")
        if hard: linhas.append(f"Hard: {', '.join(hard)}")
        if soft: linhas.append(f"Soft: {', '.join(soft)}")
        linhas.append("")

    # Certificações
    certs = p.get("certifications", [])
    if certs:
        linhas.append("*📜 CERTIFICAÇÕES*")
        for c in certs:
            linhas.append(f"• {safe_string(c.get('nome'))} – {safe_string(c.get('emissor'))} ({safe_string(c.get('ano'))})")
        linhas.append("")

    # Projetos
    projs = p.get("projects", [])
    if projs:
        linhas.append("*🚀 PROJETOS*")
        for proj in projs:
            linhas.append(f"• *{safe_string(proj.get('nome', ''))}*")
            desc = safe_string(proj.get("descricao", ""))
            if desc: linhas.append(f"  {desc[:120]}{'...' if len(desc) > 120 else ''}")
        linhas.append("")

    # Idiomas
    langs = p.get("languages", [])
    if langs:
        linhas.append("*🌍 IDIOMAS*")
        for l in langs:
            linhas.append(f"• {safe_string(l.get('idioma'))} – {safe_string(l.get('nivel'))}")
        linhas.append("")

    if len(linhas) <= 4:
        return "Nenhum histórico encontrado. Envie seu currículo para popular o perfil."

    linhas.append("_Para editar: descreva a alteração em texto livre._")
    linhas.append("_Ex: \"Remova a experiência na empresa X\" ou \"Atualize meu telefone para...\"_")
    return "\n".join(linhas)

# =========================================================
# HANDLERS DO TELEGRAM
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    nome    = update.effective_user.first_name or "Profissional"
    u = get_user_by_telegram(user_id)
    if u and u.get("email"):
        await update.message.reply_text(
            f"Olá, {nome}. Perfil ativo.\n\n"
            "📋 *Comandos disponíveis:*\n"
            "/meuperfil — Visualize o histórico salvo\n"
            "/deletar — Remova todos os seus dados\n\n"
            "Para atualizar, envie currículo (.pdf/.docx/.txt) ou texto.\n"
            "Para gerar currículo, envie descrição ou link de vaga.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END
    await update.message.reply_text(
        f"Olá, {nome}! 👋 Inicializando o assistente ATS.\n\nPor favor, informe seu *e-mail* profissional.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_EMAIL

async def ask_email(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["e"] = u.message.text
    await u.message.reply_text("Registrado. Qual é seu *telefone* (com DDD)?", parse_mode=ParseMode.MARKDOWN)
    return ASK_PHONE

async def ask_phone(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["t"] = u.message.text
    await u.message.reply_text("Certo. Envie a URL ou nome de usuário do seu *LinkedIn*.", parse_mode=ParseMode.MARKDOWN)
    return ASK_LINKEDIN

async def ask_linkedin(u: Update, c: ContextTypes.DEFAULT_TYPE):
    nome = u.effective_user.full_name or "Candidato"
    save_user_base(u.effective_user.id, {
        "nome":      nome,
        "email":     c.user_data["e"],
        "telefone":  c.user_data["t"],
        "linkedin":  u.message.text,
    })
    await u.message.reply_text(
        "✅ Perfil base criado!\n\nAgora envie seu histórico profissional (.pdf, .docx, .txt ou texto livre).",
    )
    return ConversationHandler.END

async def cmd_deletar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user_by_telegram(user_id)
    if u:
        db_client.table("users").delete().eq("id", u["id"]).execute()
        await update.message.reply_text("🗑 Todos os seus dados foram permanentemente removidos.")
    else:
        await update.message.reply_text("Nenhum registro encontrado.")

async def cmd_meuperfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o perfil completo salvo no Supabase."""
    user_id = update.effective_user.id
    usuario = get_user_by_telegram(user_id)
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Cadastro ausente. Use /start para configurar sua conta.")
        return

    status = await update.message.reply_text("🔍 Carregando seu perfil...")
    perfil_str = fetch_full_user_profile(usuario["id"])
    texto = formatar_perfil_markdown(usuario, perfil_str)

    # Telegram limita mensagens a 4096 chars
    if len(texto) > 4000:
        partes = [texto[i:i+4000] for i in range(0, len(texto), 4000)]
        await status.edit_text(partes[0], parse_mode=ParseMode.MARKDOWN)
        for parte in partes[1:]:
            await update.message.reply_text(parte, parse_mode=ParseMode.MARKDOWN)
    else:
        await status.edit_text(texto, parse_mode=ParseMode.MARKDOWN)

# =========================================================
# HANDLER PRINCIPAL — ROTEAMENTO SEMÂNTICO
# =========================================================
async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_telegram = update.effective_user.id
    nome_usuario     = update.effective_user.first_name or "Profissional"

    usuario = get_user_by_telegram(user_id_telegram)
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Cadastro ausente. Use /start para configurar sua conta.")
        return

    status = await update.message.reply_text("⏳ Analisando entrada...")

    # --- Extração de texto ---
    if update.message.document:
        f    = await context.bot.get_file(update.message.document.file_id)
        b    = await f.download_as_bytearray()
        texto     = extrair_texto_de_arquivo(b, update.message.document.file_name)
        tipo_raw  = update.message.document.file_name.rsplit(".", 1)[-1].lower()
    else:
        texto     = update.message.text
        tipo_raw  = "texto"

    if not texto or not texto.strip():
        await status.edit_text("Nenhum dado legível identificado.")
        return

    # --- Integração Scraper LinkedIn ---
    padrao_linkedin = r"https://(?:www\.)?linkedin\.com/jobs/view/[0-9]+"
    match_url = re.search(padrao_linkedin, texto)
    if match_url:
        await status.edit_text("🔗 URL do LinkedIn detectada. Extraindo dados da vaga...")
        resultado = extrair_vaga_linkedin_prod(match_url.group(0), db_client=db_client)
        if resultado.get("sucesso"):
            vaga_dados = resultado["dados"]
            origem_txt = "🗂 (cache)" if resultado["origem"] == "cache" else "🌐 (LinkedIn)"
            texto = (
                f"Título: {vaga_dados['titulo']}\n"
                f"Empresa: {vaga_dados['empresa']}\n"
                f"Localização: {vaga_dados['localizacao']}\n\n"
                f"Descrição:\n{vaga_dados['descricao']}"
            )
            await status.edit_text(f"✅ Vaga extraída {origem_txt}. Classificando conteúdo...")
        else:
            await status.edit_text(
                f"❌ Não foi possível extrair a vaga automaticamente.\n"
                f"Erro: {resultado.get('erro', 'desconhecido')}\n\n"
                "Por favor, cole o texto da vaga manualmente."
            )
            return

    from google.genai import errors as genai_errors

    try:
        classificacao    = classificar_intencao_e_idioma_llm(texto)
        intencao         = classificacao.get("intencao", "VAGA")
        idioma_detectado = classificacao.get("idioma", "Portugues")

        # --- IRRELEVANTE ---
        if intencao == "IRRELEVANTE":
            await status.edit_text(
                f"Olá, {nome_usuario}! O sistema processa históricos profissionais e vagas. "
                "Envie um currículo ou a descrição de uma vaga para continuar."
            )
            return

        # Salva input bruto para auditoria
        db_client.table("raw_inputs").insert({
            "user_id":        usuario["id"],
            "tipo":           tipo_raw[:50],
            "conteudo_texto": texto,
        }).execute()

        perfil_atual_str = fetch_full_user_profile(usuario["id"])

        # --- HISTÓRICO ou EDIÇÃO (ambos usam o pipeline de merge) ---
        if intencao in ("HISTORICO", "EDICAO"):
            acao = "Atualizando perfil..." if intencao == "HISTORICO" else "Aplicando edição solicitada..."
            await status.edit_text(f"🔄 {acao}")
            parsed_json = extrair_e_mesclar_historico(perfil_atual_str, texto)
            save_parsed_history_to_db(usuario["id"], parsed_json)
            feedback = auditar_perfil(parsed_json)
            await status.edit_text(feedback)

        # --- VAGA ---
        else:
            if not perfil_atual_str or len(perfil_atual_str) < 50:
                await status.edit_text(
                    "⚠️ Histórico insuficiente. Envie seu currículo antes de solicitar a geração do documento."
                )
                return

            await status.edit_text(f"✍️ Gerando currículo ATS em *{idioma_detectado}*...", parse_mode=ParseMode.MARKDOWN)
            dados = gerar_curriculo_json(perfil_atual_str, texto, usuario, idioma_detectado)

            # Persiste currículo gerado
            json_sem_relatorio = {k: v for k, v in dados.items() if k != "relatorio_analitico"}
            db_client.table("generated_resumes").insert({
                "user_id":     usuario["id"],
                "vaga_texto":  texto,
                "idioma":      idioma_detectado,
                "json_gerado": json_sem_relatorio,
                "score_match": dados.get("relatorio_analitico", {}).get("match_score"),
            }).execute()

            pdf      = gerar_pdf(dados)
            nome_arq = safe_string(dados.get("identificacao", {}).get("nome", "Candidato")).replace(" ", "_")

            relatorio   = dados.get("relatorio_analitico", {})
            match_score = safe_string(relatorio.get("match_score", "N/A"))
            gaps        = relatorio.get("analise_gaps", [])
            gaps_txt    = "\n".join(f"• {g}" for g in gaps) if gaps else "Nenhum gap crítico identificado."
            dica        = safe_string(relatorio.get("dica_entrevista", ""))

            caption = (
                f"📄 *Currículo ATS gerado com sucesso!*\n\n"
                f"🎯 *Match Score:* {match_score}%\n\n"
                f"⚠️ *Gaps identificados:*\n{gaps_txt}\n\n"
                f"💡 *Dica para a entrevista:*\n{dica}"
            )
            # Trunca caption se passar do limite do Telegram (1024 chars)
            if len(caption) > 1020:
                caption = caption[:1020] + "..."

            await update.message.reply_document(
                document=pdf,
                filename=f"CV_{nome_arq}_ATS.pdf",
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            await status.delete()

    except genai_errors.ClientError as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            await status.edit_text("⏱ Limite de cota da API atingido. Aguarde alguns minutos e tente novamente.")
        else:
            logger.error(f"Erro ClientError: {e}")
            await status.edit_text("❌ Falha de comunicação com o módulo de IA.")

    except json.JSONDecodeError as e:
        logger.error(f"JSONDecodeError: {e}")
        await status.edit_text("❌ Falha estrutural ao formatar a saída. Por favor, reenvie os dados.")

    except Exception as e:
        logger.error(f"Erro inesperado: {e}", exc_info=True)
        await status.edit_text("❌ Erro interno inesperado. Tente novamente em instantes.")

# =========================================================
# BOOTSTRAP
# =========================================================
def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info("Health server iniciado.")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(HTTPXRequest(connect_timeout=60, read_timeout=60))
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_EMAIL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            ASK_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_LINKEDIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_linkedin)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("deletar",    cmd_deletar))
    app.add_handler(CommandHandler("meuperfil",  cmd_meuperfil))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
            handle_input,
        )
    )

    logger.info("Bot iniciado. Aguardando mensagens...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
