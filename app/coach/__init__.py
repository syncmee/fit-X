"""AI-coach components: provider transport, prompt construction, rule-based
parsing and structured-action application.

Shared primitives live here so dashboard routes, the rules layer and the
providers can import them without cycles.
"""
from dataclasses import dataclass


@dataclass
class CoachOutcome:
    reply: str
    action: str = "general"


class CoachAIError(Exception):
    """Raised when an AI provider call fails or returns unusable output."""


# Shown once when the user logs/schedules a workout without having told the
# app where they train; the answer routes through the rules layer.
ENVIRONMENT_ASK = "One quick thing: where will you usually train — gym, home, or home with equipment? Tap a chip below."
