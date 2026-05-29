"""Harvest — full-dataset extraction stage following recon.

Reuses src/agent/session.py AgentSession with a different system prompt
and an expanded tool set (recon 14 + apply_patch + mark_done).
"""
