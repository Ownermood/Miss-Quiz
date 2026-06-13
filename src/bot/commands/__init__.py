"""
src.bot.commands — Command handler mixins.

Each mixin is a standalone class with no shared state.
TelegramQuizBot in handlers_main.py inherits all of them via MRO.
"""

from src.bot.commands.user_cmds import UserCommandsMixin
from src.bot.commands.quiz_cmds import QuizCommandsMixin
from src.bot.commands.leaderboard_cmds import LeaderboardMixin
from src.bot.commands.admin_cmds import AdminCommandsMixin

__all__ = [
    "UserCommandsMixin",
    "QuizCommandsMixin",
    "LeaderboardMixin",
    "AdminCommandsMixin",
]
