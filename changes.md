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

## Verification

The full local suite passed with:

```text
.\\.venv\\Scripts\\python.exe -m unittest tests.test_food_seed tests.test_challenges tests.test_admin tests.test_coach -q
Ran 30 tests ... OK
```

The system Python environment does not have the project's SQLAlchemy
dependency installed; use the repository virtual environment or CI command.
