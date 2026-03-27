"""Comandos do bot — busca de vagas, perfil, deletar, editar CV, notificar."""

import re
import asyncio
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from src.db import supabase_client as db
from src.services import llm, scraper
from src.services.pdf import gerar_pdf, slug, clean_null_value

logger = logging.getLogger(__name__)


# --- Helpers ---

def formatar_perfil_texto(usuario: dict, perfil: dict) -> str:
    """Formata o perfil do usuario como texto legivel."""
    linhas: list[str] = []
    nome = usuario.get("nome_completo") or "Candidato"
    cargo = usuario.get("cargo_alvo") or "Nao definido"
    sen = usuario.get("senioridade") or "Nao definido"

    linhas.append(f"Nome: {nome}")
    linhas.append(f"Objetivo: {cargo} ({sen})")

    contatos = [usuario[k] for k in ["email", "telefone", "linkedin", "cidade"] if usuario.get(k)]
    if contatos:
        linhas.append("Contato: " + " | ".join(contatos))

    exps = perfil.get("experiences", [])
    if exps:
        linhas.append("\nEXPERIENCIAS")
        for e in exps:
            inicio = clean_null_value(e.get("data_inicio"))
            fim = clean_null_value(e.get("data_fim"))
            periodo = ""
            if inicio and fim:
                periodo = f" ({inicio} a {fim})"
            elif inicio:
                periodo = f" ({inicio} a Atual)"
            elif fim:
                periodo = f" ({fim})"
            linhas.append(f"- {e.get('cargo', '')} | {e.get('empresa', '')}{periodo}")

    edus = perfil.get("education", [])
    if edus:
        linhas.append("\nFORMACAO")
        for ed in edus:
            linhas.append(f"- {ed.get('curso', '')} | {ed.get('instituicao', '')}")

    skills = perfil.get("skills", [])
    if skills:
        hard = [str(s.get("nome", "")) for s in skills if "hard" in str(s.get("categoria", "")).lower()]
        if hard:
            linhas.append("\nTECNOLOGIAS: " + ", ".join(hard))

    linhas.append("\nPara editar: envie em texto livre. Ex: 'Meu nome eh X' ou 'Adicione conhecimento em AWS'.")
    return "\n".join(linhas)


def perfil_tem_ingles_fluente(perfil: dict) -> bool:
    """Verifica se o perfil indica fluencia em ingles."""
    niveis_ok = {"intermediario", "fluente", "nativo", "avancado", "intermediate", "fluent", "native", "advanced"}
    for lang in perfil.get("languages", []):
        if not isinstance(lang, dict):
            continue
        idioma = str(lang.get("idioma", "")).lower()
        nivel = str(lang.get("nivel", "")).lower()
        if "ingl" in idioma or "english" in idioma:
            if any(n in nivel for n in niveis_ok):
                return True
    return False


# --- Pipeline: Gera CV + Envia PDF ---

async def _perguntar_tipo_cv(
    update_or_query: object,
    context: ContextTypes.DEFAULT_TYPE,
    vaga_dados: dict,
) -> None:
    """Valida perfil e pergunta ao usuario se quer CV com ou sem resumo."""
    # Valida perfil antes de oferecer geracao
    user_id = str(update_or_query.effective_user.id) if hasattr(update_or_query, "effective_user") else ""
    if user_id:
        usuario = await db.buscar_usuario(user_id)
        if usuario:
            gaps = llm.validar_perfil_para_cv(usuario)
            if gaps:
                aviso = (
                    "Antes de gerar seu curriculo, preciso de dados que estao faltando no seu perfil:\n\n"
                    + "\n".join(f"- {g}" for g in gaps)
                    + "\n\nEnvie uma mensagem com essas informacoes (ex: 'Meu nome e Joao Silva, email joao@email.com, telefone 31999998888')."
                    "\n\nDepois, envie a vaga novamente."
                )
                if hasattr(update_or_query, "message") and update_or_query.message:
                    await update_or_query.message.reply_text(aviso)
                else:
                    await update_or_query.reply_text(aviso)
                return

    context.user_data["vaga_pendente"] = vaga_dados
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Com Resumo", callback_data="cv_com_resumo"),
         InlineKeyboardButton("Sem Resumo", callback_data="cv_sem_resumo")],
    ])
    msg = "Como voce quer o curriculo gerado?"
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(msg, reply_markup=keyboard)
    else:
        await update_or_query.reply_text(msg, reply_markup=keyboard)


async def callback_tipo_cv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback do botao com/sem resumo — dispara geracao do CV."""
    query = update.callback_query
    await query.answer()
    com_resumo = query.data == "cv_com_resumo"
    vaga = context.user_data.pop("vaga_pendente", None)
    if not vaga:
        await query.edit_message_text("Sessao expirada. Envie a vaga novamente.")
        return

    user_id = str(update.effective_user.id)
    usuario = await db.buscar_usuario(user_id)
    perfil = (usuario or {}).get("perfil_estruturado") or {}
    tipo_txt = "com resumo" if com_resumo else "sem resumo"
    await query.edit_message_text(f"Gerando curriculo ATS {tipo_txt}... Aguarde.")

    try:
        await processar_e_enviar_vaga(
            bot=context.bot, telegram_id=user_id, usuario=usuario, perfil=perfil,
            titulo=vaga.get("titulo", vaga.get("title", "")),
            empresa=vaga.get("empresa", vaga.get("company", "")),
            local=vaga.get("localizacao", vaga.get("location", "")),
            descricao=vaga.get("descricao", vaga.get("description", "")),
            url=vaga.get("url", vaga.get("job_url", "")),
            com_resumo=com_resumo, context=context,
        )
        await query.delete_message()
    except Exception as e:
        logger.error("callback_tipo_cv: %s", e, exc_info=True)
        await query.edit_message_text(f"Erro ao gerar curriculo: {e}")


async def processar_e_enviar_vaga(
    bot: object,
    telegram_id: str,
    usuario: dict,
    perfil: dict,
    titulo: str,
    empresa: str,
    local: str,
    descricao: str,
    url: str = "",
    job_hash: str = "",
    indice: int = 1,
    com_resumo: bool = True,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Pipeline completo: gera CV JSON -> PDF -> envia ao usuario."""
    idioma = usuario.get("idioma", "Portugues")
    cv = await llm.gerar_cv_json(perfil, usuario, titulo, empresa, local, descricao, com_resumo)
    pdf_buf = await asyncio.to_thread(gerar_pdf, cv, idioma)

    if context is not None:
        context.user_data["ultimo_cv"] = cv
        context.user_data["ultimo_usuario"] = usuario

    nome_usuario = usuario.get("nome_completo") or "Candidato"
    nome_vaga = titulo or empresa or "Vaga"
    nome_arquivo = f"{slug(nome_usuario)}_{slug(nome_vaga)}.pdf"

    caption = f"Vaga {indice}: {titulo}\nEmpresa: {empresa}\nLocal: {local}"
    if url and url not in ("nan", ""):
        caption += f"\nLink: {url}"

    await bot.send_document(chat_id=telegram_id, document=pdf_buf, filename=nome_arquivo, caption=caption)

    rel = cv.get("relatorio_analitico", {})
    if rel:
        score = llm._parse_score(rel.get("match_score", 0))
        gaps = rel.get("analise_gaps", [])
        dica = rel.get("dica_entrevista", "")
        linhas = [f"Relatorio ATS - Vaga {indice}", f"Match Score: {score}/100"]
        if gaps:
            linhas.append("\nGaps identificados:")
            linhas += [f"- {g}" for g in gaps]
            linhas.append(
                "\nDICA DE GAPS: Se voce possui experiencia com alguma destas ferramentas (ou equivalentes), "
                "responda esta mensagem informando. O bot ira incorporar permanentemente ao seu banco de dados."
            )
        if dica:
            linhas.append(f"\nDica para entrevista:\n{dica}")
        await bot.send_message(chat_id=telegram_id, text="\n".join(linhas))

    if job_hash:
        await db.registrar_job_enviado(telegram_id, job_hash, titulo, empresa)


# --- Comandos ---

async def cmd_testar_vagas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Busca vagas e gera CVs automaticamente."""
    if update.callback_query:
        await update.callback_query.answer()
        user_id = str(update.callback_query.from_user.id)
        status_msg = await update.callback_query.edit_message_text(
            "Iniciando varredura de vagas no LinkedIn e Indeed..."
        )
    else:
        user_id = str(update.effective_user.id)
        status_msg = await update.message.reply_text(
            "Iniciando varredura de vagas no LinkedIn e Indeed..."
        )

    usuario = await db.buscar_usuario(user_id)
    perfil = usuario.get("perfil_estruturado") if usuario else None
    if not perfil:
        await status_msg.edit_text(
            "Voce ainda nao tem perfil estruturado. Envie um historico profissional base primeiro."
        )
        return

    cidade = usuario.get("cidade", "Brazil")
    cargo_alvo = usuario.get("cargo_alvo", "")
    senioridade = usuario.get("senioridade", "")
    ingles_fluente = perfil_tem_ingles_fluente(perfil)

    vagas = await scraper.buscar_vagas_jobspy(
        cargo_alvo.strip(), "", cidade, 10, True, ingles_fluente
    )
    if not vagas:
        await status_msg.edit_text("Nenhuma vaga encontrada agora. Tente novamente mais tarde.")
        return

    await status_msg.edit_text("Vagas encontradas. Avaliando aderencia tecnica com IA...")
    melhores = await llm.selecionar_melhores_vagas(perfil, vagas, senioridade)

    novas: list[dict] = []
    for v in melhores:
        ja_enviado = await db.job_ja_enviado(user_id, db.gerar_hash_vaga(v))
        if not ja_enviado:
            novas.append(v)

    if not novas:
        await status_msg.edit_text(
            "As vagas encontradas possuem baixa aderencia ao seu historico ou ja foram enviadas."
        )
        return

    await status_msg.edit_text(f"Match concluido. Gerando {len(novas)} curriculo(s) adaptado(s) em PDF...")
    for i, vaga in enumerate(novas, 1):
        try:
            await processar_e_enviar_vaga(
                bot=context.bot, telegram_id=user_id, usuario=usuario, perfil=perfil,
                titulo=vaga.get("title", ""), empresa=vaga.get("company", ""),
                local=vaga.get("location", ""), descricao=vaga.get("description", ""),
                url=vaga.get("job_url", ""), job_hash=db.gerar_hash_vaga(vaga), indice=i,
            )
        except Exception as e:
            logger.error("cmd_testar_vagas vaga %d: %s", i, e, exc_info=True)
            await context.bot.send_message(chat_id=user_id, text=f"Erro ao processar vaga {i}: {e}")

    await status_msg.delete()


async def cmd_editar_cv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Aplica edicao pontual no ultimo CV gerado."""
    user_id = str(update.effective_user.id)
    cv_atual = context.user_data.get("ultimo_cv")
    usuario = context.user_data.get("ultimo_usuario") or await db.buscar_usuario(user_id)

    if not cv_atual:
        await update.message.reply_text("Nenhum curriculo gerado nesta sessao.")
        return

    partes = update.message.text.split(maxsplit=1)
    instrucao = partes[1].strip() if len(partes) > 1 else ""
    if not instrucao:
        await update.message.reply_text("Informe o que editar apos o comando. Ex: /editar_cv Mude meu titulo")
        return

    status = await update.message.reply_text("Aplicando edicao no curriculo... Aguarde.")
    try:
        cv_novo = await llm.editar_cv_json(cv_atual, instrucao)
        idioma_edit = (usuario or {}).get("idioma", "Portugues")
        pdf_buf = await asyncio.to_thread(gerar_pdf, cv_novo, idioma_edit)
        context.user_data["ultimo_cv"] = cv_novo
        nome_usuario = (usuario or {}).get("nome_completo") or "Candidato"
        nome_arquivo = f"{slug(nome_usuario)}_editado.pdf"
        await update.message.reply_document(
            document=pdf_buf, filename=nome_arquivo, caption="Curriculo atualizado."
        )
        await status.delete()
    except Exception as e:
        logger.error("cmd_editar_cv: %s", e, exc_info=True)
        await status.edit_text(f"Erro ao editar curriculo: {e}")


async def cmd_meu_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe o perfil do usuario."""
    if update.callback_query:
        await update.callback_query.answer()
        user_id = str(update.callback_query.from_user.id)
    else:
        user_id = str(update.effective_user.id)

    usuario = await db.buscar_usuario(user_id)
    if not usuario:
        texto = "Voce ainda nao possui perfil cadastrado. Use /start para comecar."
    else:
        perfil = usuario.get("perfil_estruturado") or {}
        texto = formatar_perfil_texto(usuario, perfil)

    if update.callback_query:
        await update.callback_query.message.reply_text(texto)
    else:
        await update.message.reply_text(texto)


async def cmd_deletar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove todos os dados do usuario."""
    if update.callback_query:
        await update.callback_query.answer()
        user_id = str(update.callback_query.from_user.id)
    else:
        user_id = str(update.effective_user.id)

    try:
        await db.deletar_usuario(user_id)
        msg = "Seus dados foram removidos com sucesso. Use /start para comecar do zero."
    except Exception as e:
        logger.error("cmd_deletar: %s", e, exc_info=True)
        msg = f"Erro ao deletar dados: {e}"

    if update.callback_query:
        await update.callback_query.message.reply_text(msg)
    else:
        await update.message.reply_text(msg)


async def cmd_notificar_pendentes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Notifica usuarios com perfil incompleto."""
    await update.message.reply_text("Iniciando varredura de perfis incompletos...")
    usuarios = await db.buscar_todos_usuarios()
    notificados = 0
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Atualizar Objetivo Agora", callback_data="menu_atualizar_objetivo")]
    ])
    for u in usuarios:
        cargo = u.get("cargo_alvo")
        sen = u.get("senioridade")
        if not cargo or not sen:
            telegram_id = str(u.get("telegram_id", ""))
            if not telegram_id:
                continue
            try:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        "Atencao: Atualizamos nosso motor de inteligencia artificial para "
                        "garantir vagas muito mais precisas.\n\n"
                        "Notamos que o seu perfil esta sem o Cargo Alvo e a Senioridade definidos. "
                        "Sem isso, voce deixara de receber as sugestoes diarias.\n\n"
                        "Clique no botao abaixo para atualizar:"
                    ),
                    reply_markup=keyboard,
                )
                notificados += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error("Notificacao %s: %s", telegram_id, e)
    await update.message.reply_text(f"Varredura concluida. {notificados} usuarios notificados.")
