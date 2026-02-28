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
)
from telegram.request import HTTPXRequest
from fpdf import FPDF
from supabase import create_client, Client

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
# O Render encerra containers que nao expõem porta HTTP.
# Esta thread responde 200 OK para os health checks.
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
# SANITIZACAO — fpdf2 usa latin-1 internamente
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

def gerar_curriculo_json(historico: str, vaga: str) -> dict:
    prompt = f"HISTORICO:\n{historico}\n\nVAGA:\n{vaga}\n\nGere o JSON do curriculo."

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

def buscar_historico(telegram_id: int) -> str | None:
    r = (db_client.table("user_profiles")
         .select("raw_history")
         .eq("telegram_id", str(telegram_id))
         .maybe_single()
         .execute())
    return r.data.get("raw_history") if r.data else None

# =========================================================
# HANDLERS
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"[Handler] /start de user_id={update.effective_user.id}")
    await update.message.reply_text(
        "ATS Resume Bot - Pronto.\n\n"
        "1. Envie um arquivo .txt com seu historico profissional.\n"
        "2. Envie a descricao da vaga como texto.\n"
        "3. Receba seu PDF no padrao Harvard.\n\n"
        "Seu historico fica salvo para multiplas vagas."
    )

async def handle_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc     = update.message.document
    user_id = update.effective_user.id
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
        "Historico salvo!\nAgora envie a descricao da vaga para gerar o curriculo."
    )

async def handle_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id        = update.effective_user.id
    descricao_vaga = update.message.text
    logger.info(f"[Handler] Texto de user_id={user_id}: {descricao_vaga[:60]}")

    historico = buscar_historico(user_id)
    if not historico:
        await update.message.reply_text(
            "Nenhum historico encontrado.\n"
            "Envie primeiro um arquivo .txt com seu historico."
        )
        return

    msg = await update.message.reply_text("Analisando perfil... Aguarde.")

    try:
        dados = gerar_curriculo_json(historico, descricao_vaga)
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
        caption=f"Curriculo ATS gerado: {nome}\nEnvie outra vaga para gerar novamente.",
    )
    await msg.delete()

async def handle_erro(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[Erro] {context.error}", exc_info=context.error)

# =========================================================
# MAIN — POLLING (funciona no Render.com)
# =========================================================
def main():
    logger.info("=" * 60)
    logger.info("Iniciando ATS Resume Bot")
    logger.info("=" * 60)

    # Thread keep-alive para health check do Render
    threading.Thread(target=start_health_server, daemon=True).start()

    # Motor HTTP com timeouts altos e HTTP/1.1 forçado
    # para estabilizar TLS em nós de nuvem
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

    app.add_handler(CommandHandler("start", cmd_start))
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
