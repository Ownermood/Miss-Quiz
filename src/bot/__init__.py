"""
src.bot — Telegram bot package.

Public surface:
  TelegramQuizBot  — main bot class (assembles all handler mixins)
"""

from src.bot.handlers_main import TelegramQuizBot

__all__ = ["TelegramQuizBot"]
