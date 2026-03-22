“””
ATS Resume Bot — Hub de Carreiras

Arquitetura Serverless, processamento assincrono e pipeline integrado de vagas.
“””

import os
import io
import re
import json
import logging
import hashlib
import threading
import asyncio
import fitz  # PyMuPDF
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from groq import Groq
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
Application,
CommandHandler,
MessageHandler,
CallbackQueryHandler,
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

ASK_NOME, ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN, ASK_CITY, ASK_LANGUAGE, ASK_TARGET_ROLE, ASK_SENIORITY = range(8)

# =========================================================

# LOGGING E ENV

# =========================================================

logging.basicConfig(level=logging.INFO, format=”%(asctime)s - %(levelname)s - %(message)s”)
logger = logging.getLogger(**name**)

load_dotenv()
TELEGRAM_TOKEN = os.getenv(“TELEGRAM_TOKEN”, “”).strip()
GROQ_API_KEY   = os.getenv(“GROQ_API_KEY”, “”).strip()
SUPABASE_URL   = os.getenv(“SUPABASE_URL”, “”).strip()
SUPABASE_KEY   = os.getenv(“SUPABASE_KEY”, “”).strip()

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
logger.error(“ERRO CRITICO: Variaveis de ambiente ausentes.”)
raise SystemExit(1)

llm_client: Groq   = Groq(api_key=GROQ_API_KEY)
db_client:  Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================================================

# KEEP-ALIVE

# =========================================================

class HealthHandler(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.send_header(“Content-type”, “text/plain”)
self.end_headers()
self.wfile.write(b”ATS Bot Operacional”)

```
def log_message(self, *a):
    pass
```

def start_health_server():
port = int(os.getenv(“PORT”, 10000))
logger.info(f”[Health] Servidor operando na porta {port}”)
HTTPServer((“0.0.0.0”, port), HealthHandler).serve_forever()

# =========================================================

# SANITIZACAO DE TEXTO

# =========================================================

_SUBS = {
“\u2022”: “-”, “\u2013”: “-”, “\u2014”: “-”,
“\u2018”: “’”, “\u2019”: “’”,
“\u201c”: ‘”’, “\u201d”: ‘”’,
“\u00b7”: “-”, “\u2026”: “…”,
}

def sanitize(text: str) -> str:
if not text:
return “”
text = str(text).replace(”\t”, “ “)
for c, r in _SUBS.items():
text = text.replace(c, r)
return text.encode(“latin-1”, “ignore”).decode(“latin-1”)

# =========================================================

# EXTRACAO DE TEXTO DE ARQUIVO (PyMuPDF)

# =========================================================

def extrair_texto_arquivo(file_bytes: bytearray, filename: str) -> str:
if filename.lower().endswith(”.pdf”):
try:
doc = fitz.open(“pdf”, file_bytes)
return chr(12).join([page.get_text(“text”) for page in doc])
except Exception as e:
logger.error(f”[PDF] Falha de extracao via PyMuPDF: {e}”)
return “”
for enc in [“utf-8”, “latin-1”, “cp1252”]:
try:
return file_bytes.decode(enc)
except UnicodeDecodeError:
continue
return file_bytes.decode(“utf-8”, errors=“ignore”)

# =========================================================

# GERADOR DE PDF (Padrao Harvard)

# =========================================================

class CurriculoHarvard(FPDF):
def **init**(self):
super().**init**()
self.set_margins(20, 20, 20)
self.add_page()
self.set_auto_page_break(True, margin=15)

```
def _secao(self, titulo: str):
    self.ln(2)
    self.set_font("helvetica", "B", 11)
    self.cell(0, 7, sanitize(titulo.upper()), new_x="LMARGIN", new_y="NEXT")
    self.set_line_width(0.5)
    self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
    self.ln(2)

def _linha(self, txt: str, size: int = 10, bold: bool = False, italic: bool = False, align: str = "L"):
    style = ("B" if bold else "") + ("I" if italic else "")
    self.set_font("helvetica", style, size)
    self.multi_cell(0, 5, sanitize(txt), align=align, new_x="LMARGIN", new_y="NEXT")

def _bullet(self, txt: str, prefixo: str = "-"):
    self.set_font("helvetica", "", 10)
    self.multi_cell(0, 5, sanitize(f"{prefixo} {txt}"), new_x="LMARGIN", new_y="NEXT")

def _flatten_item(self, item) -> str:
    if isinstance(item, dict):
        return " - ".join([str(v) for v in item.values() if v])
    return str(item)

def bloco_cabecalho(self, ident: dict):
    nome   = ident.get("nome", "")
    titulo = ident.get("titulo", "")
    self.set_font("helvetica", "B", 18)
    self.multi_cell(0, 10, sanitize(nome), align="C", new_x="LMARGIN", new_y="NEXT")
    if titulo:
        self.set_font("helvetica", "I", 11)
        self.multi_cell(0, 6, sanitize(titulo), align="C", new_x="LMARGIN", new_y="NEXT")
    campos   = ["email", "telefone", "linkedin", "localizacao", "github", "portfolio"]
    contatos = [str(ident.get(c, "")).strip() for c in campos if ident.get(c, "").strip()]
    if contatos:
        self.set_font("helvetica", "", 9)
        self.multi_cell(0, 5, sanitize(" | ".join(contatos)), align="C", new_x="LMARGIN", new_y="NEXT")
    self.ln(4)

def bloco_resumo(self, titulo: str, texto: str):
    if not texto:
        return
    self._secao(titulo)
    self._linha(texto)

def bloco_competencias(self, titulo: str, lista: list):
    if not lista:
        return
    self._secao(titulo)
    itens = [sanitize(self._flatten_item(i)) for i in lista if i]
    self.set_font("helvetica", "", 10)
    self.multi_cell(0, 5, " | ".join(itens), new_x="LMARGIN", new_y="NEXT")

def bloco_experiencias(self, titulo: str, exps: list):
    if not exps:
        return
    self._secao(titulo)
    for exp in exps:
        if not isinstance(exp, dict):
            continue
        cargo      = str(exp.get("cargo", "")).strip()
        empresa    = str(exp.get("empresa", "")).strip()
        local_exp  = str(exp.get("localizacao", "")).strip()
        inicio     = str(exp.get("data_inicio", "")).strip()
        fim        = str(exp.get("data_fim", "")).strip()
        desc_emp   = str(exp.get("descricao_empresa", "")).strip()

        self.set_font("helvetica", "B", 11)
        linha_cargo = f"{cargo}"
        if empresa:
            linha_cargo += f" - {empresa}"
        self.cell(0, 6, sanitize(linha_cargo), new_x="LMARGIN", new_y="NEXT")

        # ✅ BUG #1 CORRIGIDO: 'e' → 'and'
        # ✅ BUG #6 CORRIGIDO: fallback de data quando só existe inicio
        meta = ""
        if inicio and fim:
            meta = f"{inicio} - {fim}"
        elif inicio:
            meta = f"{inicio} - Presente"
        elif fim:
            meta = fim

        if local_exp:
            meta = f"{meta} | {local_exp}" if meta else local_exp
        if meta:
            self.set_font("helvetica", "I", 9)
            self.cell(0, 5, sanitize(meta), new_x="LMARGIN", new_y="NEXT")

        if desc_emp and desc_emp.lower() != "none":
            self.set_font("helvetica", "I", 9)
            self.multi_cell(0, 4, sanitize(desc_emp), new_x="LMARGIN", new_y="NEXT")

        self.ln(1)

        resps = exp.get("responsabilidades", [])
        if isinstance(resps, str):
            resps = [resps]
        for r in resps:
            val = self._flatten_item(r)
            if val:
                self._bullet(val, "-")

        conquistas = exp.get("conquistas", [])
        if isinstance(conquistas, str):
            conquistas = [conquistas]
        for c in conquistas:
            val = self._flatten_item(c)
            if val:
                # ✅ BUG #3 CORRIGIDO: '>>' substituido por bullet tipografico
                self.set_font("helvetica", "B", 10)
                self.multi_cell(0, 5, sanitize(f"• {val}"), new_x="LMARGIN", new_y="NEXT")

        self.ln(3)

def bloco_educacao(self, titulo: str, edus: list):
    if not edus:
        return
    self._secao(titulo)
    for edu in edus:
        if not isinstance(edu, dict):
            continue
        grau  = str(edu.get("grau", "")).strip()
        curso = str(edu.get("curso", "")).strip()
        inst  = str(edu.get("instituicao", "")).strip()
        ini   = str(edu.get("ano_inicio", "")).strip()
        fim   = str(edu.get("ano_fim", "")).strip()

        cabecalho = f"{grau} em {curso}" if grau else curso
        self.set_font("helvetica", "B", 11)
        self.cell(0, 6, sanitize(cabecalho), new_x="LMARGIN", new_y="NEXT")

        meta = ""
        if ini and fim:
            meta = f"{ini} - {fim}"
        elif ini:
            meta = ini
        elif fim:
            meta = fim
        if inst:
            meta = f"{inst} | {meta}" if meta else inst
        if meta:
            self.set_font("helvetica", "I", 10)
            self.cell(0, 5, sanitize(meta), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

def bloco_lista_simples(self, titulo: str, itens: list):
    if not itens:
        return
    self._secao(titulo)
    self.set_font("helvetica", "", 10)
    for item in itens:
        val = self._flatten_item(item)
        if val:
            self.multi_cell(0, 5, sanitize(f"- {val}"), new_x="LMARGIN", new_y="NEXT")
    self.ln(1)

def bloco_projetos(self, titulo: str, projetos: list):
    if not projetos:
        return
    self._secao(titulo)
    for proj in projetos:
        if not isinstance(proj, dict):
            continue
        self.set_font("helvetica", "B", 10)
        self.cell(0, 6, sanitize(str(proj.get("nome", ""))), new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, sanitize(str(proj.get("descricao", ""))), new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

def bloco_keywords_ocultas(self, keywords: list):
    if not keywords:
        return
    termos = [sanitize(str(k)) for k in keywords if k]
    if not termos:
        return
    self.set_text_color(255, 255, 255)
    self.set_font("helvetica", "", 1)
    self.multi_cell(0, 1, " ".join(termos), new_x="LMARGIN", new_y="NEXT")
    self.set_text_color(0, 0, 0)
```

def gerar_pdf(cv: dict, idioma: str = “Portugues”) -> io.BytesIO:
if not isinstance(cv, dict):
cv = {}
# Cabecalhos sempre hardcoded — nunca usa o que o LLM devolveu
cab = _get_cabecalhos(idioma)
pdf = CurriculoHarvard()
pdf.bloco_cabecalho(cv.get(“identificacao”, {}))
pdf.bloco_resumo(cab[“resumo”], cv.get(“resumo”, “”))
pdf.bloco_competencias(cab[“competencias”], cv.get(“competencias”, []))
pdf.bloco_experiencias(cab[“experiencias”], cv.get(“experiencias”, []))
pdf.bloco_educacao(cab[“educacao”], cv.get(“educacao”, []))
pdf.bloco_lista_simples(cab[“certificacoes”], cv.get(“certificacoes”, []))
pdf.bloco_projetos(cab[“projetos”], cv.get(“projetos”, []))
pdf.bloco_lista_simples(cab[“idiomas”], cv.get(“idiomas”, []))
pdf.bloco_keywords_ocultas(cv.get(“keywords_ocultas”, []))
buf = io.BytesIO()
pdf.output(buf)
buf.seek(0)
return buf

# =========================================================

# GROQ E PROMPTS

# =========================================================

_MODEL = “llama-3.3-70b-versatile”

def _chat(system: str, prompt: str, json_mode: bool = False, temperature: float = 0.1) -> str:
kwargs = dict(
model=_MODEL,
messages=[
{“role”: “system”, “content”: system},
{“role”: “user”,   “content”: prompt},
],
temperature=temperature,
max_tokens=8000,
)
if json_mode:
kwargs[“response_format”] = {“type”: “json_object”}
resp = llm_client.chat.completions.create(**kwargs)
return resp.choices[0].message.content.strip()

def _parse_json(raw: str) -> dict:
return json.loads(re.sub(r”`json|`”, “”, raw).strip())

# =========================================================

# CABECALHOS HARDCODED — nunca dependem do LLM

# =========================================================

_CABECALHOS_PT = {
“resumo”:       “Resumo Profissional”,
“competencias”: “Competencias”,
“experiencias”: “Experiencia Profissional”,
“educacao”:     “Formacao Academica”,
“certificacoes”:“Certificacoes”,
“projetos”:     “Projetos”,
“idiomas”:      “Idiomas”,
}
_CABECALHOS_EN = {
“resumo”:       “Professional Summary”,
“competencias”: “Skills”,
“experiencias”: “Professional Experience”,
“educacao”:     “Education”,
“certificacoes”:“Certifications”,
“projetos”:     “Projects”,
“idiomas”:      “Languages”,
}
_CABECALHOS_ES = {
“resumo”:       “Resumen Profesional”,
“competencias”: “Competencias”,
“experiencias”: “Experiencia Profesional”,
“educacao”:     “Formacion Academica”,
“certificacoes”:“Certificaciones”,
“projetos”:     “Proyectos”,
“idiomas”:      “Idiomas”,
}

def _get_cabecalhos(idioma: str) -> dict:
idioma_lower = idioma.lower()
if any(k in idioma_lower for k in (“ingl”, “engl”)):
return _CABECALHOS_EN
if any(k in idioma_lower for k in (“espan”, “espa”, “spain”, “spani”)):
return _CABECALHOS_ES
return _CABECALHOS_PT

def _sanitizar_cv(cv: dict, idioma: str = “Portugues”) -> dict:
“””
Pós-processa o JSON retornado pelo LLM:
- Sobrescreve cabecalhos com valores hardcoded (nunca confia no LLM para isso)
- Trunca identificacao.titulo para máximo 6 palavras
- Remove lixo óbvio de campos que não devem ter conteúdo longo
“””
if not isinstance(cv, dict):
return cv

```
# 1. Sobrescrever cabecalhos completamente — nunca depende do LLM
cv["cabecalhos"] = _get_cabecalhos(idioma)

# 2. Sanitizar titulo: máximo 6 palavras
ident = cv.get("identificacao", {})
if isinstance(ident, dict):
    titulo = str(ident.get("titulo", "")).strip()
    palavras = titulo.split()
    if len(palavras) > 6:
        ident["titulo"] = " ".join(palavras[:6])
    cv["identificacao"] = ident

return cv
```

_SYSTEM_CONSOLIDAR = “”“INSTRUCAO: Voce atua como um Engenheiro de Dados especialista em parsing de documentos de Recursos Humanos.

Sua funcao e analisar o PERFIL ATUAL do candidato armazenado no banco de dados relacional e a NOVA ENTRADA de dados fornecida pelo usuario. Seu objetivo e retornar um JSON consolidado, normalizado e atualizado.

REGRAS DE MERGE E EXTRACAO:

1. RESOLUCAO DE CONFLITOS: Se a NOVA ENTRADA for um curriculo completo ou historico abrangente, atualize os dados existentes e remova duplicidades logicas. Se for apenas uma atualizacao pontual, insira o novo dado sem apagar o restante.
1. NORMALIZACAO DE DADOS: Padronize as datas para o formato “Mes/Ano”. Categorize as skills como “Hard Skill” ou “Soft Skill”.
1. FIDELIDADE: Nao resuma as descricoes, responsabilidades e conquistas. Mantenha a integridade do texto.
1. DADOS NAO-TRADICIONAIS: Mapeie freelances para “experiences”, projetos para “projects” e intercambios para “education” ou “experiences”.
1. FORMATO: Retorne EXCLUSIVAMENTE um objeto JSON valido.

SCHEMA EXIGIDO:

{
“experiences”: [
{“cargo”:””,“empresa”:””,“localizacao”:””,“data_inicio”:””,“data_fim”:””,“descricao_empresa”:””,“responsabilidades”:[],“conquistas”:[]}
],
“education”: [
{“grau”:””,“curso”:””,“instituicao”:””,“ano_inicio”:””,“ano_fim”:””}
],
“skills”: [
{“nome”:””,“categoria”:””,“nivel”:””}
],
“certifications”: [
{“nome”:””,“emissor”:””,“ano”:””}
],
“projects”: [
{“nome”:””,“descricao”:””}
],
“languages”: [
{“idioma”:””,“nivel”:””}
]
}”””

# ✅ BUG #2 e #4 CORRIGIDOS: cabecalhos com valores literais fixos + exemplos negativos explícitos

_SYSTEM_CV = “”“INSTRUCAO SUPREMA: Voce atua como um Recrutador Tecnico Senior e Especialista em Sistemas ATS.

Sua missao e cruzar o HISTORICO do candidato com os dados estruturados da VAGA ALVO e criar um curriculo direcionado.

REGRAS VITAIS E ALGORITMICAS:

1. MAPEAMENTO DE PALAVRAS-CHAVE: Injete as exatas palavras-chave exigidas pela vaga de forma organica no “resumo”, “competencias” e “responsabilidades”.
1. EQUIVALENCIA TECNOLOGICA (CRITICO): Se a vaga exige uma ferramenta (ex: AWS, Tableau) e o candidato possui dominio em uma concorrente direta (ex: GCP, Power BI), considere como MATCH. No curriculo, escreva no formato “Power BI (Equivalente a Tableau)”. NUNCA adicione como gap uma habilidade se o candidato possui o equivalente.
1. PREVENCAO DE ALUCINACAO: NUNCA invente experiencias. Se o candidato nao possui o requisito nem uma ferramenta equivalente, omita-o do curriculo e liste-o estritamente em “analise_gaps”.
1. CABECALHOS PADRONIZADOS (CRITICO): O objeto “cabecalhos” DEVE conter APENAS os titulos padroes de secao de curriculo traduzidos para o idioma alvo. NUNCA insira descricoes, resumos, titulos de cargos ou qualquer outro conteudo neste objeto.
- EXEMPLOS PROIBIDOS em cabecalhos: “Desenvolvedor Python Senior”, “Especialista em IA Generativa”, “Projetos de Machine Learning com foco em NLP”, “DESENVOLVIMENTO DE SOLUCOES DE IA, ANALISE DE DADOS”.
- EXEMPLOS CORRETOS em cabecalhos: “Experiencia Profissional”, “Formacao Academica”, “Competencias”, “Certificacoes”, “Projetos”, “Idiomas”, “Resumo Profissional”.
1. TITULO PROFISSIONAL: O campo “identificacao.titulo” deve ser CURTO, maximo 6 palavras, representando apenas o cargo-alvo. EXEMPLOS CORRETOS: “Cientista de Dados | IA Generativa”, “Engenheiro de Machine Learning”. EXEMPLOS PROIBIDOS: “CIENTISTA DE DADOS COM EXPERIENCIA EM MACHINE LEARNING E INTELIGENCIA ARTIFICIAL”.
1. METODO STAR: Reescreva os bullet points de experiencias focando em impacto quantificavel.
1. ALAVANCAGEM DE BACKGROUND: Valorize Projetos Pessoais e trabalhos Freelance para compensar lacunas formais.
1. KEYWORDS OCULTAS: Liste termos exigidos pela vaga que o candidato NAO possui, para o stealth ATS.
1. FORMATO: Retorne JSON puro. Para listas de strings (como idiomas e certificacoes), retorne arrays com strings diretas (Ex: [“Ingles - Fluente”]), NUNCA insira objetos JSON dentro das listas simples.

SCHEMA OBRIGATORIO:

{
“cabecalhos”: {
“resumo”: “Resumo Profissional”,
“competencias”: “Competencias”,
“experiencias”: “Experiencia Profissional”,
“educacao”: “Formacao Academica”,
“certificacoes”: “Certificacoes”,
“projetos”: “Projetos”,
“idiomas”: “Idiomas”
},
“identificacao”: {
“nome”: “”, “titulo”: “”, “localizacao”: “”, “telefone”: “”, “email”: “”, “linkedin”: “”, “github”: “”, “portfolio”: “”
},
“resumo”: “”,
“competencias”: [],
“experiencias”: [
{
“cargo”: “”, “empresa”: “”, “localizacao”: “”, “data_inicio”: “”, “data_fim”: “”, “descricao_empresa”: “”,
“responsabilidades”: [], “conquistas”: []
}
],
“educacao”: [
{“grau”: “”, “curso”: “”, “instituicao”: “”, “ano_inicio”: “”, “ano_fim”: “”}
],
“certificacoes”: [],
“projetos”: [{“nome”: “”, “descricao”: “”}],
“idiomas”: [],
“keywords_ocultas”: [],
“relatorio_analitico”: {
“match_score”: 0, “analise_gaps”: [], “dica_entrevista”: “”
}
}”””

def classificar_intencao(texto: str) -> str:
if re.search(r”linkedin.com/jobs”, texto, re.IGNORECASE):
return “URL_LINKEDIN”
raw = _chat(
system=“Classifique mensagens enviadas a um bot de carreira. Responda APENAS: VAGA, HISTORICO, EDICAO ou OUTRO.”,
prompt=(
“VAGA = descricao de cargo/emprego\n”
“HISTORICO = curriculo ou experiencias do usuario\n”
“EDICAO = instrucao de atualizacao de dados (Ex: ‘adicione que sei power bi’, ‘remova a empresa X’)\n”
“OUTRO = perguntas gerais, conversas\n\n”
f”Mensagem:\n{texto[:1500]}”
),
temperature=0.0,
)
for cat in (“VAGA”, “HISTORICO”, “EDICAO”):
if cat in raw.upper():
return cat
return “OUTRO”

def consolidar_perfil(perfil_atual: dict, nova_entrada: str) -> dict:
raw = _chat(
system=_SYSTEM_CONSOLIDAR,
prompt=(f”PERFIL ATUAL:\n{json.dumps(perfil_atual, ensure_ascii=False)}\n\n”
f”NOVA ENTRADA:\n{nova_entrada}”),
json_mode=True,
temperature=0.0,
)
return _parse_json(raw)

def editar_perfil_llm(perfil_atual: dict, instrucao: str) -> dict:
raw = _chat(
system=(_SYSTEM_CONSOLIDAR + “\n\nMODO EDICAO PONTUAL. Aplique APENAS a mudanca solicitada.”),
prompt=(f”PERFIL ATUAL:\n{json.dumps(perfil_atual, ensure_ascii=False)}\n\n”
f”INSTRUCAO:\n{instrucao}”),
json_mode=True,
temperature=0.0,
)
return _parse_json(raw)

def formatar_perfil_texto(usuario: dict, perfil: dict) -> str:
linhas = []
nome = usuario.get(“nome_completo”, “Candidato”)
linhas.append(f”Nome: {nome}”)
linhas.append(f”Objetivo: {usuario.get(‘cargo_alvo’, ‘Nao definido’)} ({usuario.get(‘senioridade’, ‘Nao definido’)})”)
contatos = []
for k in [“email”, “telefone”, “linkedin”, “cidade”]:
if usuario.get(k):
contatos.append(usuario[k])
if contatos:
linhas.append(“Contato: “ + “ | “.join(contatos))
exps = perfil.get(“experiences”, [])
if exps:
linhas.append(”\nEXPERIENCIAS”)
for e in exps:
linhas.append(f”- {e.get(‘cargo’, ‘’)} | {e.get(‘empresa’, ‘’)} ({e.get(‘data_inicio’, ‘’)} a {e.get(‘data_fim’, ‘’)})”)
edus = perfil.get(“education”, [])
if edus:
linhas.append(”\nFORMACAO”)
for ed in edus:
linhas.append(f”- {ed.get(‘curso’, ‘’)} | {ed.get(‘instituicao’, ‘’)}”)
skills = perfil.get(“skills”, [])
if skills:
hard = [s.get(“nome”, “”) for s in skills if “hard” in s.get(“categoria”, “”).lower()]
if hard:
linhas.append(”\nTECNOLOGIAS: “ + “, “.join(hard))
linhas.append(”\nPara editar: envie em texto livre. Ex: Adicione conhecimento em Docker e Kubernetes.”)
return “\n”.join(linhas)

def perfil_tem_ingles_fluente(perfil: dict) -> bool:
niveis_ok = {“intermediario”, “fluente”, “nativo”, “avancado”, “intermediate”, “fluent”, “native”, “advanced”}
for lang in perfil.get(“languages”, []):
if not isinstance(lang, dict):
continue
idioma = lang.get(“idioma”, “”).lower()
nivel  = lang.get(“nivel”, “”).lower()
if “ingl” in idioma or “english” in idioma:
if any(n in nivel for n in niveis_ok):
return True
return False

def selecionar_melhores_vagas(perfil: dict, vagas: list, senioridade_alvo: str) -> list:
lista = “\n”.join([
f”{i}. {v.get(‘title’,’’)} | {v.get(‘company’,’’)} | {v.get(‘location’,’’)}\n”
f”   Descricao: {str(v.get(‘description’,’’))[:400]}”
for i, v in enumerate(vagas)
])
regra_eliminacao = (
“- REGRA DE ELIMINACAO (Score 0): Se a vaga exige nivel Senior/Pleno e o candidato e Junior/Estagio (ou vice-versa), o score DEVE ser 0.\n”
if senioridade_alvo else
“- Avalie a vaga puramente pelas habilidades tecnicas, pois a senioridade do candidato nao esta definida.\n”
)
raw = _chat(
system=“Voce e um recrutador tecnico senior. Retorne SOMENTE JSON valido.”,
prompt=(
f”Avalie a aderencia de cada vaga ao perfil do candidato. Senioridade alvo: {senioridade_alvo or ‘Nao definida’}.\n\n”
“REGRAS DE PONTUACAO ESTUDADA (0 a 100):\n”
f”{regra_eliminacao}”
“- EQUIVALENCIA TECNOLOGICA: Ferramentas concorrentes (ex: Power BI vs Tableau, AWS vs Azure) possuem a mesma logica base. Considere como MATCH integral. Nao penalize o score se o candidato possui ferramenta equivalente.\n”
“- 80-100: Cargo exato, dominio de tecnologias ou equivalentes.\n”
“- 60-79: Cargo relacionado, dominio de tecnologias core.\n”
“- 0-59: Faltam requisitos fundamentais sem compensacao.\n\n”
“IMPORTANTE: use numeros INTEIROS de 0 a 100. Nunca retorne numeros decimais.\n”
‘Retorne APENAS este JSON:\n’
‘{“scores”: [{“indice”: 0, “score”: 75, “motivo”: “justificativa”}, …]}\n\n’
f”PERFIL DO CANDIDATO:\n{json.dumps(perfil, ensure_ascii=False)[:2500]}\n\n”
f”VAGAS PARA AVALIAR:\n{lista}”
),
json_mode=True,
temperature=0.0,
)
try:
scores = _parse_json(raw).get(“scores”, [])
if not scores:
return []
for s in scores:
if isinstance(s, dict):
try:
val = float(s.get(“score”, 0))
if 0 < val <= 1.0:
s[“score”] = int(val * 100)
else:
s[“score”] = int(val)
except Exception:
s[“score”] = 0
aprovadas = sorted(
[s for s in scores if isinstance(s, dict) and s.get(“score”, 0) >= 60],
key=lambda s: s.get(“score”, 0),
reverse=True,
)
resultado = [vagas[s[“indice”]] for s in aprovadas[:2]
if isinstance(s.get(“indice”), int) and s[“indice”] < len(vagas)]
for s in aprovadas[:2]:
idx = s.get(“indice”)
if isinstance(idx, int) and idx < len(vagas):
vagas[idx][”_match_score”] = s.get(“score”, 0)
return resultado
except Exception as e:
logger.error(f”[Score] Erro pontuacao: {e}”, exc_info=True)
return []

def gerar_cv_json(perfil: dict, usuario: dict, titulo_vaga: str, empresa_vaga: str, local_vaga: str,
descricao_vaga: str, com_resumo: bool = True) -> dict:
idioma           = usuario.get(“idioma”, “Portugues”)
instrucao_resumo = “” if com_resumo else “\nIMPORTANTE: Deixe o campo ‘resumo’ vazio.”
raw = _chat(
system=_SYSTEM_CV,
prompt=(
f”HISTORICO DO CANDIDATO:\n{json.dumps(perfil, ensure_ascii=False)}\n\n”
f”DADOS DO CANDIDATO:\nNome: {usuario.get(‘nome_completo’,’’)}\n”
f”VAGA ALVO:\nTitulo: {titulo_vaga}\nEmpresa: {empresa_vaga}\nDescricao:\n{descricao_vaga}\n\n”
f”IDIOMA: {idioma}{instrucao_resumo}”
),
json_mode=True,
temperature=0.15,
)
cv = _parse_json(raw)
return _sanitizar_cv(cv, idioma)

def editar_cv_json(cv_atual: dict, instrucao: str) -> dict:
raw = _chat(
system=“Aplique APENAS a alteracao solicitada no JSON do curriculo. Mantenha os outros campos.”,
prompt=(f”CURRICULO ATUAL:\n{json.dumps(cv_atual, ensure_ascii=False)}\n\n”
f”INSTRUCAO:\n{instrucao}”),
json_mode=True,
temperature=0.0,
)
return _parse_json(raw)

# =========================================================

# SUPABASE

# =========================================================

def salvar_perfil(telegram_id: int, dados: dict):
dados[“telegram_id”] = str(telegram_id)
db_client.table(“user_profiles”).upsert(dados, on_conflict=“telegram_id”).execute()

def atualizar_perfil_estruturado(telegram_id: int, perfil: dict):
db_client.table(“user_profiles”).update({“perfil_estruturado”: perfil}).eq(“telegram_id”, str(telegram_id)).execute()

def buscar_usuario(telegram_id: int) -> dict | None:
try:
r = db_client.table(“user_profiles”).select(”*”).eq(“telegram_id”, str(telegram_id)).execute()
return r.data[0] if r.data else None
except Exception as e:
logger.error(f”[Supabase] buscar_usuario: {e}”)
return None

def buscar_todos_usuarios() -> list:
try:
r = db_client.table(“user_profiles”).select(”*”).not_.is_(“perfil_estruturado”, “null”).execute()
return r.data or []
except Exception as e:
logger.error(f”[Supabase] buscar_todos: {e}”)
return []

def job_ja_enviado(telegram_id: str, job_hash: str) -> bool:
try:
r = db_client.table(“sent_jobs”).select(“id”).eq(“telegram_id”, telegram_id).eq(“job_hash”, job_hash).execute()
return bool(r.data)
except Exception:
return False

def registrar_job_enviado(telegram_id: str, job_hash: str, title: str, company: str):
try:
db_client.table(“sent_jobs”).insert({
“telegram_id”: telegram_id, “job_hash”: job_hash, “job_title”: title, “job_company”: company,
}).execute()
except Exception as e:
logger.error(f”[Supabase] registrar_job: {e}”)

def gerar_hash_vaga(vaga: dict) -> str:
chave = f”{vaga.get(‘title’, vaga.get(‘titulo’,’’))}{vaga.get(‘company’, vaga.get(‘empresa’,’’))}”.lower().strip()
return hashlib.md5(chave.encode()).hexdigest()

# =========================================================

# MENUS E CONTROLES DE FLUXO

# =========================================================

async def _enviar_menu(update: Update, context: ContextTypes.DEFAULT_TYPE = None, nome: str = “”):
keyboard = InlineKeyboardMarkup([
[InlineKeyboardButton(“Buscar Vagas Agora”, callback_data=“menu_buscar”)],
[InlineKeyboardButton(“Meu Perfil”,         callback_data=“menu_perfil”),
InlineKeyboardButton(“Deletar Dados”,      callback_data=“menu_deletar”)]
])
texto = (
“Perfil ativo e configurado.\n\n”
“Selecione uma acao abaixo ou envie a descricao/link de uma vaga “
“para gerar um curriculo adaptado imediatamente.”
)
if update.message:
await update.message.reply_text(texto, reply_markup=keyboard)
elif update.callback_query:
await update.callback_query.edit_message_text(texto, reply_markup=keyboard)

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
if query.data == “menu_buscar”:
await cmd_testar_vagas(update, context)
elif query.data == “menu_perfil”:
await cmd_meu_perfil(update, context)
elif query.data == “menu_deletar”:
await cmd_deletar(update, context)
elif query.data == “menu_atualizar_objetivo”:
await cmd_atualizar_objetivo(update, context)

# =========================================================

# ONBOARDING E ATUALIZACAO DE OBJETIVO

# =========================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
user_id = update.effective_user.id
usuario = buscar_usuario(user_id)
if usuario:
nome = usuario.get(“nome_completo”, update.effective_user.first_name or “”)
await _enviar_menu(update, context, nome)
return ConversationHandler.END
await update.message.reply_text(
“Bem-vindo ao ATS Resume Bot.\n\n”
“Vou configurar seu perfil em poucos passos.\n\n”
“Qual o seu NOME COMPLETO?”
)
return ASK_NOME

async def cmd_atualizar_objetivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
if update.callback_query:
await update.callback_query.answer()
msg = update.callback_query.message
else:
msg = update.message
await msg.reply_text(
“Atualizacao de Perfil necessaria.\n\n”
“Qual o cargo exato que voce busca?\n”
“Exemplos: Desenvolvedor Python, Analista de Dados, Engenheiro DevOps”
)
return ASK_TARGET_ROLE

async def ask_nome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
context.user_data[“nome_completo”] = update.message.text.strip()
await update.message.reply_text(“Qual o seu E-MAIL profissional?”)
return ASK_EMAIL

async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
context.user_data[“email”] = update.message.text.strip()
await update.message.reply_text(“Qual o seu TELEFONE? (com DDD)”)
return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
context.user_data[“telefone”] = update.message.text.strip()
await update.message.reply_text(“Qual o seu perfil no LINKEDIN? (URL completo)”)
return ASK_LINKEDIN

async def ask_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
context.user_data[“linkedin”] = update.message.text.strip()
await update.message.reply_text(“Qual a sua CIDADE e ESTADO? (Exemplo: Sao Paulo, SP)”)
return ASK_CITY

async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
context.user_data[“cidade”] = update.message.text.strip()
await update.message.reply_text(“Em qual IDIOMA deseja o curriculo? (Exemplos: Portugues, Ingles)”)
return ASK_LANGUAGE

async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
context.user_data[“idioma”] = update.message.text.strip()
await update.message.reply_text(
“Qual o cargo exato que voce busca?\n”
“Exemplos: Desenvolvedor Python, Analista de Dados, Engenheiro DevOps”
)
return ASK_TARGET_ROLE

async def ask_target_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
context.user_data[“cargo_alvo”] = update.message.text.strip()
keyboard = InlineKeyboardMarkup([
[InlineKeyboardButton(“Estagio”,     callback_data=“sen_Estagio”),
InlineKeyboardButton(“Junior”,      callback_data=“sen_Junior”)],
[InlineKeyboardButton(“Pleno”,       callback_data=“sen_Pleno”),
InlineKeyboardButton(“Senior”,      callback_data=“sen_Senior”)],
[InlineKeyboardButton(“Especialista”, callback_data=“sen_Especialista”)]
])
await update.message.reply_text(“Qual o seu nivel de experiencia atual/desejado?”, reply_markup=keyboard)
return ASK_SENIORITY

async def callback_seniority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
query = update.callback_query
await query.answer()
senioridade = query.data.split(”_”)[1]
user_id     = update.effective_user.id
cargo_alvo  = context.user_data.get(“cargo_alvo”, “”)
salvar_perfil(user_id, {
“nome_completo”: context.user_data.get(“nome_completo”, “”),
“email”:         context.user_data.get(“email”, “”),
“telefone”:      context.user_data.get(“telefone”, “”),
“linkedin”:      context.user_data.get(“linkedin”, “”),
“cidade”:        context.user_data.get(“cidade”, “”),
“idioma”:        context.user_data.get(“idioma”, “”),
“cargo_alvo”:    cargo_alvo,
“senioridade”:   senioridade,
})
context.user_data.clear()
await query.edit_message_text(
f”Perfil salvo.\n”
f”Objetivo: {cargo_alvo} ({senioridade})\n\n”
“Agora envie um arquivo .pdf ou .txt com seu historico profissional base.”
)
return ConversationHandler.END

# =========================================================

# PIPELINE: GERA CV + ENVIA PDF

# =========================================================

async def _perguntar_tipo_cv(update_or_query, context: ContextTypes.DEFAULT_TYPE, vaga_dados: dict):
context.user_data[“vaga_pendente”] = vaga_dados
keyboard = InlineKeyboardMarkup([
[InlineKeyboardButton(“Com Resumo”,  callback_data=“cv_com_resumo”),
InlineKeyboardButton(“Sem Resumo”,  callback_data=“cv_sem_resumo”)]
])
msg = “Como voce quer o curriculo gerado?”
if hasattr(update_or_query, “message”) and update_or_query.message:
await update_or_query.message.reply_text(msg, reply_markup=keyboard)
else:
await update_or_query.reply_text(msg, reply_markup=keyboard)

async def callback_tipo_cv(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
com_resumo = query.data == “cv_com_resumo”
vaga       = context.user_data.pop(“vaga_pendente”, None)
if not vaga:
await query.edit_message_text(“Sessao expirada. Envie a vaga novamente.”)
return
user_id = str(update.effective_user.id)
usuario = buscar_usuario(user_id)
perfil  = (usuario or {}).get(“perfil_estruturado”) or {}
tipo_txt = “com resumo” if com_resumo else “sem resumo”
await query.edit_message_text(f”Gerando curriculo ATS {tipo_txt}… Aguarde.”)
try:
await processar_e_enviar_vaga(
bot=context.bot, telegram_id=user_id, usuario=usuario, perfil=perfil,
titulo=vaga.get(“titulo”, vaga.get(“title”, “”)),
empresa=vaga.get(“empresa”, vaga.get(“company”, “”)),
local=vaga.get(“localizacao”, vaga.get(“location”, “”)),
descricao=vaga.get(“descricao”, vaga.get(“description”, “”)),
url=vaga.get(“url”, vaga.get(“job_url”, “”)),
com_resumo=com_resumo, context=context,
)
await query.delete_message()
except Exception as e:
logger.error(f”[TipoCV] {e}”, exc_info=True)
await query.edit_message_text(f”Erro ao gerar curriculo: {e}”)

async def processar_e_enviar_vaga(
bot, telegram_id: str, usuario: dict, perfil: dict,
titulo: str, empresa: str, local: str, descricao: str,
url: str = “”, job_hash: str = “”, indice: int = 1,
com_resumo: bool = True, context: ContextTypes.DEFAULT_TYPE = None,
):
idioma  = usuario.get(“idioma”, “Portugues”)
cv      = gerar_cv_json(perfil, usuario, titulo, empresa, local, descricao, com_resumo)
pdf_buf = gerar_pdf(cv, idioma)

```
if context is not None:
    context.user_data["ultimo_cv"]      = cv
    context.user_data["ultimo_usuario"] = usuario

nome_usuario = usuario.get("nome_completo", "Candidato")
nome_vaga    = titulo or empresa or "Vaga"

def _slug(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s.strip())[:40]

nome_arquivo = f"{_slug(nome_usuario)}_{_slug(nome_vaga)}.pdf"
caption      = f"Vaga {indice}: {titulo}\nEmpresa: {empresa}\nLocal: {local}"
if url and url not in ("nan", ""):
    caption += f"\nLink: {url}"

await bot.send_document(chat_id=telegram_id, document=pdf_buf, filename=nome_arquivo, caption=caption)

rel = cv.get("relatorio_analitico", {})
if rel:
    score = rel.get("match_score", "?")
    gaps  = rel.get("analise_gaps", [])
    dica  = rel.get("dica_entrevista", "")
    linhas = [f"Relatorio ATS - Vaga {indice}", f"Match Score: {score}/100"]
    if gaps:
        linhas.append("\nGaps identificados:")
        linhas += [f"- {g}" for g in gaps]
        linhas.append(
            "\nDICA DE GAPS: Se voce possui experiencia com alguma destas ferramentas (ou equivalentes), "
            "responda esta mensagem informando. O bot ira incorporar permanentemente ao seu banco de dados."
        )
    if dica:
        linhas.append(f"\nDica para entrevista:\n{dica}")
    await bot.send_message(chat_id=telegram_id, text="\n".join(linhas))

if job_hash:
    registrar_job_enviado(telegram_id, job_hash, titulo, empresa)
```

# =========================================================

# JOB DIARIO E ROTINAS DE SCRAPING

# =========================================================

async def enviar_sugestoes_diarias(context: ContextTypes.DEFAULT_TYPE):
logger.info(”[Scheduler] Iniciando sugestoes diarias…”)
usuarios = buscar_todos_usuarios()
for usuario in usuarios:
telegram_id = usuario.get(“telegram_id”)
perfil      = usuario.get(“perfil_estruturado”) or {}
cidade      = usuario.get(“cidade”, “Brazil”)
if not telegram_id or not perfil:
continue
cargo_alvo  = usuario.get(“cargo_alvo”, “”)
senioridade = usuario.get(“senioridade”, “”)
termo_busca = f”{cargo_alvo} {senioridade}”.strip()
if not termo_busca:
logger.info(f”[Scheduler] Usuario {telegram_id} sem cargo/senioridade. Pulando.”)
continue
ingles_fluente = perfil_tem_ingles_fluente(perfil)
try:
vagas = buscar_vagas_jobspy(
cargo=termo_busca, keywords=””, cidade=cidade, quantidade=10,
buscar_remoto=True, ingles_fluente=ingles_fluente,
)
if not vagas:
continue
melhores = selecionar_melhores_vagas(perfil, vagas, senioridade)
novas    = [v for v in melhores if not job_ja_enviado(telegram_id, gerar_hash_vaga(v))]
if not novas:
continue
await context.bot.send_message(
chat_id=telegram_id,
text=f”Bom dia. Suas {len(novas)} sugestao(es) de hoje com curriculo adaptado:”
)
for i, vaga in enumerate(novas, 1):
try:
await processar_e_enviar_vaga(
bot=context.bot, telegram_id=telegram_id, usuario=usuario, perfil=perfil,
titulo=vaga.get(“title”, “”), empresa=vaga.get(“company”, “”),
local=vaga.get(“location”, “”), descricao=vaga.get(“description”, “”),
url=vaga.get(“job_url”, “”), job_hash=gerar_hash_vaga(vaga), indice=i,
)
except Exception as e:
logger.error(f”[Scheduler] Erro vaga {i}: {e}”, exc_info=True)
except Exception as e:
logger.error(f”[Scheduler] Erro user {telegram_id}: {e}”, exc_info=True)

async def cmd_testar_vagas(update: Update, context: ContextTypes.DEFAULT_TYPE):
if update.callback_query:
await update.callback_query.answer()
user_id    = str(update.callback_query.from_user.id)
status_msg = await update.callback_query.edit_message_text(“Iniciando varredura no LinkedIn e Indeed…”)
else:
user_id    = str(update.effective_user.id)
status_msg = await update.message.reply_text(“Iniciando varredura no LinkedIn e Indeed…”)

```
usuario = buscar_usuario(user_id)
perfil  = usuario.get("perfil_estruturado") if usuario else None
if not perfil:
    await status_msg.edit_text("Voce ainda nao tem perfil estruturado. Envie um historico profissional base primeiro.")
    return

cidade      = usuario.get("cidade", "Brazil")
cargo_alvo  = usuario.get("cargo_alvo", "")
senioridade = usuario.get("senioridade", "")
termo_busca = f"{cargo_alvo} {senioridade}".strip()
ingles_fluente = perfil_tem_ingles_fluente(perfil)

vagas = buscar_vagas_jobspy(
    cargo=termo_busca, keywords="", cidade=cidade, quantidade=10,
    buscar_remoto=True, ingles_fluente=ingles_fluente,
)
if not vagas:
    await status_msg.edit_text("Nenhuma vaga encontrada agora. Tente novamente mais tarde.")
    return

await status_msg.edit_text("Vagas encontradas. Avaliando aderencia tecnica com IA...")
melhores = selecionar_melhores_vagas(perfil, vagas, senioridade)
novas    = [v for v in melhores if not job_ja_enviado(user_id, gerar_hash_vaga(v))]
if not novas:
    await status_msg.edit_text("As vagas encontradas possuem baixa aderencia ao seu historico ou ja foram enviadas.")
    return

await status_msg.edit_text(f"Match concluido. Gerando {len(novas)} curriculo(s) adaptado(s) em PDF...")
for i, vaga in enumerate(novas, 1):
    try:
        await processar_e_enviar_vaga(
            bot=context.bot, telegram_id=user_id, usuario=usuario, perfil=perfil,
            titulo=vaga.get("title", ""), empresa=vaga.get("company", ""),
            local=vaga.get("location", ""), descricao=vaga.get("description", ""),
            url=vaga.get("job_url", ""), job_hash=gerar_hash_vaga(vaga), indice=i,
        )
    except Exception as e:
        logger.error(f"[Teste] Erro vaga {i}: {e}", exc_info=True)
        await context.bot.send_message(chat_id=user_id, text=f"Erro ao processar vaga {i}: {e}")

await status_msg.delete()
```

# =========================================================

# BROADCAST - NOTIFICAR PERFIS INCOMPLETOS

# =========================================================

async def cmd_notificar_pendentes(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(“Iniciando varredura de perfis incompletos…”)
usuarios   = buscar_todos_usuarios()
notificados = 0
keyboard = InlineKeyboardMarkup([
[InlineKeyboardButton(“Atualizar Objetivo Agora”, callback_data=“menu_atualizar_objetivo”)]
])
for u in usuarios:
cargo = u.get(“cargo_alvo”)
sen   = u.get(“senioridade”)
if not cargo or not sen:
telegram_id = u.get(“telegram_id”)
if not telegram_id:
continue
try:
await context.bot.send_message(
chat_id=telegram_id,
text=(
“Atencao: Atualizamos nosso motor de inteligencia artificial para “
“garantir vagas muito mais precisas.\n\n”
“Notamos que o seu perfil esta sem o Cargo Alvo e a Senioridade definidos. “
“Sem isso, voce deixara de receber as sugestoes diarias.\n\n”
“Clique no botao abaixo para atualizar:”
),
reply_markup=keyboard
)
notificados += 1
await asyncio.sleep(0.1)
except Exception as e:
logger.error(f”[Broadcast] Erro ao notificar {telegram_id}: {e}”)
await update.message.reply_text(f”Varredura concluida. {notificados} usuarios notificados.”)

# =========================================================

# COMANDOS ADICIONAIS E HANDLER DE ENTRADA

# =========================================================

async def cmd_editar_cv(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id  = str(update.effective_user.id)
cv_atual = context.user_data.get(“ultimo_cv”)
usuario  = context.user_data.get(“ultimo_usuario”) or buscar_usuario(user_id)
if not cv_atual:
await update.message.reply_text(“Nenhum curriculo gerado nesta sessao.”)
return
partes    = update.message.text.split(maxsplit=1)
instrucao = partes[1].strip() if len(partes) > 1 else “”
if not instrucao:
await update.message.reply_text(“Informe o que editar apos o comando. Ex: /editar_cv Mude meu titulo”)
return
status = await update.message.reply_text(“Aplicando edicao no curriculo… Aguarde.”)
try:
cv_novo = editar_cv_json(cv_atual, instrucao)
idioma_edit = (usuario or {}).get(“idioma”, “Portugues”)
pdf_buf = gerar_pdf(cv_novo, idioma_edit)
context.user_data[“ultimo_cv”] = cv_novo
nome_usuario = (usuario or {}).get(“nome_completo”, “Candidato”)
def *slug(s: str) -> str:
return re.sub(r”[^\w-]”, “*”, s.strip())[:40]
nome_arquivo = f”{_slug(nome_usuario)}_editado.pdf”
await update.message.reply_document(document=pdf_buf, filename=nome_arquivo, caption=“Curriculo atualizado.”)
await status.delete()
except Exception as e:
logger.error(f”[EditarCV] {e}”, exc_info=True)
await status.edit_text(f”Erro ao editar curriculo: {e}”)

async def cmd_meu_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
if update.callback_query:
await update.callback_query.answer()
user_id = str(update.callback_query.from_user.id)
else:
user_id = str(update.effective_user.id)
usuario = buscar_usuario(user_id)
if not usuario:
texto = “Voce ainda nao possui perfil cadastrado. Use /start para comecar.”
else:
perfil = usuario.get(“perfil_estruturado”) or {}
texto  = formatar_perfil_texto(usuario, perfil)
if update.callback_query:
await update.callback_query.message.reply_text(texto)
else:
await update.message.reply_text(texto)

async def cmd_deletar(update: Update, context: ContextTypes.DEFAULT_TYPE):
if update.callback_query:
await update.callback_query.answer()
user_id = str(update.callback_query.from_user.id)
else:
user_id = str(update.effective_user.id)
try:
db_client.table(“user_profiles”).delete().eq(“telegram_id”, user_id).execute()
db_client.table(“sent_jobs”).delete().eq(“telegram_id”, user_id).execute()
msg = “Seus dados foram removidos com sucesso. Use /start para comecar do zero.”
except Exception as e:
logger.error(f”[Deletar] {e}”, exc_info=True)
msg = f”Erro ao deletar dados: {e}”
if update.callback_query:
await update.callback_query.message.reply_text(msg)
else:
await update.message.reply_text(msg)

async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = str(update.effective_user.id)
usuario = buscar_usuario(user_id)

```
# --- Arquivo (PDF ou TXT) ---
if update.message.document:
    doc      = update.message.document
    filename = doc.file_name or "arquivo.pdf"
    status   = await update.message.reply_text("Processando seu historico... Aguarde.")
    try:
        file_obj   = await doc.get_file()
        file_bytes = bytearray(await file_obj.download_as_bytearray())
        texto      = extrair_texto_arquivo(file_bytes, filename)
        if not texto.strip():
            await status.edit_text("Nao consegui extrair texto do arquivo. Tente enviar em .txt ou cole o texto diretamente.")
            return
        perfil_atual = (usuario or {}).get("perfil_estruturado") or {}
        novo_perfil  = consolidar_perfil(perfil_atual, texto)
        atualizar_perfil_estruturado(user_id, novo_perfil)
        await status.edit_text(
            "Historico processado e salvo com sucesso!\n\n"
            "Agora envie uma descricao de vaga ou link do LinkedIn para gerar seu curriculo adaptado."
        )
    except Exception as e:
        logger.error(f"[Arquivo] {e}", exc_info=True)
        await status.edit_text(f"Erro ao processar arquivo: {e}")
    return

texto_msg = (update.message.text or "").strip()
if not texto_msg:
    return

intencao = classificar_intencao(texto_msg)

# --- URL LinkedIn ---
if intencao == "URL_LINKEDIN":
    if not usuario or not usuario.get("perfil_estruturado"):
        await update.message.reply_text("Envie seu historico profissional primeiro antes de solicitar uma vaga.")
        return
    status = await update.message.reply_text("Extraindo vaga do LinkedIn... Aguarde.")
    try:
        vaga = extrair_vaga_linkedin(texto_msg)
        if not vaga:
            await status.edit_text("Nao consegui extrair a vaga. Verifique o link ou cole a descricao manualmente.")
            return
        await status.delete()
        await _perguntar_tipo_cv(update, context, vaga)
    except Exception as e:
        logger.error(f"[LinkedIn] {e}", exc_info=True)
        await status.edit_text(f"Erro ao extrair vaga: {e}")
    return

# --- Descricao de vaga em texto ---
if intencao == "VAGA":
    if not usuario or not usuario.get("perfil_estruturado"):
        await update.message.reply_text("Envie seu historico profissional primeiro antes de solicitar uma vaga.")
        return
    vaga_dados = {"titulo": "Vaga", "empresa": "", "localizacao": "", "descricao": texto_msg}
    await _perguntar_tipo_cv(update, context, vaga_dados)
    return

# --- Historico profissional em texto ---
if intencao == "HISTORICO":
    status = await update.message.reply_text("Processando seu historico... Aguarde.")
    try:
        perfil_atual = (usuario or {}).get("perfil_estruturado") or {}
        novo_perfil  = consolidar_perfil(perfil_atual, texto_msg)
        atualizar_perfil_estruturado(user_id, novo_perfil)
        await status.edit_text(
            "Historico atualizado com sucesso!\n\n"
            "Agora envie uma descricao de vaga ou link do LinkedIn para gerar seu curriculo adaptado."
        )
    except Exception as e:
        logger.error(f"[Historico] {e}", exc_info=True)
        await status.edit_text(f"Erro ao processar historico: {e}")
    return

# --- Edicao de perfil ---
if intencao == "EDICAO":
    if not usuario or not usuario.get("perfil_estruturado"):
        await update.message.reply_text("Voce ainda nao tem perfil salvo para editar.")
        return
    status = await update.message.reply_text("Aplicando atualizacao no seu perfil... Aguarde.")
    try:
        perfil_atual = usuario.get("perfil_estruturado") or {}
        novo_perfil  = editar_perfil_llm(perfil_atual, texto_msg)
        atualizar_perfil_estruturado(user_id, novo_perfil)
        await status.edit_text("Perfil atualizado com sucesso!")
    except Exception as e:
        logger.error(f"[Edicao] {e}", exc_info=True)
        await status.edit_text(f"Erro ao atualizar perfil: {e}")
    return

# --- Fallback ---
await update.message.reply_text(
    "Nao entendi sua mensagem. Voce pode:\n"
    "- Enviar a descricao de uma vaga\n"
    "- Enviar o link de uma vaga do LinkedIn\n"
    "- Enviar seu historico profissional em PDF ou texto\n"
    "- Usar /start para acessar o menu principal"
)
```

# =========================================================

# MAIN

# =========================================================

def main():
threading.Thread(target=start_health_server, daemon=True).start()

```
request = HTTPXRequest(connection_pool_size=8)
app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .request(request)
    .build()
)

onboarding = ConversationHandler(
    entry_points=[CommandHandler("start", cmd_start)],
    states={
        ASK_NOME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_nome)],
        ASK_EMAIL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
        ASK_PHONE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
        ASK_LINKEDIN:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_linkedin)],
        ASK_CITY:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_city)],
        ASK_LANGUAGE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_language)],
        ASK_TARGET_ROLE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_target_role),
            CallbackQueryHandler(callback_seniority, pattern="^sen_"),
        ],
        ASK_SENIORITY:   [CallbackQueryHandler(callback_seniority, pattern="^sen_")],
    },
    fallbacks=[CommandHandler("start", cmd_start)],
    allow_reentry=True,
)

atualizacao_objetivo = ConversationHandler(
    entry_points=[CallbackQueryHandler(cmd_atualizar_objetivo, pattern="^menu_atualizar_objetivo$")],
    states={
        ASK_TARGET_ROLE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_target_role),
            CallbackQueryHandler(callback_seniority, pattern="^sen_"),
        ],
        ASK_SENIORITY: [CallbackQueryHandler(callback_seniority, pattern="^sen_")],
    },
    fallbacks=[CommandHandler("start", cmd_start)],
    allow_reentry=True,
)

app.add_handler(onboarding)
app.add_handler(atualizacao_objetivo)
app.add_handler(CommandHandler("editar_cv",          cmd_editar_cv))
app.add_handler(CommandHandler("meu_perfil",         cmd_meu_perfil))
app.add_handler(CommandHandler("deletar",            cmd_deletar))
app.add_handler(CommandHandler("buscar_vagas",       cmd_testar_vagas))
app.add_handler(CommandHandler("notificar_pendentes", cmd_notificar_pendentes))
app.add_handler(CallbackQueryHandler(callback_menu,    pattern="^menu_"))
app.add_handler(CallbackQueryHandler(callback_tipo_cv, pattern="^cv_"))
app.add_handler(MessageHandler(filters.TEXT | filters.Document.ALL, handle_incoming_message))

brt = ZoneInfo("America/Sao_Paulo")
app.job_queue.run_daily(enviar_sugestoes_diarias, time=dtime(12, 0, tzinfo=brt))

logger.info("ATS Resume Bot iniciado. Aguardando mensagens...")
app.run_polling(allowed_updates=Update.ALL_TYPES)
```

if **name** == “**main**”:
main()