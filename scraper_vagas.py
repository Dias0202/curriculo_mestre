import requests
from bs4 import BeautifulSoup
import re
import random
import time

# Pool de User-Agents modernos para rotação
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0"
]

# Cache em memória simples (Para persistência em produção, recomenda-se SQLite ou Redis)
CACHE_VAGAS = {}

def obter_headers_aleatorios():
    """Gera headers HTTP simulando tráfego orgânico vindo do Google."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "DNT": "1", # Do Not Track
        "Connection": "keep-alive"
    }

def extrair_vaga_linkedin_prod(url_fornecida, max_tentativas=2):
    """
    Extrai dados da vaga com mecanismos anti-ban, cache e retry.
    """
    match_id = re.search(r'([0-9]{9,10})', url_fornecida)
    if not match_id:
        return {"sucesso": False, "erro": "ID numérico da vaga não encontrado na URL."}
    
    job_id = match_id.group(1)
    
    # 1. Verificação de Cache
    if job_id in CACHE_VAGAS:
        return {"sucesso": True, "dados": CACHE_VAGAS[job_id], "origem": "cache"}

    url_guest = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    
    for tentativa in range(max_tentativas):
        headers = obter_headers_aleatorios()
        
        # Atraso randômico (jitter) entre 1 e 3 segundos antes da requisição para evitar padrões
        time.sleep(random.uniform(1.0, 3.0)) 
        
        try:
            resposta = requests.get(url_guest, headers=headers, timeout=10)
            
            # Se for 200, sai do loop de tentativas e processa
            if resposta.status_code == 200:
                break
            
            # Se for bloqueio e ainda houver tentativas, aguarda e tenta novamente
            if tentativa < max_tentativas - 1:
                time.sleep(random.uniform(3.0, 6.0)) # Backoff maior em caso de erro
                continue
            else:
                return {"sucesso": False, "erro": f"Bloqueio persistente. Código HTTP: {resposta.status_code}"}
                
        except requests.RequestException as e:
            if tentativa == max_tentativas - 1:
                return {"sucesso": False, "erro": f"Erro de conexão: {str(e)}"}
            time.sleep(2)

    # 2. Parsing do HTML (mantido igual, pois já provou funcionar no seu teste)
    soup = BeautifulSoup(resposta.text, 'html.parser')
    
    try:
        titulo = soup.find('h2', class_='top-card-layout__title').text.strip()
    except AttributeError:
        titulo = "Não encontrado"
        
    try:
        empresa = soup.find('a', class_='topcard__org-name-link').text.strip()
    except AttributeError:
        empresa = "Não encontrado"
        
    try:
        localizacao = soup.find('span', class_='topcard__flavor topcard__flavor--bullet').text.strip()
    except AttributeError:
        localizacao = "Não encontrado"
        
    try:
        descricao_html = soup.find('div', class_='show-more-less-html__markup')
        descricao_texto = descricao_html.get_text(separator='\n', strip=True) if descricao_html else "Não encontrado"
    except AttributeError:
        descricao_texto = "Não encontrado"

    dados_extraidos = {
        "id": job_id,
        "titulo": titulo,
        "empresa": empresa,
        "localizacao": localizacao,
        "descricao": descricao_texto,
        "url_original": url_fornecida
    }

    # 3. Salvar no Cache
    CACHE_VAGAS[job_id] = dados_extraidos

    return {
        "sucesso": True,
        "dados": dados_extraidos,
        "origem": "api_linkedin"
    }
