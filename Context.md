# Contexto Arquitetural e Regras de Negocio: ATS Resume Bot

## 1. Visao Geral do Sistema
O projeto e um bot do Telegram focado em engenharia de curriculos e busca de vagas (ATS - Applicant Tracking System). Ele processa historicos profissionais, gera PDFs otimizados no formato Harvard utilizando IA e busca vagas compativeis utilizando web scraping.

## 2. Restricoes de Infraestrutura (Critico)
O sistema e hospedado em plataformas de nivel gratuito (Free Tier), o que impoe limitacoes estritas de arquitetura:

* **Render Free Tier (Compute):**
    * O servico esta sendo mantido ligado com o uptimerobot a cada 10 minutos.
    * Qualquer estado mantido em memoria RAM (variaveis globais, caches como `_MEM_CACHE`, FSM em memoria) sera perdido permanentemente a cada suspensao.
    * **Diretiva de Codigo:** O sistema deve ser 100% Stateless (sem estado em memoria). Qualquer dado de sessao, cache de scraping ou progresso de onboarding deve ser persistido imediatamente no Supabase.
    * **Roteamento:** O uso de `Long Polling` para o Telegram e ineficiente neste cenario. O bot deve operar via `Webhooks` integrados a uma API Web (FastAPI) rodando na porta definida pela variavel `$PORT`.

* **Supabase Free Tier (Database):**
    * O pool de conexoes do PostgreSQL e restrito.
    * **Diretiva de Codigo:** O acesso ao banco de dados deve ocorrer exclusivamente via API REST (utilizando a biblioteca `supabase-py`). O uso de conexoes TCP diretas (`psycopg2`, `asyncpg`) ou ORMs complexos (como SQLAlchemy com pooler local) e proibido para evitar o esgotamento (exhaustion) das conexoes.

## 3. Padroes de Codigo Exigidos
Todos os scripts gerados ou refatorados devem seguir rigorosamente os padroes abaixo:

* **Linguagem:** Python 3.11+.
* **Assincronismo:** Todas as operacoes de I/O (chamadas a APIs externas, Groq, Telegram e Supabase) devem ser assincronas (`async`/`await`). Evite o bloqueio da thread principal (Event Loop).
* **Tipagem Forte:** O uso de Type Hints e obrigatorio em todas as assinaturas de funcoes, metodos e atributos de classe (ex: `async def processar_vaga(url: str, user_id: int) -> dict:`).
* **Validacao de Dados:** Utilize `Pydantic` para validacao de variaveis de ambiente, mapeamento de payloads JSON recebidos do Groq e estruturacao dos dados do Supabase.
* **Tratamento de Excecoes:** Nenhuma excecao deve parar a aplicacao. Utilize blocos `try/except` especificos e registre os erros utilizando a biblioteca `logging`. O usuario final deve receber mensagens de erro tratadas.

## 4. Regras de Negocio e Fluxos

### 4.1. Processamento de Perfil (Engenharia de Dados)
* A consolidacao do perfil deve manter a fidelidade do texto original, estruturando-o em formato JSON.
* Conflitos entre o banco de dados e novas entradas do usuario devem ser resolvidos via LLM (Prompt 1), priorizando adicao ou atualizacao de dados, evitando exclusao acidental do historico.
* O esquema JSON resultante deve refletir a tipologia exigida (experiences, education, skills, projects, certifications, languages).

### 4.2. Geracao de Curriculo (Recrutamento ATS)
* **Factualidade Estrita:** E terminantemente proibido que a IA invente dados (alucinacao). O modelo de linguagem (Prompt 2) deve mapear apenas as informacoes comprovadas no historico do candidato contra as exigencias da vaga.
* **Keywords Ocultas (Stealth ATS):** Termos exigidos pela vaga que o candidato nao possui devem ser injetados no documento PDF na mesma cor do fundo (fonte branca, tamanho 1) para serem lidos pelo parser do ATS sem poluir a leitura humana.

### 4.3. Agendamento de Tarefas (CRON)
* Como o Render suspende a aplicacao, rotinas em background (`job_queue` ou `threading`) falharao se executadas localmente.
* **Solucao Exigida:** Tarefas agendadas (como as sugestoes diarias ao meio-dia) devem ser acionadas via endpoint HTTP dedicado (ex: `POST /cron/daily-jobs`), que sera chamado por um servico externo gratuito de ping (como cron-job.org) para acordar o servidor.

## 5. Arquitetura de Pastas Exigida
A arquitetura de software monolita deve ser refatorada para a seguinte estrutura modular:

```text
/src
  /api               # Endpoints FastAPI (Webhook do Telegram, Healthchecks, Rotas de Cron)
  /bot               # Logica de interface do Telegram
    /handlers        # Controladores de mensagens e comandos (start, inputs de arquivo/texto)
    /states          # Definicoes de estados (FSM) para onboarding
  /core              # Configuracoes globais (Settings via Pydantic, configuracao de Logging)
  /db                # Camada de interacao com o Supabase (Operacoes CRUD, clientes)
  /services          # Regras de negocios complexas
    llm.py           # Cliente Groq e orquestracao de prompts
    pdf.py           # Classe CurriculoHarvard e logica do fpdf2
    scraper.py       # Extração do JobSpy e LinkedIn
  /models            # Schemas do Pydantic para validacao e tipagem dos JSONs do LLM
  main.py            # Entrypoint absoluto: Inicializacao do FastAPI e atrelamento do Bot