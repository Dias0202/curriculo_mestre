"""Menu principal e callbacks de navegacao."""

import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def enviar_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    nome: str = "",
) -> None:
    """Envia o menu principal com acoes disponiveis."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Buscar Vagas Agora", callback_data="menu_buscar")],
        [InlineKeyboardButton("Meu Perfil", callback_data="menu_perfil"),
         InlineKeyboardButton("Deletar Dados", callback_data="menu_deletar")],
    ])
    texto = (
        "Perfil ativo e configurado.\n\n"
        "Selecione uma acao abaixo ou envie a descricao/link de uma vaga "
        "para gerar um curriculo adaptado imediatamente."
    )
    if update.message:
        await update.message.reply_text(texto, reply_markup=keyboard)
    elif update.callback_query:
        await update.callback_query.edit_message_text(texto, reply_markup=keyboard)


async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router dos botoes do menu principal."""
    from src.bot.handlers.commands import cmd_testar_vagas, cmd_meu_perfil, cmd_deletar
    from src.bot.handlers.onboarding import cmd_atualizar_objetivo

    query = update.callback_query
    await query.answer()

    if query.data == "menu_buscar":
        await cmd_testar_vagas(update, context)
    elif query.data == "menu_perfil":
        await cmd_meu_perfil(update, context)
    elif query.data == "menu_deletar":
        await cmd_deletar(update, context)
    elif query.data == "menu_atualizar_objetivo":
        await cmd_atualizar_objetivo(update, context)
