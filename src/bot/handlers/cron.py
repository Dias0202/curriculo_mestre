"""Logica de CRON — sugestoes diarias de vagas (stateless, acionado via HTTP)."""

import logging
from typing import Any

from src.db import supabase_client as db
from src.services import llm, scraper
from src.bot.handlers.commands import processar_e_enviar_vaga, perfil_tem_ingles_fluente

logger = logging.getLogger(__name__)


async def executar_sugestoes_diarias(bot: Any) -> dict[str, Any]:
    """Executa pipeline de sugestoes diarias para todos os usuarios."""
    logger.info("[CRON] Iniciando sugestoes diarias...")
    usuarios = await db.buscar_todos_usuarios()
    total_enviados = 0
    erros = 0

    for usuario in usuarios:
        telegram_id = str(usuario.get("telegram_id"))
        perfil = usuario.get("perfil_estruturado") or {}
        cidade = usuario.get("cidade", "Brazil")
        cargo_alvo = usuario.get("cargo_alvo", "")
        senioridade = usuario.get("senioridade", "")

        if not telegram_id or not perfil or not cargo_alvo.strip():
            continue

        ingles_fluente = perfil_tem_ingles_fluente(perfil)

        try:
            vagas = await scraper.buscar_vagas_jobspy(
                cargo_alvo.strip(), "", cidade, 10, True, ingles_fluente
            )
            if not vagas:
                continue

            melhores = await llm.selecionar_melhores_vagas(perfil, vagas, senioridade)

            novas: list[dict] = []
            for v in melhores:
                ja_enviado = await db.job_ja_enviado(telegram_id, db.gerar_hash_vaga(v))
                if not ja_enviado:
                    novas.append(v)

            if not novas:
                continue

            await bot.send_message(
                chat_id=telegram_id,
                text=f"Bom dia. Suas {len(novas)} sugestao(es) de hoje com curriculo adaptado:",
            )

            for i, vaga in enumerate(novas, 1):
                try:
                    await processar_e_enviar_vaga(
                        bot=bot, telegram_id=telegram_id, usuario=usuario, perfil=perfil,
                        titulo=vaga.get("title", ""), empresa=vaga.get("company", ""),
                        local=vaga.get("location", ""), descricao=vaga.get("description", ""),
                        url=vaga.get("job_url", ""), job_hash=db.gerar_hash_vaga(vaga), indice=i,
                    )
                    total_enviados += 1
                except Exception as e:
                    logger.error("[CRON] Erro vaga %d user %s: %s", i, telegram_id, e, exc_info=True)
                    erros += 1

        except Exception as e:
            logger.error("[CRON] Erro user %s: %s", telegram_id, e, exc_info=True)
            erros += 1

    logger.info("[CRON] Concluido: %d enviados, %d erros", total_enviados, erros)
    return {"enviados": total_enviados, "erros": erros, "usuarios": len(usuarios)}
