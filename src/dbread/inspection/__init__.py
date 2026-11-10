"""Whole-database inspection for SQL connections.

Pure builders and reshapers live here; `dbread.inspection_tools` wires them
into the tool surface so every statement that reads rows or statistics views
still runs through `ToolHandlers.query` (guard, cost guard, rate limit,
audit, statement timeout).
"""
