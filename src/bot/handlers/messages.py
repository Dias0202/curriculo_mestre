"""Handler de mensagens de texto e documentos."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.db import supabase_client as db
from src.services import llm, scraper
from src.services.pdf import extrair_texto_arquivo
from src.bot.handlers.commands import _perguntar_tipo_cv

logger = logging.getLogger(__name__)


async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router principal de mensagens — classifica intencao e despacha."""
    user_id = str(update.effective_user.id)
    usuario = await db.buscar_usuario(user_id)

    # --- Documento (PDF/TXT) ---
    if update.message.document:
        doc = update.message.document
        filename = doc.file_name or "arquivo.pdf"
        status = await update.message.reply_text("Processando seu historico... Aguarde.")
        try:
            file_obj = await doc.get_file()
            file_bytes = bytearray(await file_obj.download_as_bytearray())
            texto = await asyncio.to_thread(extrair_texto_arquivo, file_bytes, filename)
            if not texto.strip():
                await status.edit_text(
                    "Nao consegui extrair texto do arquivo. Tente enviar em .txt ou cole o texto diretamente."
                )
                return
            perfil_atual = (usuario or {}).get("perfil_estruturado") or {}
            novo_perfil = await llm.consolidar_perfil(perfil_atual, texto)
            await db.atualizar_perfil_estruturado(user_id, novo_perfil)
            await status.edit_text(
                "Historico processado e salvo com sucesso!\n\n"
                "Agora envie uma descricao de vaga ou link do LinkedIn para gerar seu curriculo adaptado."
            )
        except Exception as e:
            logger.error("handle_document: %s", e, exc_info=True)
            await status.edit_text(f"Erro ao processar arquivo: {e}")
        return

    # --- Texto ---
    texto_msg = (update.message.text or "").strip()
    if not texto_msg:
        return

    intencao = await llm.classificar_intencao(texto_msg)

    if intencao == "URL_LINKEDIN":
        if not usuario or not usuario.get("perfil_estruturado"):
            await update.message.reply_text("Envie seu historico profissional primeiro antes de solicitar uma vaga.")
            return
        status = await update.message.reply_text("Extraindo vaga do LinkedIn... Aguarde.")
        try:
            vaga = await scraper.extrair_vaga_linkedin(texto_msg)
            if not vaga.get("sucesso"):
                await status.edit_text(
                    f"Nao consegui ler a vaga ({vaga.get('erro', '')}). "
                    "O LinkedIn costuma bloquear robos. Por favor, COPIE O TEXTO da vaga e cole aqui."
                )
                return
            await status.delete()
            await _perguntar_tipo_cv(update, context, vaga.get("dados"))
        except Exception as e:
            logger.error("handle_linkedin: %s", e, exc_info=True)
            await status.edit_text(f"Erro ao extrair vaga: {e}")
        return

    if intencao == "VAGA":
        if not usuario or not usuario.get("perfil_estruturado"):
            await update.message.reply_text("Envie seu historico profissional primeiro antes de solicitar uma vaga.")
            return
        vaga_dados = {"titulo": "Vaga", "empresa": "", "localizacao": "", "descricao": texto_msg}
        await _perguntar_tipo_cv(update, context, vaga_dados)
        return

    if intencao == "HISTORICO":
        status = await update.message.reply_text("Processando seu historico... Aguarde.")
        try:
            perfil_atual = (usuario or {}).get("perfil_estruturado") or {}
            novo_perfil = await llm.consolidar_perfil(perfil_atual, texto_msg)
            await db.atualizar_perfil_estruturado(user_id, novo_perfil)
            await status.edit_text(
                "Historico atualizado com sucesso!\n\n"
                "Agora envie uma descricao de vaga ou link do LinkedIn para gerar seu curriculo adaptado."
            )
        except Exception as e:
            logger.error("handle_historico: %s", e, exc_info=True)
            await status.edit_text(f"Erro ao processar historico: {e}")
        return

    if intencao == "EDICAO":
        if not usuario or not usuario.get("perfil_estruturado"):
            await update.message.reply_text("Voce ainda nao tem perfil salvo para editar.")
            return
        status = await update.message.reply_text("Aplicando atualizacao no seu perfil... Aguarde.")
        try:
            perfil_atual = usuario.get("perfil_estruturado") or {}
            novo_perfil = await llm.editar_perfil(perfil_atual, texto_msg)
            await db.atualizar_perfil_estruturado(user_id, novo_perfil)
            await status.edit_text("Perfil atualizado. Use /meuperfil para conferir.")
        except Exception as e:
            logger.error("handle_edicao: %s", e, exc_info=True)
            await status.edit_text(f"Erro ao atualizar perfil: {e}")
        return

    await update.message.reply_text(
        "Nao entendi sua mensagem. Voce pode:\n"
        "- Enviar a descricao de uma vaga\n"
        "- Enviar o link de uma vaga do LinkedIn\n"
        "- Enviar seu historico profissional em PDF ou texto\n"
        "- Usar /start para acessar o menu principal"
    )
