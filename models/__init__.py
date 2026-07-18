"""Model package — importing registers all tables on Base.metadata."""

from .base import Base
from .user import User
from .task import Task
from .user_profile import UserProfile
from .user_memory import UserMemory
from .reminder import Reminder

__all__ = [
    "Base", "User", "Task", "UserProfile", "UserMemory",
    "Reminder",
]
