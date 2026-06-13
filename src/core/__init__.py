"""
src.core — Core application package.

Public surface:
  Config          — environment-based configuration
  DatabaseManager — MongoDB data access layer  (lazy: requires pymongo)
  QuizManager     — in-memory quiz cache + business logic
  exceptions      — QuizBotError, ConfigurationError, DatabaseError, etc.
"""

from src.core.config import Config
from src.core import exceptions

__all__ = ["Config", "exceptions"]


def get_database_manager():
    from src.core.database import DatabaseManager
    return DatabaseManager


def get_quiz_manager():
    from src.core.quiz import QuizManager
    return QuizManager
