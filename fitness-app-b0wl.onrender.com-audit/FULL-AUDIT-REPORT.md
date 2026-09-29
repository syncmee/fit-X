# FULL SEO AUDIT — fitness-app-b0wl.onrender.com

**Tool:** claude-seo v2.4.1 (`/seo audit`) · **Date:** 2026-09-29 · **Crawl:** 4 public pages + 2 auth-gated (302 → /login), robots.txt respected

**Business type detected:** SaaS / web application (consumer fitness, freemium)
**Rendering:** Server-rendered Jinja HTML ✓ (85.6 KB homepage) — good for crawlers and AI agents

## SEO Health Score: 46/100

| Category | Weight | Score | Notes |
|---|---|---|---|
| Technical SEO | 22% | 45 | No robots.txt, no sitemap, no security headers, no static caching |
| Content Quality | 23% | 65 | Strong SSR marketing copy, clear headings; thin policy-adjacent pages |
| On-Page SEO | 20% | 40 | Titles OK; meta descriptions missing on 4/4 pages; login has no H1 |
| Schema / Structured Data | 10% | 0 | Zero JSON-LD, Microdata, or RDFa on any page |
| Performance (CWV) | 10% | 45 | Brotli ✓, preconnect for fonts ✓; Tailwind Play CDN + ApexCharts render-blocking in `<head>`; 7 unsplash images eager-loaded, no lazy loading |
| AI Search Readiness | 10% | 35 | SSR ✓; no robots.txt AI-crawler directives, no llms.txt, no FAQ/answer-block schema |
| Images | 5% | 40 | All 7 homepage images have empty `alt=""`, no `loading="lazy"` |

## What works
- Single H1 on homepage, logical H2/H3 tree, `lang="en"` everywhere, viewport meta
- Favicon set + web app manifest + Search Console verification file present
- Brotli compression via Cloudflare; `display=swap` on Google Fonts; font preconnects
- Auth-gated routes (/dashboard, /onboarding) properly 302 to /login — no private content leak

## Findings

| # | Severity | Finding | Evidence |
|---|---|---|---|
| 1 | High | Meta description missing on all 4 public pages | `<meta name="description">` absent on /, /login, /privacy-policy, /terms-and-conditions |
| 2 | High | No canonical URLs | No `<link rel="canonical">` anywhere; Cloudflare/Render multi-host variants can duplicate |
| 3 | High | No Open Graph / Twitter Card tags | Zero `og:` / `twitter:` meta tags — bare link previews when shared |
| 4 | High | No structured data | 0 JSON-LD blocks; no WebApplication/Organization/WebSite markup |
| 5 | High | /robots.txt returns 404 | No crawl directives, no Sitemap declaration, AI-crawler access undeclared |
| 6 | High | /sitemap.xml returns 404 | Confirmed via sitemap discovery (no robots declaration, common paths miss) |
| 7 | Medium | 7 unsplash images eager-load with empty alt | No `loading="lazy"` / `decoding="async"`; decorative CTA bg is fine empty, content images are not |
| 8 | Medium | Render-blocking 3rd-party JS in head | `cdn.tailwindcss.com` (Play CDN — runtime CSS generation) and `cdn.apexcharts.com` load synchronously in `<head>` |
| 9 | Medium | No security headers | No HSTS, X-Content-Type-Options, Referrer-Policy, or X-Frame-Options on responses |
| 10 | Medium | No static asset cache headers | No `Cache-Control` on /static/* responses |
| 11 | Medium | /llms.txt 404 | Optional AI-discovery file absent (no Google weight; helps non-Google agents) |
| 12 | Low | /login has no H1 | H2-only structure on the sign-in page |

## Notes
- **Tailwind Play CDN is dev tooling in production** (downloads ~110 KB JS then generates CSS at runtime). Replacing it with a prebuilt stylesheet is the single biggest performance win but changes how styles are built — out of scope under the "keep the UI unchanged" constraint; flagged for a follow-up.
- **ApexCharts cannot be `defer`ed**: homepage inline scripts instantiate charts during parse (no DOMContentLoaded wrappers); deferring would break the charts.
