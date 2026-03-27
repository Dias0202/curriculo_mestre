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
    admin_ids: str = Field(default="", alias="ADMIN_IDS")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    def is_admin(self, telegram_id: int | str) -> bool:
        """Verifica se o telegram_id esta na lista de admins."""
        if not self.admin_ids:
            return False
        ids = {x.strip() for x in self.admin_ids.split(",")}
        return str(telegram_id) in ids


settings = Settings()
