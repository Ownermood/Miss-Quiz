"""Configuration management for Telegram Quiz Bot.

This module handles loading, validation, and management of bot configuration
from environment variables. It supports auto-detection of deployment mode
(webhook vs polling) based on available environment variables and provides
a centralized interface for accessing configuration throughout the application.
"""

import os
import logging
from dataclasses import dataclass
from typing import Optional, List
from pathlib import Path
from src.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv  # type: ignore
    env_path = Path('.') / '.env'
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("Loaded environment variables from .env file")
except ImportError:
    logger.debug("python-dotenv not installed, using system environment variables only")

@dataclass
class Config:
    """Bot configuration with auto-detection of deployment mode.
    
    This class manages all configuration settings for the Telegram Quiz Bot.
    It automatically loads configuration from environment variables and
    determines the appropriate deployment mode (webhook or polling) based
    on the presence of webhook-related environment variables.
    
    Attributes:
        telegram_token (str): Telegram Bot API token for authentication
        session_secret (str): Secret key for session management
        owner_id (int): Telegram user ID of the bot owner
        wifu_id (Optional[int]): Optional additional authorized user ID
        webhook_url (Optional[str]): URL for webhook mode deployment
        render_url (Optional[str]): Render.com deployment URL (takes precedence)
        host (str): Host address for web server (default: 0.0.0.0)
        port (int): Port number for web server
        database_path (str): Path to SQLite database file
        database_url (Optional[str]): PostgreSQL database URL
    """
    telegram_token: str
    session_secret: str
    owner_id: int
    wifu_id: Optional[int]
    webhook_url: Optional[str]
    render_url: Optional[str]
    host: str
    port: int
    database_path: str
    mongodb_url: Optional[str]
    
    @classmethod
    def load(cls, validate: bool = False) -> 'Config':
        """Load configuration from environment variables.
        
        Reads configuration from environment variables and creates a Config
        instance. Optionally validates required fields immediately.
        
        Args:
            validate (bool): If True, validates required fields immediately.
                           If False (default), validation happens on access.
        
        Returns:
            Config: Configured instance with settings from environment
        
        Raises:
            ConfigurationError: If validate=True and required fields are missing
        """
        telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
        session_secret = os.environ.get("SESSION_SECRET", "")
        
        owner_id = int(os.environ.get("OWNER_ID", "8403136097"))
        if owner_id == 0 and validate:
            logger.warning("⚠️ OWNER_ID not set - bot will work but admin features disabled")
        
        wifu_id = None
        wifu_id_str = os.environ.get("WIFU_ID")
        if wifu_id_str:
            try:
                wifu_id = int(wifu_id_str)
            except ValueError:
                logger.warning(f"WIFU_ID environment variable is invalid: {wifu_id_str}")
        
        webhook_url = os.environ.get("WEBHOOK_URL")
        render_url = os.environ.get("RENDER_URL")
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "5000"))
        database_path = os.path.abspath(os.environ.get("DATABASE_PATH", "data/quiz_bot.db"))
        mongodb_url = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
        
        config = cls(
            telegram_token=telegram_token,
            session_secret=session_secret,
            owner_id=owner_id,
            wifu_id=wifu_id,
            webhook_url=webhook_url,
            render_url=render_url,
            host=host,
            port=port,
            database_path=database_path,
            mongodb_url=mongodb_url
        )
        
        if validate:
            config.validate()
        
        return config
    
    def validate(self):
        """Validate required configuration fields."""
        if not self.telegram_token:
            raise ConfigurationError("TELEGRAM_TOKEN environment variable is required")
        if not self.session_secret:
            import secrets
            self.session_secret = secrets.token_hex(32)
            logger.warning("SESSION_SECRET not set — auto-generated for this session")
    
    def get_mode(self) -> str:
        """Auto-detect deployment mode based on environment.
        
        Determines whether the bot should run in webhook or polling mode
        based on the presence of webhook-related environment variables.
        
        Returns:
            str: 'webhook' if webhook_url or render_url is set, 
                 'polling' otherwise.
        """
        if self.webhook_url or self.render_url:
            return "webhook"
        return "polling"
    
    def get_webhook_url(self) -> Optional[str]:
        """Get the webhook URL for deployment.
        
        Returns the appropriate webhook URL, with RENDER_URL taking
        precedence over WEBHOOK_URL if both are set.
        
        Returns:
            Optional[str]: The webhook URL with /webhook path if available, None otherwise
        """
        base_url = self.render_url or self.webhook_url
        if base_url:
            base_url = base_url.rstrip('/')
            return f"{base_url}/webhook"
        return None
    
    def get_authorized_users(self) -> List[int]:
        """Get list of authorized user IDs.
        
        Returns a list of Telegram user IDs that have administrative
        access to the bot. Includes owner_id and wifu_id if set.
        
        Returns:
            List[int]: List of authorized Telegram user IDs
        """
        users: List[int] = [self.owner_id] if self.owner_id else []
        if self.wifu_id:
            users.append(self.wifu_id)
        return users

OWNER_ID = int(os.environ.get("OWNER_ID", "8403136097"))
WIFU_ID = None
wifu_id_str = os.environ.get("WIFU_ID")
if wifu_id_str:
    try:
        WIFU_ID = int(wifu_id_str)
    except ValueError:
        pass

AUTHORIZED_USERS: List[int] = [OWNER_ID] if OWNER_ID else []
if WIFU_ID:
    AUTHORIZED_USERS.append(WIFU_ID)

UNAUTHORIZED_MESSAGE = (
    "🔒 ACCESS RESTRICTED\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "  This command requires elevated privileges.\n"
    "  Owner or Developer access only.\n\n"
    "──────────────────────────\n"
    "  Contact @CLAT_Vision for access."
)

DATABASE_PATH = os.path.abspath(os.environ.get("DATABASE_PATH", "data/quiz_bot.db"))
