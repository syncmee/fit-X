# SITEMAP GENERATION — fitness-app-b0wl.onrender.com

**Command:** `/seo sitemap generate` · claude-seo v2.4.1

## Discovery (Mode 1 run first)
`sitemap_discovery.py` found no sitemap: robots.txt 404, common candidates (/sitemap.xml, /sitemap_index.xml, /sitemap.txt) 404. → Generation required.

## Structure (Mode 2)
- Business type: SaaS web app, 4 canonical public URLs — single small urlset, no index needed (well under 50k/50MB).
- Included: `/`, `/login`, `/privacy-policy`, `/terms-and-conditions` (all HTTP 200, canonical https, none noindexed, none redirected).
- Excluded: `/dashboard`, `/onboarding` (auth-gated, 302 → /login — not indexable).
- No `<priority>`/`<changefreq>` (ignored by Google). `<lastmod>` = deploy date constant, bumped with content releases so it stays verifiably accurate.
- Implementation: dynamic Flask route (not a static file) so the Sitemap host always matches the serving host.

## Generated output
Served at `/sitemap.xml` (see `app/routes.py` in this repo); declared in `/robots.txt` via `Sitemap:` line.
