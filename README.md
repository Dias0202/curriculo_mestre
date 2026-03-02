🧱 1️⃣ TABELA: users
📌 Nome no projeto:

users

📌 O que armazena:

Perfil base do usuário (dados fixos)

SQL:
create table users (
    id uuid primary key default gen_random_uuid(),
    telegram_id text unique not null,
    nome text,
    email text,
    telefone text,
    linkedin text,
    github text,
    portfolio text,
    idioma_preferencial text default 'Portugues',
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);
🧾 2️⃣ TABELA: raw_inputs
📌 Nome:

raw_inputs

📌 O que armazena:

Tudo que o usuário já enviou (PDF, Word, texto livre, wizard)

Permite:

Reprocessamento futuro

Auditoria

Melhorar parsing

SQL:
create table raw_inputs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    tipo text check (tipo in ('pdf', 'word', 'texto', 'wizard')),
    conteudo_texto text,
    arquivo_url text,
    processado boolean default false,
    created_at timestamptz default now()
);
💼 3️⃣ TABELA: experiences
📌 Nome:

experiences

📌 O que armazena:

Experiências profissionais estruturadas

SQL:
create table experiences (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    cargo text,
    empresa text,
    localizacao text,
    data_inicio text,
    data_fim text,
    descricao_empresa text,
    created_at timestamptz default now()
);
📝 4️⃣ TABELA: experience_bullets
📌 Nome:

experience_bullets

📌 O que armazena:

Responsabilidades e conquistas de cada experiência

SQL:
create table experience_bullets (
    id uuid primary key default gen_random_uuid(),
    experience_id uuid references experiences(id) on delete cascade,
    tipo text check (tipo in ('responsabilidade', 'conquista')),
    texto text,
    ordem integer default 0
);
🎓 5️⃣ TABELA: education
📌 Nome:

education

📌 O que armazena:

Formação acadêmica

SQL:
create table education (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    grau text,
    instituicao text,
    ano_inicio text,
    ano_fim text,
    created_at timestamptz default now()
);
🛠 6️⃣ TABELA: skills
📌 Nome:

skills

📌 O que armazena:

Competências técnicas e soft skills do usuário

SQL:
create table skills (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    nome text,
    categoria text,
    nivel text
);
📜 7️⃣ TABELA: certifications
📌 Nome:

certifications

📌 O que armazena:

Certificações profissionais

SQL:
create table certifications (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    nome text,
    emissor text,
    ano text
);
🚀 8️⃣ TABELA: projects
📌 Nome:

projects

📌 O que armazena:

Projetos relevantes (acadêmicos ou profissionais)

SQL:
create table projects (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    nome text,
    descricao text,
    created_at timestamptz default now()
);
🌍 9️⃣ TABELA: languages
📌 Nome:

languages

📌 O que armazena:

Idiomas do usuário

SQL:
create table languages (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    idioma text,
    nivel text
);
🧠 🔟 TABELA: generated_resumes
📌 Nome:

generated_resumes

📌 O que armazena:

Currículos gerados para vagas específicas

Permite:

Versionamento

Histórico

Analytics

Score de match

SQL:
create table generated_resumes (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id) on delete cascade,
    vaga_texto text,
    vaga_hash text,
    idioma text,
    json_gerado jsonb,
    pdf_url text,
    score_match numeric,
    created_at timestamptz default now()
);
📊 ÍNDICES IMPORTANTES (Performance)

Execute também:

create index idx_users_telegram on users(telegram_id);
create index idx_experiences_user on experiences(user_id);
create index idx_skills_user on skills(user_id);
create index idx_generated_resumes_user on generated_resumes(user_id);
