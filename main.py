"""Entrypoint — inicializa FastAPI + Bot Telegram (Webhook em producao, Polling local)."""

import asyncio
import logging

import uvicorn

from src.core.config import settings
from src.core.logging import setup_logging
from src.api.routes import app
from src.bot.setup import get_application

logger = logging.getLogger(__name__)


async def start_webhook() -> None:
    """Configura webhook do Telegram e inicia FastAPI via uvicorn."""
    application = get_application()
    await application.initialize()
    await application.start()

    # Limpa webhook anterior antes de registrar o novo
    await application.bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{settings.webhook_url}/webhook"
    await application.bot.set_webhook(url=webhook_url)
    logger.info("Webhook configurado: %s", webhook_url)

    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=settings.port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def _delete_webhook() -> None:
    """Remove webhook ativo para permitir modo polling."""
    application = get_application()
    await application.initialize()
    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook anterior removido para modo polling.")
    await application.shutdown()


def start_polling() -> None:
    """Inicia o bot em modo polling (desenvolvimento local)."""
    # Limpa qualquer webhook ativo antes de iniciar polling
    asyncio.run(_delete_webhook())

    application = get_application()
    logger.info("Modo POLLING (desenvolvimento local) na porta %d", settings.port)

    # Inicia servidor health em background thread
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ATS Bot Operacional")

        def log_message(self, *a):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", settings.port), HealthHandler).serve_forever(),
        daemon=True,
    ).start()

    from telegram import Update
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    """Entrypoint principal — escolhe modo baseado na presenca de WEBHOOK_URL."""
    setup_logging()
    logger.info("ATS Resume Bot iniciando...")

    if settings.webhook_url:
        logger.info("WEBHOOK_URL detectada — modo WEBHOOK + FastAPI")
        asyncio.run(start_webhook())
    else:
        logger.info("WEBHOOK_URL ausente — modo POLLING (dev)")
        start_polling()


if __name__ == "__main__":
    main()
