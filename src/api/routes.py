"""Endpoints FastAPI — Webhook do Telegram, Healthcheck e CRON."""

import logging
from typing import Any

from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)

app = FastAPI(title="ATS Resume Bot", docs_url=None, redoc_url=None)


@app.get("/")
@app.get("/health")
async def health() -> dict[str, str]:
    """Healthcheck para UptimeRobot / Render (responde em / e /health)."""
    return {"status": "ok", "service": "ats-resume-bot"}


@app.post("/webhook")
async def telegram_webhook(request: Request) -> Response:
    """Recebe updates do Telegram via webhook."""
    from src.bot.setup import get_application

    application = get_application()
    data = await request.json()

    from telegram import Update
    update = Update.de_json(data, application.bot)
    await application.process_update(update)

    return Response(status_code=200)


@app.post("/cron/daily-jobs")
async def cron_daily_jobs() -> dict[str, Any]:
    """Endpoint CRON para sugestoes diarias — chamado por servico externo (cron-job.org)."""
    from src.bot.setup import get_application
    from src.bot.handlers.cron import executar_sugestoes_diarias

    application = get_application()
    resultado = await executar_sugestoes_diarias(application.bot)
    return resultado
