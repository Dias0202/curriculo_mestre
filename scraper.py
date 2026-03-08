"""
scraper.py — Extrator de vagas do LinkedIn
  - extrair_vaga_linkedin: Guest API publica (para URLs coladas pelo usuario)
  - buscar_vagas_jobspy:   JobSpy scraper com fallback multi-site e retry
"""

import re
import random
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_MEM_CACHE: dict = {}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
]


def _headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
    }


def _cache_get(job_id: str, db_client) -> dict | None:
    if db_client:
        try:
            r = db_client.table("scraped_jobs").select("dados").eq("job_id", job_id).execute()
            if r.data:
                logger.info(f"[Cache] HIT Supabase job_id={job_id}")
                return r.data[0]["dados"]
        except Exception as e:
            logger.warning(f"[Cache] Falha Supabase: {e}")
    if job_id in _MEM_CACHE:
        logger.info(f"[Cache] HIT memoria job_id={job_id}")
        return _MEM_CACHE[job_id]
    return None


def _cache_set(job_id: str, dados: dict, db_client) -> None:
    _MEM_CACHE[job_id] = dados
    if db_client:
        try:
            db_client.table("scraped_jobs").upsert(
                {"job_id": job_id, "dados": dados}
            ).execute()
        except Exception as e:
            logger.warning(f"[Cache] Falha salvar: {e}")


def extrair_vaga_linkedin(url: str, db_client=None) -> dict:
    """
    Extrai dados de vaga do LinkedIn via Guest API publica.
    """
    match_id = re.search(r"([0-9]{9,10})", url)
    if not match_id:
        return {"sucesso": False, "erro": "ID numerico da vaga nao encontrado na URL."}

    job_id = match_id.group(1)
    cached = _cache_get(job_id, db_client)
    if cached:
        return {"sucesso": True, "dados": cached, "origem": "cache"}

    url_guest = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    resposta  = None

    for tentativa in range(3):
        time.sleep(random.uniform(1.0, 3.0))
        try:
            resposta = requests.get(url_guest, headers=_headers(), timeout=15)
            if resposta.status_code == 200:
                break
            if tentativa < 2:
                time.sleep(random.uniform(3.0, 6.0))
            else:
                return {"sucesso": False, "erro": f"LinkedIn retornou HTTP {resposta.status_code}"}
        except requests.RequestException as e:
            if tentativa == 2:
                return {"sucesso": False, "erro": f"Erro de conexao: {e}"}
            time.sleep(2)

    if not resposta or resposta.status_code != 200:
        return {"sucesso": False, "erro": "Resposta invalida do LinkedIn."}

    soup = BeautifulSoup(resposta.text, "html.parser")

    def _get(tag, cls):
        el = soup.find(tag, class_=cls)
        return el.text.strip() if el else ""

    titulo      = _get("h2", "top-card-layout__title")
    empresa     = _get("a",  "topcard__org-name-link")
    localizacao = _get("span", "topcard__flavor topcard__flavor--bullet")
    desc_el     = soup.find("div", class_="show-more-less-html__markup")
    descricao   = desc_el.get_text(separator="\n", strip=True) if desc_el else ""

    dados = {
        "id": job_id, "titulo": titulo, "empresa": empresa,
        "localizacao": localizacao, "descricao": descricao, "url": url,
    }
    _cache_set(job_id, dados, db_client)
    return {"sucesso": True, "dados": dados, "origem": "api_linkedin"}


def _normalizar_location(cidade: str) -> str:
    """
    Normaliza a cidade para o formato que o JobSpy aceita melhor.
    Remove estado (ex: 'Belo Horizonte, MG' -> 'Belo Horizonte')
    e trata casos vazios.
    """
    if not cidade or cidade.strip().lower() in ("brazil", "brasil", ""):
        return "Brazil"
    # Remove sufixo ', UF' se presente (ex: 'Sao Paulo, SP' -> 'Sao Paulo')
    partes = cidade.split(",")
    return partes[0].strip()


def _scrape_jobspy(site_name: list, search_term: str, location: str,
                   results_wanted: int, hours_old: int) -> list:
    """Helper interno que chama o JobSpy e retorna lista normalizada."""
    from jobspy import scrape_jobs

    logger.info(
        f"[JobSpy] sites={site_name} | termo='{search_term}' "
        f"| local='{location}' | hours_old={hours_old}"
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

    vagas = []
    for _, row in df.iterrows():
        title = str(row.get("title", "")).strip()
        company = str(row.get("company", "")).strip()
        if not title:
            continue
        vagas.append({
            "title":       title,
            "company":     company,
            "location":    str(row.get("location", "")).strip(),
            "description": str(row.get("description", ""))[:3000],
            "job_url":     str(row.get("job_url", "")).strip(),
        })

    logger.info(f"[JobSpy] {len(vagas)} vagas encontradas com termo='{search_term}'")
    return vagas


def buscar_vagas_jobspy(cargo: str, keywords: str, cidade: str, quantidade: int = 10) -> list:
    """
    Busca vagas com estrategia de fallback em 4 niveis:

    Nivel 1 — LinkedIn, cargo (ate 3 palavras) + 1a keyword, cidade, 7 dias
    Nivel 2 — LinkedIn + Indeed, apenas cargo curto, cidade, 30 dias
    Nivel 3 — LinkedIn + Indeed, apenas cargo curto, Brasil, 30 dias
    Nivel 4 — LinkedIn + Indeed + Glassdoor, 1a keyword, Brasil, 60 dias

    Retorna a primeira lista nao-vazia ou [] se todos os niveis falharem.
    """
    try:
        from jobspy import scrape_jobs  # noqa: F401 — valida instalacao
    except ImportError:
        logger.error("[JobSpy] python-jobspy nao instalado. Execute: pip install python-jobspy")
        return []

    location = _normalizar_location(cidade)

    # Cargo limitado a 3 palavras — termos longos retornam zero resultados
    cargo_curto = " ".join(cargo.split()[:3]) if cargo.strip() else ""

    # Primeira keyword mais relevante — evita queries com 5+ termos
    kw_principal = keywords.split()[0] if keywords.strip() else ""

    termo_completo = f"{cargo_curto} {kw_principal}".strip()

    estrategias = [
        # (sites,                              termo,          local,    hours_old)
        (["linkedin"],                          termo_completo, location,  168),   # Nivel 1: 7 dias
        (["linkedin", "indeed"],                cargo_curto,    location,  720),   # Nivel 2: 30 dias cidade
        (["linkedin", "indeed"],                cargo_curto,    "Brazil",  720),   # Nivel 3: 30 dias Brasil
        (["linkedin", "indeed", "glassdoor"],   kw_principal or cargo_curto, "Brazil", 1440),  # Nivel 4: 60 dias
    ]

    for sites, termo, loc, hours in estrategias:
        if not termo.strip():
            continue
        try:
            vagas = _scrape_jobspy(sites, termo, loc, quantidade, hours)
            if vagas:
                logger.info(
                    f"[JobSpy] Sucesso | sites={sites} | termo='{termo}' | loc='{loc}'"
                )
                return vagas
            time.sleep(2)  # pausa entre tentativas para evitar rate-limit
        except Exception as e:
            logger.warning(
                f"[JobSpy] Estrategia falhou (sites={sites}, termo='{termo}'): {e}"
            )
            time.sleep(3)

    logger.warning("[JobSpy] Todas as estrategias falharam. Nenhuma vaga retornada.")
    return []
