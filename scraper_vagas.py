import re
import random
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Cache em memória como fallback (limpo a cada restart)
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
    """Tenta o cache em DB primeiro, depois em memória."""
    if db_client:
        try:
            r = db_client.table("scraped_jobs").select("dados").eq("job_id", job_id).execute()
            if r.data:
                logger.info(f"Cache HIT (Supabase) para job_id={job_id}")
                return r.data[0]["dados"]
        except Exception as e:
            logger.warning(f"Falha ao ler cache Supabase: {e}")
    if job_id in _MEM_CACHE:
        logger.info(f"Cache HIT (memória) para job_id={job_id}")
        return _MEM_CACHE[job_id]
    return None


def _cache_set(job_id: str, dados: dict, db_client) -> None:
    """Salva no Supabase e na memória."""
    _MEM_CACHE[job_id] = dados
    if db_client:
        try:
            db_client.table("scraped_jobs").upsert(
                {"job_id": job_id, "dados": dados}
            ).execute()
        except Exception as e:
            logger.warning(f"Falha ao salvar cache Supabase: {e}")


def extrair_vaga_linkedin_prod(url_fornecida: str, db_client=None, max_tentativas: int = 2) -> dict:
    """
    Extrai dados de vaga do LinkedIn via Guest API.
    - db_client (opcional): instância Supabase para cache persistente.
    - Fallback: cache em memória RAM.
    """
    match_id = re.search(r"([0-9]{9,10})", url_fornecida)
    if not match_id:
        return {"sucesso": False, "erro": "ID numérico da vaga não encontrado na URL."}

    job_id = match_id.group(1)

    cached = _cache_get(job_id, db_client)
    if cached:
        return {"sucesso": True, "dados": cached, "origem": "cache"}

    url_guest = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    resposta = None

    for tentativa in range(max_tentativas):
        time.sleep(random.uniform(1.0, 3.0))
        try:
            resposta = requests.get(url_guest, headers=_headers(), timeout=10)
            if resposta.status_code == 200:
                break
            if tentativa < max_tentativas - 1:
                time.sleep(random.uniform(3.0, 6.0))
            else:
                return {"sucesso": False, "erro": f"Bloqueio persistente. HTTP {resposta.status_code}"}
        except requests.RequestException as e:
            if tentativa == max_tentativas - 1:
                return {"sucesso": False, "erro": f"Erro de conexão: {e}"}
            time.sleep(2)

    if not resposta or resposta.status_code != 200:
        return {"sucesso": False, "erro": "Resposta inválida do LinkedIn."}

    soup = BeautifulSoup(resposta.text, "html.parser")

    def _get(tag, cls):
        el = soup.find(tag, class_=cls)
        return el.text.strip() if el else "Não encontrado"

    titulo = _get("h2", "top-card-layout__title")
    empresa = _get("a", "topcard__org-name-link")
    localizacao = _get("span", "topcard__flavor topcard__flavor--bullet")

    descricao_html = soup.find("div", class_="show-more-less-html__markup")
    descricao = descricao_html.get_text(separator="\n", strip=True) if descricao_html else "Não encontrado"

    dados = {
        "id": job_id,
        "titulo": titulo,
        "empresa": empresa,
        "localizacao": localizacao,
        "descricao": descricao,
        "url_original": url_fornecida,
    }

    _cache_set(job_id, dados, db_client)
    return {"sucesso": True, "dados": dados, "origem": "api_linkedin"}
