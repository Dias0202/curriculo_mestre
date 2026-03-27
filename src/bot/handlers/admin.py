"""Comandos administrativos — broadcast, reset de perfis, estatisticas."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.core.config import settings
from src.db import supabase_client as db

logger = logging.getLogger(__name__)


def _is_admin(update: Update) -> bool:
    """Verifica se o usuario que enviou o comando e admin."""
    return settings.is_admin(update.effective_user.id)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe comandos administrativos disponiveis."""
    if not _is_admin(update):
        await update.message.reply_text("Acesso negado.")
        return

    await update.message.reply_text(
        "Comandos de Admin:\n\n"
        "/broadcast <mensagem> — Envia mensagem para TODOS os usuarios\n"
        "/reset_perfis — Limpa perfil_estruturado de todos (mantém dados basicos)\n"
        "/stats — Estatisticas de usuarios cadastrados"
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia uma mensagem para todos os usuarios cadastrados."""
    if not _is_admin(update):
        await update.message.reply_text("Acesso negado.")
        return

    partes = update.message.text.split(maxsplit=1)
    if len(partes) < 2 or not partes[1].strip():
        await update.message.reply_text(
            "Use: /broadcast <mensagem>\n\n"
            "Exemplo: /broadcast Atualizamos o sistema! Envie seu historico novamente."
        )
        return

    mensagem = partes[1].strip()
    status = await update.message.reply_text("Enviando broadcast...")

    todos_ids = await db.buscar_todos_telegram_ids()
    enviados = 0
    falhas = 0

    for tid in todos_ids:
        try:
            await context.bot.send_message(chat_id=tid, text=mensagem)
            enviados += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Broadcast falhou para %s: %s", tid, e)
            falhas += 1

    await status.edit_text(
        f"Broadcast concluido.\n"
        f"Enviados: {enviados}\n"
        f"Falhas: {falhas}\n"
        f"Total: {len(todos_ids)}"
    )


async def cmd_reset_perfis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reseta perfil_estruturado de todos os usuarios e notifica cada um."""
    if not _is_admin(update):
        await update.message.reply_text("Acesso negado.")
        return

    status = await update.message.reply_text("Resetando perfis estruturados de todos os usuarios...")

    todos_ids = await db.buscar_todos_telegram_ids()
    resetados = 0
    notificados = 0

    for tid in todos_ids:
        try:
            await db.resetar_perfil_estruturado(tid)
            resetados += 1
        except Exception as e:
            logger.error("Reset perfil %s: %s", tid, e)

        try:
            await context.bot.send_message(
                chat_id=tid,
                text=(
                    "Atualizacao importante do ATS Resume Bot!\n\n"
                    "Melhoramos significativamente a qualidade dos curriculos gerados. "
                    "Para aproveitar as melhorias, precisamos que voce reenvie seu historico profissional.\n\n"
                    "Envie seu curriculo em PDF ou cole seu historico em texto."
                ),
            )
            notificados += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning("Notificacao reset %s: %s", tid, e)

    await status.edit_text(
        f"Reset concluido.\n"
        f"Perfis resetados: {resetados}\n"
        f"Usuarios notificados: {notificados}\n"
        f"Total: {len(todos_ids)}"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe estatisticas basicas."""
    if not _is_admin(update):
        await update.message.reply_text("Acesso negado.")
        return

    todos_ids = await db.buscar_todos_telegram_ids()
    usuarios_completos = await db.buscar_todos_usuarios()

    await update.message.reply_text(
        f"Estatisticas:\n\n"
        f"Total cadastrados: {len(todos_ids)}\n"
        f"Com perfil estruturado: {len(usuarios_completos)}\n"
        f"Sem perfil: {len(todos_ids) - len(usuarios_completos)}"
    )
