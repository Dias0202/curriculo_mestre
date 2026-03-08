# ATS Resume Bot

Sistema assincrono de orquestracao de carreira focado em ATS (Applicant Tracking Systems). Este bot do Telegram atua como um pipeline de dados automatizado que ingere historicos profissionais, raspa vagas em tempo real via LinkedIn Guest API, realiza o match inteligente via LLM e gera curriculos otimizados em PDF.

## Arquitetura

O sistema opera sob uma arquitetura serverless/micro-containers e utiliza processamento assincrono para lidar com I/O de rede intensivo.

* **Linguagem:** Python 3.11
* **Orquestracao / UI:** `python-telegram-bot` (Modo Polling Assincrono)
* **Motor LLM:** API Groq (`llama-3.3-70b-versatile`) focada em processamento rapido e saidas JSON estruturadas.
* **Persistencia:** Supabase (PostgreSQL), utilizado puramente como Document Store (JSONB) para os perfis estruturados e controle transacional de vagas ja enviadas.
* **Geracao de PDF:** `fpdf2` com customizacao estrutural para layout padrao Harvard.
* **Web Scraping:** `jobspy` para abstracao de crawling da Guest API do LinkedIn.
* **Hospedagem:** Preparado para Render.com (inclui servidor HTTP keep-alive na porta 10000).

## Funcionalidades Core

1.  **Ingestao Dinamica de Perfil:**
    * Faz o parsing de PDFs (`pypdf`) ou textos brutos enviados pelo usuario.
    * O LLM (Prompt 1) atua como Engenheiro de Dados, normalizando datas, separando hard/soft skills e estruturando freelances e projetos academicos em um schema JSON rigoroso.
2.  **Match de Vagas (Scraping + Scoring):**
    * Job diario via APScheduler (executado as 12:00 BRT).
    * Extrai termos-chave amplos do JSON do usuario.
    * Aciona o `scraper.py` (JobSpy) para buscar vagas recentes.
    * O LLM pontua as vagas (0 a 100). Apenas vagas >= 60 avancam no pipeline.
3.  **Geracao de Curriculo (ATS SEO):**
    * Injeta palavras-chave exatas da descricao da vaga nas secoes de competencias e resumo.
    * Aplica o metodo STAR (Situation, Task, Action, Result) nos bullet points de experiencia.
    * **ATS Stealth / White Fonting:** Emprega estampa de texto invisivel (RGB 255,255,255 tamanho 1) contendo hard skills exigidas pela vaga que o candidato nao possui experiencia formal comprovada, garantindo pontuacao em parsers ATS rudimentares sem corromper a leitura humana.
4.  **Anti-Spam de Vagas:**
    * Hash MD5 (Titulo + Empresa) e cruzado de forma relacional na tabela `sent_jobs` no Supabase para garantir idempotencia no envio diario.

## Dependencias

Consulte o `requirements.txt`. As bibliotecas principais incluem:
* `python-telegram-bot[job-queue]>=21.0.1`
* `groq>=0.9.0`
* `supabase>=2.11.0`
* `fpdf2>=2.7.9`
* `pypdf>=5.0.0`
* `python-jobspy>=1.1.80`

## Configuracao e Deploy

### Variaveis de Ambiente (.env ou Dashboard)
O sistema exige as seguintes variaveis de ambiente na infraestrutura para instanciacao:

```env
TELEGRAM_TOKEN=token_do_botfather
GROQ_API_KEY=api_groq
SUPABASE_URL=url_supabase
SUPABASE_KEY=anon_key_supabase
PORT=10000 #Health check do Render
