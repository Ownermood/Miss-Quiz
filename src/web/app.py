"""Flask web app for CLAT Vision Quiz Bot."""

import os
import logging
import asyncio
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from telegram import Update

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# ── Flask app created once at import time ─────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(root_dir, 'templates'),
    static_folder=os.path.join(root_dir, 'static'),
)
_raw_secret = os.environ.get("SESSION_SECRET", "")
if not _raw_secret:
    import secrets as _sec
    _raw_secret = _sec.token_hex(32)
app.secret_key = _raw_secret

# Module-level singletons
quiz_manager   = None
telegram_bot   = None
db_manager     = None
event_loop     = None
app_start_time = datetime.now()


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_coroutine_threadsafe(coro, loop):
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        def _cb(fut):
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Update processing error: {e}", exc_info=True)
        future.add_done_callback(_cb)
    else:
        logger.error("Event loop not running — cannot process update")


def create_app(injected_db=None, injected_quiz=None):
    """Set shared managers. Returns the already-created Flask app."""
    global quiz_manager, db_manager
    if injected_db:
        db_manager   = injected_db
        quiz_manager = injected_quiz
    elif quiz_manager is None:
        try:
            from src.core.database import DatabaseManager
            from src.core.quiz import QuizManager
            db_manager   = DatabaseManager()
            quiz_manager = QuizManager(db_manager=db_manager)
            logger.info("QuizManager initialized in Flask app factory")
        except Exception as e:
            logger.error(f"Failed to initialize managers: {e}")
            raise
    return app


def get_app():
    return app


def _check_api_auth():
    """Return True if the request has a valid API key.
    In production (Heroku DYNO set), an unset key blocks all API access.
    In dev mode (no DYNO), an unset key allows access."""
    api_key = os.environ.get("ADMIN_API_KEY", "")
    if not api_key:
        if os.environ.get("DYNO"):  # Heroku production environment
            logger.warning("[SECURITY] ADMIN_API_KEY not configured — blocking API access")
            return False
        return True  # dev mode — no key configured
    req_key = request.headers.get("X-Admin-Key", "") or request.args.get("api_key", "")
    return bool(req_key and req_key == api_key)


# ── Bot lifecycle (webhook mode) ──────────────────────────────────────────────

async def _bot_lifecycle():
    global telegram_bot
    from src.core.database import DatabaseManager
    from src.core.quiz import QuizManager
    from src.bot.handlers import TelegramQuizBot
    from src.utils.scheduler import AutoQuizScheduler

    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_TOKEN not set")

    owner_id = os.environ.get("OWNER_ID", "")
    if not owner_id or owner_id == "0":
        raise ValueError(
            "OWNER_ID not set or invalid — set OWNER_ID to your Telegram user ID before starting"
        )

    if not os.environ.get("ADMIN_API_KEY"):
        logger.critical(
            "[SECURITY] ADMIN_API_KEY is not set — admin API endpoints are unprotected. "
            "Set ADMIN_API_KEY to a strong random secret."
        )

    mongo_url = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
    db_mgr    = DatabaseManager(mongo_url=mongo_url)
    q_mgr     = QuizManager(db_manager=db_mgr)
    bot       = TelegramQuizBot(q_mgr, db_manager=db_mgr)

    # Share managers with Flask routes
    create_app(injected_db=db_mgr, injected_quiz=q_mgr)

    webhook_url = os.environ.get("WEBHOOK_URL", "")
    render_url  = os.environ.get("RENDER_URL", "")
    final_url   = (render_url or webhook_url).rstrip("/") + "/webhook"

    await bot.initialize_webhook(token, final_url)
    await bot.application.start()
    telegram_bot = bot
    logger.info("✅ Telegram bot ready")
    bot.run_startup_tasks()

    # Auto-quiz scheduler — was missing from webhook mode entirely.
    # Must start AFTER application.start() so bot.application.bot is live.
    scheduler = AutoQuizScheduler(bot, q_mgr, db_manager=db_mgr, interval_minutes=30)
    scheduler.start()
    logger.info("✅ Auto-quiz scheduler started (30-min interval)")

    try:
        await asyncio.Event().wait()  # keep loop alive forever
    finally:
        scheduler.stop()


def init_bot_webhook(webhook_url: str):
    """Non-blocking: starts bot in a background daemon thread."""
    global event_loop

    new_loop   = asyncio.new_event_loop()
    event_loop = new_loop  # set before thread starts

    def _run():
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(_bot_lifecycle())
        except Exception as e:
            logger.error(f"Bot lifecycle error: {e}", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="bot-main").start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'bot': 'CLAT Vision Quiz Bot'})


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'uptime_s': int((datetime.now() - app_start_time).total_seconds())})


@app.route('/admin')
def admin_panel():
    return render_template('admin.html')


@app.route('/webhook', methods=['POST'])
def webhook():
    global telegram_bot, event_loop
    bot_ready  = telegram_bot is not None and telegram_bot.application is not None
    loop_alive = event_loop is not None and event_loop.is_running()
    logger.info(f"[WEBHOOK] POST — bot_ready={bot_ready} loop_running={loop_alive}")
    try:
        if not bot_ready:
            logger.error("[WEBHOOK] bot not ready — returning 500")
            return jsonify({'status': 'error', 'message': 'Bot not initialized'}), 500

        # Validate webhook secret token if configured
        _expected_secret = os.environ.get("WEBHOOK_SECRET_TOKEN", "")
        if _expected_secret:
            received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if received_secret != _expected_secret:
                logger.warning("[WEBHOOK] Invalid secret token — rejecting request")
                return jsonify({'status': 'error', 'message': 'Forbidden'}), 403

        update_data = request.get_json(force=True)
        if not update_data:
            return jsonify({'status': 'ok'}), 200

        logger.info(f"[WEBHOOK] update_id={update_data.get('update_id')}")
        update = Update.de_json(update_data, telegram_bot.application.bot)
        run_coroutine_threadsafe(
            telegram_bot.application.process_update(update), event_loop)
        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        logger.error(f"[WEBHOOK] error: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.route('/api/questions', methods=['GET'])
def api_get_questions():
    if not _check_api_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    if not quiz_manager:
        return jsonify({'error': 'Not ready'}), 500
    return jsonify({'questions': quiz_manager.questions, 'total': len(quiz_manager.questions)})


@app.route('/api/questions', methods=['POST'])
def api_add_question():
    if not _check_api_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        if not quiz_manager:
            return jsonify({'success': False, 'error': 'Not ready'}), 500
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400
        result = quiz_manager.add_questions([{
            'question':       data['question'],
            'options':        data['options'],
            'correct_answer': int(data['correct_answer']),
            'category':       data.get('category', 'General'),
        }])
        added = result.get('added', 0) if isinstance(result, dict) else 0
        if added > 0:
            new_id = None
            if quiz_manager.questions:
                last_q = quiz_manager.questions[-1]
                new_id = last_q.get('id') if isinstance(last_q, dict) else None
            return jsonify({'success': True, 'id': new_id})
        elif result.get('rejected', {}).get('duplicates', 0) > 0:
            return jsonify({'success': False, 'error': 'Duplicate question'}), 409
        else:
            errors = result.get('errors', [])
            return jsonify({'success': False, 'error': errors[0] if errors else 'Unknown'}), 500
    except KeyError as e:
        return jsonify({'success': False, 'error': f'Missing field: {e}'}), 400
    except Exception as e:
        logger.error(f"api_add_question: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/questions/<int:qid>', methods=['PUT'])
def api_edit_question(qid):
    if not _check_api_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        if not quiz_manager:
            return jsonify({'success': False, 'error': 'Not ready'}), 500
        data = request.get_json()
        ok   = quiz_manager.edit_question_by_db_id(qid, data)
        return jsonify({'success': ok}) if ok else (jsonify({'success': False, 'error': 'Not found'}), 404)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/questions/<int:qid>', methods=['DELETE'])
def api_delete_question(qid):
    if not _check_api_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        if not quiz_manager:
            return jsonify({'success': False, 'error': 'Not ready'}), 500
        ok = quiz_manager.delete_question_by_db_id(qid)
        return jsonify({'success': ok}) if ok else (jsonify({'success': False, 'error': 'Not found'}), 404)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/users')
def api_users():
    if not _check_api_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        if not db_manager:
            return jsonify({'users': [], 'error': 'DB not ready'}), 500
        users = db_manager.get_all_users_stats()
        return jsonify({'users': users, 'total': len(users)})
    except Exception as e:
        return jsonify({'users': [], 'error': str(e)}), 500


@app.route('/api/broadcast', methods=['POST'])
def api_broadcast():
    if not _check_api_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        if not db_manager:
            return jsonify({'error': 'DB not ready'}), 500
        data    = request.get_json()
        message = (data or {}).get('message', '').strip()
        if not message:
            return jsonify({'error': 'Empty message'}), 400
        users  = db_manager.get_pm_accessible_users()
        groups = db_manager.get_all_groups()
        return jsonify({
            'status': 'not_sent',
            'users': len(users), 'groups': len(groups),
            'total': len(users) + len(groups),
            'note': 'Broadcast must be triggered via Telegram /broadcast command. This endpoint is for preview only.'
        }), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reload', methods=['POST'])
def api_reload():
    if not _check_api_auth():
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        if not quiz_manager:
            return jsonify({'success': False, 'error': 'Not ready'}), 500
        quiz_manager.reload_data()
        return jsonify({'success': True, 'total': len(quiz_manager.questions)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


