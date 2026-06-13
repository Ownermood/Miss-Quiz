"""Rate Limiter for Telegram Quiz Bot

This module implements a comprehensive rate limiting system to prevent command spam
and abuse while maintaining smooth UX for legitimate users. It uses a sliding window
algorithm to track command usage per user with different limits for different command types.
"""

import time
import logging
from collections import defaultdict, deque
from threading import Lock
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)

RATE_LIMITS = {
    'heavy': {
        'per_minute': 5,
        'per_hour': 20,
        'commands': ['quiz', 'broadcast', 'addquiz', 'delquiz', 'editquiz']
    },
    'medium': {
        'per_minute': 10,
        'per_hour': 50,
        'commands': ['mystats', 'leaderboard', 'category', 'stats', 'status', 'myrank']
    },
    'light': {
        'per_minute': 15,
        'per_hour': -1,
        'commands': ['start', 'help', 'ping']
    }
}


class RateLimiter:
    """Rate limiter with sliding window algorithm for command spam prevention"""
    
    def __init__(self):
        """Initialize rate limiter with in-memory storage"""
        self.user_commands: Dict[int, Dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
        self.lock = Lock()
        self.ttl_seconds = 3600
        logger.info("RateLimiter initialized with sliding window algorithm")
    
    def _get_command_limits(self, command: str) -> Optional[Tuple[int, int, str]]:
        """Get rate limits for a specific command
        
        Args:
            command: Command name (without /)
            
        Returns:
            Tuple of (per_minute_limit, per_hour_limit, limit_type) or None if not found
        """
        for limit_type, config in RATE_LIMITS.items():
            if command in config['commands']:
                return config['per_minute'], config['per_hour'], limit_type
        return None
    
    def _cleanup_old_timestamps(self, timestamps: deque, window_seconds: int) -> None:
        """Remove timestamps older than the specified window
        
        Args:
            timestamps: Deque of timestamps
            window_seconds: Time window in seconds
        """
        current_time = time.time()
        cutoff_time = current_time - window_seconds
        
        while timestamps and timestamps[0] < cutoff_time:
            timestamps.popleft()
    
    def check_limit(self, user_id: int, command: str, is_developer: bool = False) -> Tuple[bool, int, str]:
        """Check if user has exceeded rate limit for command
        
        Args:
            user_id: Telegram user ID
            command: Command name (without /)
            is_developer: Whether user is a developer (bypasses limits)
            
        Returns:
            Tuple of (allowed: bool, wait_seconds: int, limit_type: str)
        """
        if is_developer:
            logger.debug(f"Developer {user_id} bypasses rate limit for /{command}")
            return True, 0, "developer"
        
        limits = self._get_command_limits(command)
        if not limits:
            logger.debug(f"No rate limits configured for /{command}, allowing")
            return True, 0, "unlimited"
        
        per_minute, per_hour, limit_type = limits
        
        with self.lock:
            timestamps = self.user_commands[user_id][command]
            current_time = time.time()
            
            self._cleanup_old_timestamps(timestamps, 3600)
            
            minute_timestamps = [ts for ts in timestamps if ts > current_time - 60]
            hour_timestamps = list(timestamps)
            
            if len(minute_timestamps) >= per_minute:
                oldest_in_minute = minute_timestamps[0]
                wait_seconds = int(60 - (current_time - oldest_in_minute)) + 1
                logger.warning(
                    f"Rate limit exceeded for user {user_id}, command /{command}: "
                    f"{len(minute_timestamps)}/{per_minute} per minute"
                )
                return False, wait_seconds, f"{limit_type}_minute"
            
            if per_hour > 0 and len(hour_timestamps) >= per_hour:
                oldest_in_hour = hour_timestamps[0]
                wait_seconds = int(3600 - (current_time - oldest_in_hour)) + 1
                logger.warning(
                    f"Rate limit exceeded for user {user_id}, command /{command}: "
                    f"{len(hour_timestamps)}/{per_hour} per hour"
                )
                return False, wait_seconds, f"{limit_type}_hour"
            
            logger.debug(
                f"Rate limit check passed for user {user_id}, /{command}: "
                f"{len(minute_timestamps)}/{per_minute} per min, "
                f"{len(hour_timestamps)}/{per_hour if per_hour > 0 else 'unlimited'} per hour"
            )
            return True, 0, limit_type
    
    def record_command(self, user_id: int, command: str) -> None:
        """Record command execution for rate limiting
        
        Args:
            user_id: Telegram user ID
            command: Command name (without /)
        """
        with self.lock:
            current_time = time.time()
            self.user_commands[user_id][command].append(current_time)
            logger.debug(f"Recorded command /{command} for user {user_id}")
    
    def cleanup_old_entries(self) -> int:
        """Remove entries older than TTL (1 hour)
        
        Returns:
            Number of entries cleaned up
        """
        cleaned_count = 0
        current_time = time.time()
        cutoff_time = current_time - self.ttl_seconds
        
        with self.lock:
            users_to_remove = []
            
            for user_id, commands in list(self.user_commands.items()):
                commands_to_remove = []
                
                for command, timestamps in list(commands.items()):
                    self._cleanup_old_timestamps(timestamps, self.ttl_seconds)
                    
                    if not timestamps:
                        commands_to_remove.append(command)
                        cleaned_count += 1
                
                for command in commands_to_remove:
                    del commands[command]
                
                if not commands:
                    users_to_remove.append(user_id)
            
            for user_id in users_to_remove:
                del self.user_commands[user_id]
        
        if cleaned_count > 0:
            logger.info(f"Rate limiter cleanup: removed {cleaned_count} old command entries")
        
        return cleaned_count
    
    def get_user_stats(self, user_id: int) -> Dict[str, Dict[str, int]]:
        """Get rate limit stats for user (for debugging and /mystats)
        
        Args:
            user_id: Telegram user ID
            
        Returns:
            Dictionary of command stats with counts per minute/hour
        """
        stats = {}
        current_time = time.time()
        
        with self.lock:
            if user_id not in self.user_commands:
                return stats
            
            for command, timestamps in self.user_commands[user_id].items():
                self._cleanup_old_timestamps(timestamps, 3600)
                
                minute_count = len([ts for ts in timestamps if ts > current_time - 60])
                hour_count = len(timestamps)
                
                limits = self._get_command_limits(command)
                if limits:
                    per_minute, per_hour, limit_type = limits
                    stats[command] = {
                        'minute_count': minute_count,
                        'minute_limit': per_minute,
                        'hour_count': hour_count,
                        'hour_limit': per_hour if per_hour > 0 else -1,
                        'limit_type': limit_type
                    }
        
        return stats
    
    def get_total_stats(self) -> Dict[str, int]:
        """Get overall rate limiter statistics
        
        Returns:
            Dictionary with total users and commands tracked
        """
        with self.lock:
            total_users = len(self.user_commands)
            total_commands = sum(len(commands) for commands in self.user_commands.values())
            total_entries = sum(
                sum(len(timestamps) for timestamps in commands.values())
                for commands in self.user_commands.values()
            )
        
        return {
            'total_users': total_users,
            'total_commands': total_commands,
            'total_entries': total_entries
        }
