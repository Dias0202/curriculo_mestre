import os
import io
import re
import json
import logging
import hashlib
import threading
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo  # Python 3.9+ nativo, sem dependencia extra

from dotenv import load_dotenv
from groq import Groq
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from fpdf import FPDF
from supabase import create_client, Client

# =========================================================
# ESTADOS DA CONVERSA (ONBOARDING)
# =========================================================
ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN, ASK_LANGUAGE, ASK_CITY = range(5)

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
GROQ_API_KEY    = os.getenv("GROQ_API_KEY",    "").strip()
SUPABASE_URL    = os.getenv("SUPABASE_URL",    "").strip()
SUPABASE_KEY    = os.getenv("SUPABASE_KEY",    "").strip()

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("ERRO CRITICO: Variaveis de ambiente ausentes.")
    raise SystemExit(1)

# =========================================================
# CLIENTES EXTERNOS
# =========================================================
llm_client: Groq = Groq(api_key=GROQ_API_KEY)
db_client:  Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# SERVIDOR WEB — KEEP-ALIVE (Render.com)
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
    text = str(text).replace("\t", " ")
    for char, rep in _SUBS.items():
        text = text.replace(char, rep)
    return text.encode("latin-1", "ignore").decode("latin-1")

# =========================================================
# EXTRACAO DE TEXTO DE ARQUIVO (.txt e .pdf)
# =========================================================
def extrair_texto_de_arquivo(file_bytes: bytearray, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(bytes(file_bytes)))
            return "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
        except Exception as e:
            logger.error(f"[PDF Extract] Erro: {e}")
            return ""
    for enc in ["utf-8", "latin-1", "cp1252"]:
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
        self.set_margins(20, 20, 20)
        self.add_page()
        self.set_auto_page_break(True, margin=15)

    def cabecalho_candidato(self, d: dict):
        self.set_font("helvetica", "B", 16)
        self.multi_cell(0, 10, sanitize(d.get("nome", "Candidato")),
                        align="C", new_x="LMARGIN", new_y="NEXT")
        partes = [v for k, v in d.items() if k != "nome" and v and isinstance(v, str)]
        if partes:
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 6, sanitize(" | ".join(partes)),
                            align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def secao(self, titulo: str):
        self.set_font("helvetica", "B", 12)
        self.cell(0, 8, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def item_experiencia(self, exp: dict):
        if not isinstance(exp, dict):
            return
        self.set_font("helvetica", "B", 11)
        self.multi_cell(0, 6,
                        sanitize(f"{exp.get('cargo', '')} -- {exp.get('empresa', '')}"),
                        new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "I", 10)
        self.multi_cell(0, 5, sanitize(exp.get("periodo", "")),
                        new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "", 10)
        conquistas = exp.get("conquistas", [])
        if isinstance(conquistas, str):
            conquistas = [conquistas]
        if isinstance(conquistas, list):
            for b in conquistas:
                if b:
                    self.multi_cell(0, 5, sanitize(f"- {b}"),
                                    new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def item_educacao(self, item: dict):
        if not isinstance(item, dict):
            return
        self.set_font("helvetica", "B", 11)
        self.multi_cell(0, 6, sanitize(item.get("curso", "")),
                        new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5,
                        sanitize(f"{item.get('instituicao', '')} | {item.get('periodo', '')}"),
                        new_x="LMARGIN", new_y="NEXT")
        self.ln(2)


def gerar_pdf(dados: dict) -> io.BytesIO:
    if not isinstance(dados, dict):
        dados = {}
    pdf = CurriculoHarvard()
    pdf.cabecalho_candidato(dados.get("contato", {}))

    if dados.get("resumo"):
        pdf.secao("Resumo Profissional")
        pdf.set_font("helvetica", "", 10)
        pdf.multi_cell(0, 5, sanitize(dados["resumo"]), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    if dados.get("experiencias"):
        pdf.secao("Experiencia Profissional")
        for exp in dados["experiencias"]:
            pdf.item_experiencia(exp)

    if dados.get("educacao"):
        pdf.secao("Formacao Academica")
        for edu in dados["educacao"]:
            pdf.item_educacao(edu)

    for titulo, chave in [("Habilidades", "competencias"), ("Idiomas", "idiomas")]:
        lista = dados.get(chave, [])
        if isinstance(lista, str):
            lista = [lista]
        if lista and isinstance(lista, list):
            pdf.secao(titulo)
            pdf.set_font("helvetica", "", 10)
            for i in lista:
                if i:
                    pdf.multi_cell(0, 5, sanitize(f"- {i}"),
                                   new_x="LMARGIN", new_y="NEXT")

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# INTEGRACAO GROQ
# Modelo: llama-3.3-70b-versatile — melhor custo/beneficio gratuito
# API compativel com OpenAI: client.chat.completions.create()
# =========================================================
_MODEL = "llama-3.3-70b-versatile"


def _chat(system: str, prompt: str, json_mode: bool = False, temperature: float = 0.1) -> str:
    """
    Helper centralizado para todas as chamadas ao Groq.
    json_mode=True ativa response_format JSON garantido.
    """
    kwargs = dict(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        temperature=temperature,
        max_tokens=4096,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = llm_client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()

_SCHEMA = """{
  "contato": {"nome":"","email":"","telefone":"","linkedin":"","cidade":""},
  "resumo": "2-4 frases objetivas de impacto alinhadas a vaga",
  "experiencias": [{"cargo":"","empresa":"","periodo":"","conquistas":["verbo de acao + metrica"]}],
  "educacao": [{"curso":"","instituicao":"","periodo":""}],
  "competencias": [""],
  "certificacoes": [""],
  "idiomas": ["Idioma - Nivel"]
}"""

_SYSTEM_CV = (
    "Voce e um recrutador tecnico senior especialista em curriculos ATS no padrao Harvard. "
    "REGRAS: Retorne SOMENTE JSON valido, sem markdown. "
    f"Schema: {_SCHEMA}. "
    "Nunca invente dados. Use apenas ASCII. "
    "Campos sem info: string vazia ou lista vazia."
)


def classificar_intencao_llm(texto: str) -> str:
    raw = _chat(
        system="Voce classifica textos profissionais. Responda APENAS com uma palavra: VAGA ou HISTORICO.",
        prompt=(
            "Classifique como 'VAGA' (descricao de cargo/emprego) "
            "ou 'HISTORICO' (historico profissional/curriculo de uma pessoa).\n\n"
            f"{texto[:1500]}"
        ),
        temperature=0.0,
    )
    return "VAGA" if "VAGA" in raw.upper() else "HISTORICO"


def consolidar_historico_llm(atual: str, novo: str) -> str:
    if not atual:
        return novo
    return _chat(
        system="Voce e um assistente de RH. Consolide historicos profissionais sem inventar dados.",
        prompt=(
            "Mescle ao historico atual as novas informacoes, removendo duplicatas. "
            "Retorne apenas o texto consolidado.\n\n"
            f"ATUAL:\n{atual}\n\nNOVO:\n{novo}"
        ),
        temperature=0.0,
    )


def extrair_keywords_perfil(historico: str) -> dict:
    """Extrai cargo-alvo e palavras-chave do historico para busca de vagas."""
    raw = _chat(
        system='Voce extrai informacoes de historicos profissionais. Retorne SOMENTE JSON valido.',
        prompt=(
            "Analise o historico e retorne JSON com:\n"
            '{"cargo": "cargo mais recente ou desejado", '
            '"keywords": "3-5 palavras-chave tecnicas separadas por espaco", '
            '"area": "area profissional em 2 palavras"}\n\n'
            f"{historico[:3000]}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    try:
        return json.loads(re.sub(r"```json|```", "", raw).strip())
    except Exception:
        return {"cargo": "profissional", "keywords": "", "area": "geral"}


def selecionar_melhores_vagas(historico: str, vagas: list) -> list:
    """Groq seleciona as 2 vagas com maior aderencia ao perfil."""
    lista_vagas = "\n".join([
        f"{i}. {v.get('title','')} - {v.get('company','')} ({v.get('location','')}): {v.get('description','')[:300]}"
        for i, v in enumerate(vagas)
    ])
    raw = _chat(
        system='Voce e um recrutador tecnico. Retorne SOMENTE JSON valido.',
        prompt=(
            "Selecione os indices das 2 vagas com MAIOR aderencia ao perfil do candidato.\n"
            'Retorne APENAS: {"indices": [0, 1]}\n\n'
            f"HISTORICO:\n{historico[:2000]}\n\n"
            f"VAGAS:\n{lista_vagas}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    try:
        indices = json.loads(re.sub(r"```json|```", "", raw).strip()).get("indices", [0, 1])
        return [vagas[i] for i in indices if i < len(vagas)][:2]
    except Exception:
        return vagas[:2]


def gerar_curriculo_json(historico: str, vaga: str, perfil: dict) -> dict:
    raw = _chat(
        system=_SYSTEM_CV,
        prompt=(
            f"HISTORICO DO CANDIDATO:\n{historico}\n\n"
            f"DESCRICAO DA VAGA:\n{vaga}\n\n"
            f"CONTATOS: Email: {perfil.get('email','')}, "
            f"Telefone: {perfil.get('telefone','')}, "
            f"LinkedIn: {perfil.get('linkedin','')}, "
            f"Cidade: {perfil.get('cidade','')}\n"
            f"IDIOMA: {perfil.get('idioma', 'Portugues')}\n\n"
            "Gere o curriculo otimizado no formato JSON especificado."
        ),
        json_mode=True,
        temperature=0.1,
    )
    return json.loads(re.sub(r"```json|```", "", raw).strip())

# =========================================================
# SCRAPER DE VAGAS — LINKEDIN (via python-jobspy, 100% gratuito)
# JobSpy raspa vagas publicas do LinkedIn sem autenticacao.
# Documentacao: https://github.com/Bunsly/JobSpy
# =========================================================
def buscar_vagas_linkedin(cargo: str, keywords: str, cidade: str, quantidade: int = 10) -> list:
    """
    Busca vagas no LinkedIn usando JobSpy (scraper gratuito).
    Retorna lista de dicts com title, company, location, description, job_url.
    """
    try:
        from jobspy import scrape_jobs
        import pandas as pd

        # Monta a query de busca
        search_term = f"{cargo} {keywords}".strip()
        location    = cidade if cidade else "Brazil"

        logger.info(f"[JobSpy] Buscando: '{search_term}' em '{location}'")

        jobs_df = scrape_jobs(
            site_name=["linkedin"],
            search_term=search_term,
            location=location,
            results_wanted=quantidade,
            hours_old=72,          # Vagas dos ultimos 3 dias
            country_indeed="Brazil",
        )

        if jobs_df is None or jobs_df.empty:
            logger.warning("[JobSpy] Nenhuma vaga encontrada.")
            return []

        vagas = []
        for _, row in jobs_df.iterrows():
            vagas.append({
                "title":       str(row.get("title", "")),
                "company":     str(row.get("company", "")),
                "location":    str(row.get("location", "")),
                "description": str(row.get("description", ""))[:2000],
                "job_url":     str(row.get("job_url", "")),
            })

        logger.info(f"[JobSpy] {len(vagas)} vagas encontradas.")
        return vagas

    except ImportError:
        logger.error("[JobSpy] jobspy nao instalado. Adicione 'python-jobspy' ao requirements.txt")
        return []
    except Exception as e:
        logger.error(f"[JobSpy] Erro ao buscar vagas: {e}", exc_info=True)
        return []

# =========================================================
# SUPABASE
# =========================================================
def salvar_perfil(telegram_id: int, dados: dict):
    dados["telegram_id"] = str(telegram_id)
    db_client.table("user_profiles").upsert(
        dados, on_conflict="telegram_id"
    ).execute()


def buscar_usuario(telegram_id: int) -> dict | None:
    try:
        r = (db_client.table("user_profiles")
             .select("*")
             .eq("telegram_id", str(telegram_id))
             .execute())
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"[Supabase] Erro buscar_usuario: {e}")
        return None


def buscar_todos_usuarios() -> list:
    """Retorna todos os usuarios com historico salvo."""
    try:
        r = (db_client.table("user_profiles")
             .select("*")
             .not_.is_("raw_history", "null")
             .execute())
        return r.data or []
    except Exception as e:
        logger.error(f"[Supabase] Erro buscar_todos_usuarios: {e}")
        return []


def job_ja_enviado(telegram_id: int, job_hash: str) -> bool:
    """Verifica se esta vaga ja foi enviada para este usuario."""
    try:
        r = (db_client.table("sent_jobs")
             .select("id")
             .eq("telegram_id", str(telegram_id))
             .eq("job_hash", job_hash)
             .execute())
        return bool(r.data)
    except Exception:
        return False


def registrar_job_enviado(telegram_id: int, job_hash: str, job_title: str, job_company: str):
    """Registra vaga como enviada para evitar repeticao."""
    try:
        db_client.table("sent_jobs").insert({
            "telegram_id": str(telegram_id),
            "job_hash":    job_hash,
            "job_title":   job_title,
            "job_company": job_company,
        }).execute()
    except Exception as e:
        logger.error(f"[Supabase] Erro registrar_job_enviado: {e}")


def gerar_hash_vaga(vaga: dict) -> str:
    """Gera hash unico para identificar a vaga (title + company)."""
    chave = f"{vaga.get('title','')}{vaga.get('company','')}".lower().strip()
    return hashlib.md5(chave.encode()).hexdigest()

# =========================================================
# JOB DIARIO — ENVIO DE SUGESTOES AO MEIO-DIA (BRASILIA)
#
# Fluxo para cada usuario com historico:
# 1. Extrai keywords do perfil via Gemini
# 2. Busca vagas no LinkedIn via JobSpy
# 3. Gemini seleciona as 2 com maior match
# 4. Filtra vagas ja enviadas
# 5. Para cada vaga nova: gera CV adaptado + envia PDF + link
# =========================================================
async def enviar_sugestoes_diarias(context: ContextTypes.DEFAULT_TYPE):
    """Executado automaticamente todos os dias ao meio-dia (horario de Brasilia)."""
    logger.info("[Scheduler] Iniciando envio de sugestoes diarias...")

    usuarios = buscar_todos_usuarios()
    logger.info(f"[Scheduler] {len(usuarios)} usuarios com historico encontrados.")

    for usuario in usuarios:
        telegram_id  = usuario.get("telegram_id")
        historico    = usuario.get("raw_history", "")
        cidade       = usuario.get("cidade", "Brazil")

        if not telegram_id or not historico:
            continue

        try:
            # 1. Extrai palavras-chave do perfil
            perfil_keywords = extrair_keywords_perfil(historico)
            cargo    = perfil_keywords.get("cargo", "")
            keywords = perfil_keywords.get("keywords", "")
            logger.info(f"[Scheduler] user={telegram_id} | cargo={cargo} | keywords={keywords}")

            # 2. Busca vagas no LinkedIn
            vagas_encontradas = buscar_vagas_linkedin(cargo, keywords, cidade, quantidade=10)

            if not vagas_encontradas:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        "Hoje nao encontrei vagas novas para o seu perfil no LinkedIn.\n"
                        "Tente atualizar seu historico para melhorar as sugestoes."
                    )
                )
                continue

            # 3. Gemini seleciona as 2 melhores
            melhores_vagas = selecionar_melhores_vagas(historico, vagas_encontradas)

            # 4. Filtra vagas ja enviadas anteriormente
            vagas_novas = [
                v for v in melhores_vagas
                if not job_ja_enviado(telegram_id, gerar_hash_vaga(v))
            ]

            if not vagas_novas:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        "Hoje as melhores vagas para seu perfil ja foram enviadas anteriormente.\n"
                        "Fique atento as proximas sugestoes!"
                    )
                )
                continue

            # Mensagem de abertura
            await context.bot.send_message(
                chat_id=telegram_id,
                text=(
                    f"Bom dia! Aqui estao suas {len(vagas_novas)} sugestao(es) de vaga de hoje,"
                    " com curriculo ja adaptado para cada uma:"
                )
            )

            # 5. Para cada vaga: gera e envia CV adaptado
            for i, vaga in enumerate(vagas_novas, start=1):
                job_hash = gerar_hash_vaga(vaga)
                descricao_vaga = (
                    f"{vaga.get('title','')} em {vaga.get('company','')}\n"
                    f"Local: {vaga.get('location','')}\n\n"
                    f"{vaga.get('description','')}"
                )

                try:
                    dados_cv = gerar_curriculo_json(historico, descricao_vaga, usuario)
                    pdf_buf  = gerar_pdf(dados_cv)
                    nome     = dados_cv.get("contato", {}).get("nome", "Candidato")
                    nome_arquivo = f"CV_{nome.replace(' ','_')}_{vaga.get('company','').replace(' ','_')}.pdf"

                    # Monta caption da vaga
                    caption = (
                        f"Vaga {i}: {vaga.get('title','')}\n"
                        f"Empresa: {vaga.get('company','')}\n"
                        f"Local: {vaga.get('location','')}\n"
                    )
                    if vaga.get("job_url") and vaga["job_url"] != "nan":
                        caption += f"Link: {vaga['job_url']}"

                    await context.bot.send_document(
                        chat_id=telegram_id,
                        document=pdf_buf,
                        filename=nome_arquivo,
                        caption=caption,
                    )

                    # Registra como enviada
                    registrar_job_enviado(
                        telegram_id, job_hash,
                        vaga.get("title", ""), vaga.get("company", "")
                    )
                    logger.info(f"[Scheduler] Vaga enviada para {telegram_id}: {vaga.get('title')}")

                except Exception as e:
                    logger.error(f"[Scheduler] Erro ao gerar CV para vaga '{vaga.get('title')}': {e}", exc_info=True)

        except Exception as e:
            logger.error(f"[Scheduler] Erro ao processar usuario {telegram_id}: {e}", exc_info=True)

    logger.info("[Scheduler] Envio de sugestoes diarias concluido.")

# =========================================================
# COMANDO DE TESTE — /testar_vagas
# Dispara o envio imediato para TODOS os usuarios cadastrados.
# Util para validar o fluxo sem esperar o meio-dia.
# =========================================================
async def cmd_testar_vagas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"[Teste] /testar_vagas acionado por user_id={user_id}")

    await update.message.reply_text(
        "Iniciando envio de teste para todos os usuarios cadastrados...\n"
        "Aguarde, isso pode levar alguns minutos dependendo do numero de usuarios."
    )

    # Reutiliza exatamente a mesma funcao do scheduler diario
    await enviar_sugestoes_diarias(context)

    await update.message.reply_text("Envio de teste concluido!")

# =========================================================
# CONVERSATION HANDLER — ONBOARDING
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.info(f"[Handler] /start de user_id={user_id}")
    usuario = buscar_usuario(user_id)

    if usuario and usuario.get("email"):
        await update.message.reply_text(
            "Perfil ativo!\n\n"
            "Voce pode:\n"
            "- Enviar .txt ou .pdf com seu historico\n"
            "- Enviar a descricao de uma vaga para gerar o curriculo\n"
            "- Enviar novas experiencias para atualizar seu historico\n\n"
            "Todo dia ao meio-dia voce recebera 2 sugestoes de vagas do LinkedIn "
            "com o curriculo ja adaptado para cada uma."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Bem-vindo ao ATS Resume Bot!\n\n"
        "Vou configurar seu perfil rapidinho.\n\n"
        "Qual o seu E-MAIL profissional?"
    )
    return ASK_EMAIL


async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboard_email"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu TELEFONE? (com DDD)")
    return ASK_PHONE


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboard_telefone"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu LINKEDIN? (URL ou usuario)")
    return ASK_LINKEDIN


async def ask_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboard_linkedin"] = update.message.text.strip()
    await update.message.reply_text(
        "Qual a sua CIDADE e ESTADO?\n"
        "Exemplo: Sao Paulo, SP"
    )
    return ASK_CITY


async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onboard_cidade"] = update.message.text.strip()
    await update.message.reply_text(
        "Em qual IDIOMA deseja o curriculo?\n"
        "Exemplos: Portugues, Ingles, Espanhol"
    )
    return ASK_LANGUAGE


async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    salvar_perfil(user_id, {
        "email":    context.user_data.get("onboard_email", ""),
        "telefone": context.user_data.get("onboard_telefone", ""),
        "linkedin": context.user_data.get("onboard_linkedin", ""),
        "cidade":   context.user_data.get("onboard_cidade", ""),
        "idioma":   update.message.text.strip(),
    })
    for key in ["onboard_email", "onboard_telefone", "onboard_linkedin", "onboard_cidade"]:
        context.user_data.pop(key, None)

    await update.message.reply_text(
        "Perfil salvo com sucesso!\n\n"
        "Agora envie um arquivo .txt ou .pdf com seu historico profissional.\n\n"
        "Todo dia ao meio-dia voce recebera 2 sugestoes de vagas do LinkedIn "
        "com o curriculo ja adaptado."
    )
    return ConversationHandler.END

# =========================================================
# HANDLER PRINCIPAL — TEXTO E ARQUIVOS
# =========================================================
async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)

    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Use /start para configurar seu perfil primeiro.")
        return

    status = await update.message.reply_text("Processando... Aguarde.")

    try:
        if update.message.document:
            doc  = update.message.document
            file = await context.bot.get_file(doc.file_id)
            buf  = bytearray()
            await file.download_as_bytearray(out=buf)
            texto = extrair_texto_de_arquivo(buf, doc.file_name)
            logger.info(f"[Handler] Arquivo de user_id={user_id}: {doc.file_name}")
        else:
            texto = update.message.text.strip()
            logger.info(f"[Handler] Texto de user_id={user_id}: {texto[:60]}")
    except Exception as e:
        logger.error(f"[Input] Erro ao extrair: {e}")
        await status.edit_text("Nao consegui ler o arquivo. Tente novamente.")
        return

    if not texto.strip():
        await status.edit_text("Nenhum conteudo detectado.")
        return

    try:
        intencao = classificar_intencao_llm(texto)
        logger.info(f"[LLM] Intencao: {intencao} para user_id={user_id}")
    except Exception as e:
        logger.error(f"[LLM] Erro: {e}")
        await status.edit_text("Erro ao processar. Tente novamente.")
        return

    if intencao == "HISTORICO":
        try:
            novo_historico = consolidar_historico_llm(usuario.get("raw_history", ""), texto)
            salvar_perfil(user_id, {"raw_history": novo_historico})
            await status.edit_text(
                "Historico atualizado!\n\n"
                "Envie a descricao de uma vaga para gerar o curriculo agora,\n"
                "ou aguarde as sugestoes automaticas do meio-dia."
            )
        except Exception as e:
            logger.error(f"[Historico] Erro: {e}")
            await status.edit_text("Erro ao salvar historico. Tente novamente.")

    else:  # VAGA manual
        if not usuario.get("raw_history"):
            await status.edit_text(
                "Nenhum historico encontrado.\n"
                "Envie primeiro um .txt ou .pdf com seu historico."
            )
            return

        try:
            await status.edit_text("Gerando curriculo otimizado...")
            dados = gerar_curriculo_json(usuario["raw_history"], texto, usuario)
        except json.JSONDecodeError as e:
            logger.error(f"[Gemini] JSON invalido: {e}")
            await status.edit_text("O modelo gerou documento invalido. Tente novamente.")
            return
        except Exception as e:
            logger.error(f"[Gemini] Erro: {e}", exc_info=True)
            await status.edit_text("Erro no modelo de IA. Tente novamente.")
            return

        try:
            pdf_buf = gerar_pdf(dados)
        except Exception as e:
            logger.error(f"[PDF] Erro: {e}", exc_info=True)
            await status.edit_text("Erro ao compilar o PDF.")
            return

        nome         = dados.get("contato", {}).get("nome", "Candidato")
        nome_arquivo = f"CV_{nome.replace(' ', '_')}_ATS.pdf"

        await update.message.reply_document(
            document=pdf_buf,
            filename=nome_arquivo,
            caption=f"Curriculo ATS gerado: {nome}\nEnvie outra vaga para gerar novamente.",
        )
        await status.delete()


async def cmd_testar_vagas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando de teste: dispara o job de sugestoes diarias imediatamente
    apenas para o usuario que enviou o comando.
    """
    user_id = update.effective_user.id
    logger.info(f"[Teste] /testar_vagas acionado por user_id={user_id}")

    usuario = buscar_usuario(user_id)
    if not usuario or not usuario.get("raw_history"):
        await update.message.reply_text(
            "Voce ainda nao tem historico salvo.\n"
            "Envie primeiro um .txt ou .pdf com seu historico profissional."
        )
        return

    await update.message.reply_text("Buscando vagas para o seu perfil... Aguarde.")

    # Reutiliza exatamente o mesmo fluxo do job diario, mas so para este usuario
    class FakeContext:
        """Contexto minimo para reusar enviar_sugestoes_diarias com um unico usuario."""
        def __init__(self, bot):
            self.bot = bot

    # Filtra a lista de todos os usuarios para conter apenas o usuario atual
    todos = buscar_todos_usuarios()
    usuario_atual = [u for u in todos if u.get("telegram_id") == str(user_id)]

    if not usuario_atual:
        await update.message.reply_text("Perfil nao encontrado no banco. Use /start.")
        return

    # Injeta o bot real no contexto fake e chama o job diretamente
    fake_ctx = FakeContext(bot=context.bot)

    # Salva lista original e substitui temporariamente
    import types

    async def _rodar_para_um_usuario(ctx):
        historico = usuario_atual[0].get("raw_history", "")
        cidade    = usuario_atual[0].get("cidade", "Brazil")
        telegram_id = str(user_id)

        try:
            perfil_keywords   = extrair_keywords_perfil(historico)
            cargo             = perfil_keywords.get("cargo", "")
            keywords          = perfil_keywords.get("keywords", "")
            vagas_encontradas = buscar_vagas_linkedin(cargo, keywords, cidade, quantidade=10)

            if not vagas_encontradas:
                await ctx.bot.send_message(
                    chat_id=telegram_id,
                    text="Nenhuma vaga encontrada para o seu perfil agora. Tente mais tarde."
                )
                return

            melhores_vagas = selecionar_melhores_vagas(historico, vagas_encontradas)
            vagas_novas    = [
                v for v in melhores_vagas
                if not job_ja_enviado(telegram_id, gerar_hash_vaga(v))
            ]

            if not vagas_novas:
                await ctx.bot.send_message(
                    chat_id=telegram_id,
                    text="As melhores vagas para hoje ja foram enviadas anteriormente."
                )
                return

            await ctx.bot.send_message(
                chat_id=telegram_id,
                text=f"Aqui estao suas {len(vagas_novas)} sugestao(es) de vaga com curriculo adaptado:"
            )

            for i, vaga in enumerate(vagas_novas, start=1):
                job_hash       = gerar_hash_vaga(vaga)
                descricao_vaga = (
                    f"{vaga.get('title','')} em {vaga.get('company','')}\n"
                    f"Local: {vaga.get('location','')}\n\n"
                    f"{vaga.get('description','')}"
                )
                dados_cv     = gerar_curriculo_json(historico, descricao_vaga, usuario_atual[0])
                pdf_buf      = gerar_pdf(dados_cv)
                nome         = dados_cv.get("contato", {}).get("nome", "Candidato")
                nome_arquivo = f"CV_{nome.replace(' ','_')}_{vaga.get('company','').replace(' ','_')}.pdf"

                caption = (
                    f"Vaga {i}: {vaga.get('title','')}\n"
                    f"Empresa: {vaga.get('company','')}\n"
                    f"Local: {vaga.get('location','')}\n"
                )
                if vaga.get("job_url") and vaga["job_url"] != "nan":
                    caption += f"Link: {vaga['job_url']}"

                await ctx.bot.send_document(
                    chat_id=telegram_id,
                    document=pdf_buf,
                    filename=nome_arquivo,
                    caption=caption,
                )
                registrar_job_enviado(telegram_id, job_hash, vaga.get("title",""), vaga.get("company",""))
                logger.info(f"[Teste] Vaga enviada: {vaga.get('title')}")

        except Exception as e:
            logger.error(f"[Teste] Erro: {e}", exc_info=True)
            await ctx.bot.send_message(
                chat_id=telegram_id,
                text=f"Erro durante o teste: {e}"
            )

    await _rodar_para_um_usuario(fake_ctx)


async def handle_erro(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[Erro Global] {context.error}", exc_info=context.error)

# =========================================================
# MAIN
# =========================================================
def main():
    logger.info("=" * 60)
    logger.info("Iniciando ATS Resume Bot com Sugestoes Diarias")
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

    # ---------------------------------------------------------
    # AGENDAMENTO DIARIO — MEIO-DIA (HORARIO DE BRASILIA)
    # PTB JobQueue usa APScheduler internamente.
    # zoneinfo e nativo do Python 3.9+, sem dependencia extra.
    # ---------------------------------------------------------
    BRASILIA = ZoneInfo("America/Sao_Paulo")
    horario_envio = dtime(hour=12, minute=0, second=0, tzinfo=BRASILIA)

    app.job_queue.run_daily(
        enviar_sugestoes_diarias,
        time=horario_envio,
        name="sugestoes_diarias",
    )
    logger.info(f"[Scheduler] Job agendado para todos os dias às 12:00 (Brasilia).")

    # Handlers
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_EMAIL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            ASK_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_LINKEDIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_linkedin)],
            ASK_CITY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_city)],
            ASK_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_language)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("testar_vagas", cmd_testar_vagas))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
            handle_input,
        )
    )
    app.add_error_handler(handle_erro)

    logger.info("Bot em modo de escuta (polling)...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
