# Phase D: Lighthouse results (mobile, throttled)

Runs: Lighthouse 12.x, mobile emulation + default simulated throttling.
Live baseline = Render production (gunicorn, real network).
Local baseline = `git worktree` at the pre-Phase-D commit (2cf1561), Flask dev
server on 127.0.0.1 — the same harness the "after" runs use, so local rows are
apples-to-apples. Dashboard runs authenticated via a session cookie passed
with `--extra-headers`.

## Performance score

| Page     | Live baseline | Local baseline (old code) | Local after (new code) |
|----------|--------------:|--------------------------:|-----------------------:|
| Homepage | 77            | 75                        | 79                     |
| Login    | 94            | 89                        | 88–96 (rerun band)     |
| Dashboard| 54            | 49                        | **81**                 |

## Key metrics (local baseline → local after)

| Metric | Homepage | Login | Dashboard |
|---|---|---|---|
| First Contentful Paint | 3.6s → 3.7s | 2.7s → 2.8s | 5.1s → **2.4s** |
| Largest Contentful Paint | 4.6s → **3.7s** | 2.8s → 2.8s | 6.1s → **3.4s** |
| Total Blocking Time | 0ms → 150ms | 0ms → 0ms | 650ms → **260ms** |

## Other categories

| Category    | Homepage | Login | Dashboard |
|---|---|---|---|
| Accessibility | 74 → **84** | 62 → 62 (pre-existing contrast issues, unchanged) | 74 → 74 |
| Best Practices | 96 → **100** | 96 → 96 | 93 → 93 |
| SEO | 100 → 100 | 100 → 100 | 58 (localhost artifact: no sitemap/robots URLs) |

## What changed between the runs

- cdn.tailwindcss.com (a ~300 KB runtime JIT compiled on every page view)
  replaced by a committed, minified 32 KB static/css/app.css built with
  Tailwind 3.4.17 from the same theme config.
- ApexCharts: one pinned URL (jsdelivr @8.7.0, was an unpinned URL split
  across two CDNs), loaded with defer; chart init moved to
  requestAnimationFrame after first paint.
- Flask-Compress (br/gzip) for HTML/CSS/JSON/JS > 1 KB.
- ?v=RENDER_GIT_COMMIT cache busting on app.css and app-install.js.
- PWA install screenshots: PNG → WebP (397 KB → 58 KB), manifest updated.
- Fonts: verified all loaded weights (Inter 400–900, JetBrains Mono
  400–600) are actually used — no trim needed.

Re-run these against the live Render URL after the next deploy to record
production numbers; expect the render-blocking-resources finding (3.4s on
the old dashboard baseline) to disappear from the live report.
