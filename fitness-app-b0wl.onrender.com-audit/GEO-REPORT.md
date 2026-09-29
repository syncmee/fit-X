# GEO REPORT (AI Search Readiness) — fitness-app-b0wl.onrender.com

**Command:** `/seo geo https://fitness-app-b0wl.onrender.com` · claude-seo v2.4.1 (May 2026 criteria)

## GEO Readiness Score: 52/100

## AI Crawler Access Status
robots.txt returns 404 — by RFC 9309, absent robots.txt means **all crawlers are allowed by default**. Nothing is blocked, but access is also *undeclared*, and there is no sitemap to guide crawlers. Reported per-crawler (access is currently uniform "allow by omission"):

| Crawler | Governs | Current status |
|---|---|---|
| OAI-SearchBot | ChatGPT Search citability | Allowed (by omission) |
| Claude-SearchBot | Claude search citability | Allowed (by omission) |
| PerplexityBot | Perplexity AI search | Allowed (by omission) |
| Googlebot | Google Search + AI Overviews/AI Mode | Allowed (by omission) |
| GPTBot / ClaudeBot / CCBot / Applebot-Extended | Model training only | Allowed (licensing preference — owner's call, not a search-visibility issue) |
| Google-Extended | Gemini/Vertex grounding & training | Allowed (same) |

Recommendation implemented: an explicit robots.txt that keeps everything allowed, denies the two auth-gated paths, declares the sitemap, and names the key AI search crawlers so access is a stated policy, not an accident.

## llms.txt Status
**Missing** (404). Per Google's AI optimization guide (2026-05-15, clarified 2026-06-15) llms.txt has no Google Search effect; it may serve non-Google agents. Added as a low-cost courtesy file with key facts and page map.

## Citability / structural signals (qualitative — no third-party measurement tool configured)
- ✅ Server-side rendered HTML: content visible to raw-HTML AI crawlers (GPTBot, PerplexityBot fetch without JS)
- ✅ Clear H1/H2 hierarchy, specific stats on the homepage (14,827 sessions, 47-day streaks, 90-second onboarding)
- ✅ WebApplication + Organization + WebSite JSON-LD added (AI systems lean on structured data for entity grounding)
- ⚠️ No self-contained "What is fiT-X?" answer block in the first 60 words — the hero is headline-style. Left as-is (adding visible copy would change the UI); the meta description + JSON-LD `description` now carry the definitional sentence instead.
- ⚠️ No brand mentions yet on Wikipedia/Reddit/YouTube (brand mentions correlate ~3× more strongly with AI visibility than backlinks) — out of code scope, listed in the action plan.

## Top 5 highest-impact changes (from this audit)
1. Explicit robots.txt with sitemap + AI-crawler policy
2. JSON-LD entity markup (WebApplication/Organization/WebSite)
3. Meta descriptions + canonicals so AI systems quote consistent snippets
4. og:/twitter: tags so shared/cited previews render the brand
5. llms.txt with key facts for non-Google agents
