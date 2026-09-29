"""Single home for SEO constants: canonical site URL, JSON-LD graph,
robots.txt, llms.txt, and the sitemap's public page list.

SITE_URL comes from the environment so a custom domain only needs a config
change; every canonical/OG/schema URL is built from it.
"""
from __future__ import annotations

import os
from datetime import date

# Canonical origin: used for canonical links, Open Graph URLs, JSON-LD ids,
# and the sitemap. Set SITE_URL when a custom domain takes over.
SITE_URL = os.getenv("SITE_URL", "https://fitness-app-b0wl.onrender.com").rstrip("/")

# Bump alongside content releases; a stale lastmod is worse than a fresh
# deploy date, so this is updated by the same commit that changes content.
SITEMAP_LASTMOD = date(2026, 9, 29).isoformat()

# Canonical, indexable pages. Auth-gated routes (/dashboard, /onboarding)
# are deliberately excluded — they redirect to /login. <priority> and
# <changefreq> are omitted from the sitemap: Google ignores both.
SITEMAP_PAGES = ("/", "/login", "/privacy-policy", "/terms-and-conditions")

_JSONLD_URLS = {
    "org": f"{SITE_URL}/#organization",
    "website": f"{SITE_URL}/#website",
    "app": f"{SITE_URL}/#app",
}

JSONLD_GRAPH = {
    "@context": "https://schema.org",
    "@graph": [
        {
            "@type": "Organization",
            "@id": _JSONLD_URLS["org"],
            "name": "fiT-X",
            "url": f"{SITE_URL}/",
            "logo": {"@type": "ImageObject", "url": f"{SITE_URL}/static/android-chrome-512x512.png"},
        },
        {
            "@type": "WebSite",
            "@id": _JSONLD_URLS["website"],
            "url": f"{SITE_URL}/",
            "name": "fiT-X",
            "publisher": {"@id": _JSONLD_URLS["org"]},
            "inLanguage": "en",
        },
        {
            "@type": "WebApplication",
            "@id": _JSONLD_URLS["app"],
            "name": "fiT-X",
            "url": f"{SITE_URL}/",
            "applicationCategory": "HealthApplication",
            "operatingSystem": "Web",
            "browserRequirements": "Requires JavaScript",
            "description": (
                "AI-coached fitness tracker: log meals, workouts, water and weight in plain "
                "language, with daily calorie targets, gym session plans and streaks."
            ),
            "featureList": [
                "Natural-language meal, workout, water and weight logging",
                "AI coach (Gemini with Groq fallback) with rule-based safety net",
                "Daily calorie targets from Mifflin-St Jeor + goal pacing",
                "Gym session plans composed from the workout title's muscle groups",
                "Streaks, activity heatmap, weight trend and weekly net-calorie charts",
                "Workout reminder emails timed to the user's local timezone",
            ],
            "inLanguage": "en",
            "isAccessibleForFree": True,
            "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
            "publisher": {"@id": _JSONLD_URLS["org"]},
        },
    ],
}

ROBOTS_TXT = f"""# fiT-X robots.txt
User-agent: *
Disallow: /dashboard
Disallow: /onboarding

# AI search crawlers — explicit so access is policy, not an accident.
# (Training-only crawlers such as GPTBot, ClaudeBot, CCBot and Google-Extended
# are also allowed; block them here if licensing preference changes.)
User-agent: OAI-SearchBot
Allow: /

User-agent: Claude-SearchBot
Allow: /

User-agent: PerplexityBot
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
"""

LLMS_TXT = f"""# fiT-X

> fiT-X is an AI-coached fitness web app: members log meals, workouts, water and
> weight by typing plain sentences; an AI coach (Google Gemini, Groq fallback)
> parses them into structured logs and keeps the score.

## Key facts
- Natural-language logging for meals, gym and cardio sessions, water, and weight
- Daily calorie targets from Mifflin-St Jeor with goal pacing (cut / maintain / bulk)
- Gym session plans composed from the workout title's muscle groups
- Streaks, activity heatmap, weight trend and weekly net-calorie charts
- Workout reminder emails timed to each member's local timezone
- Free to use; sign-up by email or Google

## Pages
- [Home]({SITE_URL}/): product overview and live demo charts
- [Sign in]({SITE_URL}/login): member sign-in and registration
- [Privacy Policy]({SITE_URL}/privacy-policy): what data fiT-X collects and how it is used
- [Terms and Conditions]({SITE_URL}/terms-and-conditions): the terms of using fiT-X
"""


def og_image_url() -> str:
    return f"{SITE_URL}/static/og-image.png"
