"""Model package — importing registers all tables on Base.metadata."""

from .base import Base
from .user import User
from .task import Task

__all__ = ["Base", "User", "Task"]
