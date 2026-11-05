"""Structured action application entry point."""


def apply_actions(user, actions: list, now):
    from app import dashboard
    return dashboard._apply_ai_actions(user, actions, now)
