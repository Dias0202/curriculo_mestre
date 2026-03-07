"""
scraper.py — Extrator de vagas do LinkedIn
  - extrair_vaga_linkedin: Guest API publica (para URLs coladas pelo usuario)
  - buscar_vagas_jobspy:   JobSpy scraper (para busca ativa diaria)
"""

import re
import random
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Cache em memoria como fallback (limpo a cada restart)
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
    """Tenta o cache no Supabase primeiro, depois em memoria."""
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
    """Salva no Supabase e na memoria."""
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
    Retorna {"sucesso": bool, "dados": dict, "erro": str, "origem": str}
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


def buscar_vagas_jobspy(cargo: str, keywords: str, cidade: str, quantidade: int = 10) -> list:
    """
    Busca vagas no LinkedIn via JobSpy (scraper gratuito, sem autenticacao).
    Retorna lista de dicts: title, company, location, description, job_url
    """
    try:
        from jobspy import scrape_jobs

        search_term = f"{cargo} {keywords}".strip()
        location    = cidade if cidade else "Brazil"
        logger.info(f"[JobSpy] Buscando '{search_term}' em '{location}'")

        df = scrape_jobs(
            site_name=["linkedin"],
            search_term=search_term,
            location=location,
            results_wanted=quantidade,
            hours_old=72,
            country_indeed="Brazil",
        )

        if df is None or df.empty:
            logger.warning("[JobSpy] Nenhuma vaga encontrada.")
            return []

        vagas = []
        for _, row in df.iterrows():
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
        logger.error("[JobSpy] python-jobspy nao instalado.")
        return []
    except Exception as e:
        logger.error(f"[JobSpy] Erro: {e}", exc_info=True)
        return []
