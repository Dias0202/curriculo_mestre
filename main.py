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
# CONFIGURACAO DE ESTADOS E LOGGING
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

llm_client: genai.Client = genai.Client(api_key=GEMINI_API_KEY)
db_client:  Client       = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# SERVIDOR WEB (KEEP-ALIVE)
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
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# =========================================================
# UTILITARIOS CORE
# =========================================================
_SUBS = {"\u2022": "-", "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00b7": "-", "\u2026": "..."}

def sanitize(text: str) -> str:
    if not text: return ""
    text = str(text).replace("\t", " ")
    for char, rep in _SUBS.items(): text = text.replace(char, rep)
    return text.encode("latin-1", "ignore").decode("latin-1")

def safe_string(val) -> str:
    if isinstance(val, dict): return " - ".join([str(v) for v in val.values() if v])
    if isinstance(val, list): return ", ".join([str(v) for v in val if v])
    if val is None: return ""
    return str(val).strip()

def extrair_texto_de_arquivo(file_bytes: bytearray, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            return "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
        except Exception as e:
            logger.error(f"Erro PDF: {e}")
            return ""
    for enc in ["utf-8", "latin-1", "cp1252"]:
        try: return file_bytes.decode(enc)
        except UnicodeDecodeError: continue
    return file_bytes.decode("utf-8", errors="ignore")

# =========================================================
# GERADOR DE PDF - FORMATO HARVARD
# =========================================================
class CurriculoHarvard(FPDF):
    def __init__(self):
        super().__init__()
        self.set_margins(15, 15, 15)
        self.add_page()
        self.set_auto_page_break(True, margin=15)

    def cabecalho_candidato(self, d: dict):
        # Nome
        self.set_font("helvetica", "B", 18)
        self.multi_cell(0, 8, sanitize(safe_string(d.get("nome", "Candidato")).upper()), align="C", new_x="LMARGIN", new_y="NEXT")
        
        # Titulo Profissional
        titulo = safe_string(d.get("titulo", ""))
        if titulo:
            self.set_font("helvetica", "B", 12)
            self.multi_cell(0, 6, sanitize(titulo), align="C", new_x="LMARGIN", new_y="NEXT")

        # Contatos
        partes = [safe_string(d.get(k)) for k in ["localizacao", "telefone", "email", "linkedin", "github", "portfolio"] if d.get(k)]
        if partes:
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 5, sanitize(" | ".join(partes)), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def secao(self, titulo):
        if not titulo: return
        self.set_font("helvetica", "B", 12)
        self.cell(0, 8, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def item_experiencia(self, exp: dict):
        if not isinstance(exp, dict): return
        
        self.set_font("helvetica", "B", 11)
        cargo = safe_string(exp.get('cargo',''))
        empresa = safe_string(exp.get('empresa',''))
        header = f"{cargo} - {empresa}" if cargo and empresa else cargo or empresa
        self.multi_cell(0, 6, sanitize(header), new_x="LMARGIN", new_y="NEXT")
        
        self.set_font("helvetica", "I", 10)
        loc = safe_string(exp.get('localizacao', ''))
        dt_in = safe_string(exp.get('data_inicio', ''))
        dt_fim = safe_string(exp.get('data_fim', ''))
        periodo = f"{dt_in} - {dt_fim}" if dt_in and dt_fim else dt_in or dt_fim
        sub_header = f"{loc} | {periodo}" if loc and periodo else loc or periodo
        if sub_header:
            self.multi_cell(0, 5, sanitize(sub_header), new_x="LMARGIN", new_y="NEXT")
            
        desc_empresa = safe_string(exp.get('descricao_empresa', ''))
        if desc_empresa:
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 5, sanitize(desc_empresa), new_x="LMARGIN", new_y="NEXT")

        self.set_font("helvetica", "", 10)
        
        resp = exp.get("responsabilidades", [])
        if isinstance(resp, list):
            for r in resp:
                r_str = safe_string(r)
                if r_str: self.multi_cell(0, 5, sanitize(f"- {r_str}"), new_x="LMARGIN", new_y="NEXT")
                
        conquistas = exp.get("conquistas", [])
        if isinstance(conquistas, list):
            for c in conquistas:
                c_str = safe_string(c)
                if c_str: self.multi_cell(0, 5, sanitize(f"- {c_str}"), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def lista_duas_colunas(self, itens: list):
        self.set_font("helvetica", "", 10)
        col_w = (self.w - self.l_margin - self.r_margin) / 2.0
        x_left = self.l_margin
        x_right = self.l_margin + col_w

        # Filtra itens nulos ou vazios
        itens_limpos = [safe_string(i) for i in itens if safe_string(i)]

        for i in range(0, len(itens_limpos), 2):
            y_start = self.get_y()
            
            # Item Esquerda
            item1 = sanitize(f"- {itens_limpos[i]}")
            self.set_xy(x_left, y_start)
            self.multi_cell(col_w - 5, 5, item1)
            y_end_1 = self.get_y()
            
            # Item Direita
            y_end_2 = y_start
            if i + 1 < len(itens_limpos):
                item2 = sanitize(f"- {itens_limpos[i+1]}")
                self.set_xy(x_right, y_start)
                self.multi_cell(col_w - 5, 5, item2)
                y_end_2 = self.get_y()
            
            # Retorna o cursor Y para a base do maior item renderizado
            self.set_y(max(y_end_1, y_end_2))
            self.set_x(self.l_margin)
            
        self.ln(3)

def gerar_pdf(dados: dict) -> io.BytesIO:
    if not isinstance(dados, dict): dados = {}
    pdf = CurriculoHarvard()
    
    cabecalhos = dados.get("cabecalhos", {})
    
    pdf.cabecalho_candidato(dados.get("identificacao", {}))
    
    if dados.get("resumo"):
        pdf.secao(safe_string(cabecalhos.get("resumo", "Professional Summary")))
        pdf.set_font("helvetica", "", 10)
        pdf.multi_cell(0, 5, sanitize(safe_string(dados["resumo"])), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    habilidades = dados.get("competencias", [])
    if habilidades and isinstance(habilidades, list):
        pdf.secao(safe_string(cabecalhos.get("competencias", "Core Competencies")))
        pdf.lista_duas_colunas(habilidades)

    exps = dados.get("experiencias", [])
    if exps and isinstance(exps, list):
        pdf.secao(safe_string(cabecalhos.get("experiencias", "Professional Experience")))
        for item in exps:
            if isinstance(item, dict): pdf.item_experiencia(item)

    edus = dados.get("educacao", [])
    if edus and isinstance(edus, list):
        pdf.secao(safe_string(cabecalhos.get("educacao", "Education")))
        for item in edus:
            if not isinstance(item, dict): continue
            pdf.set_font("helvetica", "B", 11)
            grau = safe_string(item.get("grau", ""))
            inst = safe_string(item.get("instituicao", ""))
            header = f"{grau} - {inst}" if grau and inst else grau or inst
            pdf.multi_cell(0, 6, sanitize(header), new_x="LMARGIN", new_y="NEXT")
            
            pdf.set_font("helvetica", "I", 10)
            dt_in = safe_string(item.get('ano_inicio', ''))
            dt_fim = safe_string(item.get('ano_fim', ''))
            periodo = f"{dt_in} - {dt_fim}" if dt_in and dt_fim else dt_in or dt_fim
            if periodo:
                pdf.multi_cell(0, 5, sanitize(periodo), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    projetos = dados.get("projetos", [])
    if projetos and isinstance(projetos, list):
        pdf.secao(safe_string(cabecalhos.get("projetos", "Projects")))
        for p in projetos:
            if not isinstance(p, dict): continue
            pdf.set_font("helvetica", "B", 11)
            pdf.multi_cell(0, 6, sanitize(safe_string(p.get("nome", ""))), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("helvetica", "", 10)
            pdf.multi_cell(0, 5, sanitize(safe_string(p.get("descricao", ""))), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    certificacoes = dados.get("certificacoes", [])
    if certificacoes and isinstance(certificacoes, list):
        pdf.secao(safe_string(cabecalhos.get("certificacoes", "Certifications")))
        pdf.lista_duas_colunas(certificacoes)

    idiomas = dados.get("idiomas", [])
    if idiomas and isinstance(idiomas, list):
        pdf.secao(safe_string(cabecalhos.get("idiomas", "Languages")))
        pdf.lista_duas_colunas(idiomas)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# LOGICA LLM 
# =========================================================
def classificar_intencao_e_idioma_llm(texto) -> dict:
    p = (
        "Responda APENAS com um JSON valido contendo as chaves 'intencao' e 'idioma'.\n"
        "Regras:\n"
        "1. 'intencao': 'VAGA' se for descricao de emprego, ou 'HISTORICO' se for curriculo base.\n"
        "2. 'idioma': Idioma original do texto ou o idioma solicitado explicitamente.\n\n"
        f"TEXTO:\n{texto[:1500]}"
    )
    try:
        resp = llm_client.models.generate_content(model="gemma-3-27b-it", contents=p, config=genai.types.GenerateContentConfig(temperature=0.0))
        t = re.sub(r'^```json|```$', '', resp.text.strip(), flags=re.IGNORECASE).strip()
        return json.loads(t)
    except:
        return {"intencao": "VAGA", "idioma": "Ingles"}

def consolidar_historico_llm(atual, novo):
    p = f"Mescle as novas informacoes ao historico atual. Retorne apenas o texto consolidado:\n\nATUAL:\n{atual}\n\nNOVA:\n{novo}"
    return llm_client.models.generate_content(model="gemma-3-27b-it", contents=p).text.strip()

def gerar_curriculo_json(hist, vaga, perfil, idioma_detectado):
    try:
        with open("prompt.md", "r", encoding="utf-8") as f:
            template_prompt = f.read()
    except Exception as e:
        logger.error("Erro ao carregar prompt.md. Certifique-se de que o arquivo existe.")
        raise e

    # Substitui as variaveis no arquivo markdown
    prompt_final = template_prompt.replace("{idioma_detectado}", idioma_detectado)\
                                  .replace("{nome}", perfil.get("nome", ""))\
                                  .replace("{telefone}", perfil.get("telefone", ""))\
                                  .replace("{email}", perfil.get("email", ""))\
                                  .replace("{linkedin}", perfil.get("linkedin", ""))\
                                  .replace("{historico}", hist)\
                                  .replace("{vaga}", vaga)
    
    resp = llm_client.models.generate_content(
        model="gemma-3-27b-it",
        contents=prompt_final,
        config=genai.types.GenerateContentConfig(temperature=0.1)
    )
    
    texto_json = resp.text.strip()
    texto_json = re.sub(r'^```json', '', texto_json, flags=re.IGNORECASE)
    texto_json = re.sub(r'^```', '', texto_json)
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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = buscar_usuario(update.effective_user.id)
    if u and u.get("email"):
        await update.message.reply_text("Bem-vindo de volta! Seu perfil ja esta configurado.\n\n"
                                        "1. Envie ou cole seu Historico Profissional.\n"
                                        "2. Cole a Descricao da Vaga.\n"
                                        "O idioma e a formatacao ideal serao aplicados automaticamente.")
        return ConversationHandler.END
    
    await update.message.reply_text("Bem-vindo ao Gerador de Curriculos ATS.\n\nQual o seu E-MAIL profissional?")
    return ASK_EMAIL

async def ask_email(u, c): 
    c.user_data['e'] = u.message.text
    await u.message.reply_text("Qual o seu numero de TELEFONE (com DDD)?")
    return ASK_PHONE

async def ask_phone(u, c): 
    c.user_data['t'] = u.message.text
    await u.message.reply_text("Envie o link ou usuario do seu LINKEDIN.")
    return ASK_LINKEDIN

async def ask_linkedin(u, c):
    # Recupera o nome de usuario do Telegram para preencher o nome no CV padrao
    nome_usuario = u.effective_user.full_name or "Candidato"
    salvar_perfil(u.effective_user.id, {
        "nome": nome_usuario,
        "email": c.user_data['e'], 
        "telefone": c.user_data['t'], 
        "linkedin": u.message.text
    })
    await u.message.reply_text("Perfil salvo! Envie seu historico (.pdf, .txt ou texto livre) e depois a vaga.")
    return ConversationHandler.END

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Use o comando /start para iniciar.")
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
        idioma_detectado = classificacao.get("idioma", "Ingles")
        
        if intencao == "HISTORICO":
            novo_h = consolidar_historico_llm(usuario.get("raw_history", ""), texto)
            salvar_perfil(user_id, {"raw_history": novo_h})
            await status.edit_text("Historico atualizado com sucesso! Agora voce pode enviar a descricao da vaga.")
        else:
            if not usuario.get("raw_history"):
                await status.edit_text("Envie seu historico profissional antes de mandar a vaga."); return
            
            await status.edit_text(f"Vaga detectada! Gerando curriculo focado em {idioma_detectado}...")
            dados = gerar_curriculo_json(usuario["raw_history"], texto, usuario, idioma_detectado)
            pdf = gerar_pdf(dados)
            nome = safe_string(dados.get("identificacao", {}).get("nome", "Candidato")).replace(" ", "_")
            await update.message.reply_document(document=pdf, filename=f"CV_{nome}_ATS.pdf")
            await status.delete()

    except genai_errors.ClientError as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            await status.edit_text("Servidor sobrecarregado por limite de cota. Aguarde 1 minuto.")
        else:
            logger.error(f"Erro da API do Gemini: {e}")
            await status.edit_text("Ocorreu um erro ao comunicar com a IA.")
            
    except json.JSONDecodeError as e:
        logger.error(f"Erro JSON: {e}")
        await status.edit_text("A Inteligencia Artificial falhou ao formatar o documento final.")
        
    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        await status.edit_text("Erro interno ao gerar o PDF da sua aplicacao.")

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
