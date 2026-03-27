# ATS Resume Bot

Bot do Telegram para engenharia de curriculos otimizados para ATS (Applicant Tracking Systems). Processa historicos profissionais via LLM, gera PDFs no formato Harvard e busca vagas compativeis via web scraping.

## Arquitetura

O sistema opera 100% no free tier (Render + Supabase), stateless por design.

* **Runtime:** Python 3.11 + FastAPI (Webhooks) + uvicorn
* **LLM:** API Groq (`llama-3.3-70b-versatile`)
* **Persistencia:** Supabase via REST API (`supabase-py`) — sem conexao TCP direta
* **PDF:** `fpdf2` com layout padrao Harvard
* **Scraping:** `python-jobspy` + `httpx` (async) para LinkedIn Guest API
* **Deploy:** Docker no Render.com com health check na porta 10000

### Modos de Operacao

| Variavel `WEBHOOK_URL` | Modo | Uso |
|---|---|---|
| Definida | **Webhook + FastAPI** | Producao (Render) |
| Ausente | **Long Polling** | Desenvolvimento local |

### Estrutura de Pastas

```
/src
  /api               # Endpoints FastAPI (Webhook, Health, CRON)
  /bot               # Logica do Telegram
    /handlers        # Comandos, mensagens, menus, onboarding, cron
    /states          # Estados FSM do onboarding
  /core              # Config (Pydantic Settings) e Logging
  /db                # Camada Supabase (CRUD async via REST)
  /services          # Regras de negocio
    llm.py           # Cliente Groq e prompts
    pdf.py           # CurriculoHarvard (fpdf2)
    scraper.py       # LinkedIn Guest API + JobSpy (async)
  /models            # Schemas Pydantic
main.py              # Entrypoint (FastAPI + Bot)
```

## Funcionalidades

1. **Ingestao de Perfil** — parsing de PDFs (PyMuPDF) e textos; LLM normaliza em JSON estruturado
2. **Match de Vagas** — scraping via JobSpy, scoring por LLM (0-100), apenas vagas >= 60 avancam
3. **Geracao de CV ATS** — keywords da vaga injetadas no curriculo, metodo STAR, stealth ATS
4. **Sugestoes Diarias** — endpoint `POST /cron/daily-jobs` acionado por cron-job.org
5. **Anti-Spam** — hash MD5 da vaga cruzado no Supabase para idempotencia

## Variaveis de Ambiente

```env
TELEGRAM_TOKEN=token_do_botfather
GROQ_API_KEY=api_groq
SUPABASE_URL=url_supabase
SUPABASE_KEY=anon_key_supabase
PORT=10000
WEBHOOK_URL=https://seu-app.onrender.com  # Omitir para modo polling local
LLM_MODEL=llama-3.3-70b-versatile         # Opcional
```

## Deploy (Render)

1. Configure as variaveis de ambiente no dashboard do Render
2. Defina `WEBHOOK_URL` como a URL publica do servico (ex: `https://ats-resume-bot.onrender.com`)
3. Configure o UptimeRobot para pingar `GET /health` a cada 10 minutos
4. Configure o cron-job.org para chamar `POST /cron/daily-jobs` diariamente as 12:00 BRT

## Desenvolvimento Local

```bash
cp .env.example .env  # Preencha as variaveis
pip install -r requirements.txt
python main.py        # Inicia em modo polling
```
