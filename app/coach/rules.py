"""Rule-based coach entry point."""


def process_message(user, message_text: str, now=None):
    from app import dashboard
    return dashboard.process_coach_message_rules(user, message_text, now)
