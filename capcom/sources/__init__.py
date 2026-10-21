"""capcom.sources: log adapters that turn log files into normalized records
(see docs/design.md). One adapter per source; Claude Code's is `ClaudeCodeSource`."""

from capcom.sources.claude_code import ClaudeCodeSource

__all__ = ["ClaudeCodeSource"]
