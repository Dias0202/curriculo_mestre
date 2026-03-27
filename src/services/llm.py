"""Servico de LLM via Groq — orquestracao de prompts e parsing de JSON."""

import re
import json
import logging
from typing import Any

from groq import AsyncGroq

from src.core.config import settings

logger = logging.getLogger(__name__)

_groq_client: AsyncGroq | None = None


def get_llm_client() -> AsyncGroq:
    """Retorna singleton do cliente Groq."""
    global _groq_client
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=settings.groq_api_key)
    return _groq_client


async def _chat(
    system: str,
    prompt: str,
    json_mode: bool = False,
    temperature: float = 0.1,
) -> str:
    """Chamada generica ao LLM com suporte a JSON mode."""
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": 8000,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = await get_llm_client().chat.completions.create(**kwargs)
    return resp.choices[0].message.content.strip()


def _parse_json(raw: str) -> dict[str, Any]:
    """Remove blocos markdown e faz parse seguro do JSON do LLM."""
    cleaned = re.sub(r"```json|```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("Falha ao parsear JSON do LLM: %s\nRaw: %s", e, cleaned[:500])
        raise


def _parse_score(score_raw: Any) -> int:
    """Converte score do LLM para inteiro 0-100."""
    try:
        match = re.search(r"([0-9]+[.,]?[0-9]*)", str(score_raw))
        if match:
            val = float(match.group(1).replace(",", "."))
            if 0 < val <= 1.0:
                return int(val * 100)
            return min(int(val), 100)
        return 0
    except Exception:
        return 0


# --- System Prompts ---

_SYSTEM_CONSOLIDAR = """INSTRUCAO: Voce atua como um Engenheiro de Dados especialista em parsing de perfis e curriculos.

Sua funcao e analisar o PERFIL ATUAL do candidato armazenado no banco de dados e a NOVA ENTRADA de dados.
Seu objetivo e retornar um JSON consolidado, normalizado e atualizado.

REGRAS VITAIS:
1. DADOS PESSOAIS: Se o usuario informar seu nome, telefone, cidade ou linkedin, atualize o bloco "dados_pessoais". NUNCA apague contatos previamente existentes a menos que explicitamente solicitado.
2. PREENCHIMENTO DE GAPS (CRITICO): Se a instrucao do usuario for sobre adicionar ou editar alguma ferramenta/experiencia, adicione IMEDIATAMENTE as habilidades no bloco "skills" ou "experiences". Seja proativo na equivalencia.
3. DADOS NAO-TRADICIONAIS: Mapeie freelances para "experiences", projetos para "projects".
4. FORMATO STRICT: Retorne EXCLUSIVAMENTE um objeto JSON valido.

SCHEMA EXIGIDO:
{
  "dados_pessoais": {
    "nome": "", "email": "", "telefone": "", "linkedin": "", "cidade": ""
  },
  "experiences": [
    {"cargo":"","empresa":"","localizacao":"","data_inicio":"","data_fim":"","descricao_empresa":"","responsabilidades":[],"conquistas":[]}
  ],
  "education": [
    {"grau":"","curso":"","instituicao":"","ano_inicio":"","ano_fim":""}
  ],
  "skills": [
    {"nome":"","categoria":"","nivel":""}
  ],
  "certifications": [
    {"nome":"","emissor":"","ano":""}
  ],
  "projects": [
    {"nome":"","descricao":""}
  ],
  "languages": [
    {"idioma":"","nivel":""}
  ]
}"""

_SYSTEM_CV = """INSTRUCAO SUPREMA: Voce atua como um Recrutador Tecnico Senior e Especialista em Sistemas ATS.

Sua missao e cruzar o HISTORICO do candidato com os dados da VAGA ALVO e criar um curriculo otimizado em JSON.

REGRAS VITAIS E ALGORITMICAS:
1. EQUIVALENCIA TECNOLOGICA (CRITICO): Se a vaga exige uma ferramenta (ex: Tableau, AWS) e o candidato possui dominio em concorrente (ex: Power BI, GCP), isto e MATCH ABSOLUTO. Escreva "Power BI (Equivalente a Tableau)" e NUNCA liste a ferramenta original como gap.
2. PREVENCAO DE ALUCINACAO: NUNCA invente experiencias. Se o candidato nao possui o requisito (nem equivalente), liste em "analise_gaps".
3. METODO STAR: Reescreva os bullet points de experiencias focando em impacto quantificavel.
4. KEYWORDS OCULTAS: Liste os gaps exigidos pela vaga que o candidato NAO possui, para o stealth ATS.
5. FORMATO DAS LISTAS (CRITICO): Para "idiomas" e "certificacoes", retorne um ARRAY CONTENDO APENAS STRINGS (Ex: ["Ingles - Avancado"]). E ESTRITAMENTE PROIBIDO INSERIR OBJETOS JSON, DICIONARIOS OU CHAVES NESTES ARRAYS.

SCHEMA OBRIGATORIO:
{
  "identificacao": {
    "titulo": "Titulo curto do cargo (Maximo 6 palavras)"
  },
  "resumo": "Paragrafo resumo direcionado a vaga",
  "competencias": ["Competencia 1", "Competencia 2"],
  "experiencias": [
    {
      "cargo": "Nome do Cargo", "empresa": "Nome da Empresa", "localizacao": "", "data_inicio": "", "data_fim": "", "descricao_empresa": "",
      "responsabilidades": ["Tarefa executada"], "conquistas": ["Resultado atingido"]
    }
  ],
  "educacao": [
    {"grau": "", "curso": "", "instituicao": "", "ano_inicio": "", "ano_fim": ""}
  ],
  "certificacoes": ["Nome da Certificacao - Emissor"],
  "projetos": [{"nome": "", "descricao": ""}],
  "idiomas": ["Idioma - Nivel"],
  "keywords_ocultas": ["tecnologia ausente no curriculo"],
  "relatorio_analitico": {
    "match_score": "Numero inteiro de 0 a 100",
    "analise_gaps": ["Requisitos criticos que o candidato NAO possui (nem equivalentes)"],
    "dica_entrevista": "Dica comportamental"
  }
}"""


# --- Funcoes de Negocio ---

async def classificar_intencao(texto: str) -> str:
    """Classifica a intencao da mensagem do usuario."""
    if re.search(r"linkedin.com/jobs", texto, re.IGNORECASE):
        return "URL_LINKEDIN"
    raw = await _chat(
        system="Classifique mensagens enviadas a um bot de carreira. Responda APENAS: VAGA, HISTORICO, EDICAO ou OUTRO.",
        prompt=(
            "VAGA = descricao de cargo/emprego\n"
            "HISTORICO = curriculo ou experiencias do usuario\n"
            "EDICAO = instrucao de atualizacao de dados (Ex: 'meu nome e X', 'sei power bi', 'remova a empresa X')\n"
            "OUTRO = perguntas gerais, conversas\n\n"
            f"Mensagem:\n{texto[:1500]}"
        ),
        temperature=0.0,
    )
    for cat in ("VAGA", "HISTORICO", "EDICAO"):
        if cat in raw.upper():
            return cat
    return "OUTRO"


async def consolidar_perfil(perfil_atual: dict, nova_entrada: str) -> dict:
    """Consolida perfil existente com nova entrada via LLM."""
    raw = await _chat(
        system=_SYSTEM_CONSOLIDAR,
        prompt=(
            f"PERFIL ATUAL:\n{json.dumps(perfil_atual, ensure_ascii=False)}\n\n"
            f"NOVA ENTRADA:\n{nova_entrada}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    return _parse_json(raw)


async def editar_perfil(perfil_atual: dict, instrucao: str) -> dict:
    """Aplica edicao pontual no perfil via LLM."""
    raw = await _chat(
        system=(
            _SYSTEM_CONSOLIDAR
            + "\n\nMODO EDICAO PONTUAL. Aplique as mudancas solicitadas. "
            "Atualize 'dados_pessoais' caso o usuario mencione seu nome/contato."
        ),
        prompt=(
            f"PERFIL ATUAL:\n{json.dumps(perfil_atual, ensure_ascii=False)}\n\n"
            f"INSTRUCAO:\n{instrucao}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    return _parse_json(raw)


async def gerar_cv_json(
    perfil: dict,
    usuario: dict,
    titulo_vaga: str,
    empresa_vaga: str,
    local_vaga: str,
    descricao_vaga: str,
    com_resumo: bool = True,
) -> dict:
    """Gera curriculo otimizado em JSON a partir do perfil vs vaga."""
    idioma = usuario.get("idioma", "Portugues")
    instrucao_resumo = "" if com_resumo else "\nIMPORTANTE: Deixe o campo 'resumo' vazio."
    raw = await _chat(
        system=_SYSTEM_CV,
        prompt=(
            f"HISTORICO DO CANDIDATO:\n{json.dumps(perfil, ensure_ascii=False)}\n\n"
            f"DADOS DO CANDIDATO:\nNome: {usuario.get('nome_completo', '')}\n"
            f"VAGA ALVO:\nTitulo: {titulo_vaga}\nEmpresa: {empresa_vaga}\nDescricao:\n{descricao_vaga}\n\n"
            f"IDIOMA: {idioma}{instrucao_resumo}"
        ),
        json_mode=True,
        temperature=0.15,
    )
    cv_bruto = _parse_json(raw)
    return _sanitizar_cv(cv_bruto, usuario, idioma)


async def editar_cv_json(cv_atual: dict, instrucao: str) -> dict:
    """Aplica edicao pontual no curriculo gerado."""
    raw = await _chat(
        system="Aplique APENAS a alteracao solicitada no JSON do curriculo. Mantenha os outros campos.",
        prompt=(
            f"CURRICULO ATUAL:\n{json.dumps(cv_atual, ensure_ascii=False)}\n\n"
            f"INSTRUCAO:\n{instrucao}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    return _parse_json(raw)


async def selecionar_melhores_vagas(
    perfil: dict, vagas: list[dict], senioridade_alvo: str
) -> list[dict]:
    """Usa LLM para pontuar e selecionar as melhores vagas para o candidato."""
    lista = "\n".join([
        f"{i}. {v.get('title', '')}\n"
        f"   Empresa: {v.get('company', '')}\n"
        f"   Descricao: {str(v.get('description', ''))[:400]}"
        for i, v in enumerate(vagas)
    ])
    regra_eliminacao = (
        "- REGRA DE ELIMINACAO (Score 0): Se a vaga exige nivel Senior/Pleno e o candidato e Junior/Estagio (ou vice-versa), o score DEVE ser 0.\n"
        if senioridade_alvo else
        "- Avalie a vaga puramente pelas habilidades tecnicas.\n"
    )
    raw = await _chat(
        system="Voce e um recrutador tecnico senior. Retorne SOMENTE JSON valido.",
        prompt=(
            f"Avalie a aderencia de cada vaga ao perfil do candidato. Senioridade alvo: {senioridade_alvo or 'Nao definida'}.\n\n"
            "REGRAS DE PONTUACAO (0 a 100):\n"
            f"{regra_eliminacao}"
            "- EQUIVALENCIA TECNOLOGICA: Ferramentas concorrentes sao MATCH integral.\n"
            "- 80-100: Cargo exato, dominio de tecnologias ou equivalentes.\n"
            "- 60-79: Cargo relacionado, dominio de tecnologias core.\n"
            "- 0-59: Faltam requisitos fundamentais sem compensacao.\n\n"
            "IMPORTANTE: use numeros INTEIROS de 0 a 100. NUNCA DEVOLVA DECIMAIS.\n"
            '{"scores": [{"indice": 0, "score": 75, "motivo": "justificativa"}, ...]}\n\n'
            f"PERFIL DO CANDIDATO:\n{json.dumps(perfil, ensure_ascii=False)[:2500]}\n\n"
            f"VAGAS PARA AVALIAR:\n{lista}"
        ),
        json_mode=True,
        temperature=0.0,
    )
    try:
        scores = _parse_json(raw).get("scores", [])
        if not scores:
            return []

        for s in scores:
            if isinstance(s, dict):
                s["score"] = _parse_score(s.get("score", 0))

        aprovadas = sorted(
            [s for s in scores if isinstance(s, dict) and s.get("score", 0) >= 60],
            key=lambda s: s.get("score", 0),
            reverse=True,
        )

        resultado: list[dict] = []
        for s in aprovadas[:2]:
            idx = s.get("indice")
            if isinstance(idx, int) and idx < len(vagas):
                vagas[idx]["_match_score"] = s.get("score", 0)
                resultado.append(vagas[idx])
        return resultado
    except Exception as e:
        logger.error("Erro na pontuacao de vagas: %s", e, exc_info=True)
        return []


# --- Cabecalhos e Sanitizacao de CV ---

_CABECALHOS = {
    "pt": {
        "resumo": "Resumo Profissional",
        "competencias": "Competencias",
        "experiencias": "Experiencia Profissional",
        "educacao": "Formacao Academica",
        "certificacoes": "Certificacoes",
        "projetos": "Projetos",
        "idiomas": "Idiomas",
    },
    "en": {
        "resumo": "Professional Summary",
        "competencias": "Skills",
        "experiencias": "Professional Experience",
        "educacao": "Education",
        "certificacoes": "Certifications",
        "projetos": "Projects",
        "idiomas": "Languages",
    },
    "es": {
        "resumo": "Resumen Profesional",
        "competencias": "Competencias",
        "experiencias": "Experiencia Profesional",
        "educacao": "Formacion Academica",
        "certificacoes": "Certificaciones",
        "projetos": "Proyectos",
        "idiomas": "Idiomas",
    },
}


def get_cabecalhos(idioma: str) -> dict[str, str]:
    """Retorna cabecalhos traduzidos baseado no idioma."""
    idioma_lower = idioma.lower()
    if any(k in idioma_lower for k in ("ingl", "engl")):
        return _CABECALHOS["en"]
    if any(k in idioma_lower for k in ("espan", "espa", "spain", "spani")):
        return _CABECALHOS["es"]
    return _CABECALHOS["pt"]


def _sanitizar_cv(cv: dict, usuario: dict, idioma: str = "Portugues") -> dict:
    """Preenche cabecalhos e dados pessoais a partir do registro do usuario."""
    if not isinstance(cv, dict):
        cv = {}

    cv["cabecalhos"] = get_cabecalhos(idioma)

    if "identificacao" not in cv or not isinstance(cv["identificacao"], dict):
        cv["identificacao"] = {}

    ident = cv["identificacao"]
    ident["nome"] = usuario.get("nome_completo") or "Candidato"
    ident["email"] = usuario.get("email") or ""
    ident["telefone"] = usuario.get("telefone") or ""
    ident["linkedin"] = usuario.get("linkedin") or ""
    ident["localizacao"] = usuario.get("cidade") or ""

    titulo = str(ident.get("titulo", "")).strip()
    palavras = titulo.split()
    if len(palavras) > 6:
        ident["titulo"] = " ".join(palavras[:6])

    return cv
