"""
ATS Resume Bot — Versao Definitiva
Integra:
  - Prompt 1: Engenheiro de Dados (consolidacao de perfil -> JSON estruturado)
  - Prompt 2: Recrutador Senior (geracao de CV ATS -> JSON com schema completo)
  - PDF Harvard (consume o JSON do Prompt 2 fielmente)
  - Scraper LinkedIn (URL colada pelo usuario + busca ativa diaria via JobSpy)
  - Sugestoes diarias automaticas ao meio-dia (Brasilia)
"""

import os
import io
import re
import json
import logging
import hashlib
import threading
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from groq import Groq
from telegram import Update
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

from scraper import extrair_vaga_linkedin, buscar_vagas_jobspy

# =========================================================
# ESTADOS DO ONBOARDING
# =========================================================
ASK_NOME, ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN, ASK_CITY, ASK_LANGUAGE = range(6)

# =========================================================
# LOGGING E ENV
# =========================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY   = os.getenv("GROQ_API_KEY",   "").strip()
SUPABASE_URL   = os.getenv("SUPABASE_URL",   "").strip()
SUPABASE_KEY   = os.getenv("SUPABASE_KEY",   "").strip()

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    logger.error("ERRO CRITICO: Variaveis de ambiente ausentes.")
    raise SystemExit(1)

# =========================================================
# CLIENTES
# =========================================================
llm_client: Groq   = Groq(api_key=GROQ_API_KEY)
db_client:  Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================
# KEEP-ALIVE — Render.com health check
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ATS Bot OK")
    def log_message(self, *a): pass

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    logger.info(f"[Health] Porta {port}")
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# =========================================================
# SANITIZACAO — fpdf2 usa latin-1 internamente
# =========================================================
_SUBS = {
    "\u2022": "-", "\u2013": "-", "\u2014": "-",
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"',  "\u201d": '"',
    "\u00b7": "-",  "\u2026": "...",
}

def sanitize(text: str) -> str:
    if not text: return ""
    text = str(text).replace("\t", " ")
    for c, r in _SUBS.items():
        text = text.replace(c, r)
    return text.encode("latin-1", "ignore").decode("latin-1")

# =========================================================
# EXTRACAO DE TEXTO DE ARQUIVO
# =========================================================
def extrair_texto_arquivo(file_bytes: bytearray, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(bytes(file_bytes)))
            return "\n".join(p.extract_text() for p in reader.pages if p.extract_text())
        except Exception as e:
            logger.error(f"[PDF] {e}")
            return ""
    for enc in ["utf-8", "latin-1", "cp1252"]:
        try: return file_bytes.decode(enc)
        except UnicodeDecodeError: continue
    return file_bytes.decode("utf-8", errors="ignore")

# =========================================================
# GERADOR DE PDF — Consome o JSON exato do Prompt 2 (Recrutador Senior)
#
# Secoes (em ordem Harvard):
#   cabecalho (identificacao) | resumo | competencias |
#   experiencias | educacao | certificacoes | projetos | idiomas
#
# "relatorio_analitico" NAO vai no PDF — enviado como mensagem separada.
# =========================================================
class CurriculoHarvard(FPDF):
    def __init__(self):
        super().__init__()
        self.set_margins(20, 20, 20)
        self.add_page()
        self.set_auto_page_break(True, margin=15)

    # ------ Primitivas ------
    def _secao(self, titulo: str):
        """Linha de secao com underline, estilo Harvard."""
        self.ln(2)
        self.set_font("helvetica", "B", 11)
        self.cell(0, 7, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
        self.set_line_width(0.5)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def _linha(self, txt: str, size: int = 10, bold: bool = False, italic: bool = False,
               align: str = "L"):
        style = ("B" if bold else "") + ("I" if italic else "")
        self.set_font("helvetica", style, size)
        self.multi_cell(0, 5, sanitize(txt), align=align, new_x="LMARGIN", new_y="NEXT")

    def _bullet(self, txt: str, prefixo: str = "-"):
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, sanitize(f"{prefixo} {txt}"), new_x="LMARGIN", new_y="NEXT")

    # ------ Blocos ------
    def bloco_cabecalho(self, ident: dict):
        """Nome, titulo profissional e faixa de contatos centrados."""
        nome   = ident.get("nome", "")
        titulo = ident.get("titulo", "")

        self.set_font("helvetica", "B", 18)
        self.multi_cell(0, 10, sanitize(nome), align="C", new_x="LMARGIN", new_y="NEXT")

        if titulo:
            self.set_font("helvetica", "I", 11)
            self.multi_cell(0, 6, sanitize(titulo), align="C", new_x="LMARGIN", new_y="NEXT")

        # Linha de contatos: email | telefone | linkedin | localizacao | github | portfolio
        campos = ["email", "telefone", "linkedin", "localizacao", "github", "portfolio"]
        contatos = [str(ident.get(c, "")).strip() for c in campos if ident.get(c, "").strip()]
        if contatos:
            self.set_font("helvetica", "", 9)
            self.multi_cell(0, 5, sanitize("  |  ".join(contatos)),
                            align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def bloco_resumo(self, titulo: str, texto: str):
        if not texto: return
        self._secao(titulo)
        self._linha(texto)

    def bloco_competencias(self, titulo: str, lista: list):
        if not lista: return
        self._secao(titulo)
        # Separador "|" — latin-1 seguro (bullet \u2022 nao e suportado pelo helvetica)
        itens = [sanitize(str(i)) for i in lista if i]
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, "  |  ".join(itens), new_x="LMARGIN", new_y="NEXT")

    def bloco_experiencias(self, titulo: str, exps: list):
        if not exps: return
        self._secao(titulo)
        for exp in exps:
            if not isinstance(exp, dict): continue

            cargo     = exp.get("cargo", "")
            empresa   = exp.get("empresa", "")
            local_exp = exp.get("localizacao", "")
            inicio    = exp.get("data_inicio", "")
            fim       = exp.get("data_fim", "")
            desc_emp  = exp.get("descricao_empresa", "")

            # Linha 1: Cargo — Empresa
            self.set_font("helvetica", "B", 11)
            self.cell(0, 6, sanitize(f"{cargo}  —  {empresa}"), new_x="LMARGIN", new_y="NEXT")

            # Linha 2: Periodo | Local (italico, menor)
            meta = f"{inicio} – {fim}"
            if local_exp: meta += f"  |  {local_exp}"
            self.set_font("helvetica", "I", 9)
            self.cell(0, 5, sanitize(meta), new_x="LMARGIN", new_y="NEXT")

            # Descricao da empresa (opcional)
            if desc_emp:
                self.set_font("helvetica", "I", 9)
                self.multi_cell(0, 4, sanitize(desc_emp), new_x="LMARGIN", new_y="NEXT")

            self.ln(1)

            # Responsabilidades
            resps = exp.get("responsabilidades", [])
            if isinstance(resps, str): resps = [resps]
            for r in resps:
                if r: self._bullet(r, "-")

            # Conquistas (destaque com seta)
            conquistas = exp.get("conquistas", [])
            if isinstance(conquistas, str): conquistas = [conquistas]
            for c in conquistas:
                if c:
                    self.set_font("helvetica", "B", 10)
                    self.multi_cell(0, 5, sanitize(f"» {c}"), new_x="LMARGIN", new_y="NEXT")

            self.ln(3)

    def bloco_educacao(self, titulo: str, edus: list):
        if not edus: return
        self._secao(titulo)
        for edu in edus:
            if not isinstance(edu, dict): continue
            grau  = edu.get("grau", "")
            curso = edu.get("curso", "")
            inst  = edu.get("instituicao", "")
            ini   = edu.get("ano_inicio", "")
            fim   = edu.get("ano_fim", "")

            cabecalho = f"{grau} em {curso}" if grau else curso
            self.set_font("helvetica", "B", 11)
            self.cell(0, 6, sanitize(cabecalho), new_x="LMARGIN", new_y="NEXT")
            self.set_font("helvetica", "I", 10)
            self.cell(0, 5, sanitize(f"{inst}  |  {ini} – {fim}"), new_x="LMARGIN", new_y="NEXT")
            self.ln(3)

    def bloco_lista_simples(self, titulo: str, itens: list):
        """Certificacoes e Idiomas — lista simples de strings."""
        if not itens: return
        self._secao(titulo)
        self.set_font("helvetica", "", 10)
        for item in itens:
            if item:
                self.multi_cell(0, 5, sanitize(f"- {item}"), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def bloco_projetos(self, titulo: str, projetos: list):
        if not projetos: return
        self._secao(titulo)
        for proj in projetos:
            if not isinstance(proj, dict): continue
            self.set_font("helvetica", "B", 10)
            self.cell(0, 6, sanitize(proj.get("nome", "")), new_x="LMARGIN", new_y="NEXT")
            self.set_font("helvetica", "", 10)
            self.multi_cell(0, 5, sanitize(proj.get("descricao", "")),
                            new_x="LMARGIN", new_y="NEXT")
            self.ln(2)


def gerar_pdf(cv: dict) -> io.BytesIO:
    """
    Recebe o JSON gerado pelo Prompt 2 (Recrutador Senior) e compila o PDF Harvard.
    Campos usados: cabecalhos, identificacao, resumo, competencias,
                   experiencias, educacao, certificacoes, projetos, idiomas.
    'relatorio_analitico' e ignorado aqui (enviado como mensagem).
    """
    if not isinstance(cv, dict): cv = {}
    cab = cv.get("cabecalhos", {})
    pdf = CurriculoHarvard()

    pdf.bloco_cabecalho(cv.get("identificacao", {}))
    pdf.bloco_resumo(cab.get("resumo", "Resumo Profissional"), cv.get("resumo", ""))
    pdf.bloco_competencias(cab.get("competencias", "Competencias"), cv.get("competencias", []))
    pdf.bloco_experiencias(cab.get("experiencias", "Experiencia Profissional"), cv.get("experiencias", []))
    pdf.bloco_educacao(cab.get("educacao", "Formacao Academica"), cv.get("educacao", []))
    pdf.bloco_lista_simples(cab.get("certificacoes", "Certificacoes"), cv.get("certificacoes", []))
    pdf.bloco_projetos(cab.get("projetos", "Projetos"), cv.get("projetos", []))
    pdf.bloco_lista_simples(cab.get("idiomas", "Idiomas"), cv.get("idiomas", []))

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

# =========================================================
# GROQ — HELPER CENTRALIZADO
# =========================================================
_MODEL = "llama-3.3-70b-versatile"

def _chat(system: str, prompt: str, json_mode: bool = False, temperature: float = 0.1) -> str:
    kwargs = dict(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        temperature=temperature,
        max_tokens=8000,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = llm_client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()

def _parse_json(raw: str) -> dict:
    return json.loads(re.sub(r"```json|```", "", raw).strip())

# =========================================================
# PROMPT 1 — ENGENHEIRO DE DADOS
# Consolida historico bruto no perfil estruturado do Supabase
# =========================================================
_SYSTEM_CONSOLIDAR = """INSTRUCAO: Voce atua como um Engenheiro de Dados especialista em parsing de documentos de Recursos Humanos.
Sua funcao e analisar o PERFIL ATUAL do candidato armazenado no banco de dados relacional e a NOVA ENTRADA de dados fornecida pelo usuario. Seu objetivo e retornar um JSON consolidado, normalizado e atualizado que mapeia perfeitamente para as tabelas do sistema (Supabase).

REGRAS DE MERGE E EXTRACAO:
1. RESOLUCAO DE CONFLITOS: Se a NOVA ENTRADA for um curriculo completo ou historico abrangente, atualize os dados existentes e remova duplicidades logicas. Se for apenas uma atualizacao pontual, insira o novo dado sem apagar o restante do PERFIL ATUAL.
2. NORMALIZACAO DE DADOS: Padronize as datas para o formato "Mes/Ano" (ex: Jan/2020) ou apenas "Ano". Categorize as skills estritamente como "Hard Skill" ou "Soft Skill".
3. FIDELIDADE: Nao resuma as descricoes, responsabilidades e conquistas. Mantenha a integridade do texto original.
4. DADOS NAO-TRADICIONAIS (CRITICO): Intercambios, trabalhos voluntarios, freelances e projetos pessoais SAO dados validos. Aplique o seguinte roteamento estrutural:
   - FREELANCES: Mapeie para "experiences" (ex: cargo = "Desenvolvedor Freelance", empresa = "Autonomo" ou Nome do Cliente).
   - PROJETOS PESSOAIS / ACADEMICOS: Mapeie para "projects", capturando nome e descricao tecnica (incluindo tecnologias).
   - INTERCAMBIOS: Se primariamente estudo, mapeie para "education". Se envolveu trabalho/vivencia pratica, mapeie para "experiences".
5. FORMATO: Retorne EXCLUSIVAMENTE um objeto JSON valido, sem marcadores de markdown.

SCHEMA EXIGIDO:
{
  "experiences": [
    {"cargo":"","empresa":"","localizacao":"","data_inicio":"","data_fim":"Presente ou data","descricao_empresa":"","responsabilidades":[],"conquistas":[]}
  ],
  "education": [
    {"grau":"Bacharelado/Mestrado/etc","curso":"Nome exato","instituicao":"","ano_inicio":"","ano_fim":""}
  ],
  "skills": [
    {"nome":"","categoria":"Hard Skill ou Soft Skill","nivel":"Iniciante, Intermediario ou Avancado"}
  ],
  "certifications": [
    {"nome":"","emissor":"","ano":""}
  ],
  "projects": [
    {"nome":"","descricao":""}
  ],
  "languages": [
    {"idioma":"","nivel":"Basico, Intermediario, Fluente ou Nativo"}
  ]
}"""

# =========================================================
# PROMPT 2 — RECRUTADOR SENIOR
# Gera o JSON completo do CV otimizado para ATS
# =========================================================
_SYSTEM_CV = """INSTRUCAO SUPREMA: Voce atua como um Recrutador Tecnico Senior, Especialista em Sistemas ATS (Applicant Tracking Systems) e Estrategista de Carreira.
Sua missao e cruzar o HISTORICO do candidato com os dados estruturados da VAGA ALVO e criar um curriculo ALTAMENTE DIRECIONADO para burlar os filtros algoritmicos do ATS e fornecer insights de carreira acionaveis.

REGRAS VITAIS E ALGORITMICAS:
1. MAPEAMENTO DE PALAVRAS-CHAVE (ATS SEO): Analise a VAGA ALVO, identifique as hard skills, soft skills e ferramentas exigidas. Injete essas exatas palavras-chave de forma organica e contextualizada no "resumo", "competencias" e "responsabilidades", SEMPRE que o historico original der suporte a isso.
2. PREVENCAO DE ALUCINACAO (STRICT FACTUALITY): NUNCA invente experiencias, cargos, ferramentas ou graduacoes que nao existam no HISTORICO do candidato. Se o candidato nao possui um requisito da vaga, omita-o do curriculo e liste-o em "analise_gaps".
3. METODO STAR OTIMIZADO: Reescreva os bullet points de experiencias focando em impacto quantificavel (Situacao, Tarefa, Acao, Resultado). Inicie sempre com verbos de acao fortes.
4. ALAVANCAGEM DE BACKGROUND (CRITICO): Valorize intensamente Projetos Pessoais, Academicos e trabalhos Freelance. Use-os estrategicamente para compensar eventuais faltas de experiencia formal.
5. IDIOMA: Todo o conteudo gerado deve ser rigorosamente redigido no idioma especificado pelo campo IDIOMA.
6. FORMATO STRICT JSON: Retorne apenas o JSON puro, sem formatacao markdown, sem explicacoes adicionais.

SCHEMA OBRIGATORIO:
{
  "cabecalhos": {
    "resumo": "NOME DA SECAO NA LINGUA ALVO",
    "competencias": "NOME DA SECAO NA LINGUA ALVO",
    "experiencias": "NOME DA SECAO NA LINGUA ALVO",
    "educacao": "NOME DA SECAO NA LINGUA ALVO",
    "certificacoes": "NOME DA SECAO NA LINGUA ALVO",
    "projetos": "NOME DA SECAO NA LINGUA ALVO",
    "idiomas": "NOME DA SECAO NA LINGUA ALVO"
  },
  "identificacao": {
    "nome": "",
    "titulo": "Titulo profissional forte contendo a palavra-chave principal da vaga",
    "localizacao": "Cidade, Estado",
    "telefone": "",
    "email": "",
    "linkedin": "",
    "github": "",
    "portfolio": ""
  },
  "resumo": "Paragrafo estrategico de 4 a 6 linhas com resumo de qualificacoes focado na vaga alvo, contendo as principais palavras-chave da descricao da vaga.",
  "competencias": ["Palavra-chave 1 (priorize termos exatos da vaga)", "Palavra-chave 2"],
  "experiencias": [
    {
      "cargo": "Nome do Cargo",
      "empresa": "Nome da Empresa",
      "localizacao": "Local",
      "data_inicio": "Mes/Ano",
      "data_fim": "Mes/Ano ou Presente",
      "descricao_empresa": "1 linha sobre a empresa (opcional)",
      "responsabilidades": ["Verbo de Acao + Contexto + Palavra-chave da vaga utilizada"],
      "conquistas": ["Resultado metrico claro via metodo STAR (ex: Aumentou X% fazendo Y)"]
    }
  ],
  "educacao": [
    {
      "grau": "Nivel academico",
      "curso": "Nome do curso + Enfoque direcionado a vaga",
      "instituicao": "Nome da Instituicao",
      "ano_inicio": "Ano",
      "ano_fim": "Ano"
    }
  ],
  "certificacoes": ["Nome da Certificacao - Emissor - Ano"],
  "projetos": [
    {
      "nome": "Nome do Projeto",
      "descricao": "Descricao focada em resolucao de problemas, tecnologias alinhadas a vaga e link do repositorio se existir"
    }
  ],
  "idiomas": ["Idioma - Nivel"],
  "relatorio_analitico": {
    "match_score": 0,
    "analise_gaps": ["requisito da vaga que o candidato NAO possui"],
    "dica_entrevista": "Pergunta tecnica/comportamental que os recrutadores fariam + como o candidato deve responder usando experiencia real do historico"
  }
}"""

# =========================================================
# FUNCOES LLM
# =========================================================
def classificar_intencao(texto: str) -> str:
    """Retorna 'URL_LINKEDIN', 'VAGA' ou 'HISTORICO'."""
    if re.search(r"linkedin\.com/jobs", texto, re.IGNORECASE):
        return "URL_LINKEDIN"
    raw = _chat(
        system="Classifique textos profissionais. Responda APENAS com uma palavra: VAGA ou HISTORICO.",
        prompt=(
            "VAGA = descricao de cargo/emprego de uma empresa.\n"
            "HISTORICO = perfil/curriculo/historico profissional de uma pessoa.\n\n"
            f"Texto:\n{texto[:1500]}"
        ),
        temperature=0.0,
    )
    return "VAGA" if "VAGA" in raw.upper() else "HISTORICO"


def consolidar_perfil(perfil_atual: dict, nova_entrada: str) -> dict:
    """Prompt 1: mescla nova entrada ao perfil estruturado existente."""
    raw = _chat(
        system=_SYSTEM_CONSOLIDAR,
        prompt=(
            f"PERFIL ATUAL NO BANCO DE DADOS:\n{json.dumps(perfil_atual, ensure_ascii=False)}\n\n"
            f"NOVA ENTRADA DO USUARIO (Texto bruto, mensagem, PDF ou Word extraido):\n{nova_entrada}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    return _parse_json(raw)


def extrair_keywords_para_busca(perfil: dict) -> dict:
    """
    Extrai cargo e keywords do perfil estruturado para o JobSpy.

    CRITICO: O campo 'cargo' deve ter NO MAXIMO 3 palavras simples e amplas,
    prontas para serem usadas como query de busca em sites de emprego.
    Termos longos ou especificos retornam zero resultados.
    """
    raw = _chat(
        system="Voce extrai dados de perfis profissionais para busca de empregos. Retorne SOMENTE JSON valido.",
        prompt=(
            "Analise o perfil abaixo e retorne um JSON com:\n"
            '- "cargo": titulo do cargo mais recente reduzido a NO MAXIMO 3 palavras curtas e amplas, '
            'como se fosse uma busca no LinkedIn. NUNCA use frases longas. '
            'Exemplos corretos: "Desenvolvedor Python", "Analista de Dados", "Engenheiro ML", "Cientista de Dados". '
            'Exemplos ERRADOS: "Desenvolvedor de Projetos Machine Learning IA Bioinformatica".\n'
            '- "keywords": apenas 1 ou 2 ferramentas ou tecnologias principais separadas por espaco. '
            'Exemplos: "Python SQL", "React Node", "AWS Terraform".\n'
            '- "area": area de atuacao em 2 palavras. Exemplos: "Tecnologia Dados", "Engenharia Software".\n\n'
            f"PERFIL:\n{json.dumps(perfil, ensure_ascii=False)[:3000]}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    try:
        resultado = _parse_json(raw)
        # Garante que o cargo nao ultrapasse 3 palavras mesmo se o LLM nao seguir a instrucao
        cargo = resultado.get("cargo", "")
        palavras = cargo.split()
        if len(palavras) > 3:
            resultado["cargo"] = " ".join(palavras[:3])
            logger.warning(f"[Keywords] Cargo truncado de '{cargo}' para '{resultado['cargo']}'")
        logger.info(f"[Keywords] cargo='{resultado.get('cargo')}' keywords='{resultado.get('keywords')}'")
        return resultado
    except Exception:
        return {"cargo": "Analista", "keywords": "", "area": ""}


def perfil_tem_ingles_fluente(perfil: dict) -> bool:
    """
    Retorna True se o perfil indicar ingles em nivel Intermediario ou superior.
    Verifica o campo 'languages' do perfil estruturado (Prompt 1).
    """
    niveis_ok = {"intermediario", "fluente", "nativo", "avancado",
                 "intermediate", "fluent", "native", "advanced", "proficient"}
    for lang in perfil.get("languages", []):
        if not isinstance(lang, dict):
            continue
        idioma = lang.get("idioma", "").lower()
        nivel  = lang.get("nivel",  "").lower()
        if "ingl" in idioma or "english" in idioma:
            if any(n in nivel for n in niveis_ok):
                logger.info(f"[Remoto] Ingles detectado: nivel='{nivel}'")
                return True
    return False


def selecionar_melhores_vagas(perfil: dict, vagas: list) -> list:
    """LLM seleciona as 2 vagas com maior aderencia ao perfil."""
    lista = "\n".join([
        f"{i}. {v.get('title','')} - {v.get('company','')} ({v.get('location','')}): {str(v.get('description',''))[:300]}"
        for i, v in enumerate(vagas)
    ])
    raw = _chat(
        system="Voce e um recrutador tecnico. Retorne SOMENTE JSON valido.",
        prompt=(
            'Selecione os 2 indices das vagas com MAIOR aderencia ao perfil do candidato.\n'
            'Retorne APENAS: {"indices": [0, 1]}\n\n'
            f"PERFIL:\n{json.dumps(perfil, ensure_ascii=False)[:2000]}\n\n"
            f"VAGAS:\n{lista}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    try:
        indices = _parse_json(raw).get("indices", [0, 1])
        return [vagas[i] for i in indices if i < len(vagas)][:2]
    except Exception:
        return vagas[:2]


def gerar_cv_json(perfil: dict, usuario: dict,
                  titulo_vaga: str, empresa_vaga: str,
                  local_vaga: str, descricao_vaga: str) -> dict:
    """Prompt 2: gera o JSON completo do curriculo ATS."""
    idioma = usuario.get("idioma", "Portugues")
    raw = _chat(
        system=_SYSTEM_CV,
        prompt=(
            f"HISTORICO ESTRUTURADO DO CANDIDATO:\n{json.dumps(perfil, ensure_ascii=False)}\n\n"
            f"DADOS PESSOAIS DO CANDIDATO:\n"
            f"Nome: {usuario.get('nome_completo','')}\n"
            f"Email: {usuario.get('email','')}\n"
            f"Telefone: {usuario.get('telefone','')}\n"
            f"LinkedIn: {usuario.get('linkedin','')}\n"
            f"Cidade: {usuario.get('cidade','')}\n\n"
            f"VAGA ALVO (Extraida via Scraper):\n"
            f"Titulo: {titulo_vaga}\n"
            f"Empresa: {empresa_vaga}\n"
            f"Local: {local_vaga}\n"
            f"Descricao:\n{descricao_vaga}\n\n"
            f"IDIOMA DO CURRICULO: {idioma}"
        ),
        json_mode=True,
        temperature=0.15,
    )
    return _parse_json(raw)

# =========================================================
# SUPABASE
# =========================================================
def salvar_perfil(telegram_id: int, dados: dict):
    dados["telegram_id"] = str(telegram_id)
    db_client.table("user_profiles").upsert(dados, on_conflict="telegram_id").execute()


def atualizar_perfil_estruturado(telegram_id: int, perfil: dict):
    db_client.table("user_profiles").update(
        {"perfil_estruturado": perfil}
    ).eq("telegram_id", str(telegram_id)).execute()


def buscar_usuario(telegram_id: int) -> dict | None:
    try:
        r = db_client.table("user_profiles").select("*").eq("telegram_id", str(telegram_id)).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"[Supabase] buscar_usuario: {e}")
        return None


def buscar_todos_usuarios() -> list:
    try:
        r = db_client.table("user_profiles").select("*").not_.is_("perfil_estruturado", "null").execute()
        return r.data or []
    except Exception as e:
        logger.error(f"[Supabase] buscar_todos: {e}")
        return []


def job_ja_enviado(telegram_id: str, job_hash: str) -> bool:
    try:
        r = db_client.table("sent_jobs").select("id").eq("telegram_id", telegram_id).eq("job_hash", job_hash).execute()
        return bool(r.data)
    except Exception:
        return False


def registrar_job_enviado(telegram_id: str, job_hash: str, title: str, company: str):
    try:
        db_client.table("sent_jobs").insert({
            "telegram_id": telegram_id,
            "job_hash": job_hash,
            "job_title": title,
            "job_company": company,
        }).execute()
    except Exception as e:
        logger.error(f"[Supabase] registrar_job: {e}")


def gerar_hash_vaga(vaga: dict) -> str:
    chave = f"{vaga.get('title', vaga.get('titulo',''))}{vaga.get('company', vaga.get('empresa',''))}".lower().strip()
    return hashlib.md5(chave.encode()).hexdigest()

# =========================================================
# PIPELINE: GERA CV + ENVIA PDF + RELATORIO
# =========================================================
async def processar_e_enviar_vaga(
    bot, telegram_id: str, usuario: dict, perfil: dict,
    titulo: str, empresa: str, local: str, descricao: str,
    url: str = "", job_hash: str = "", indice: int = 1
):
    cv      = gerar_cv_json(perfil, usuario, titulo, empresa, local, descricao)
    pdf_buf = gerar_pdf(cv)

    nome         = cv.get("identificacao", {}).get("nome", usuario.get("nome_completo", "Candidato"))
    nome_arquivo = f"CV_{nome.replace(' ','_')}_{empresa.replace(' ','_')}.pdf"

    caption = f"Vaga {indice}: {titulo}\nEmpresa: {empresa}\nLocal: {local}"
    if url and url not in ("nan", ""):
        caption += f"\nLink: {url}"

    await bot.send_document(
        chat_id=telegram_id,
        document=pdf_buf,
        filename=nome_arquivo,
        caption=caption,
    )

    # Relatorio analitico — enviado como mensagem separada
    rel = cv.get("relatorio_analitico", {})
    if rel:
        score = rel.get("match_score", "?")
        gaps  = rel.get("analise_gaps", [])
        dica  = rel.get("dica_entrevista", "")
        linhas = [f"Relatorio ATS — Vaga {indice}", f"Match Score: {score}/100"]
        if gaps:
            linhas.append("\nGaps identificados:")
            linhas += [f"- {g}" for g in gaps]
        if dica:
            linhas.append(f"\nDica para entrevista:\n{dica}")
        await bot.send_message(chat_id=telegram_id, text="\n".join(linhas))

    if job_hash:
        registrar_job_enviado(telegram_id, job_hash, titulo, empresa)
    logger.info(f"[Pipeline] Vaga '{titulo}' enviada para {telegram_id}")

# =========================================================
# JOB DIARIO — MEIO-DIA (BRASILIA)
# =========================================================
async def enviar_sugestoes_diarias(context: ContextTypes.DEFAULT_TYPE):
    logger.info("[Scheduler] Iniciando sugestoes diarias...")
    usuarios = buscar_todos_usuarios()
    logger.info(f"[Scheduler] {len(usuarios)} usuarios com perfil.")

    for usuario in usuarios:
        telegram_id = usuario.get("telegram_id")
        perfil      = usuario.get("perfil_estruturado") or {}
        cidade      = usuario.get("cidade", "Brazil")
        if not telegram_id or not perfil: continue

        try:
            kw              = extrair_keywords_para_busca(perfil)
            ingles_fluente  = perfil_tem_ingles_fluente(perfil)
            vagas = buscar_vagas_jobspy(
                kw.get("cargo", ""), kw.get("keywords", ""), cidade,
                quantidade=10,
                buscar_remoto=True,
                ingles_fluente=ingles_fluente,
            )

            if not vagas:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text="Hoje nao encontrei vagas novas para o seu perfil. Tente atualizar seu historico."
                )
                continue

            melhores = selecionar_melhores_vagas(perfil, vagas)
            novas    = [v for v in melhores if not job_ja_enviado(telegram_id, gerar_hash_vaga(v))]

            if not novas:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text="As melhores vagas de hoje ja foram enviadas anteriormente."
                )
                continue

            await context.bot.send_message(
                chat_id=telegram_id,
                text=f"Bom dia! Suas {len(novas)} sugestao(es) de hoje com curriculo adaptado:"
            )

            for i, vaga in enumerate(novas, 1):
                try:
                    await processar_e_enviar_vaga(
                        bot=context.bot,
                        telegram_id=telegram_id,
                        usuario=usuario,
                        perfil=perfil,
                        titulo=vaga.get("title",""),
                        empresa=vaga.get("company",""),
                        local=vaga.get("location",""),
                        descricao=vaga.get("description",""),
                        url=vaga.get("job_url",""),
                        job_hash=gerar_hash_vaga(vaga),
                        indice=i,
                    )
                except Exception as e:
                    logger.error(f"[Scheduler] Erro vaga {i} user {telegram_id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"[Scheduler] Erro user {telegram_id}: {e}", exc_info=True)

    logger.info("[Scheduler] Sugestoes diarias concluidas.")

# =========================================================
# COMANDO /testar_vagas — dispara o job SO para quem enviou o comando
# =========================================================
async def cmd_testar_vagas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    logger.info(f"[Teste] /testar_vagas por user_id={user_id}")

    usuario = buscar_usuario(user_id)
    perfil  = usuario.get("perfil_estruturado") if usuario else None

    if not perfil:
        await update.message.reply_text(
            "Voce ainda nao tem perfil estruturado.\n"
            "Envie primeiro um .txt ou .pdf com seu historico profissional."
        )
        return

    cidade          = usuario.get("cidade", "Brazil")
    kw              = extrair_keywords_para_busca(perfil)
    ingles_fluente  = perfil_tem_ingles_fluente(perfil)

    msg_busca = "Buscando vagas locais"
    if ingles_fluente:
        msg_busca += " + remotas (PT e EN)"
    else:
        msg_busca += " + remotas"
    await update.message.reply_text(f"{msg_busca}... Aguarde.")

    vagas = buscar_vagas_jobspy(
        kw.get("cargo", ""), kw.get("keywords", ""), cidade,
        quantidade=10,
        buscar_remoto=True,
        ingles_fluente=ingles_fluente,
    )

    if not vagas:
        await update.message.reply_text("Nenhuma vaga encontrada agora. Tente novamente mais tarde.")
        return

    melhores = selecionar_melhores_vagas(perfil, vagas)
    novas    = [v for v in melhores if not job_ja_enviado(user_id, gerar_hash_vaga(v))]

    if not novas:
        await update.message.reply_text("As melhores vagas de hoje ja foram enviadas. Tente amanha!")
        return

    await update.message.reply_text(f"Encontrei {len(novas)} vaga(s)! Gerando curriculos adaptados...")

    for i, vaga in enumerate(novas, 1):
        try:
            await processar_e_enviar_vaga(
                bot=context.bot,
                telegram_id=user_id,
                usuario=usuario,
                perfil=perfil,
                titulo=vaga.get("title",""),
                empresa=vaga.get("company",""),
                local=vaga.get("location",""),
                descricao=vaga.get("description",""),
                url=vaga.get("job_url",""),
                job_hash=gerar_hash_vaga(vaga),
                indice=i,
            )
        except Exception as e:
            logger.error(f"[Teste] Erro vaga {i}: {e}", exc_info=True)
            await update.message.reply_text(f"Erro ao processar vaga {i}: {e}")

# =========================================================
# ONBOARDING — CONVERSATION HANDLER
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)

    if usuario and usuario.get("email"):
        await update.message.reply_text(
            "Perfil ativo!\n\n"
            "O que voce pode fazer:\n"
            "- Enviar .txt ou .pdf com seu historico para atualizar o perfil\n"
            "- Colar a descricao de uma vaga (texto livre) para gerar o curriculo agora\n"
            "- Colar um link do LinkedIn para gerar o curriculo para aquela vaga\n"
            "- /testar_vagas para buscar vagas e receber curriculos agora\n\n"
            "Todo dia ao meio-dia voce recebe 2 sugestoes automaticas com curriculo ja adaptado."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Bem-vindo ao ATS Resume Bot!\n\n"
        "Vou configurar seu perfil em poucos passos.\n\n"
        "Qual o seu NOME COMPLETO?"
    )
    return ASK_NOME


async def ask_nome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["nome_completo"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu E-MAIL profissional?")
    return ASK_EMAIL


async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["email"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu TELEFONE? (com DDD)")
    return ASK_PHONE


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["telefone"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu perfil no LINKEDIN? (URL completo)")
    return ASK_LINKEDIN


async def ask_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["linkedin"] = update.message.text.strip()
    await update.message.reply_text("Qual a sua CIDADE e ESTADO?\nExemplo: Sao Paulo, SP")
    return ASK_CITY


async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cidade"] = update.message.text.strip()
    await update.message.reply_text(
        "Em qual IDIOMA deseja o curriculo?\nExemplos: Portugues, Ingles, Espanhol"
    )
    return ASK_LANGUAGE


async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    salvar_perfil(user_id, {
        "nome_completo": context.user_data.get("nome_completo", ""),
        "email":         context.user_data.get("email", ""),
        "telefone":      context.user_data.get("telefone", ""),
        "linkedin":      context.user_data.get("linkedin", ""),
        "cidade":        context.user_data.get("cidade", ""),
        "idioma":        update.message.text.strip(),
    })
    context.user_data.clear()

    await update.message.reply_text(
        "Perfil salvo!\n\n"
        "Agora envie um arquivo .txt ou .pdf com seu historico profissional.\n"
        "O sistema vai estruturar o seu perfil automaticamente.\n\n"
        "Depois, envie uma vaga ou link do LinkedIn para gerar o curriculo,\n"
        "ou use /testar_vagas para buscar vagas agora."
    )
    return ConversationHandler.END

# =========================================================
# HANDLER PRINCIPAL — TEXTO, ARQUIVO E URL LINKEDIN
# =========================================================
async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    usuario = buscar_usuario(user_id)

    if not usuario or not usuario.get("email"):
        await update.message.reply_text("Use /start para configurar seu perfil primeiro.")
        return

    status = await update.message.reply_text("Processando... Aguarde.")

    # --- Extrai texto ---
    try:
        if update.message.document:
            doc  = update.message.document
            file = await context.bot.get_file(doc.file_id)
            buf  = bytearray()
            await file.download_as_bytearray(out=buf)
            texto = extrair_texto_arquivo(buf, doc.file_name)
            logger.info(f"[Input] Arquivo '{doc.file_name}' de user={user_id}")
        else:
            texto = update.message.text.strip()
            logger.info(f"[Input] Texto de user={user_id}: {texto[:60]}")
    except Exception as e:
        logger.error(f"[Input] {e}")
        await status.edit_text("Nao consegui ler o arquivo. Tente novamente.")
        return

    if not texto.strip():
        await status.edit_text("Nenhum conteudo detectado.")
        return

    intencao = classificar_intencao(texto)
    logger.info(f"[Input] Intencao: {intencao} para user={user_id}")
    perfil = usuario.get("perfil_estruturado") or {}

    # ============================
    # HISTORICO — Consolida perfil
    # ============================
    if intencao == "HISTORICO":
        await status.edit_text("Extraindo e estruturando seu perfil com IA...")
        try:
            novo_perfil = consolidar_perfil(perfil, texto)
            atualizar_perfil_estruturado(user_id, novo_perfil)
            await status.edit_text(
                "Perfil atualizado com sucesso!\n\n"
                "Agora voce pode:\n"
                "- Colar a descricao de uma vaga para gerar o curriculo\n"
                "- Colar um link do LinkedIn\n"
                "- Usar /testar_vagas para buscar vagas agora\n"
                "- Aguardar as sugestoes automaticas do meio-dia"
            )
        except Exception as e:
            logger.error(f"[Historico] {e}", exc_info=True)
            await status.edit_text("Erro ao estruturar perfil. Tente novamente.")
        return

    # ============================
    # URL LINKEDIN — Extrai a vaga
    # ============================
    if intencao == "URL_LINKEDIN":
        if not perfil:
            await status.edit_text(
                "Voce ainda nao tem historico salvo.\n"
                "Envie primeiro um .txt ou .pdf com seu historico."
            )
            return

        await status.edit_text("Extraindo dados da vaga no LinkedIn...")
        urls = re.findall(r"https?://[^\s]+linkedin\.com/jobs[^\s]*", texto, re.IGNORECASE)
        url  = urls[0] if urls else texto.strip()

        resultado = extrair_vaga_linkedin(url, db_client)
        if not resultado.get("sucesso"):
            await status.edit_text(
                f"Nao consegui extrair a vaga: {resultado.get('erro','')}\n\n"
                "Cole a descricao completa da vaga como texto e tente novamente."
            )
            return

        vaga_dados = resultado["dados"]
        await status.edit_text("Gerando curriculo ATS otimizado para a vaga...")
        try:
            await processar_e_enviar_vaga(
                bot=context.bot,
                telegram_id=str(user_id),
                usuario=usuario,
                perfil=perfil,
                titulo=vaga_dados.get("titulo",""),
                empresa=vaga_dados.get("empresa",""),
                local=vaga_dados.get("localizacao",""),
                descricao=vaga_dados.get("descricao",""),
                url=vaga_dados.get("url",""),
                indice=1,
            )
            await status.delete()
        except Exception as e:
            logger.error(f"[URL LinkedIn] {e}", exc_info=True)
            await status.edit_text("Erro ao gerar curriculo. Tente novamente.")
        return

    # ============================
    # VAGA (texto livre)
    # ============================
    if not perfil:
        await status.edit_text(
            "Voce ainda nao tem historico salvo.\n"
            "Envie primeiro um .txt ou .pdf com seu historico."
        )
        return

    await status.edit_text("Gerando curriculo ATS otimizado para a vaga...")
    try:
        await processar_e_enviar_vaga(
            bot=context.bot,
            telegram_id=str(user_id),
            usuario=usuario,
            perfil=perfil,
            titulo="",
            empresa="",
            local="",
            descricao=texto,
            indice=1,
        )
        await status.delete()
    except json.JSONDecodeError as e:
        logger.error(f"[Vaga] JSON invalido: {e}")
        await status.edit_text("O modelo gerou um documento invalido. Tente novamente.")
    except Exception as e:
        logger.error(f"[Vaga] {e}", exc_info=True)
        await status.edit_text("Erro ao gerar curriculo. Tente novamente.")


async def handle_erro(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[Erro Global] {context.error}", exc_info=context.error)

# =========================================================
# MAIN
# =========================================================
def main():
    logger.info("=" * 60)
    logger.info("ATS Resume Bot — Versao Definitiva")
    logger.info("=" * 60)

    threading.Thread(target=start_health_server, daemon=True).start()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(HTTPXRequest(connect_timeout=60.0, read_timeout=60.0, http_version="1.1"))
        .build()
    )

    # Agendamento diario — 12:00 Brasilia
    BRASILIA = ZoneInfo("America/Sao_Paulo")
    app.job_queue.run_daily(
        enviar_sugestoes_diarias,
        time=dtime(hour=12, minute=0, second=0, tzinfo=BRASILIA),
        name="sugestoes_diarias",
    )
    logger.info("[Scheduler] Job agendado: 12:00 Brasilia")

    # Onboarding: ASK_NOME(0) -> ASK_EMAIL(1) -> ASK_PHONE(2) -> ASK_LINKEDIN(3) -> ASK_CITY(4) -> ASK_LANGUAGE(5)
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_NOME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_nome)],
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
        MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), handle_input)
    )
    app.add_error_handler(handle_erro)

    logger.info("Bot em modo de escuta (polling)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
