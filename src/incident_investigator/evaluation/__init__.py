"""Evaluation (ticket 09): run both systems on every incident, score, and summarise.

Besides `dataset.py`, this package is the only code allowed to load answer keys
(tests/test_answer_key_isolation.py). Nothing under it is imported by agent,
graph, tool or LLM code.
"""
