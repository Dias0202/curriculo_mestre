"""Handlers do fluxo de onboarding e atualizacao de objetivo."""

import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from src.db import supabase_client as db
from src.bot.states.onboarding import (
    ASK_NOME, ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN,
    ASK_CITY, ASK_LANGUAGE, ASK_TARGET_ROLE, ASK_SENIORITY,
)
from src.bot.handlers.menu import enviar_menu

logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — verifica se usuario ja existe ou inicia onboarding."""
    user_id = str(update.effective_user.id)
    usuario = await db.buscar_usuario(user_id)
    if usuario:
        nome = usuario.get("nome_completo") or update.effective_user.first_name or ""
        await enviar_menu(update, context, nome)
        return ConversationHandler.END
    await update.message.reply_text(
        "Bem-vindo ao ATS Resume Bot.\n\n"
        "Vou configurar seu perfil em poucos passos.\n\n"
        "Qual o seu NOME COMPLETO?"
    )
    return ASK_NOME


async def cmd_atualizar_objetivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia fluxo de atualizacao de cargo alvo."""
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
    else:
        msg = update.message
    await msg.reply_text(
        "Atualizacao de Perfil necessaria.\n\n"
        "Qual o cargo exato que voce busca?\n"
        "Exemplos: Desenvolvedor Python, Analista de Dados, Engenheiro DevOps"
    )
    return ASK_TARGET_ROLE


async def ask_nome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["nome_completo"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu E-MAIL profissional?")
    return ASK_EMAIL


async def ask_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["email"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu TELEFONE? (com DDD)")
    return ASK_PHONE


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["telefone"] = update.message.text.strip()
    await update.message.reply_text("Qual o seu perfil no LINKEDIN? (URL completo)")
    return ASK_LINKEDIN


async def ask_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["linkedin"] = update.message.text.strip()
    await update.message.reply_text("Qual a sua CIDADE e ESTADO? (Exemplo: Sao Paulo, SP)")
    return ASK_CITY


async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cidade"] = update.message.text.strip()
    await update.message.reply_text("Em qual IDIOMA deseja o curriculo? (Exemplos: Portugues, Ingles)")
    return ASK_LANGUAGE


async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["idioma"] = update.message.text.strip()
    await update.message.reply_text(
        "Qual o cargo exato que voce busca?\n"
        "Exemplos: Desenvolvedor Python, Analista de Dados, Engenheiro DevOps"
    )
    return ASK_TARGET_ROLE


async def ask_target_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["cargo_alvo"] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Estagio", callback_data="sen_Estagio"),
         InlineKeyboardButton("Junior", callback_data="sen_Junior")],
        [InlineKeyboardButton("Pleno", callback_data="sen_Pleno"),
         InlineKeyboardButton("Senior", callback_data="sen_Senior")],
        [InlineKeyboardButton("Especialista", callback_data="sen_Especialista")],
    ])
    await update.message.reply_text(
        "Qual o seu nivel de experiencia atual/desejado?", reply_markup=keyboard
    )
    return ASK_SENIORITY


async def callback_seniority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finaliza onboarding salvando o perfil."""
    query = update.callback_query
    await query.answer()
    senioridade = query.data.split("_")[1]
    user_id = update.effective_user.id
    cargo_alvo = context.user_data.get("cargo_alvo", "")

    dados = {
        "nome_completo": context.user_data.get("nome_completo", ""),
        "email": context.user_data.get("email", ""),
        "telefone": context.user_data.get("telefone", ""),
        "linkedin": context.user_data.get("linkedin", ""),
        "cidade": context.user_data.get("cidade", ""),
        "idioma": context.user_data.get("idioma", ""),
        "cargo_alvo": cargo_alvo,
        "senioridade": senioridade,
    }
    await db.salvar_perfil(user_id, dados)
    context.user_data.clear()

    await query.edit_message_text(
        f"Perfil salvo.\n"
        f"Objetivo: {cargo_alvo} ({senioridade})\n\n"
        "Agora envie um arquivo .pdf ou .txt com seu historico profissional base."
    )
    return ConversationHandler.END
