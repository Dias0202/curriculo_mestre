import os
import io
import json
import logging
from dotenv import dotenv_values
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from fpdf import FPDF
from supabase import create_client, Client

# =========================================================
# CONFIGURAÇÃO DE LOGGING E ENV
# =========================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
env = dotenv_values(ENV_PATH)

TELEGRAM_TOKEN = env.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = env.get("GEMINI_API_KEY")
SUPABASE_URL = env.get("SUPABASE_URL")
SUPABASE_KEY = env.get("SUPABASE_KEY")

MODEL_NAME = "gemini-2.5-flash"

# =========================================================
# INICIALIZAÇÃO DE CLIENTES
# =========================================================
llm_client = genai.Client(api_key=GEMINI_API_KEY)
db_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================================================
# CLASSE PDF HARVARD (MANTIDA)
# =========================================================
class CurriculoHarvard(FPDF):
    def __init__(self):
        super().__init__(format='A4')
        self.set_margins(15, 15, 15)
        self.add_page()
        self.set_auto_page_break(auto=True, margin=15)

    def cabecalho_candidato(self, nome, contatos):
        self.set_font("Times", 'B', 16)
        self.cell(0, 8, nome.upper(), align='C', new_x="LMARGIN", new_y="NEXT")
        self.set_font("Times", '', 10)
        linha_contato = " | ".join(contatos)
        self.cell(0, 5, linha_contato, align='C', new_x="LMARGIN", new_y="NEXT")
        self.ln(5)

    def titulo_secao(self, titulo):
        self.set_font("Times", 'B', 11)
        self.cell(0, 6, titulo.upper(), new_x="LMARGIN", new_y="NEXT")
        self.line(self.get_x(), self.get_y(), 210 - self.r_margin, self.get_y())
        self.ln(2)

    def paragrafo_resumo(self, texto):
        self.set_font("Times", '', 10)
        self.multi_cell(0, 5, texto)
        self.ln(4)

    def item_experiencia(self, titulo, empresa, local, data, atividades):
        self.set_font("Times", 'B', 10)
        self.cell(100, 5, titulo)
        self.set_font("Times", '', 10)
        self.cell(0, 5, data, align='R', new_x="LMARGIN", new_y="NEXT")
        self.set_font("Times", 'I', 10)
        self.cell(100, 5, empresa)
        self.set_font("Times", '', 10)
        self.cell(0, 5, local, align='R', new_x="LMARGIN", new_y="NEXT")
        self.set_font("Times", '', 10)
        if isinstance(atividades, list):
            for bullet in atividades:
                self.set_x(20)
                self.multi_cell(0, 5, f"- {bullet}")
        self.ln(3)

    def item_simples(self, titulo, detalhes):
        self.set_font("Times", 'B', 10)
        self.write(5, f"{titulo}: ")
        self.set_font("Times", '', 10)
        self.multi_cell(0, 5, detalhes)
        self.ln(1)


# =========================================================
# SANITIZAÇÃO E BANCO DE DADOS
# =========================================================
def sanitizar_texto(texto: str) -> str:
    if not isinstance(texto, str): return texto
    substituicoes = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "•": "-", "\u200b": ""}
    for busca, troca in substituicoes.items(): texto = texto.replace(busca, troca)
    return texto.encode('latin-1', 'ignore').decode('latin-1')


def sanitizar_dados(dados):
    if isinstance(dados, dict):
        return {k: sanitizar_dados(v) for k, v in dados.items()}
    elif isinstance(dados, list):
        return [sanitizar_dados(v) for v in dados]
    elif isinstance(dados, str):
        return sanitizar_texto(dados)
    return dados


def salvar_historico_supabase(telegram_id: int, conteudo_raw: str):
    # Opcional: Aqui poderíamos usar o LLM para já gerar o JSON estruturado na ingestão.
    # Por eficiência, salvaremos o raw text e estruturamos na geração.
    data = {
        "telegram_id": telegram_id,
        "raw_history": conteudo_raw
    }
    db_client.table("user_profiles").upsert(data).execute()


def recuperar_historico_supabase(telegram_id: int) -> str:
    response = db_client.table("user_profiles").select("raw_history").eq("telegram_id", telegram_id).execute()
    if response.data and len(response.data) > 0:
        return response.data[0]["raw_history"]
    return None


def detectar_idioma(descricao: str) -> str:
    return "English" if any(
        termo in descricao.lower() for termo in ["requirements", "experience", "responsibilities"]) else "Portuguese"


# =========================================================
# LÓGICA DE GERAÇÃO COM ANCORAGEM (TAILORING)
# =========================================================
def gerar_dados_cv_json(descricao_vaga: str, curriculo_base: str, idioma: str) -> dict:
    prompt = f"""
    You are an expert technical recruiter and ATS optimization specialist.

    CRITICAL INSTRUCTION: Your primary goal is to TAILOR the resume to the exact JOB DESCRIPTION provided.
    Do NOT lie or invent facts, but you MUST REFRAME, HIGHLIGHT, and PRIORITIZE the candidate's existing experience to match the keywords and requirements of the job.

    Alignment rules:
    1. If the job requires "developer experience", rewrite bullet points to emphasize programming, API development, and software engineering tasks.
    2. If the job asks for "data loads and data quality", prioritize ETL, data pipelines, and validation tasks in the candidate's history.
    3. The "Professional Summary" MUST explicitly state how the candidate's background solves the core needs mentioned in the Job Description.

    Output Language: {idioma}

    You MUST return a STRICTLY VALID JSON matching this structure:
    {{
      "nome": "Full Name",
      "contatos": ["Phone", "Email", "LinkedIn"],
      "resumo": "Highly tailored professional summary matching the job description.",
      "experiencia": [
        {{
          "cargo": "Job Title",
          "empresa": "Company Name",
          "local": "Location",
          "data": "Month Year - Month Year",
          "atividades": ["Bullet point specifically adapted to highlight job description requirements", "Another tailored bullet point"]
        }}
      ],
      "educacao": [
        {{"curso": "Degree", "instituicao": "Institution", "local": "Location", "data": "Year", "detalhes": ["Detail"]}}
      ],
      "habilidades": [
        {{"categoria": "Category", "itens": "Skill 1, Skill 2 (Prioritize skills mentioned in job description)"}}
      ]
    }}

    CANDIDATE HISTORY:
    {curriculo_base}

    JOB DESCRIPTION:
    {descricao_vaga}
    """

    response = llm_client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3  # Levemente maior para permitir flexibilidade na reescrita
        )
    )
    return json.loads(response.text)


def compilar_pdf_harvard(dados_cv: dict, idioma: str) -> io.BytesIO:
    pdf = CurriculoHarvard()
    pdf.cabecalho_candidato(dados_cv.get("nome", ""), dados_cv.get("contatos", []))

    if dados_cv.get("resumo"):
        pdf.titulo_secao("Professional Summary" if idioma == "English" else "Resumo Profissional")
        pdf.paragrafo_resumo(dados_cv["resumo"])

    if dados_cv.get("experiencia"):
        pdf.titulo_secao("Experience" if idioma == "English" else "Experiência Profissional")
        for exp in dados_cv["experiencia"]:
            pdf.item_experiencia(exp.get("cargo", ""), exp.get("empresa", ""), exp.get("local", ""),
                                 exp.get("data", ""), exp.get("atividades", []))

    if dados_cv.get("educacao"):
        pdf.titulo_secao("Education" if idioma == "English" else "Formação Acadêmica")
        for edu in dados_cv["educacao"]:
            pdf.item_experiencia(edu.get("curso", ""), edu.get("instituicao", ""), edu.get("local", ""),
                                 edu.get("data", ""), edu.get("detalhes", []))

    if dados_cv.get("habilidades"):
        pdf.titulo_secao("Technical Skills" if idioma == "English" else "Habilidades Técnicas")
        for hab in dados_cv["habilidades"]:
            pdf.item_simples(hab.get("categoria", ""), hab.get("itens", ""))

    buffer = io.BytesIO()
    pdf.output(buffer)
    buffer.seek(0)
    return buffer


# =========================================================
# TELEGRAM HANDLERS
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "Sistema ATS conectado ao banco de dados.\n1. Envie seu .txt com histórico.\n2. Envie a descrição da vaga."
    await update.message.reply_text(msg)


async def receber_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    documento = update.message.document
    if not documento.file_name.endswith(".txt"):
        await update.message.reply_text("Erro: Apenas arquivos .txt.")
        return

    arquivo = await context.bot.get_file(documento.file_id)
    conteudo_bytes = await arquivo.download_as_bytearray()
    conteudo_str = conteudo_bytes.decode('utf-8')

    telegram_id = update.effective_user.id
    salvar_historico_supabase(telegram_id, conteudo_str)

    await update.message.reply_text("Perfil salvo estruturalmente no banco de dados.")


async def processar_vaga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    descricao = update.message.text
    telegram_id = update.effective_user.id

    curriculo_base = recuperar_historico_supabase(telegram_id)

    if not curriculo_base:
        await update.message.reply_text("Histórico não encontrado no banco de dados. Envie o .txt primeiro.")
        return

    await update.message.reply_text("Analisando requisitos da vaga e realizando cross-match com seu perfil...")

    try:
        idioma = detectar_idioma(descricao)
        dados_json = gerar_dados_cv_json(descricao, curriculo_base, idioma)
        dados_json = sanitizar_dados(dados_json)
        arquivo_pdf = compilar_pdf_harvard(dados_json, idioma)

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=arquivo_pdf,
            filename=f"CV_Tailored_{idioma}.pdf"
        )
    except Exception as e:
        logger.exception("Falha na pipeline.")
        await update.message.reply_text(f"Erro no processamento: {str(e)}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, receber_documento))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_vaga))
    logger.info("Serviço em execução.")
    app.run_polling()


if __name__ == "__main__":
    main()