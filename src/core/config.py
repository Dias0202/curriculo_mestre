"""Configuracoes globais via Pydantic Settings — valida variaveis de ambiente no startup."""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Variaveis de ambiente obrigatorias para o sistema."""

    telegram_token: str = Field(..., alias="TELEGRAM_TOKEN")
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")
    supabase_url: str = Field(..., alias="SUPABASE_URL")
    supabase_key: str = Field(..., alias="SUPABASE_KEY")
    port: int = Field(default=10000, alias="PORT")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    llm_model: str = Field(default="llama-3.3-70b-versatile", alias="LLM_MODEL")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
