import os
import io
import re
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
# CONFIGURAÇÃO DE ESTADOS E LOGGING
# =========================================================
ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN, ASK_LANGUAGE = range(4)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "").strip()
SUPABASE_URL    = os.getenv("SUPABASE_URL",    "").strip()
SUPABASE_KEY    = os.getenv("SUPABASE_KEY",    "").strip()

if not all([TELEGRAM_TOKEN, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("ERRO CRITICO: Variaveis de ambiente ausentes.")
    raise SystemExit(1)

# Inicializacao de Clientes
llm_client: genai.Client = genai.Client(api_key=GEMINI_API_KEY)
db_client:  Client       = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# SERVIDOR WEB (KEEP-ALIVE RENDER)
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ATS Bot Operacional")
    def log_message(self, format, *args): pass

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    logger.info(f"[Health] Servidor iniciado na porta {port}")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# =========================================================
# UTILITARIOS: PDF E EXTRACAO DE TEXTO
# =========================================================
_SUBS = {"\u2022": "-", "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00b7": "-", "\u2026": "..."}

def sanitize(text: str) -> str:
    if not text: return ""
    text = str(text).replace("\t", " ")
    for char, rep in _SUBS.items():
        text = text.replace(char, rep)
    return text.encode("latin-1", "ignore").decode("latin-1")

def extrair_texto_de_arquivo(file_bytes: bytearray, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            return "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
        except Exception as e:
            logger.error(f"Erro PDF: {e}")
            return ""
    else:
        for enc in ["utf-8", "latin-1", "cp1252"]:
            try: return file_bytes.decode(enc)
            except UnicodeDecodeError: continue
        return file_bytes.decode("utf-8", errors="ignore")

# =========================================================
# GERADOR DE PDF BLINDADO (NOVA API FPDF2)
# =========================================================
class CurriculoHarvard(FPDF):
    def __init__(self):
        super().__init__()
        self.set_margins(20, 20, 20)
        self.add_page()
        self.set_auto_page_break(True, margin=15)

    def cabecalho_candidato(self, d: dict):
        self.set_font("helvetica", "B", 16)
        # O reset de cursor (new_x, new_y) é obrigatório no fpdf2 para evitar o erro de falta de espaço
        self.multi_cell(0, 10, sanitize(d.get("nome", "Candidato")), align="C", new_x="LMARGIN", new_y="NEXT")
        
        partes = [v for k, v in d.items() if k != "nome" and v and isinstance(v, str)]
        if partes:
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 6, sanitize(" | ".join(partes)), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def secao(self, titulo):
        self.set_font("helvetica", "B", 12)
        self.cell(0, 8, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def item_experiencia(self, exp: dict):
        if not isinstance(exp, dict): return
        self.set_font("helvetica", "B", 11)
        self.multi_cell(0, 6, sanitize(f"{exp.get('cargo','')} -- {exp.get('empresa','')}"), new_x="LMARGIN", new_y="NEXT")
        
        self.set_font("helvetica", "I", 10)
        self.multi_cell(0, 5, sanitize(exp.get("periodo", "")), new_x="LMARGIN", new_y="NEXT")
        
        self.set_font("helvetica", "", 10)
        conquistas = exp.get("conquistas", [])
        
        if isinstance(conquistas, str): conquistas = [conquistas]
        if isinstance(conquistas, list):
            for b in conquistas:
                if b: self.multi_cell(0, 5, sanitize(f"- {b}"), new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

def gerar_pdf(dados: dict) -> io.BytesIO:
    if not isinstance(dados, dict): dados = {}
    pdf = CurriculoHarvard()
    pdf.cabecalho_candidato(dados.get("contato", {}))
    
    if dados.get("resumo"):
        pdf.secao("Resumo Profissional")
        pdf.set_font("helvetica", "", 10)
        pdf.multi_cell(0, 5, sanitize(dados["resumo"]), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    for titulo, chave in [("Experiencia", "experiencias"), ("Formacao", "educacao")]:
        itens = dados.get(chave, [])
        if itens and isinstance(itens, list):
            pdf.secao(titulo)
            for item in itens:
                if not isinstance(item, dict): continue
                if chave == "experiencias": 
                    pdf.item_experiencia(item)
                else:
                    pdf.set_font("helvetica", "B", 11)
                    pdf.multi_cell(0, 6, sanitize(item.get("curso", "")), new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("helvetica", "", 10)
                    pdf.multi_cell(0, 5, sanitize(f"{item.get('instituicao','')} | {item.get('periodo','')}"), new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(2)

    for tit, ch in [("Habilidades", "competencias"), ("Idiomas", "idiomas")]:
        lista = dados.get(ch, [])
        if isinstance(lista, str): lista = [lista]
        if lista and isinstance(lista, list):
            pdf.secao(tit)
            pdf.set_font("helvetica", "", 10)
            for i in lista: 
                if i: pdf.multi_cell(0, 5, sanitize(f"- {i}"), new_x="LMARGIN", new_y="NEXT")

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# LOGICA LLM (ROTEAMENTO E GERACAO)
# =========================================================
def classificar_intencao_llm(texto):
    p = f"Responda 'VAGA' se o texto for descricao de cargo, ou 'HISTORICO' se for perfil/curriculo:\n\n{texto[:1000]}"
    resp = llm_client.models.generate_content(model="gemini-2.5-flash", contents=p)
    return "VAGA" if "VAGA" in resp.text.upper() else "HISTORICO"

def consolidar_historico_llm(atual, novo):
    p = f"Mescle as novas informacoes ao historico atual, removendo duplicatas e mantendo consistencia. Retorne apenas o texto consolidado:\n\nATUAL:\n{atual}\n\nNOVA:\n{novo}"
    return llm_client.models.generate_content(model="gemini-2.5-flash", contents=p).text.strip()

_SCHEMA = '{"contato": {"nome":"","email":"","telefone":"","linkedin":""}, "resumo": "", "experiencias": [{"cargo":"","empresa":"","periodo":"","conquistas":[]}], "educacao": [], "competencias": [], "idiomas": []}'

def gerar_curriculo_json(hist, vaga, perfil):
    p = (
        f"HISTORICO:\n{hist}\n\n"
        f"VAGA:\n{vaga}\n\n"
        f"IDIOMA: {perfil.get('idioma')}\n"
        f"CONTATOS: {perfil.get('email')}, {perfil.get('telefone')}, {perfil.get('linkedin')}"
    )
    resp = llm_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=p,
        config=genai.types.GenerateContentConfig(
            system_instruction=f"Gere um JSON ATS no padrao Harvard. SOMENTE JSON PURO sem delimitadores Markdown. Schema: {_SCHEMA}",
            response_mime_type="application/json",
            temperature=0.1
        )
    )
    
    texto_json = resp.text.strip()
    texto_json = re.sub(r'^```json', '', texto_json, flags=re.IGNORECASE)
    texto_json = re.sub(r'```$', '', texto_json).strip()
    
    return json.loads(texto_json)

# =========================================================
# BANCO DE DADOS E HANDLERS
# =========================================================
def salvar_perfil(tid, d):
    d["telegram_id"] = str(tid)
    db_client.table("user_profiles").upsert(d, on_conflict="telegram_id").execute()

def buscar_usuario(tid):
    try:
        r = db_client.table("user_profiles").select("*").eq("telegram_id", str(tid)).execute()
        return r.data[0] if r.data and len(r.data) > 0 else None
    except Exception as e:
        logger.error(f"Erro buscar_usuario: {e}")
        return None

async def cmd_start(update, context):
    u = buscar_usuario(update.effective_user.id)
    if u and u.get("email"):
        await update.message.reply_text("Perfil ativo. Pode enviar seu historico ou uma vaga.")
        return ConversationHandler.END
    await update.message.reply_text("Bem-vindo! Qual seu E-MAIL profissional?")
    return ASK_EMAIL

async def ask_email(u, c): c.user_data['e'] = u.message.text; await u.message.reply_text("Qual seu TELEFONE?"); return ASK_PHONE
async def ask_phone(u, c): c.user_data['t'] = u.message.text; await u.message.reply_text("Seu LINKEDIN?"); return ASK_LINKEDIN
async def ask_linkedin(u, c): c.user_data['l'] = u.message.text; await u.message.reply_text("Idioma padrao (Ex: Portugues)?"); return ASK_LANGUAGE
async def ask_language(u, c):
    salvar_perfil(u.effective_user.id, {"email": c.user_data['e'], "telefone": c.user_data['t'], "linkedin": c.user_data['l'], "idioma": u.message.text})
    await u.message.reply_text("Perfil salvo! O bot aceita agora envio de textos, atualizacoes ou PDFs.")
    return ConversationHandler.END

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Use /start para configurar seu perfil primeiro.")
        return

    status = await update.message.reply_text("Processando...")
    
    if update.message.document:
        f = await context.bot.get_file(update.message.document.file_id)
        b = bytearray(); await f.download_as_bytearray(out=b)
        texto = extrair_texto_de_arquivo(b, update.message.document.file_name)
    else:
        texto = update.message.text

    if not texto.strip():
        await status.edit_text("Nenhum texto detectado.")
        return

    intencao = classificar_intencao_llm(texto)
    
    if intencao == "HISTORICO":
        novo_h = consolidar_historico_llm(usuario.get("raw_history", ""), texto)
        salvar_perfil(user_id, {"raw_history": novo_h})
        await status.edit_text("Historico atualizado!")
    else:
        if not usuario.get("raw_history"):
            await status.edit_text("Envie seu historico antes da vaga."); return
        try:
            dados = gerar_curriculo_json(usuario["raw_history"], texto, usuario)
            pdf = gerar_pdf(dados)
            nome = dados.get("contato", {}).get("nome", "Candidato").replace(" ", "_")
            await update.message.reply_document(document=pdf, filename=f"CV_{nome}_ATS.pdf")
            await status.delete()
        except json.JSONDecodeError as e:
            logger.error(f"Erro JSON: {e}")
            await status.edit_text("Falha ao formatar os dados. O modelo gerou um documento invalido. Tente enviar novamente.")
        except Exception as e:
            logger.error(f"Erro: {e}", exc_info=True)
            await status.edit_text("Erro ao gerar PDF.")

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
        fallbacks=[CommandHandler("start", cmd_start)]
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), handle_input))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
