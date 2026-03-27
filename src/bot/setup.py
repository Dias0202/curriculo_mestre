"""Configuracao e inicializacao do bot Telegram com handlers."""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from telegram.request import HTTPXRequest

from src.core.config import settings
from src.bot.states.onboarding import (
    ASK_NOME, ASK_EMAIL, ASK_PHONE, ASK_LINKEDIN,
    ASK_CITY, ASK_LANGUAGE, ASK_TARGET_ROLE, ASK_SENIORITY,
)
from src.bot.handlers.onboarding import (
    cmd_start, cmd_atualizar_objetivo,
    ask_nome, ask_email, ask_phone, ask_linkedin,
    ask_city, ask_language, ask_target_role, callback_seniority,
)
from src.bot.handlers.commands import (
    cmd_editar_cv, cmd_meu_perfil, cmd_deletar,
    cmd_testar_vagas, cmd_notificar_pendentes,
    callback_tipo_cv,
)
from src.bot.handlers.menu import callback_menu
from src.bot.handlers.messages import handle_incoming_message

logger = logging.getLogger(__name__)

_application: Application | None = None


def get_application() -> Application:
    """Retorna singleton da Application do Telegram."""
    global _application
    if _application is None:
        _application = _build_application()
    return _application


def _build_application() -> Application:
    """Constroi a Application com todos os handlers registrados."""
    request = HTTPXRequest(connection_pool_size=8)
    app = (
        Application.builder()
        .token(settings.telegram_token)
        .request(request)
        .build()
    )

    # Onboarding (ConversationHandler)
    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_NOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_nome)],
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_email)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_LINKEDIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_linkedin)],
            ASK_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_city)],
            ASK_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_language)],
            ASK_TARGET_ROLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_target_role),
                CallbackQueryHandler(callback_seniority, pattern="^sen_"),
            ],
            ASK_SENIORITY: [CallbackQueryHandler(callback_seniority, pattern="^sen_")],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )

    # Atualizacao de Objetivo (ConversationHandler)
    atualizacao_objetivo = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cmd_atualizar_objetivo, pattern="^menu_atualizar_objetivo$")
        ],
        states={
            ASK_TARGET_ROLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_target_role),
                CallbackQueryHandler(callback_seniority, pattern="^sen_"),
            ],
            ASK_SENIORITY: [CallbackQueryHandler(callback_seniority, pattern="^sen_")],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )

    # Registro de handlers
    app.add_handler(onboarding)
    app.add_handler(atualizacao_objetivo)
    app.add_handler(CommandHandler("editar_cv", cmd_editar_cv))
    app.add_handler(CommandHandler("meuperfil", cmd_meu_perfil))
    app.add_handler(CommandHandler("meu_perfil", cmd_meu_perfil))
    app.add_handler(CommandHandler("deletar", cmd_deletar))
    app.add_handler(CommandHandler("buscar_vagas", cmd_testar_vagas))
    app.add_handler(CommandHandler("testar_vagas", cmd_testar_vagas))
    app.add_handler(CommandHandler("notificar_pendentes", cmd_notificar_pendentes))
    app.add_handler(CallbackQueryHandler(callback_menu, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(callback_tipo_cv, pattern="^cv_"))
    app.add_handler(MessageHandler(filters.TEXT | filters.Document.ALL, handle_incoming_message))

    logger.info("Bot handlers registrados com sucesso.")
    return app
