"""Custom exceptions for the Quiz Bot application.

This module provides a hierarchy of exceptions for better error handling
and debugging throughout the quiz bot application. All custom exceptions
inherit from QuizBotError for easy exception handling.
"""


class QuizBotError(Exception):
    """Base exception for all quiz bot errors.
    
    This is the parent class for all custom exceptions in the quiz bot.
    Catching this exception will handle all quiz bot-specific errors.
    Use this when you need to catch any quiz bot error generically.
    """
    pass


class ConfigurationError(QuizBotError, ValueError):
    """Raised when configuration is invalid or missing.
    
    This exception is raised when:
    - Required environment variables are missing
    - Configuration values are invalid or malformed
    - Configuration validation fails
    
    Inherits from both QuizBotError and ValueError for compatibility.
    """
    pass


class DatabaseError(QuizBotError):
    """Raised when database operations fail.
    
    This exception is raised when:
    - Database connection cannot be established
    - SQL queries fail to execute
    - Database transactions need to be rolled back
    - Data integrity constraints are violated
    """
    pass


class QuestionNotFoundError(QuizBotError):
    """Raised when no questions are available.
    
    This exception is raised when:
    - The question database is empty
    - No questions match the requested category
    - All questions have been recently used in a chat
    """
    pass


class ValidationError(QuizBotError):
    """Raised when input validation fails.
    
    This exception is raised when:
    - User input does not match expected format
    - Question data structure is invalid
    - Required fields are missing from input
    - Data types are incorrect
    """
    pass
