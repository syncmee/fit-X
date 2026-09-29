# SCHEMA REPORT — fitness-app-b0wl.onrender.com

**Command:** `/seo schema https://fitness-app-b0wl.onrender.com` · claude-seo v2.4.1

## Detection

| Page | JSON-LD | Microdata | RDFa |
|---|---|---|---|
| / | ❌ none | ❌ none | ❌ none |
| /login | ❌ none | ❌ none | ❌ none |
| /privacy-policy | ❌ none | ❌ none | ❌ none |
| /terms-and-conditions | ❌ none | ❌ none | ❌ none |

**Result: no structured data of any format on any public page.**

## Validation
Nothing to validate — generation is required.

## Recommendations (per schema-types.md, June 2026 status)
- **WebApplication** — primary entity for a web app (`applicationCategory: HealthApplication`, `operatingSystem: Web`, free `Offer`). ACTIVE, rich-result eligible.
- **Organization** — brand entity with logo + sameAs. ACTIVE.
- **WebSite** — site entity for sitelinks searchbox foundations. ACTIVE.
- ❌ FAQPage — NOT recommended: Google retired FAQ rich results for all sites on 2026-05-07, and the page has no visible FAQ content (markup must mirror visible content). Skipped deliberately.
- ❌ HowTo — deprecated (Sept 2023). Never.
- All generated markup is server-rendered in the initial HTML (per Google's JS-generated structured-data guidance) and contains only truthful, verifiable values.

## Generated code
See `generated-schema.json` (implemented verbatim in `templates/homepage.html`).
