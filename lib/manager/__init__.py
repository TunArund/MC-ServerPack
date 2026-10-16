"""Terminal-side Manager adapter."""

from .modpacks import ModpackError
from .service import ManagerService

__all__ = ["ManagerService", "ModpackError"]
