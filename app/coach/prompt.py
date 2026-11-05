"""Coach prompt entry point.

The compatibility shim is intentionally explicit: prompt assembly still needs
dashboard-derived activity context until that context builder moves in the next
small extraction.
"""


def build_system_prompt(user, now, message_text: str = "") -> str:
    from app import dashboard
    return dashboard._ai_system_prompt(user, now, message_text)
