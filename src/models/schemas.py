"""Schemas Pydantic para validacao de dados do LLM, perfil e vagas."""

from pydantic import BaseModel, Field


# --- Perfil Estruturado (Prompt Parser) ---

class Experience(BaseModel):
    cargo: str = ""
    empresa: str = ""
    localizacao: str = ""
    data_inicio: str = ""
    data_fim: str = ""
    descricao_empresa: str = ""
    responsabilidades: list[str] = Field(default_factory=list)
    conquistas: list[str] = Field(default_factory=list)


class Education(BaseModel):
    grau: str = ""
    curso: str = ""
    instituicao: str = ""
    ano_inicio: str = ""
    ano_fim: str = ""


class Skill(BaseModel):
    nome: str = ""
    categoria: str = ""
    nivel: str = ""


class Certification(BaseModel):
    nome: str = ""
    emissor: str = ""
    ano: str = ""


class Project(BaseModel):
    nome: str = ""
    descricao: str = ""


class Language(BaseModel):
    idioma: str = ""
    nivel: str = ""


class DadosPessoais(BaseModel):
    nome: str = ""
    email: str = ""
    telefone: str = ""
    linkedin: str = ""
    cidade: str = ""


class PerfilEstruturado(BaseModel):
    """Schema completo do perfil consolidado pelo LLM Parser."""
    dados_pessoais: DadosPessoais = Field(default_factory=DadosPessoais)
    experiences: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)


# --- Curriculo Gerado (Prompt Generator) ---

class Identificacao(BaseModel):
    nome: str = ""
    titulo: str = ""
    localizacao: str = ""
    telefone: str = ""
    email: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class ExperienciaCV(BaseModel):
    cargo: str = ""
    empresa: str = ""
    localizacao: str = ""
    data_inicio: str = ""
    data_fim: str = ""
    descricao_empresa: str = ""
    responsabilidades: list[str] = Field(default_factory=list)
    conquistas: list[str] = Field(default_factory=list)


class EducacaoCV(BaseModel):
    grau: str = ""
    curso: str = ""
    instituicao: str = ""
    ano_inicio: str = ""
    ano_fim: str = ""


class ProjetoCV(BaseModel):
    nome: str = ""
    descricao: str = ""


class RelatorioAnalitico(BaseModel):
    match_score: int = 0
    analise_gaps: list[str] = Field(default_factory=list)
    dica_entrevista: str = ""


class CurriculoGerado(BaseModel):
    """Schema do curriculo gerado pelo LLM Generator."""
    cabecalhos: dict[str, str] = Field(default_factory=dict)
    identificacao: Identificacao = Field(default_factory=Identificacao)
    resumo: str = ""
    competencias: list[str] = Field(default_factory=list)
    experiencias: list[ExperienciaCV] = Field(default_factory=list)
    educacao: list[EducacaoCV] = Field(default_factory=list)
    certificacoes: list[str] = Field(default_factory=list)
    projetos: list[ProjetoCV] = Field(default_factory=list)
    idiomas: list[str] = Field(default_factory=list)
    keywords_ocultas: list[str] = Field(default_factory=list)
    relatorio_analitico: RelatorioAnalitico = Field(default_factory=RelatorioAnalitico)


# --- Vaga ---

class VagaDados(BaseModel):
    """Dados de uma vaga extraida (LinkedIn ou texto livre)."""
    id: str = ""
    titulo: str = ""
    empresa: str = ""
    localizacao: str = ""
    descricao: str = ""
    url: str = ""


class VagaJobSpy(BaseModel):
    """Dados de uma vaga retornada pelo JobSpy."""
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    job_url: str = ""
