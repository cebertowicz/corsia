"""Prompt text library — all prompt constants and text formatting functions.

This package separates prompt text from logic code to:
1. Reduce context window usage for local LLMs
2. Avoid accidental prompt injection (model editing code that contains
   instructions meant for other agents)

Each module contains ONLY text constants and text formatting functions.
No DB queries, no LLM calls, no business logic.
"""
