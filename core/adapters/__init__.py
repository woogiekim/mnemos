"""mnemos host adapters package.

Exports the abstract HostAdapter base class and the two built-in implementations:
ClaudeCodeAdapter and CursorAdapter.
"""
from core.adapters.base import HostAdapter
from core.adapters.claude import ClaudeCodeAdapter
from core.adapters.cursor import CursorAdapter

__all__ = ["HostAdapter", "ClaudeCodeAdapter", "CursorAdapter"]
