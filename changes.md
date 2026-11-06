# fiT-X implementation notes

## Implemented safety and reliability work

### CI test safety net

- Added `.github/workflows/tests.yml`. GitHub Actions now installs the pinned
  dependencies on Python 3.12 and runs `python -m unittest discover -s tests -v`
  for every push and pull request. This is deliberately the same command a
  contributor can run locally, so CI cannot silently exercise a different test
  collection.
- Extended `tests/test_coach.py` with a regression for every data-writing
  `_apply_ai_actions` branch: weight, meal, water, and completed workout. The
  test flushes the SQLAlchemy unit of work and asserts the persisted rows, which
  catches a response that sounds correct but did not write data.

### Production and PWA login protection

- `create_app` now refuses to boot when `FLASK_ENV=production` and `SECRET_KEY`
  is absent. Flask signs sessions and Flask-Login remember cookies with this
  key; a generated/default production key would invalidate sessions on restart
  and is unsafe.
- Production uses Werkzeug's `ProxyFix(x_for=1, x_proto=1, x_host=1)`. Render
  terminates HTTPS one proxy hop before Gunicorn; this makes Flask recognise the
  original HTTPS request while not trusting unbounded forwarded-header chains.
- Login and Google login now use `login_user(..., remember=True)` and
  `session.permanent = True`. The explicit 90-day `REMEMBER_COOKIE_DURATION`
  and `REMEMBER_COOKIE_SAMESITE` settings are in `Config`. This addresses iOS
  installed-PWA launches after Safari has discarded the short-lived session.
- Invalid/missing CSRF tokens now redirect to `/login` with a clear flash
  message instead of an unhelpful 400. A fresh login session also receives a
  fresh CSRF token.

**Render action required:** set `FLASK_ENV=production` and a long random
`SECRET_KEY` in the Render service environment, deploy, then verify the
response has `Secure` cookies and debug is disabled. An iPhone still needs the
requested real-device test after 3-7 unused days; that cannot be performed by
the repository.

### Faster bounded dashboard reads

- Added migration `0008_performance_and_request_safety` with composite indexes
  for `(user_id, logged_at/date/scheduled_for/created_at)` on meals, water,
  weight, workouts, and coach messages. These match the dashboard's user plus
  date-range predicates, so the database can seek directly to one user's recent
  records instead of scanning a shared table.
- The heatmap explicitly fetches at most its displayed 84 days. The weekly
  workout history now fetches only the eight displayed weeks before Python
  groups it. This preserves SQLite compatibility while reducing transferred
  rows. The existing SQL `COUNT` for scheduled workouts remains database-side.
- The migration is defensive: it checks existing columns/indexes before adding
  them because this app has legacy guarded startup `ALTER TABLE` work as well
  as Alembic migrations.

Run these before/after deployment against PostgreSQL to record the requested
evidence (using a representative user ID):

```sql
EXPLAIN ANALYZE SELECT logged_at FROM meal_entry
WHERE user_id = 123 AND logged_at >= now() - interval '84 days';
EXPLAIN ANALYZE SELECT scheduled_for, duration_minutes FROM scheduled_workout
WHERE user_id = 123 AND status = 'completed'
  AND scheduled_for >= now() - interval '56 days';
```

### Duplicate-submit protection and coach audit data

- Added a cryptographically random hidden `idempotency_key` to meal, water,
  and coach forms. `IdempotencyKey` has `UNIQUE(user_id, key)`, and its insert
  is flushed in the same SQLAlchemy transaction as the real write. A duplicate
  token therefore produces no second log, including across two Gunicorn
  workers. A nested transaction catches only the uniqueness collision without
  discarding the outer request transaction.
- Added `CoachMessage.raw_model_json` and save the parsed provider result on
  the assistant record. This supports incident review without exposing raw
  model data in the chat UI.

## Deliberately not claimed as complete

The following roadmap items require larger, separately reviewable changes and
are not implemented by this patch: the `dashboard.py` module split; Redis-backed
Flask-Limiter/email failure limits; a provider circuit breaker; database-derived
food quantities/units and the `2 eggs -> 20` guard; htmx partial rendering;
daily idempotency-key cleanup; PostgreSQL `pg_trgm`/full-text/pgvector RAG; and
the 50-phrase evaluation suite. They need infrastructure/configuration and
behavioural design decisions that cannot safely be represented as a small
follow-on tweak to the existing SQLite-compatible application.

## Phase 2: dashboard module split (started)

- Added `app/health.py` as the dependency-free home for BMI, Mifflin-St Jeor
  calorie targets, macro-facing activity labels, and cached CDC BMI-for-age
  parsing. `csv`, `NormalDist`, and `lru_cache` live there because they are
  health-domain concerns: the cache prevents repeatedly parsing the bundled
  CDC CSV, while `NormalDist.cdf` converts the CDC LMS z-score into a
  percentile.
- `app.dashboard` re-exports the established public health symbols from the
  new module. This intentionally preserves imports such as
  `from app.dashboard import estimate_calorie_target` for one compatibility
  release before call sites are migrated. The original implementations remain
  temporarily in place solely to minimise this first extraction's diff; the
  re-export is authoritative at runtime and the next split removes that legacy
  block after imports have moved.
- Added `app/foods.py`; its SQLite matcher and five-minute per-app catalogue
  cache are now the active implementation used by dashboard compatibility
  exports. Keeping `Food` SQL and alias matching together is the prerequisite
  for later swapping in PostgreSQL search without touching route code.
- Added `app/coach/providers.py`; Gemini and Groq HTTP transport are now the
  active provider functions. The providers perform late dashboard imports only
  for transitional prompt/history dependencies, which avoids a circular import
  and preserves tests that patch `app.dashboard._gemini_generate`.
- Added the `coach.prompt`, `coach.actions`, `coach.rules`, `emails`, `push`,
  and `cron` module interfaces. SMTP delivery is now active from `emails.py`.
  Cron/push interfaces deliberately remain delegating facades until their
  coupled dashboard routes move together.

## Phase 3: protection (started)

- Added Flask-Limiter and Redis support. The login endpoint is limited by IP
  (10/minute); the authenticated coach endpoint is limited by user ID
  (10/minute and 100/day). Redis must be configured in Render with
  `RATELIMIT_STORAGE_URI` so the two Gunicorn workers share counters;
  `memory://` is intentionally a local-development fallback only.
- Added a friendly 429 response rather than a default HTML error page.
- Added a per-process AI circuit breaker: two consecutive provider failures
  open a 60-second cooldown. `monotonic()` is used rather than wall-clock time
  so clock adjustments cannot accidentally extend or shorten an outage window.
  The rule-based coach remains the immediate fallback.

## Phase 4: htmx experience (deferred)

The htmx coach implementation was removed after it regressed the existing
smooth fetch-based console: it introduced a second submission path, replaced
the hand-built typing flow, and could swap a redirect document into the chat
panel. The original fetch, typing bubble, idempotency workflow, and reload
remain authoritative. Any future partial-update work should begin with a
dedicated UI regression test suite and one isolated interaction at a time.

## Phase 5: food intelligence (started)

- Added a portable `Food.aliases` JSON-text field and migration `0009`. JSON
  text keeps the schema usable in both current SQLite development databases and
  PostgreSQL production; `food_name_keys` parses and normalizes it alongside
  names such as `Dal, cooked` and `Chapati/Roti`.
- Added a regression proving an explicit catalogue alias (`chapati`) resolves
  to its canonical food record. This is the schema foundation for PostgreSQL
  trigram/full-text ranking; adding PostgreSQL-only indexes before the
  production database is confirmed would make the app's SQLite fallback fail.

## Phase A: fix what's broken (complete, minus Render env)

### CI hermetic tests

- The Tests workflow was red because CI has no `.env`: with empty `SMTP_USER`
  the admin announce route silently skips email delivery, so the three
  announce tests (which patch `_send_via_smtp` and assert delivery) failed,
  one with a `KeyError` on the never-filled capture dict.
- `tests/base.py` TestConfig now carries dummy `SMTP_USER`,
  `SMTP_APP_PASSWORD`, and `SMTP_FROM`. Tests that assert delivery still patch
  `_send_via_smtp`, so nothing touches a real SMTP server. GitHub Actions
  needs no secrets or environment variables; the run is green on `2de91d1`.

### Limiter storage follows config

- `extensions.py` no longer hardcodes `storage_uri="memory://"` in the
  `Limiter(...)` constructor (which would override app config). Flask-Limiter
  now reads `RATELIMIT_STORAGE_URI`, so Render's Redis URI will take effect
  while `memory://` stays the local fallback via `Config`.

### Idempotency: missing key is not a duplicate

- `_claim_idempotency_key` returns `claimed` / `duplicate` / `missing`
  instead of a bool. A form with no usable token (an old open tab rendered
  before the hidden field existed) now says "please refresh the page" instead
  of the misleading "already processed", and writes no dedupe row. Genuine
  duplicates keep the "already processed / already logged" messages. Covered
  by `tests/test_login_limits.py`.

### Login limits

- The 10/minute IP limit on `/login` now applies to POST only, so rendering
  the form does not consume an IP's attempt budget.
- Added a per-email failed-attempt counter in `app/security.py`: 5 failures
  within 15 minutes blocks further sign-in attempts for that address (HTTP
  429), a success clears it. It complements the IP limit — an attacker cycling
  IPs still trips a per-account counter, while one user's typos do not lock a
  shared office IP. Counters are per-process by design (documented in the
  module); sharing them via Redis is a later hardening step, not needed for
  the current threat model.

## Verification (Phase A)

```text
.\\.venv\\Scripts\\python.exe -m unittest discover -s tests   (run without .env, mirroring CI)
Ran 37 tests ... OK
```

GitHub Actions Tests run on `2de91d1`: success.

## Phase B: finish the protection work

### Food sanity limits: the app, not the model, decides the numbers

- `app/foods.py` grew the food-domain guards: `PER_UNIT_CEILINGS` (per-food
  one-meal ceilings — eggs 10, rotis 12, dosa 8, …), `MEAL_ENTRY_CALORIE_CAP`
  (1500 kcal) and `MEAL_ENTRY_HARD_CAP` (3000 kcal), plus `resolve_food`,
  `compute_meal_from_catalogue`, and `unit_ceiling_for`.
- The `log_meal` AI action now carries `quantity` and `unit` (prompt updated).
  When the named food matches the curated catalogue, calories and macros are
  **computed from the database** — per-serving values scaled by the portion
  (mass/volume units scale by the parsed serving grams; countable units treat
  one unit as one catalogue serving) — and the model's own estimate is
  discarded for matched foods. Unmatched foods keep the model estimate.
- Ceilings and the cap **reject or ask, never clamp**: a "2 eggs became 20"
  misread logs nothing and the coach reply ends with an explicit confirmation
  question ("Say 'yes, log it' to confirm"). The model can pass
  `confirmed: true` only after the user explicitly re-confirms; confirmation
  relaxes limits up to 3× the ceiling / the hard cap, never past them.
  The regression suite (`tests/test_food_limits.py`) pins the exact "2 eggs
  → 20" case, the catalogue overrides, kg/g unit scaling, cap and hard-cap
  behavior, and the confirmation surfaced through `process_coach_message_ai`.

### Idempotency-key cleanup

- `_cleanup_old_idempotency_keys` deletes accepted dedupe tokens older than
  7 days; it runs once per day inside the existing `/cron/send-reminders`
  route behind a per-worker date gate (a second worker's pass finds nothing
  to delete). Tokens are never read after their request completes, so
  deletion cannot weaken double-submit protection.

### One AI deadline across Gemini and Groq

- `app/coach/providers.py` gained `TOTAL_AI_DEADLINE_SECONDS` (25 s). Each
  provider's per-request timeout is capped by what remains of the chain-wide
  budget, and a provider with under 2 s remaining raises immediately, so a
  hung Gemini no longer pushes the worst case to 12 s + 45 s. The rule-based
  coach remains the fallback. `process_coach_message` starts the deadline
  once and passes it through both provider attempts.

## Phase C: dashboard.py is routes and context building only

`dashboard.py` went from 3,396 lines to 1,225: the fifteen dashboard routes,
the dashboard page context builders, the AI/rules dispatch logic, and the
route-level form helpers (idempotency claim, cleanup gate). Everything else
now lives in its domain module:

- `app/coach/__init__.py` — shared primitives: `CoachOutcome`, `CoachAIError`,
  `ENVIRONMENT_ASK`.
- `app/coach/prompt.py` — AI context assembly, system prompt, per-fiT-X-day
  chat memory (prune + recent history), and `parse_coach_json`. Meal/week
  context totals remain dashboard context builders and are late-imported (the
  same cycle-avoidance pattern the providers already used).
- `app/coach/actions.py` — `apply_actions` (the structured AI-action writer)
  with the food sanity limits, plus the shared `default_meal_type` and
  `sync_current_weight` helpers. `MEAL_TYPE_ORDER` moved to `app/models.py`
  next to `MealEntry`.
- `app/coach/rules.py` — the full rule-based coach: intent matchers, the
  weight/water/meal/workout log parsers, schedule/progress/challenge lookups,
  the environment chip handler, and `process_coach_message_rules`.
- `app/coach/providers.py` — now imports prompt/history/parse from
  `app.coach.prompt` directly instead of reaching into dashboard.
- `app/cron.py` — the real implementations behind the old delegating facade:
  `send_workout_reminders`, `send_challenge_reminders`, `send_engagement_nudges`
  (with their email builders and engagement-window helpers), and
  `cleanup_old_idempotency_keys` with the per-day gate inside it. Emails and
  pushes are referenced as modules (`emails_module.send_via_smtp`,
  `push_module.send_web_push`) so patches target the source of truth.
- `app/push.py` — `send_web_push` and the hand-built ES256 VAPID header signer.
- Legacy originals deleted: the duplicate BMI/health math block, the duplicate
  food matcher, the duplicate `_gemini_generate`/`_groq_generate` transports,
  and the duplicate SMTP sender. `app/admin.py` now imports
  `emails.send_via_smtp` / `push.send_web_push` directly.

Test patches were moved to the authoritative locations: `tests/test_coach.py`
and `tests/test_food_limits.py` patch `app.coach.providers.gemini_generate` /
`groq_generate` (the dispatcher reads them through `providers_module` for
exactly that reason), email/push patches in `tests/test_admin.py` target
`app.emails` / `app.push`, and `_food_db_matches` imports are now
`app.foods.food_db_matches`.

## Phase D: speed (Lighthouse-driven)

Full numbers and methodology: `docs/lighthouse/phase-d-results.md`.
Mobile, throttled, Lighthouse 12. Local before/after used the same harness
(a `git worktree` at the pre-change commit serving through the same Flask dev
server), so those rows are directly comparable; live Render baselines are
also recorded.

Headline (local baseline → after):

| Page | Performance | FCP | LCP | TBT |
|---|---|---|---|---|
| Homepage | 75 → 79 | 3.6s → 3.7s | 4.6s → 3.7s | — |
| Login | 89 → 88–96 (noise band) | 2.7s → 2.8s | 2.8s → 2.8s | 0 → 0 |
| Dashboard | **49 → 81** | 5.1s → **2.4s** | 6.1s → **3.4s** | 650ms → **260ms** |

Changes:

- **Built Tailwind CSS.** `cdn.tailwindcss.com` — a ~300 KB runtime JIT
  compiled in the browser on every page view — is replaced by a committed,
  minified 32 KB `static/css/app.css` built from the identical theme config.
  `package.json` pins `tailwindcss@3.4.17`; `npm run build:css` regenerates
  after template changes (content scanning covers templates, Python, and
  static JS). All seven templates swapped; page-local `<style>` blocks and
  the shared custom classes (`.grad-*`, `.glass`, `.tracking-tighter-2`)
  are unchanged.
- **ApexCharts pinned and deferred.** One URL everywhere
  (`cdn.jsdelivr.net/npm/apexcharts@8.7.0`, replacing an unpinned URL split
  across jsdelivr and cdn.apexcharts.com), `defer`, and chart constructors
  run in `requestAnimationFrame` after first paint. Onboarding's chart was
  already user-triggered. `sw.js` caches jsdelivr now; SW version bumped.
- **Flask-Compress**: brotli/gzip for HTML/CSS/JSON/JS over 1 KB
  (verified `Content-Encoding: br` locally; live Render previously sent
  responses uncompressed).
- **Cache busting**: `/static/css/app.css?v={{ STATIC_V }}` and the same for
  `app-install.js`; `STATIC_V` is `RENDER_GIT_COMMIT` on Render, so each
  deploy gets a fresh URL alongside the existing 30-day static cache.
- **Screenshots to WebP**: PWA install screenshots 397 KB → 58 KB, manifest
  updated. All content images already had `loading="lazy" decoding="async"`
  inside fixed-size containers (CLS 0), and the homepage LCP element is text,
  so no `fetchpriority` image change applied.
- **Fonts verified**: Inter 400–900 and JetBrains Mono 400–600 are all
  actually used (thin/light are not) — nothing to trim.
- Not in scope but visible in the reports: login accessibility 62
  (contrast issues) predates this phase; dashboard SEO 58 on localhost is a
  measurement artifact (no sitemap/robots at 127.0.0.1).

**Re-run the three live Lighthouse reports after the next Render deploy to
record production numbers.**

## Phase D follow-up: PWA launch black screen

**What the black screen was:** the manifest declared
`background_color: #000000`, so Windows/Android painted a pure-black launch
splash, and iOS (which ignores manifest splashes) had no
`apple-touch-startup-image` at all — just blank. Either way the blank held
for the whole first network response: `start_url` is `/dashboard` and
navigations were strictly network-first, so every launch waited on a full
server render, which on Render's free tier can include a cold start
(tens of seconds after the service sleeps).

Three-layer fix:

- **Branded iOS startup images.** Ten `apple-touch-startup-image` PNGs
  covering every iPhone generation from SE/8 through the 16/17 series (the
  first set of five missed the 402×874 and 440×956 devices, which is why a
  16/17-series iPhone still saw a blank launch), generated from the trimmed
  logo on the app background with a soft brand-gradient glow, served from
  `/static/splash/v2/` and linked with device media queries in dashboard,
  login, and homepage (the pages the PWA can start on). iOS shows the fiT-X
  mark for the whole launch instead of blank.
- **Manifest splash color** `#000000` → `#0A0A0A` so the Windows/Android
  splash flows seamlessly into the app's real background.
- **Service-worker navigation timeout fallback.** Navigations stay
  network-first (fresh data wins) but with a 2.5 s budget: past that, the
  SW paints the user's last copy of that page from a dedicated
  `fitx-v3-nav` runtime cache while the fresh response continues loading in
  the background and refreshes the cache. Redirect responses (the
  logged-out 302 to /login) are never cached, error responses still fall
  back to `offline.html`, and a first-ever launch (nothing cached) waits
  for the network as before. SW version bumped to `fitx-v3`.

Effect: repeat launches paint instantly (cached page + splash), first-ever
launches show the branded splash instead of black, and a sleeping server no
longer holds an empty screen.

## Fixes after the PWA launch work

- **Stray comment text at the top of the page.** The splash-link edit left an
  orphaned second line of an HTML comment
  (`so branded startup images stand in while /dashboard loads. -->`) outside
  a comment, which browsers rendered as visible text above the dashboard
  (login/homepage too). Removed in all three templates.
- **Session Queue empty state misaligned.** The timeline container reserved
  40 px of left padding for the timeline avatar nodes, but the empty state
  has no nodes — so the dashed box sat indented relative to the
  "Session Queue" heading and the History list. The padding is now applied
  only when there are upcoming workouts (`pl-10` conditional in
  dashboard.html).

## Verification (Phase B)

```text
.\\.venv\\Scripts\\python.exe -m unittest discover -s tests   (run without .env, mirroring CI)
Ran 53 tests ... OK
```

Note: `test_coach`'s meal-branch test now logs an *unmatched* food
("Chicken momos"); matched foods are DB-computed by design, which its old
"Dal rice" fixture would have exercised differently.

## Verification

The full local suite passed with:

```text
.\\.venv\\Scripts\\python.exe -m unittest tests.test_food_seed tests.test_challenges tests.test_admin tests.test_coach -q
Ran 30 tests ... OK
```

The system Python environment does not have the project's SQLAlchemy
dependency installed; use the repository virtual environment or CI command.
