"""Camada de acesso ao Supabase via REST API (stateless, sem conexao TCP direta)."""

import asyncio
import hashlib
import logging
from typing import Any

from supabase import create_client, Client

from src.core.config import settings

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    """Retorna singleton do cliente Supabase (REST, nao TCP)."""
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_key)
    return _client


# --- Operacoes de Perfil ---

async def buscar_usuario(telegram_id: str) -> dict[str, Any] | None:
    """Busca perfil do usuario por telegram_id."""
    try:
        r = await asyncio.to_thread(
            lambda: get_client()
            .table("user_profiles")
            .select("*")
            .eq("telegram_id", str(telegram_id))
            .execute()
        )
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error("buscar_usuario telegram_id=%s: %s", telegram_id, e)
        return None


async def salvar_perfil(telegram_id: int, dados: dict[str, Any]) -> None:
    """Upsert do perfil basico (onboarding)."""
    dados["telegram_id"] = str(telegram_id)
    await asyncio.to_thread(
        lambda: get_client()
        .table("user_profiles")
        .upsert(dados, on_conflict="telegram_id")
        .execute()
    )


async def atualizar_perfil_estruturado(telegram_id: str, perfil: dict[str, Any]) -> None:
    """Atualiza o perfil_estruturado e sincroniza dados_pessoais se presentes."""
    update_data: dict[str, Any] = {"perfil_estruturado": perfil}

    dp = perfil.get("dados_pessoais", {})
    if dp.get("nome"):
        update_data["nome_completo"] = dp["nome"]
    if dp.get("email"):
        update_data["email"] = dp["email"]
    if dp.get("telefone"):
        update_data["telefone"] = dp["telefone"]
    if dp.get("linkedin"):
        update_data["linkedin"] = dp["linkedin"]
    if dp.get("cidade"):
        update_data["cidade"] = dp["cidade"]

    await asyncio.to_thread(
        lambda: get_client()
        .table("user_profiles")
        .update(update_data)
        .eq("telegram_id", str(telegram_id))
        .execute()
    )


async def buscar_todos_usuarios() -> list[dict[str, Any]]:
    """Retorna todos os usuarios com perfil_estruturado preenchido."""
    try:
        r = await asyncio.to_thread(
            lambda: get_client()
            .table("user_profiles")
            .select("*")
            .not_.is_("perfil_estruturado", "null")
            .execute()
        )
        return r.data or []
    except Exception as e:
        logger.error("buscar_todos_usuarios: %s", e)
        return []


async def deletar_usuario(telegram_id: str) -> None:
    """Remove todos os dados do usuario."""
    client = get_client()
    await asyncio.to_thread(
        lambda: client.table("user_profiles").delete().eq("telegram_id", telegram_id).execute()
    )
    await asyncio.to_thread(
        lambda: client.table("sent_jobs").delete().eq("telegram_id", telegram_id).execute()
    )


# --- Operacoes de Vagas Enviadas ---

async def job_ja_enviado(telegram_id: str, job_hash: str) -> bool:
    """Verifica se uma vaga ja foi enviada para o usuario."""
    try:
        r = await asyncio.to_thread(
            lambda: get_client()
            .table("sent_jobs")
            .select("id")
            .eq("telegram_id", telegram_id)
            .eq("job_hash", job_hash)
            .execute()
        )
        return bool(r.data)
    except Exception:
        return False


async def registrar_job_enviado(
    telegram_id: str, job_hash: str, title: str, company: str
) -> None:
    """Registra que uma vaga foi enviada para evitar duplicatas."""
    try:
        data = {
            "telegram_id": telegram_id,
            "job_hash": job_hash,
            "job_title": title,
            "job_company": company,
        }
        await asyncio.to_thread(
            lambda: get_client().table("sent_jobs").insert(data).execute()
        )
    except Exception as e:
        logger.error("registrar_job_enviado: %s", e)


def gerar_hash_vaga(vaga: dict[str, Any]) -> str:
    """Gera hash MD5 da vaga para idempotencia."""
    chave = (
        f"{vaga.get('title', vaga.get('titulo', ''))}"
        f"{vaga.get('company', vaga.get('empresa', ''))}"
    ).lower().strip()
    return hashlib.md5(chave.encode()).hexdigest()


# --- Cache de Scraping ---

async def cache_get_job(job_id: str) -> dict[str, Any] | None:
    """Busca vaga cacheada no Supabase."""
    try:
        r = await asyncio.to_thread(
            lambda: get_client()
            .table("scraped_jobs")
            .select("dados")
            .eq("job_id", job_id)
            .execute()
        )
        if r.data:
            return r.data[0]["dados"]
    except Exception as e:
        logger.warning("cache_get_job: %s", e)
    return None


async def cache_set_job(job_id: str, dados: dict[str, Any]) -> None:
    """Salva vaga no cache do Supabase."""
    try:
        await asyncio.to_thread(
            lambda: get_client()
            .table("scraped_jobs")
            .upsert({"job_id": job_id, "dados": dados})
            .execute()
        )
    except Exception as e:
        logger.warning("cache_set_job: %s", e)
