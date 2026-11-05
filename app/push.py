"""Push delivery public interface.

The current implementation remains route-adjacent until notification cron
handlers move; this module prevents future callers from importing dashboard.
"""


def send_web_push(user, **kwargs) -> bool:
    from app import dashboard
    return dashboard._send_web_push(user, **kwargs)
