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
ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN = range(3)

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

def safe_string(val) -> str:
    """Extrai string com segurança mesmo se a IA retornar um dicionário acidentalmente"""
    if isinstance(val, dict):
        return " - ".join([str(v) for v in val.values() if v])
    if val is None:
        return ""
    return str(val)

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
        self.multi_cell(0, 10, sanitize(safe_string(d.get("nome", "Candidato"))), align="C", new_x="LMARGIN", new_y="NEXT")
        
        partes = [safe_string(v) for k, v in d.items() if k != "nome" and v]
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
        cargo_empresa = f"{safe_string(exp.get('cargo',''))} -- {safe_string(exp.get('empresa',''))}"
        self.multi_cell(0, 6, sanitize(cargo_empresa), new_x="LMARGIN", new_y="NEXT")
        
        periodo = safe_string(exp.get("periodo", ""))
        if periodo:
            self.set_font("helvetica", "I", 10)
            self.multi_cell(0, 5, sanitize(periodo), new_x="LMARGIN", new_y="NEXT")
        
        self.set_font("helvetica", "", 10)
        conquistas = exp.get("conquistas", [])
        
        if isinstance(conquistas, str): conquistas = [conquistas]
        if isinstance(conquistas, list):
            for b in conquistas:
                b_str = safe_string(b)
                if b_str: self.multi_cell(0, 5, sanitize(f"- {b_str}"), new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

def gerar_pdf(dados: dict) -> io.BytesIO:
    if not isinstance(dados, dict): dados = {}
    pdf = CurriculoHarvard()
    pdf.cabecalho_candidato(dados.get("contato", {}))
    
    if dados.get("resumo"):
        pdf.secao("Resumo Profissional")
        pdf.set_font("helvetica", "", 10)
        pdf.multi_cell(0, 5, sanitize(safe_string(dados["resumo"])), new_x="LMARGIN", new_y="NEXT")
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
                    pdf.multi_cell(0, 6, sanitize(safe_string(item.get("curso", ""))), new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("helvetica", "", 10)
                    
                    inst = safe_string(item.get('instituicao',''))
                    per = safe_string(item.get('periodo',''))
                    info = f"{inst} | {per}" if inst and per else inst or per
                    
                    if info:
                        pdf.multi_cell(0, 5, sanitize(info), new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(2)

    for tit, ch in [("Habilidades", "competencias"), ("Idiomas", "idiomas")]:
        lista = dados.get(ch, [])
        if isinstance(lista, str): lista = [lista]
        if lista and isinstance(lista, list):
            pdf.secao(tit)
            pdf.set_font("helvetica", "", 10)
            for i in lista: 
                i_str = safe_string(i)
                if i_str: pdf.multi_cell(0, 5, sanitize(f"- {i_str}"), new_x="LMARGIN", new_y="NEXT")

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# LOGICA LLM (ROTEAMENTO E GERACAO COM GEMMA)
# =========================================================
def classificar_intencao_e_idioma_llm(texto) -> dict:
    p = (
        "Analise o texto fornecido. Responda APENAS com um JSON valido contendo as chaves 'intencao' e 'idioma'.\n"
        "Regras:\n"
        "1. 'intencao': Retorne 'VAGA' se o texto for uma descricao de emprego/requisitos, ou 'HISTORICO' se for o perfil/curriculo do candidato.\n"
        "2. 'idioma': Identifique o idioma em que o texto esta escrito (ex: 'Ingles', 'Portugues', 'Espanhol'). Se o usuario fez um pedido explicito de idioma (ex: 'gere em ingles a vaga...'), retorne o idioma solicitado.\n\n"
        f"TEXTO:\n{texto[:1500]}"
    )
    try:
        resp = llm_client.models.generate_content(model="gemma-3-27b-it", contents=p, config=genai.types.GenerateContentConfig(temperature=0.0))
        t = re.sub(r'^```json|```$', '', resp.text.strip(), flags=re.IGNORECASE).strip()
        return json.loads(t)
    except Exception as e:
        logger.error(f"Erro ao classificar intencao: {e}")
        return {"intencao": "VAGA", "idioma": "Portugues"} # Fallback padrao

def consolidar_historico_llm(atual, novo):
    p = f"Mescle as novas informacoes ao historico atual. Retorne apenas o texto consolidado:\n\nATUAL:\n{atual}\n\nNOVA:\n{novo}"
    return llm_client.models.generate_content(model="gemma-3-27b-it", contents=p).text.strip()

_SCHEMA = '{"contato": {"nome":"","email":"","telefone":"","linkedin":""}, "resumo": "Paragrafo focado na vaga", "experiencias": [{"cargo":"","empresa":"","periodo":"","conquistas":["Acao + Resultado focado na vaga"]}], "educacao": [{"curso":"","instituicao":"","periodo":""}], "competencias": ["Hab1", "Hab2"], "idiomas": ["Idioma - Nivel"]}'

def gerar_curriculo_json(hist, vaga, perfil, idioma_detectado):
    p = (
        f"INSTRUCAO SUPREMA: Voce e um recrutador especialista em curriculos ATS.\n"
        f"Sua missao e cruzar o HISTORICO do candidato com a VAGA e criar um curriculo ALTAMENTE DIRECIONADO.\n"
        f"REGRAS VITAIS:\n"
        f"1. OCULTE experiencias, habilidades e formacoes que nao sejam relevantes para a VAGA.\n"
        f"2. DESTAQUE E EXPANTA os pontos do historico que dao match com a VAGA.\n"
        f"3. TRADUZA TODO O TEXTO gerado para o idioma exigido: {idioma_detectado}.\n"
        f"4. As chaves 'competencias' e 'idiomas' devem ser obrigatoriamente listas de strings (ex: [\"Ingles - Fluente\"]). NUNCA use dicionarios dentro destas listas.\n"
        f"5. Retorne SOMENTE o JSON puro.\n\n"
        f"Schema exigido: {_SCHEMA}\n\n"
        f"HISTORICO:\n{hist}\n\n"
        f"VAGA:\n{vaga}\n\n"
        f"CONTATOS: {perfil.get('email')}, {perfil.get('telefone')}, {perfil.get('linkedin')}"
    )
    
    resp = llm_client.models.generate_content(
        model="gemma-3-27b-it",
        contents=p,
        config=genai.types.GenerateContentConfig(temperature=0.1)
    )
    
    texto_json = resp.text.strip()
    texto_json = re.sub(r'^```json', '', texto_json, flags=re.IGNORECASE)
    texto_json = re.sub(r'^```', '', texto_json)
    texto_json = re.sub(r'```$', '', texto_json).strip()
    
    return json.loads(texto_json)

# =========================================================
# BANCO DE DADOS E HANDLERS (WELCOME PIPELINE)
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = buscar_usuario(update.effective_user.id)
    if u and u.get("email"):
        await update.message.reply_text("👋 Bem-vindo de volta! Seu perfil já está configurado.\n\n"
                                        "📌 *Como usar:*\n"
                                        "1️⃣ Envie ou cole seu *Histórico Profissional* para atualizar sua base.\n"
                                        "2️⃣ Cole a *Descrição da Vaga* que você deseja aplicar.\n\n"
                                        "O idioma e a formatação ideal serão detectados automaticamente!", parse_mode="Markdown")
        return ConversationHandler.END
    
    await update.message.reply_text("🚀 Olá! Eu sou seu **Assistente de Currículos ATS**.\n\n"
                                    "Vou te ajudar a criar currículos focados e otimizados para cada vaga de emprego, aumentando suas chances de contratação.\n\n"
                                    "Para começar, vamos configurar seus dados fixos.\n"
                                    "✉️ Qual o seu **E-MAIL** profissional?", parse_mode="Markdown")
    return ASK_EMAIL

async def ask_email(u, c): 
    c.user_data['e'] = u.message.text
    await u.message.reply_text("📱 Perfeito. Qual o seu número de **TELEFONE** (com DDD)?", parse_mode="Markdown")
    return ASK_PHONE

async def ask_phone(u, c): 
    c.user_data['t'] = u.message.text
    await u.message.reply_text("🔗 Ótimo! Para finalizar, envie o link ou usuário do seu **LINKEDIN**.", parse_mode="Markdown")
    return ASK_LINKEDIN

async def ask_linkedin(u, c):
    salvar_perfil(u.effective_user.id, {
        "email": c.user_data['e'], 
        "telefone": c.user_data['t'], 
        "linkedin": u.message.text
    })
    await u.message.reply_text("✅ **Perfil salvo com sucesso!**\n\n"
                               "Agora a mágica acontece. Siga os dois passos abaixo:\n\n"
                               "📄 **Passo 1:** Me envie seu currículo atual (pode ser arquivo PDF, TXT, ou colar o texto aqui) para eu criar sua base de dados.\n"
                               "🎯 **Passo 2:** Cole o texto da vaga de emprego que você quer aplicar.\n\n"
                               "O idioma de saída será detectado automaticamente baseado na vaga que você enviar. Vamos lá?", parse_mode="Markdown")
    return ConversationHandler.END

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Use o comando /start para configurar seu perfil primeiro.")
        return

    status = await update.message.reply_text("Analisando seus dados...")
    
    if update.message.document:
        f = await context.bot.get_file(update.message.document.file_id)
        b = bytearray(); await f.download_as_bytearray(out=b)
        texto = extrair_texto_de_arquivo(b, update.message.document.file_name)
    else:
        texto = update.message.text

    if not texto.strip():
        await status.edit_text("Nenhum texto detectado no envio.")
        return

    from google.genai import errors as genai_errors

    try:
        classificacao = classificar_intencao_e_idioma_llm(texto)
        intencao = classificacao.get("intencao", "VAGA")
        idioma_detectado = classificacao.get("idioma", "Idioma original do texto")
        
        if intencao == "HISTORICO":
            await status.edit_text("🔄 Atualizando sua base de conhecimentos profissional...")
            novo_h = consolidar_historico_llm(usuario.get("raw_history", ""), texto)
            salvar_perfil(user_id, {"raw_history": novo_h})
            await status.edit_text("✅ Histórico atualizado com sucesso! Agora você pode enviar a descrição da vaga.")
        else:
            if not usuario.get("raw_history"):
                await status.edit_text("⚠️ Envie seu histórico profissional antes de mandar a vaga."); return
            
            await status.edit_text(f"🎯 Vaga detectada! Gerando currículo focado em {idioma_detectado}...")
            dados = gerar_curriculo_json(usuario["raw_history"], texto, usuario, idioma_detectado)
            pdf = gerar_pdf(dados)
            nome = safe_string(dados.get("contato", {}).get("nome", "Candidato")).replace(" ", "_")
            await update.message.reply_document(document=pdf, filename=f"CV_{nome}_ATS.pdf", caption=f"📄 Currículo gerado em: {idioma_detectado}")
            await status.delete()

    except genai_errors.ClientError as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            logger.warning(f"Limite de cota da API atingido.")
            await status.edit_text("⏳ O servidor de Inteligência Artificial está sobrecarregado. Por favor, aguarde cerca de 1 minuto e envie sua mensagem novamente.")
        else:
            logger.error(f"Erro da API do Gemini: {e}")
            await status.edit_text("Ocorreu um erro ao comunicar com a IA. Tente novamente.")
            
    except json.JSONDecodeError as e:
        logger.error(f"Erro JSON: {e}")
        await status.edit_text("⚠️ A Inteligência Artificial falhou ao formatar o documento final. Por favor, tente enviar a vaga novamente.")
        
    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        await status.edit_text("❌ Erro interno ao gerar o PDF da sua aplicação.")

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=60, read_timeout=60)).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_LINKEDIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_linkedin)],
        },
        fallbacks=[CommandHandler("start", cmd_start)]
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), handle_input))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
