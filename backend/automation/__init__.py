"""Reliability automations (coordination MVP S7).

Expected-event watches, plan/thread liveness, and startup re-arm / restart drain.
These are the platform reliability primitives — NOT broad user-authored
automations and not a config UI. A missing expected event routes through Event
Delivery (S4b) as a delivery_failure, never a silent success.
"""
