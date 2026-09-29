# ACTION PLAN — fitness-app-b0wl.onrender.com

## Phase 1 — Critical / High (Week 1) → implemented in this pass
1. Add meta descriptions to all 4 public pages (High)
2. Add canonical URLs via a configurable SITE_URL (High)
3. Add Open Graph + Twitter Card tags, with a 1200×630 og-image (High)
4. Add JSON-LD: WebApplication + Organization + WebSite on the homepage (High)
5. Serve /robots.txt (crawl directives + Sitemap declaration + AI-crawler access) (High)
6. Serve /sitemap.xml with the 4 canonical public URLs (High)

## Phase 2 — Performance & technical (Week 1–2) → implemented in this pass
7. `loading="lazy"` + `decoding="async"` on below-the-fold images; descriptive alt text on content images (Medium)
8. Security headers: HSTS (prod), X-Content-Type-Options, Referrer-Policy, X-Frame-Options (Medium)
9. Static asset cache headers via SEND_FILE_MAX_AGE_DEFAULT (Medium)
10. Preconnect hints for images.unsplash.com / cdn.apexcharts.com (Medium)
11. /llms.txt for non-Google AI agents (Medium)

## Phase 3 — Follow-up (not in this pass)
12. Replace Tailwind Play CDN with a prebuilt stylesheet (biggest perf win; needs a build step — deliberately deferred to keep the UI pixel-identical)
13. Add an H1 to /login
14. Build entity presence (Wikipedia/Reddit/YouTube mentions) for GEO brand-mention signals

## Dependency order
1 → 2 → 3/4 (same templates) → 5/6 (new routes) → 7 (template attrs) → 8/9 (Flask layer) → 10/11 (head + route)
