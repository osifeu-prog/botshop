from __future__ import annotations

import asyncio
from typing import Optional

from telegram.ext import Application, ApplicationBuilder

from .config import Config
from core.logging import logger


class TelegramAppManager:
    _app: Optional[Application] = None
    _initialized: bool = False

    @classmethod
    async def start(cls):
        """Create and initialize the Telegram Application once.

        We do NOT run polling here – only webhook-style processing.
        """
        if cls._app is not None and cls._initialized:
            return

        logger.info("Initializing Telegram Application")
        builder = ApplicationBuilder().token(Config.BOT_TOKEN)
        app = builder.build()

        # Register handlers
        from .handlers.commands import register_command_handlers
        from .handlers.callbacks import register_callback_handlers

        register_command_handlers(app)
        register_callback_handlers(app)

        await app.initialize()
        await app.start()

        cls._app = app
        cls._initialized = True
        logger.info("Telegram Application initialized and started")

    @classmethod
    def get_app(cls) -> Application:
        if cls._app is None:
            raise RuntimeError("Telegram Application instance not initialized")
        return cls._app

    @classmethod
    async def stop(cls):
        if cls._app is not None:
            logger.info("Stopping Telegram Application")
            await cls._app.stop()
            await cls._app.shutdown()
            cls._app = None
            cls._initialized = False
