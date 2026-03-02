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
import docx

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
# UTILITARIOS E EXTRACAO
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
    ext = filename.lower()
    texto_extraido = ""
    
    if ext.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            paginas = []
            for p in reader.pages:
                ext_txt = p.extract_text(extraction_mode="layout")
                if ext_txt: paginas.append(ext_txt)
            texto_extraido = "\n".join(paginas)
        except Exception as e:
            logger.error(f"Erro PDF: {e}")
    elif ext.endswith(".docx"):
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            texto_extraido = "\n".join([p.text for p in doc.paragraphs])
        except Exception as e:
            logger.error(f"Erro DOCX: {e}")
    else:
        for enc in ["utf-8", "latin-1", "cp1252"]:
            try: 
                texto_extraido = file_bytes.decode(enc)
                break
            except UnicodeDecodeError: continue
        if not texto_extraido:
            texto_extraido = file_bytes.decode("utf-8", errors="ignore")

    return texto_extraido.replace('\x00', ' ').strip()

# =========================================================
# OPERACOES DE BANCO DE DADOS RELACIONAL (SUPABASE)
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
        bull_res = db_client.table("experience_bullets").select("*").eq("experience_id", exp["id"]).execute()
        bullets = bull_res.data or []
        exp["responsabilidades"] = [b["texto"] for b in bullets if b["tipo"] == "responsabilidade"]
        exp["conquistas"] = [b["texto"] for b in bullets if b["tipo"] == "conquista"]
    profile["experiences"] = experiences

    profile["education"] = db_client.table("education").select("*").eq("user_id", user_uuid).execute().data or []
    profile["skills"] = db_client.table("skills").select("*").eq("user_id", user_uuid).execute().data or []
    profile["certifications"] = db_client.table("certifications").select("*").eq("user_id", user_uuid).execute().data or []
    profile["projects"] = db_client.table("projects").select("*").eq("user_id", user_uuid).execute().data or []
    profile["languages"] = db_client.table("languages").select("*").eq("user_id", user_uuid).execute().data or []
    
    return json.dumps(profile, ensure_ascii=False, indent=2)

def save_parsed_history_to_db(user_uuid: str, parsed_json: dict):
    if not user_uuid: return

    db_client.table("experiences").delete().eq("user_id", user_uuid).execute()
    db_client.table("education").delete().eq("user_id", user_uuid).execute()
    db_client.table("skills").delete().eq("user_id", user_uuid).execute()
    db_client.table("certifications").delete().eq("user_id", user_uuid).execute()
    db_client.table("projects").delete().eq("user_id", user_uuid).execute()
    db_client.table("languages").delete().eq("user_id", user_uuid).execute()

    for exp in parsed_json.get("experiences", []):
        exp_data = {
            "user_id": user_uuid,
            "cargo": exp.get("cargo"),
            "empresa": exp.get("empresa"),
            "localizacao": exp.get("localizacao"),
            "data_inicio": exp.get("data_inicio"),
            "data_fim": exp.get("data_fim"),
            "descricao_empresa": exp.get("descricao_empresa")
        }
        res = db_client.table("experiences").insert(exp_data).execute()
        if res.data:
            exp_id = res.data[0]["id"]
            bullets = []
            for r in exp.get("responsabilidades", []):
                if r: bullets.append({"experience_id": exp_id, "tipo": "responsabilidade", "texto": r})
            for c in exp.get("conquistas", []):
                if c: bullets.append({"experience_id": exp_id, "tipo": "conquista", "texto": c})
            if bullets:
                db_client.table("experience_bullets").insert(bullets).execute()

    edu_list = [{"user_id": user_uuid, "grau": ed.get("grau"), "instituicao": ed.get("instituicao"), "ano_inicio": ed.get("ano_inicio"), "ano_fim": ed.get("ano_fim")} for ed in parsed_json.get("education", [])]
    if edu_list: db_client.table("education").insert(edu_list).execute()

    skill_list = [{"user_id": user_uuid, "nome": sk.get("nome"), "categoria": sk.get("categoria"), "nivel": sk.get("nivel")} for sk in parsed_json.get("skills", [])]
    if skill_list: db_client.table("skills").insert(skill_list).execute()

    cert_list = [{"user_id": user_uuid, "nome": cert.get("nome"), "emissor": cert.get("emissor"), "ano": cert.get("ano")} for cert in parsed_json.get("certifications", [])]
    if cert_list: db_client.table("certifications").insert(cert_list).execute()

    proj_list = [{"user_id": user_uuid, "nome": proj.get("nome"), "descricao": proj.get("descricao")} for proj in parsed_json.get("projects", [])]
    if proj_list: db_client.table("projects").insert(proj_list).execute()

    lang_list = [{"user_id": user_uuid, "idioma": lang.get("idioma"), "nivel": lang.get("nivel")} for lang in parsed_json.get("languages", [])]
    if lang_list: db_client.table("languages").insert(lang_list).execute()

# =========================================================
# GERADOR DE PDF
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

        itens_limpos = [safe_string(i) for i in itens if safe_string(i)]

        for i in range(0, len(itens_limpos), 2):
            y_start = self.get_y()
            
            item1 = sanitize(f"- {itens_limpos[i]}")
            self.set_xy(x_left, y_start)
            self.multi_cell(col_w - 5, 5, item1)
            y_end_1 = self.get_y()
            
            y_end_2 = y_start
            if i + 1 < len(itens_limpos):
                item2 = sanitize(f"- {itens_limpos[i+1]}")
                self.set_xy(x_right, y_start)
                self.multi_cell(col_w - 5, 5, item2)
                y_end_2 = self.get_y()
            
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
# LOGICA LLM E ROTEAMENTO
# =========================================================
def classificar_intencao_e_idioma_llm(texto) -> dict:
    p = (
        "Analise o texto fornecido e classifique a intencao do usuario. Responda APENAS com um JSON valido contendo as chaves 'intencao' e 'idioma'.\n"
        "Regras:\n"
        "1. 'intencao': Retorne 'VAGA' se o texto for uma descricao de emprego. Retorne 'HISTORICO' se o texto contiver dados profissionais reais. Retorne 'IRRELEVANTE' se for apenas uma saudacao, mensagem fora de contexto profissional ou conversa fiada.\n"
        "2. 'idioma': Idioma original do texto ou idioma explicitamente exigido.\n\n"
        f"TEXTO:\n{texto[:1500]}"
    )
    try:
        resp = llm_client.models.generate_content(model="gemma-3-27b-it", contents=p, config=genai.types.GenerateContentConfig(temperature=0.0))
        t = re.sub(r'^```json|```$', '', resp.text.strip(), flags=re.IGNORECASE).strip()
        return json.loads(t)
    except:
        return {"intencao": "VAGA", "idioma": "Ingles"}

def extrair_e_mesclar_historico(perfil_atual_str, nova_entrada):
    try:
        with open("prompt_parser.md", "r", encoding="utf-8") as f:
            template = f.read()
    except Exception as e:
        logger.error("Erro ao carregar prompt_parser.md.")
        raise e
        
    prompt_final = template.replace("{perfil_atual}", perfil_atual_str).replace("{nova_entrada}", nova_entrada)
    
    resp = llm_client.models.generate_content(model="gemma-3-27b-it", contents=prompt_final, config=genai.types.GenerateContentConfig(temperature=0.1))
    texto_json = re.sub(r'^```json|```$', '', resp.text.strip(), flags=re.IGNORECASE).strip()
    return json.loads(texto_json)

def gerar_curriculo_json(hist, vaga, perfil, idioma_detectado):
    try:
        with open("prompt_generator.md", "r", encoding="utf-8") as f:
            template = f.read()
    except Exception as e:
        logger.error("Erro ao carregar prompt_generator.md.")
        raise e

    prompt_final = template.replace("{idioma_detectado}", safe_string(idioma_detectado))\
                           .replace("{nome}", safe_string(perfil.get("nome")))\
                           .replace("{telefone}", safe_string(perfil.get("telefone")))\
                           .replace("{email}", safe_string(perfil.get("email")))\
                           .replace("{linkedin}", safe_string(perfil.get("linkedin")))\
                           .replace("{github}", safe_string(perfil.get("github")))\
                           .replace("{portfolio}", safe_string(perfil.get("portfolio")))\
                           .replace("{historico}", safe_string(hist))\
                           .replace("{vaga}", safe_string(vaga))
    
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
# HANDLERS DO TELEGRAM
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    nome_usuario = update.effective_user.first_name or "Profissional"
    
    u = get_user_by_telegram(user_id)
    if u and u.get("email"):
        await update.message.reply_text(
            f"Ola, {nome_usuario}. O seu perfil encontra-se ativo no banco de dados.\n\n"
            "Instrucoes operacionais:\n"
            "1. Envie seu Historico Profissional para atualizar o sistema.\n"
            "2. Envie a Descricao da Vaga para gerar o documento formatado e obter o relatorio analitico.\n"
            "3. Utilize o comando /deletar caso deseje remover seus dados de nossos servidores."
        )
        return ConversationHandler.END
    
    await update.message.reply_text(f"Ola, {nome_usuario}. Iniciando o assistente de otimizacao ATS.\n\nPor favor, informe o seu E-MAIL corporativo ou profissional.")
    return ASK_EMAIL

async def ask_email(u, c): 
    c.user_data['e'] = u.message.text
    await u.message.reply_text("Informacao registrada. Qual e o seu numero de TELEFONE (com DDD)?")
    return ASK_PHONE

async def ask_phone(u, c): 
    c.user_data['t'] = u.message.text
    await u.message.reply_text("Certo. Agora, envie a URL ou o nome de usuario correspondente ao seu perfil no LINKEDIN.")
    return ASK_LINKEDIN

async def ask_linkedin(u, c):
    nome_usuario = u.effective_user.full_name or "Candidato"
    save_user_base(u.effective_user.id, {
        "nome": nome_usuario,
        "email": c.user_data['e'], 
        "telefone": c.user_data['t'], 
        "linkedin": u.message.text
    })
    await u.message.reply_text("Perfil base estruturado. Voce ja pode submeter o seu historico profissional nos formatos .pdf, .docx, .txt ou em texto simples.")
    return ConversationHandler.END

async def cmd_deletar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = get_user_by_telegram(user_id)
    if u:
        db_client.table("users").delete().eq("id", u["id"]).execute()
        await update.message.reply_text("Processo concluido. Todos os seus dados foram permanentemente excluidos da base de dados.")
    else:
        await update.message.reply_text("Nenhum registro correspondente foi localizado no sistema.")

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_telegram = update.effective_user.id
    nome_usuario = update.effective_user.first_name or "Profissional"
    
    usuario = get_user_by_telegram(user_id_telegram)
    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Cadastro base ausente. Execute o comando /start para configurar a sua conta.")
        return

    status = await update.message.reply_text("Aguarde. Inicializando processos de analise e validacao...")
    
    if update.message.document:
        f = await context.bot.get_file(update.message.document.file_id)
        b = await f.download_as_bytearray()
        texto = extrair_texto_de_arquivo(b, update.message.document.file_name)
        tipo_raw = update.message.document.file_name.split('.')[-1].lower() if '.' in update.message.document.file_name else 'arquivo'
    else:
        texto = update.message.text
        tipo_raw = 'texto'

    if not texto or not texto.strip():
        await status.edit_text("Nenhum dado legivel identificado na entrada.")
        return

    from google.genai import errors as genai_errors

    try:
        classificacao = classificar_intencao_e_idioma_llm(texto)
        intencao = classificacao.get("intencao", "VAGA")
        idioma_detectado = classificacao.get("idioma", "Ingles")
        
        if intencao == "IRRELEVANTE":
            await status.edit_text(f"Ola, {nome_usuario}. O sistema atua com o processamento de dados profissionais ou requisitos de vagas. Forneca uma entrada estruturada para prosseguirmos.")
            return

        db_client.table("raw_inputs").insert({"user_id": usuario["id"], "tipo": tipo_raw[:50], "conteudo_texto": texto}).execute()
        
        perfil_atual_str = fetch_full_user_profile(usuario["id"])

        if intencao == "HISTORICO":
            await status.edit_text("Atualizando as tabelas relacionais do seu perfil com os novos dados...")
            
            parsed_json = extrair_e_mesclar_historico(perfil_atual_str, texto)
            save_parsed_history_to_db(usuario["id"], parsed_json)
            
            await status.edit_text("Concluido. O banco de dados foi integrado. O sistema esta pronto para processar os requisitos de uma vaga.")
        else:
            if not perfil_atual_str or len(perfil_atual_str) < 50:
                await status.edit_text("Base de dados insuficiente. Carregue o seu historico profissional antes de prosseguir."); return
            
            await status.edit_text(f"Iniciando a geracao e traducao do documento ATS para: {idioma_detectado}.")
            dados = gerar_curriculo_json(perfil_atual_str, texto, usuario, idioma_detectado)
            
            # Remocao da chave extra para n quebrar o construtor JSON do Supabase
            json_para_banco = {k: v for k, v in dados.items() if k != "relatorio_analitico"}
            db_client.table("generated_resumes").insert({
                "user_id": usuario["id"], 
                "vaga_texto": texto, 
                "idioma": idioma_detectado, 
                "json_gerado": json_para_banco,
                "score_match": dados.get("relatorio_analitico", {}).get("match_score")
            }).execute()

            pdf = gerar_pdf(dados)
            nome_arquivo = safe_string(dados.get("identificacao", {}).get("nome", "Candidato")).replace(" ", "_")
            
            relatorio = dados.get("relatorio_analitico", {})
            match_score = safe_string(relatorio.get("match_score", "N/A"))
            gaps = ", ".join(relatorio.get("analise_gaps", []))
            dica = safe_string(relatorio.get("dica_entrevista", "N/A"))
            
            resumo_executivo = (
                f"Documento gerado com exito.\n\n"
                f"Taxa de Compatibilidade (Match): {match_score}%\n"
                f"Lacunas Identificadas (Gaps): {gaps}\n\n"
                f"Preparacao Estrategica para a Entrevista:\n{dica}"
            )
            
            await update.message.reply_document(document=pdf, filename=f"CV_{nome_arquivo}_ATS.pdf", caption=resumo_executivo)
            await status.delete()

    except genai_errors.ClientError as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            await status.edit_text("O limite de cota da API foi atingido. Aguarde um instante e repita a operacao.")
        else:
            logger.error(f"Erro da API: {e}")
            await status.edit_text("Falha de comunicacao com o modulo de Inteligencia Artificial.")
            
    except json.JSONDecodeError as e:
        logger.error(f"Erro JSON: {e}")
        await status.edit_text("Ocorreu uma falha estrutural ao formatar a saida. Envie os dados novamente.")
        
    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        await status.edit_text("Ocorreu um erro interno imprevisto durante o processamento da requisicao.")

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
    app.add_handler(CommandHandler("deletar", cmd_deletar))
    app.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), handle_input))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
