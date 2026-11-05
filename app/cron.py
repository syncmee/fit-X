"""Cron service interface for reminder and engagement jobs."""


def send_workout_reminders() -> dict:
    from app import dashboard
    return dashboard._send_due_workout_reminders()


def send_challenge_reminders() -> dict:
    from app import dashboard
    return dashboard.send_due_challenge_reminders()


def send_engagement_nudges(ignore_hour: bool = False) -> dict:
    from app import dashboard
    return dashboard.send_due_engagement_nudges(ignore_hour=ignore_hour)
