"""Obligation Graph store (shared).

The single read/write contract for all five agents: people as nodes, open loops
as directed edges, with central validation, last-write-wins, persistence
guarantees, and the workspace boundary guard (Req 1, 14). Scaffolding only.
"""
