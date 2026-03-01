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
# SANITIZACAO E PARSER DE ARQUIVOS
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

def extrair_texto_de_arquivo(file_bytes: bytearray, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
            return text
        except Exception as e:
            logger.error(f"Erro ao extrair PDF: {e}")
            raise Exception("Falha na extração do PDF.")
    else:
        encodings = ["utf-8", "utf-16", "latin-1", "cp1252"]
        for enc in encodings:
            try:
                return file_bytes.decode(enc)
            except UnicodeDecodeError:
                continue
        return file_bytes.decode("utf-8", errors="ignore")

# =========================================================
# GERADOR DE PDF — PADRAO HARVARD (BLINDADO)
# =========================================================
class CurriculoHarvard(FPDF):
    def __init__(self):
        super().__init__()
        self.set_margins(left=20, top=20, right=20)
        self.add_page()
        self.set_auto_page_break(auto=True, margin=15)

    def cabecalho_candidato(self, dados: dict):
        self.set_font("Arial", "B", 16)
        nome = dados.get("nome") or "Candidato"
        self.cell(0, 10, sanitize(nome), new_x="LMARGIN", new_y="NEXT", align="C")
        
        partes = [v for k, v in dados.items() if k != "nome" and v and isinstance(v, str)]
        if partes:
            self.set_font("Arial", "", 10)
            self.cell(0, 6, sanitize(" | ".join(partes)), new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(4)

    def secao(self, titulo: str):
        self.set_font("Arial", "B", 12)
        self.cell(0, 8, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def item_experiencia(self, exp: dict):
        if not isinstance(exp, dict): return
        self.set_font("Arial", "B", 11)
        header = f"{exp.get('cargo','')} -- {exp.get('empresa','')}"
        self.cell(0, 6, sanitize(header), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Arial", "I", 10)
        self.cell(0, 5, sanitize(exp.get("periodo", "")), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        self.set_font("Arial", "", 10)
        conquistas = exp.get("conquistas", [])
        if isinstance(conquistas, list):
            for b in conquistas:
                self.multi_cell(0, 5, sanitize(f"- {b}"))
        self.ln(3)

    def item_educacao(self, edu: dict):
        if not isinstance(edu, dict): return
        self.set_font("Arial", "B", 11)
        self.cell(0, 6, sanitize(edu.get("curso", "")), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Arial", "", 10)
        info = f"{edu.get('instituicao','')} | {edu.get('periodo','')}"
        self.cell(0, 5, sanitize(info), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def bloco_texto(self, texto: str):
        self.set_font("Arial", "", 10)
        self.multi_cell(0, 5, sanitize(str(texto)))
        self.ln(3)

    def lista_simples(self, itens: list):
        self.set_font("Arial", "", 10)
        if isinstance(itens, list):
            for item in itens:
                self.multi_cell(0, 5, sanitize(f"- {item}"))
        self.ln(3)

def gerar_pdf(dados: dict) -> io.BytesIO:
    if not isinstance(dados, dict): dados = {}
    pdf = CurriculoHarvard()
    
    contato = dados.get("contato")
    if not isinstance(contato, dict): contato = {}
    pdf.cabecalho_candidato(contato)

    if dados.get("resumo"):
        pdf.secao("Resumo Profissional")
        pdf.bloco_texto(dados["resumo"])
        
    exps = dados.get("experiencias")
    if isinstance(exps, list) and exps:
        pdf.secao("Experiencia Profissional")
        for exp in exps: pdf.item_experiencia(exp)
        
    edus = dados.get("educacao")
    if isinstance(edus, list) and edus:
        pdf.secao("Formacao Academica")
        for edu in edus: pdf.item_educacao(edu)
        
    for secao_nome, chave in [("Competencias Tecnicas", "competencias"), 
                             ("Certificacoes", "certificacoes"), 
                             ("Idiomas", "idiomas")]:
        lista = dados.get(chave)
        if isinstance(lista, list) and lista:
            pdf.secao(secao_nome)
            pdf.lista_simples(lista)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# MOTORES LLM
# =========================================================
def classificar_intencao_llm(texto: str) -> str:
    prompt = (
        "Analise o texto e determine a intencao. "
        "Se for curriculo, historico ou atualizacao de carreira, responda 'HISTORICO'. "
        "Se for descricao de vaga/requisitos, responda 'VAGA'.\n\n"
        f"TEXTO:\n{texto[:1500]}"
    )
    response = llm_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(temperature=0.0)
    )
    return "VAGA" if "VAGA" in response.text.upper() else "HISTORICO"

def consolidar_historico_llm(historico_atual: str, nova_interacao: str) -> str:
    prompt = (
        "Reescreva o historico incorporando a nova interacao. "
        "Mantenha os dados antigos e adicione/atualize com os novos. "
        "Retorne APENAS o texto consolidado.\n\n"
        f"ATUAL:\n{historico_atual}\n\nNOVA:\n{nova_interacao}"
    )
    response = llm_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(temperature=0.2)
    )
    return response.text.strip()

_SCHEMA = """{
  "contato": {"nome":"","email":"","telefone":"","linkedin":"","cidade":""},
  "resumo": "frases de impacto",
  "experiencias": [{"cargo":"","empresa":"","periodo":"","conquistas":[]}],
  "educacao": [{"curso":"","instituicao":"","periodo":""}],
  "competencias": [], "certificacoes": [], "idiomas": []
}"""

def gerar_curriculo_json(historico: str, vaga: str, perfil: dict) -> dict:
    prompt = (
        f"HISTORICO:\n{historico}\n\nVAGA:\n{vaga}\n\n"
        f"IDIOMA OBRIGATORIO: {perfil.get('idioma', 'Ingles')}\n"
        f"DADOS DE CONTATO: Email: {perfil.get('email')}, Tel: {perfil.get('telefone')}, LinkedIn: {perfil.get('linkedin')}\n"
        "Gere o JSON ATS seguindo o schema."
    )
    response = llm_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=f"Recrutador Harvard. Retorne SOMENTE JSON. Schema: {_SCHEMA}",
            response_mime_type="application/json",
            temperature=0.3,
        ),
    )
    return json.loads(response.text)

# =========================================================
# SUPABASE
# =========================================================
def salvar_perfil(telegram_id: int, dados: dict):
    dados["telegram_id"] = str(telegram_id)
    db_client.table("user_profiles").upsert(dados, on_conflict="telegram_id").execute()

def buscar_usuario(telegram_id: int) -> dict | None:
    try:
        r = db_client.table("user_profiles").select("*").eq("telegram_id", str(telegram_id)).execute()
        if hasattr(r, 'data') and len(r.data) > 0: return r.data[0]
    except Exception as e: logger.error(f"Erro buscar_usuario: {e}")
    return None

# =========================================================
# HANDLERS
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    if usuario and usuario.get("email"):
        await update.message.reply_text("Perfil configurado. Envie seu historico ou uma vaga.")
        return ConversationHandler.END
    await update.message.reply_text("Bem-vindo! Qual o seu E-MAIL profissional?")
    return ASK_EMAIL

async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['email'] = update.message.text
    await update.message.reply_text("Qual o seu TELEFONE?")
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['telefone'] = update.message.text
    await update.message.reply_text("Qual seu LINKEDIN?")
    return ASK_LINKEDIN

async def ask_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['linkedin'] = update.message.text
    await update.message.reply_text("Idioma padrao (Ex: Ingles, Portugues)?")
    return ASK_LANGUAGE

async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    dados = {
        "email": context.user_data['email'], "telefone": context.user_data['telefone'],
        "linkedin": context.user_data['linkedin'], "idioma": update.message.text
    }
    salvar_perfil(user_id, dados)
    await update.message.reply_text("Perfil salvo! Envie seu historico (.pdf, .txt ou texto).")
    return ConversationHandler.END

async def handle_input_inteligente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Use /start primeiro.")
        return

    texto_extraido = ""
    status = await update.message.reply_text("Processando...")

    if update.message.document:
        doc = update.message.document
        file = await context.bot.get_file(doc.file_id)
        buf = bytearray()
        await file.download_as_bytearray(out=buf)
        texto_extraido = extrair_texto_de_arquivo(buf, doc.file_name)
    else:
        texto_extraido = update.message.text

    intencao = classificar_intencao_llm(texto_extraido)

    if intencao == "HISTORICO":
        historico_atualizado = consolidar_historico_llm(usuario.get("raw_history", ""), texto_extraido)
        salvar_perfil(user_id, {"raw_history": historico_atualizado})
        await status.edit_text("Historico atualizado com sucesso!")
    else:
        if not usuario.get("raw_history"):
            await status.edit_text("Envie seu historico primeiro.")
            return
        try:
            dados = gerar_curriculo_json(usuario["raw_history"], texto_extraido, usuario)
            pdf_buf = gerar_pdf(dados)
            nome = dados.get("contato", {}).get("nome") or "CV"
            await update.message.reply_document(document=pdf_buf, filename=f"{nome}_ATS.pdf")
            await status.delete()
        except Exception as e:
            logger.error(f"Erro geracao: {e}", exc_info=True)
            await status.edit_text("Erro ao gerar curriculo.")

async def handle_erro(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Erro Global: {context.error}")

# =========================================================
# MAIN
# =========================================================
def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=60, read_timeout=60)).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_LINKEDIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_linkedin)],
            ASK_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_language)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), handle_input_inteligente))
    app.add_error_handler(handle_erro)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
