"""Scraper async de vagas — LinkedIn Guest API + JobSpy com fallback estrategico."""

import re
import random
import asyncio
import logging

import httpx
from bs4 import BeautifulSoup

from src.db import supabase_client as db

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
]


def _headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
    }


async def extrair_vaga_linkedin(url: str) -> dict:
    """Extrai dados de uma vaga do LinkedIn via Guest API (async, stateless)."""
    match_id = re.search(r"([0-9]{9,10})", url)
    if not match_id:
        return {"sucesso": False, "erro": "ID numerico da vaga nao encontrado na URL."}

    job_id = match_id.group(1)

    cached = await db.cache_get_job(job_id)
    if cached:
        return {"sucesso": True, "dados": cached, "origem": "cache"}

    url_guest = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for tentativa in range(3):
            await asyncio.sleep(random.uniform(1.0, 3.0))
            try:
                resposta = await client.get(url_guest, headers=_headers())
                if resposta.status_code == 200:
                    break
                if tentativa < 2:
                    await asyncio.sleep(random.uniform(3.0, 6.0))
                else:
                    return {"sucesso": False, "erro": f"LinkedIn retornou HTTP {resposta.status_code}"}
            except httpx.HTTPError as e:
                if tentativa == 2:
                    return {"sucesso": False, "erro": f"Erro de conexao: {e}"}
                await asyncio.sleep(2)
        else:
            return {"sucesso": False, "erro": "Resposta invalida do LinkedIn."}

    soup = BeautifulSoup(resposta.text, "html.parser")

    titulo_el = (
        soup.find("h2", class_=lambda x: x and "title" in x.lower())
        or soup.find("h1")
    )
    titulo = titulo_el.text.strip() if titulo_el else ""

    if not titulo:
        return {"sucesso": False, "erro": "Bloqueio por Captcha ativado."}

    empresa_el = (
        soup.find("a", class_=lambda x: x and "org-name" in x.lower())
        or soup.find("a", class_="topcard__flavor--black-link")
    )
    empresa = empresa_el.text.strip() if empresa_el else "Empresa Confidencial"

    localizacao_el = (
        soup.find("span", class_="topcard__flavor--bullet")
        or soup.find("span", class_="topcard__flavor")
    )
    localizacao = localizacao_el.text.strip() if localizacao_el else ""

    desc_el = soup.find("div", class_="show-more-less-html__markup")
    descricao = desc_el.get_text(separator="\n", strip=True) if desc_el else ""

    dados = {
        "id": job_id,
        "titulo": titulo,
        "empresa": empresa,
        "localizacao": localizacao,
        "descricao": descricao,
        "url": url,
    }

    await db.cache_set_job(job_id, dados)
    return {"sucesso": True, "dados": dados, "origem": "api_linkedin"}


# --- JobSpy (scraping agregado) ---

def _normalizar_location(cidade: str) -> str:
    if not cidade or cidade.strip().lower() in ("brazil", "brasil", ""):
        return "Brazil"
    return cidade.split(",")[0].strip()


_CARGO_MAP: dict[str, str] = {
    "desenvolvedor": "developer", "engenheiro": "engineer", "analista": "analyst",
    "cientista": "scientist", "arquiteto": "architect", "gerente": "manager",
    "coordenador": "coordinator", "especialista": "specialist", "consultor": "consultant",
    "dados": "data", "software": "software", "sistemas": "systems",
    "infraestrutura": "infrastructure", "seguranca": "security", "qualidade": "quality",
    "produto": "product", "ml": "machine learning", "ia": "ai",
    "backend": "backend", "frontend": "frontend", "fullstack": "full stack",
    "devops": "devops", "cloud": "cloud", "mobile": "mobile",
    "estagio": "intern", "junior": "junior", "pleno": "mid-level", "senior": "senior",
}


def _traduzir_cargo_en(cargo: str) -> str:
    resultado = cargo.lower()
    for pt, en in _CARGO_MAP.items():
        resultado = resultado.replace(pt, en)
    return resultado.strip().title()


def _scrape_jobspy(
    site_name: list[str],
    search_term: str,
    location: str,
    results_wanted: int,
    hours_old: int,
) -> list[dict]:
    """Executa scraping via JobSpy (sincrono — sera chamado via to_thread)."""
    from jobspy import scrape_jobs

    logger.info(
        "[JobSpy] sites=%s termo='%s' loc='%s' hours=%d",
        site_name, search_term, location, hours_old,
    )

    df = scrape_jobs(
        site_name=site_name,
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        country_indeed="Brazil",
        linkedin_fetch_description=True,
        verbose=0,
    )

    if df is None or df.empty:
        return []

    vagas: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get("title", "")).strip()
        if not title:
            continue
        vagas.append({
            "title": title,
            "company": str(row.get("company", "")).strip(),
            "location": str(row.get("location", "")).strip(),
            "description": str(row.get("description", ""))[:3000],
            "job_url": str(row.get("job_url", "")).strip(),
        })
    return vagas


async def buscar_vagas_jobspy(
    cargo: str,
    keywords: str,
    cidade: str,
    quantidade: int = 10,
    buscar_remoto: bool = False,
    ingles_fluente: bool = False,
) -> list[dict]:
    """Busca vagas via JobSpy com estrategia de fallback progressivo (async wrapper)."""
    try:
        from jobspy import scrape_jobs  # noqa: F401 — verificacao de instalacao
    except ImportError:
        logger.error("[JobSpy] python-jobspy nao instalado.")
        return []

    location = _normalizar_location(cidade)
    termo_completo = f"{cargo} {keywords}".strip()

    estrategias_locais = [
        (["linkedin"], termo_completo, location, 168),
        (["linkedin", "indeed"], cargo, location, 720),
        (["linkedin", "indeed"], cargo, "Brazil", 720),
    ]

    vagas_locais: list[dict] = []
    for sites, termo, loc, hours in estrategias_locais:
        if not termo.strip():
            continue
        try:
            resultado = await asyncio.to_thread(
                _scrape_jobspy, sites, termo, loc, quantidade, hours
            )
            if resultado:
                vagas_locais = resultado
                break
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("[JobSpy] Estrategia local falhou: %s", e)
            await asyncio.sleep(3)

    vagas_remotas: list[dict] = []
    if buscar_remoto:
        cargo_en = _traduzir_cargo_en(cargo)
        estrategias_remotas = [
            (["linkedin", "indeed"], f"{cargo} remoto", "Brazil", 720),
            (["linkedin"], f"{cargo} home office", "Brazil", 720),
        ]
        if ingles_fluente:
            estrategias_remotas += [
                (["linkedin"], f"{cargo_en} remote", "Worldwide", 720),
                (["linkedin", "indeed"], f"{cargo_en} remote", "Worldwide", 1440),
            ]

        for sites, termo, loc, hours in estrategias_remotas:
            if not termo.strip():
                continue
            try:
                resultado = await asyncio.to_thread(
                    _scrape_jobspy, sites, termo, loc, max(quantidade // 2, 5), hours
                )
                if resultado:
                    vagas_remotas = resultado
                    break
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning("[JobSpy] Estrategia remota falhou: %s", e)
                await asyncio.sleep(3)

    # Deduplicacao
    todas = vagas_locais + vagas_remotas
    vistas: set[str] = set()
    unicas: list[dict] = []
    for v in todas:
        chave = f"{v.get('title', '').lower().strip()}|{v.get('company', '').lower().strip()}"
        if chave not in vistas:
            vistas.add(chave)
            unicas.append(v)

    return unicas
