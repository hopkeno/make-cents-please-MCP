"""Charity Record provider: file-based (Charity Record has no API)."""

from .files import Folder
from .tools import register

__all__ = ["Folder", "register"]
