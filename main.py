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
        # Tenta múltiplas codificações para suportar arquivos do Windows
        encodings = ["utf-8", "utf-16", "latin-1", "cp1252"]
        for enc in encodings:
            try:
                return file_bytes.decode(enc)
            except UnicodeDecodeError:
                continue
        return file_bytes.decode("utf-8", errors="ignore")

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
# MOTORES LLM (ROTEADOR, CONSOLIDADOR E GERADOR)
# =========================================================
def classificar_intencao_llm(texto: str) -> str:
    prompt = (
        "Analise o texto fornecido e determine a intencao do usuario. "
        "Se o texto contiver um curriculo, historico profissional, instrucoes para adicionar/remover competencias, "
        "ou atualizacoes de carreira, responda estritamente com a palavra 'HISTORICO'. "
        "Se o texto for a descricao de uma vaga de emprego, listando requisitos de contratacao, "
        "responda estritamente com a palavra 'VAGA'.\n\n"
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
        "Voce atua como um sistema de banco de dados de curriculos. O usuario enviou uma nova informacao ou instrucao.\n\n"
        f"HISTORICO ATUAL SALVO:\n{historico_atual}\n\n"
        f"NOVA INTERACAO DO USUARIO:\n{nova_interacao}\n\n"
        "Sua tarefa: Reescreva o historico atual incorporando a nova interacao. "
        "Se a nova interacao for um curriculo novo, substitua e complemente. "
        "Se for uma instrucao (ex: 'adicione que falo ingles'), aplique a mudanca. "
        "Se for uma instrucao de exclusao, remova os dados. "
        "Nao invente dados. Retorne APENAS o texto do historico consolidado, sem introducoes ou comentarios adicionais."
    )
    response = llm_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(temperature=0.2)
    )
    return response.text.strip()

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
    try:
        r = (db_client.table("user_profiles")
             .select("*")
             .eq("telegram_id", str(telegram_id))
             .execute())
        
        # Verifica com segurança se a propriedade data existe e não está vazia
        if hasattr(r, 'data') and isinstance(r.data, list) and len(r.data) > 0:
            return r.data[0]
            
    except Exception as e:
        logger.error(f"[Supabase] Falha ao buscar usuario {telegram_id}: {e}")
        
    return None
# =========================================================
# HANDLERS DO FLUXO DE CADASTRO
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"[Handler] /start de user_id={user_id}")
    
    usuario = buscar_usuario(user_id)
    
    if usuario and usuario.get("email") and usuario.get("idioma"):
        await update.message.reply_text(
            "Seu perfil ja esta configurado.\n"
            "Voce pode colar textos no chat ou enviar arquivos (.txt, .pdf).\n"
            "O sistema determinara automaticamente se voce esta atualizando o seu perfil ou solicitando uma vaga."
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
            "O bot trabalha de forma automatica e contextual. Voce pode colar seu historico profissional "
            "ou a descricao de uma vaga de emprego a qualquer momento. O sistema sabera o que fazer."
        )
    except Exception as e:
        logger.error(f"[Cadastro] Erro ao salvar perfil: {e}")
        await update.message.reply_text("Erro interno. Tente novamente com /start.")
    
    return ConversationHandler.END

# =========================================================
# HANDLER UNIFICADO DE PROCESSAMENTO DE CONTEXTO
# =========================================================
async def handle_input_inteligente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Processo recusado. Conclua seu cadastro basico enviando o comando /start.")
        return

    texto_extraido = ""
    msg_status = await update.message.reply_text("Recebendo dados...")

    # Extração de dados (Suporta Arquivos TXT, PDF ou Texto Direto)
    if update.message.document:
        doc = update.message.document
        if not doc.file_name.lower().endswith(('.txt', '.pdf')):
            await msg_status.edit_text("Formato invalido. Envie apenas .txt ou .pdf.")
            return
        try:
            file = await context.bot.get_file(doc.file_id)
            buf = bytearray()
            await file.download_as_bytearray(out=buf)
            texto_extraido = extrair_texto_de_arquivo(buf, doc.file_name)
        except Exception as e:
            logger.error(f"[Documento] Erro extração: {e}")
            await msg_status.edit_text("Falha ao extrair texto do arquivo submetido.")
            return
    elif update.message.text:
        texto_extraido = update.message.text

    if not texto_extraido.strip():
        await msg_status.edit_text("Nenhum texto detectado para processamento.")
        return

    await msg_status.edit_text("Classificando contexto da requisicao...")
    intencao = classificar_intencao_llm(texto_extraido)

    # Fluxo 1: Atualização de Histórico
    if intencao == "HISTORICO":
        await msg_status.edit_text("Atualizando a sua base de dados profissional...")
        historico_atual = usuario.get("raw_history", "")
        
        if historico_atual:
            historico_atualizado = consolidar_historico_llm(historico_atual, texto_extraido)
        else:
            historico_atualizado = texto_extraido

        try:
            salvar_historico(user_id, historico_atualizado)
            await msg_status.edit_text("Contexto profissional processado e salvo. O bot esta pronto para receber a vaga de emprego.")
        except Exception as e:
            logger.error(f"[Supabase] Erro ao salvar historico: {e}")
            await msg_status.edit_text("Falha na persistencia dos dados.")

    # Fluxo 2: Geração de Currículo para Vaga
    elif intencao == "VAGA":
        historico_atual = usuario.get("raw_history")
        if not historico_atual:
            await msg_status.edit_text("Nenhum historico de carreira encontrado no sistema. Envie os seus dados primeiro.")
            return

        await msg_status.edit_text("Contexto de vaga identificado. Estruturando documento otimizado...")
        try:
            dados = gerar_curriculo_json(historico_atual, texto_extraido, usuario)
            pdf_buf = gerar_pdf(dados)
        except json.JSONDecodeError:
            await msg_status.edit_text("Falha na geracao da estrutura ATS. Requisite novamente.")
            return
        except Exception as e:
            logger.error(f"[Geracao] Erro: {e}")
            await msg_status.edit_text("Erro critico de geracao.")
            return

        nome_arquivo = f"CV_{dados.get('contato', {}).get('nome', 'Candidato').replace(' ', '_')}_ATS.pdf"
        await msg_status.edit_text("Concluido. Enviando PDF...")
        await update.message.reply_document(
            document=pdf_buf,
            filename=nome_arquivo,
            caption=f"Processo finalizado. Idioma aplicado: {usuario.get('idioma')}."
        )
        await msg_status.delete()

async def handle_erro(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[Erro Global] {context.error}", exc_info=context.error)

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
    # Direciona documentos (TXT, PDF) e mensagens de texto puro para o analisador inteligente
    app.add_handler(MessageHandler(filters.Document.ALL, handle_input_inteligente))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_inteligente))
    app.add_error_handler(handle_erro)

    logger.info("Sistema ativo aguardando requisicoes.")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
