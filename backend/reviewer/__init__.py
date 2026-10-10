"""Reviewer gate support modules.

Webhook ingress routes through ``events.ingress``; the reviewer gate calls
``reviewer.on_pr_review.on_pr_review`` directly when the CI gate is ready.
"""
