import os
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.request import HTTPXRequest
from fpdf import FPDF
from supabase import create_client, Client

# =========================================================
# ESTADOS DA CONVERSA
# =========================================================
ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN, ASK_LANGUAGE = range(4)

# =========================================================
# LOGGING E ENV
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "").strip()
SUPABASE_URL    = os.getenv("SUPABASE_URL",    "").strip()
SUPABASE_KEY    = os.getenv("SUPABASE_KEY",    "").strip()

if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("ERRO CRITICO: Variaveis de ambiente ausentes.")
    raise SystemExit(1)

# =========================================================
# CLIENTES EXTERNOS
# =========================================================
llm_client: genai.Client = genai.Client(api_key=GEMINI_API_KEY)
db_client:  Client       = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# SERVIDOR WEB — KEEP-ALIVE
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ATS Bot Operacional")

    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    logger.info(f"[Health] Servidor iniciado na porta {port}")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# =========================================================
# SANITIZACAO
# =========================================================
_SUBS = {
    "\u2022": "-", "\u2013": "-", "\u2014": "-",
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u00b7": "-", "\u2026": "...",
}

def sanitize(text: str) -> str:
    if not text:
        return ""
    for char, rep in _SUBS.items():
        text = text.replace(char, rep)
    return text.encode("latin-1", "ignore").decode("latin-1")

# =========================================================
# GERADOR DE PDF — PADRAO HARVARD
# =========================================================
class CurriculoHarvard(FPDF):
    def __init__(self):
        super().__init__()
        self.set_margins(left=20, top=20, right=20)
        self.add_page()
        self.set_auto_page_break(auto=True, margin=15)

    def cabecalho_candidato(self, dados: dict):
        self.set_font("Arial", "B", 16)
        self.cell(0, 10, sanitize(dados.get("nome", "")),
                  new_x="LMARGIN", new_y="NEXT", align="C")
        partes = [v for k, v in dados.items() if k != "nome" and v]
        if partes:
            self.set_font("Arial", "", 10)
            self.cell(0, 6, sanitize(" | ".join(partes)),
                      new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(4)

    def secao(self, titulo: str):
        self.set_font("Arial", "B", 12)
        self.cell(0, 8, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def item_experiencia(self, exp: dict):
        self.set_font("Arial", "B", 11)
        self.cell(0, 6,
                  sanitize(f"{exp.get('cargo','')} -- {exp.get('empresa','')}"),
                  new_x="LMARGIN", new_y="NEXT")
        self.set_font("Arial", "I", 10)
        self.cell(0, 5, sanitize(exp.get("periodo", "")), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        self.set_font("Arial", "", 10)
        for b in exp.get("conquistas", []):
            self.multi_cell(0, 5, sanitize(f"- {b}"))
        self.ln(3)

    def item_educacao(self, edu: dict):
        self.set_font("Arial", "B", 11)
        self.cell(0, 6, sanitize(edu.get("curso", "")), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Arial", "", 10)
        self.cell(0, 5,
                  sanitize(f"{edu.get('instituicao','')} | {edu.get('periodo','')}"),
                  new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def bloco_texto(self, texto: str):
        self.set_font("Arial", "", 10)
        self.multi_cell(0, 5, sanitize(texto))
        self.ln(3)

    def lista_simples(self, itens: list):
        self.set_font("Arial", "", 10)
        for item in itens:
            self.multi_cell(0, 5, sanitize(f"- {item}"))
        self.ln(3)

def gerar_pdf(dados: dict) -> io.BytesIO:
    pdf = CurriculoHarvard()
    pdf.cabecalho_candidato(dados.get("contato", {}))

    if dados.get("resumo"):
        pdf.secao("Resumo Profissional")
        pdf.bloco_texto(dados["resumo"])
    if dados.get("experiencias"):
        pdf.secao("Experiencia Profissional")
        for exp in dados["experiencias"]:
            pdf.item_experiencia(exp)
    if dados.get("educacao"):
        pdf.secao("Formacao Academica")
        for edu in dados["educacao"]:
            pdf.item_educacao(edu)
    if dados.get("competencias"):
        pdf.secao("Competencias Tecnicas")
        pdf.lista_simples(dados["competencias"])
    if dados.get("certificacoes"):
        pdf.secao("Certificacoes")
        pdf.lista_simples(dados["certificacoes"])
    if dados.get("idiomas"):
        pdf.secao("Idiomas")
        pdf.lista_simples(dados["idiomas"])

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# GEMINI — JSON ESTRUTURADO
# =========================================================
_SCHEMA = """{
  "contato": {"nome":"","email":"","telefone":"","linkedin":"","cidade":""},
  "resumo": "2-4 frases de impacto alinhadas a vaga",
  "experiencias": [{"cargo":"","empresa":"","periodo":"","conquistas":["verbo + metrica"]}],
  "educacao": [{"curso":"","instituicao":"","periodo":""}],
  "competencias": [""],
  "certificacoes": [""],
  "idiomas": ["Idioma - Nivel"]
}"""

_SYSTEM = (
    "Voce e um recrutador tecnico senior especialista em curriculos ATS no padrao Harvard.\n"
    "REGRAS: Retorne SOMENTE JSON valido sem markdown. "
    f"Schema: {_SCHEMA}. "
    "Nunca invente dados. Use apenas ASCII (sem acentos). "
    "Campos sem info: string vazia ou lista vazia."
)

def gerar_curriculo_json(historico: str, vaga: str, perfil: dict) -> dict:
    idioma = perfil.get('idioma', 'Ingles')
    email = perfil.get('email', '')
    telefone = perfil.get('telefone', '')
    linkedin = perfil.get('linkedin', '')

    prompt = (
        f"HISTORICO:\n{historico}\n\n"
        f"VAGA:\n{vaga}\n\n"
        f"INSTRUCOES OBRIGATORIAS:\n"
        f"1. O curriculo DEVE ser traduzido e gerado estritamente no idioma: {idioma}.\n"
        f"2. Preencha os dados de contato EXATAMENTE com os seguintes valores:\n"
        f"   - Email: {email}\n"
        f"   - Telefone: {telefone}\n"
        f"   - LinkedIn: {linkedin}\n\n"
        f"Gere o JSON do curriculo respeitando o idioma e substituindo os placeholders."
    )

    response = llm_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(raw)

# =========================================================
# SUPABASE
# =========================================================
def salvar_historico(telegram_id: int, raw_history: str):
    db_client.table("user_profiles").upsert(
        {"telegram_id": str(telegram_id), "raw_history": raw_history},
        on_conflict="telegram_id"
    ).execute()

def salvar_perfil(telegram_id: int, dados: dict):
    dados["telegram_id"] = str(telegram_id)
    db_client.table("user_profiles").upsert(
        dados,
        on_conflict="telegram_id"
    ).execute()

def buscar_usuario(telegram_id: int) -> dict | None:
    r = (db_client.table("user_profiles")
         .select("*")
         .eq("telegram_id", str(telegram_id))
         .maybe_single()
         .execute())
    return r.data if r.data else None

# =========================================================
# HANDLERS DO FLUXO DE CADASTRO
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"[Handler] /start de user_id={user_id}")
    
    usuario = buscar_usuario(user_id)
    
    # Verifica se os dados obrigatorios ja existem
    if usuario and usuario.get("email") and usuario.get("idioma"):
        await update.message.reply_text(
            "Seu perfil ja esta configurado.\n"
            "Envie seu arquivo .txt com seu historico ou a descricao da vaga."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Bem-vindo ao ATS Resume Bot.\n"
        "Vamos configurar seu perfil para nao precisarmos perguntar novamente nas proximas vagas.\n\n"
        "Qual o seu E-MAIL profissional?"
    )
    return ASK_EMAIL

async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text
    await update.message.reply_text("Qual o seu numero de TELEFONE (com DDD)?")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['telefone'] = update.message.text
    await update.message.reply_text("Qual a URL ou o usuario do seu LINKEDIN?")
    return ASK_LINKEDIN

async def ask_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['linkedin'] = update.message.text
    await update.message.reply_text(
        "Em qual IDIOMA os curriculos devem ser gerados como padrao? (Ex: Ingles, Portugues, Espanhol)"
    )
    return ASK_LANGUAGE

async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['idioma'] = update.message.text
    user_id = update.effective_user.id
    
    dados = {
        "email": context.user_data['email'],
        "telefone": context.user_data['telefone'],
        "linkedin": context.user_data['linkedin'],
        "idioma": context.user_data['idioma']
    }
    
    try:
        salvar_perfil(user_id, dados)
        await update.message.reply_text(
            "Perfil salvo com sucesso!\n\n"
            "Passo 1: Envie um arquivo .txt contendo o seu historico bruto.\n"
            "Passo 2: Apos enviar o historico, envie a descricao da vaga."
        )
    except Exception as e:
        logger.error(f"[Cadastro] Erro ao salvar perfil: {e}")
        await update.message.reply_text("Erro ao processar o cadastro. Tente novamente executando /start.")
    
    return ConversationHandler.END

# =========================================================
# HANDLERS DE PROCESSAMENTO
# =========================================================
async def handle_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Conclua seu cadastro basico primeiro enviando o comando /start.")
        return

    doc = update.message.document
    logger.info(f"[Handler] Documento de user_id={user_id}: {doc.file_name}")

    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Envie um arquivo .txt com seu historico.")
        return

    await update.message.reply_text("Recebendo historico...")

    try:
        file = await context.bot.get_file(doc.file_id)
        buf  = bytearray()
        await file.download_as_bytearray(out=buf)
        historico = buf.decode("utf-8")
    except Exception as e:
        logger.error(f"[Documento] Erro: {e}")
        await update.message.reply_text("Falha ao ler arquivo. Use encoding UTF-8.")
        return

    try:
        salvar_historico(user_id, historico)
        logger.info(f"[Supabase] Historico salvo para {user_id}")
    except Exception as e:
        logger.error(f"[Supabase] Erro: {e}")
        await update.message.reply_text("Falha ao salvar no banco. Tente novamente.")
        return

    await update.message.reply_text(
        "Historico salvo!\nAgora envie a descricao da vaga em texto para gerar o curriculo."
    )

async def handle_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Conclua seu cadastro basico primeiro enviando o comando /start.")
        return

    descricao_vaga = update.message.text
    logger.info(f"[Handler] Texto de user_id={user_id}: {descricao_vaga[:60]}")

    historico = usuario.get("raw_history")
    if not historico:
        await update.message.reply_text(
            "Nenhum historico encontrado.\n"
            "Envie primeiro um arquivo .txt com seu historico."
        )
        return

    msg = await update.message.reply_text("Analisando perfil... Aguarde.")

    try:
        dados = gerar_curriculo_json(historico, descricao_vaga, usuario)
    except json.JSONDecodeError as e:
        logger.error(f"[Gemini] JSON invalido: {e}")
        await msg.edit_text("Modelo retornou resposta invalida. Tente novamente.")
        return
    except Exception as e:
        logger.error(f"[Gemini] Erro: {e}")
        await msg.edit_text("Falha no modelo de IA. Tente novamente.")
        return

    try:
        pdf_buf = gerar_pdf(dados)
    except Exception as e:
        logger.error(f"[PDF] Erro: {e}")
        await msg.edit_text("Falha ao gerar PDF.")
        return

    nome         = dados.get("contato", {}).get("nome", "Candidato")
    nome_arquivo = f"CV_{nome.replace(' ', '_')}_ATS.pdf"

    await msg.edit_text("Compilando PDF...")
    await update.message.reply_document(
        document=pdf_buf,
        filename=nome_arquivo,
        caption=f"Curriculo ATS gerado: {nome}\nIdioma Base: {usuario.get('idioma')}\nEnvie outra vaga para gerar novamente.",
    )
    await msg.delete()

async def handle_erro(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[Erro] {context.error}", exc_info=context.error)

# =========================================================
# MAIN — POLLING
# =========================================================
def main():
    logger.info("=" * 60)
    logger.info("Iniciando ATS Resume Bot")
    logger.info("=" * 60)

    threading.Thread(target=start_health_server, daemon=True).start()

    custom_request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        http_version="1.1",
    )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(custom_request)
        .build()
    )

    # Inicializacao da maquina de estados para onboarding
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_LINKEDIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_linkedin)],
            ASK_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_language)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
    )

    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.Document.MimeType("text/plain"), handle_documento))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_texto))
    app.add_error_handler(handle_erro)

    logger.info("Bot em modo de escuta (polling)...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
