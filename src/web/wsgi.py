"""WSGI entry point — supports both webhook and polling mode via env vars."""

import os
import logging
import asyncio
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from src.web.app import app, init_bot_webhook  # noqa: E402

_token       = os.environ.get("TELEGRAM_TOKEN", "")
_mode        = os.environ.get("MODE", "polling").lower()
_webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
_render_url  = os.environ.get("RENDER_URL", "").rstrip("/")
_base_url    = _render_url or _webhook_url

if not _token:
    logger.warning("⚠️  TELEGRAM_TOKEN not set — bot will not start")

elif _base_url or _mode == "webhook":
    # ── Webhook mode ─────────────────────────────────────────
    if not _base_url:
        logger.error("❌ MODE=webhook but WEBHOOK_URL not set — bot not started")
    else:
        logger.info(f"🌐 Webhook mode → {_base_url}/webhook")
        init_bot_webhook(_base_url)
        logger.info("✅ Bot webhook thread started")

else:
    # ── Polling mode ─────────────────────────────────────────
    logger.info("🔄 Polling mode — starting bot in background thread")

    def _run_polling():
        async def _lifecycle():
            from src.core.database import DatabaseManager
            from src.core.quiz import QuizManager
            from src.bot.handlers import TelegramQuizBot

            mongo_url = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
            db_mgr = DatabaseManager(mongo_url=mongo_url)
            q_mgr  = QuizManager(db_manager=db_mgr)
            bot    = TelegramQuizBot(q_mgr, db_manager=db_mgr)

            # Share managers with Flask routes
            from src.web.app import create_app
            create_app(injected_db=db_mgr, injected_quiz=q_mgr)

            await bot.initialize(_token)
            logger.info("✅ Bot initialized — polling started")

            async with bot.application:
                await bot.application.start()
                bot.run_startup_tasks()
                await bot.application.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["message", "poll_answer", "callback_query",
                                     "my_chat_member", "chat_member"],
                )
                await asyncio.Event().wait()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_lifecycle())
        except Exception as e:
            logger.error(f"Polling bot error: {e}", exc_info=True)

    threading.Thread(target=_run_polling, daemon=True, name="bot-polling").start()
    logger.info("✅ Bot polling thread started")

logger.info("✅ WSGI app ready")

# 'app' is re-exported so Gunicorn (src.web.wsgi:app) finds it
