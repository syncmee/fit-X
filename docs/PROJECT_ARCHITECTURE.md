# fiT — Project Architecture

> Reference map of the existing Flask application. Written during the initial audit (2026-08-26).
> Purpose: understand the system **before** changing it. See also [`DESIGN_SYSTEM.md`](./DESIGN_SYSTEM.md).

---

## 1. Overview

**fiT** (dashboard brand: **fiT-X**) is a server-rendered Flask fitness web app. Users sign up, complete an onboarding profile, and land on a dashboard that shows a calorie target, BMI summary, a weight-trend chart, upcoming workouts, and a chat-style "coach" panel. It is deployed on Render with gunicorn.

- **Language / framework:** Python + Flask (app-factory pattern)
- **Templating:** Jinja2, server-rendered HTML
- **Styling / JS:** Tailwind (CDN) + Flowbite + ApexCharts, all inline in templates
- **ORM:** SQLAlchemy (via Flask-SQLAlchemy)
- **Auth:** Flask-Login (session cookies)
- **DB:** SQLite in dev (`instance/userdata.db`), PostgreSQL in prod (`DATABASE_URL`)

> **Do NOT migrate this to React/Next/Vue or rewrite the architecture.** The goal is to improve, extend, debug, and polish the existing Flask app in place.

---

## 2. Application structure

```
Fitness-App-1/
├── main.py                # Entrypoint: imports app, create_all(), app.run()
├── Procfile               # web: gunicorn main:app --bind 0.0.0.0:$PORT
├── requirements.txt
├── .env.example           # ⚠ contains a real Postgres credential (see §11)
├── instance/
│   └── userdata.db        # dev SQLite DB (currently tracked in git — see §11)
├── app/
│   ├── __init__.py        # create_app(): config, extensions, before_request, blueprint, CLI
│   ├── config.py          # Config class (env-driven)
│   ├── extensions.py      # db, login_manager, migrate singletons
│   ├── models.py          # User, WeightLog, ScheduledWorkout, CoachMessage, MealEntry, WaterLog
│   ├── routes.py          # main_bp — homepage, auth, onboarding, logout
│   ├── dashboard.py       # dashboard_bp — ONE file with all dashboard logic:
│   │                      #   health math (BMI + EER calories), the AI coach
│   │                      #   (Gemini, JSON actions; rule parser as fallback),
│   │                      #   context builder, /dashboard + /coach/message +
│   │                      #   /meals/* + /water/add + /workouts/<id>/done
│   ├── security.py        # custom CSRF token generation + validation
│   ├── validation.py      # signup / login / onboarding form validators
│   └── data/
│       └── bmiagerev.csv  # CDC BMI-for-age percentile lookup table
├── templates/
│   ├── homepage.html      # marketing landing (welcome/about/plans/mission)
│   ├── login.html         # combined sign-in + sign-up
│   ├── onboarding.html    # multi-step profile capture (the live flow)
│   └── dashboard.html     # main app screen (fiT-X dashboard, the only dashboard file)
└── static/
    └── logos/             # build.png, build-white.png
```

### App factory (`app/__init__.py`)
`create_app(config_class=Config)` performs, in order:
1. Creates the Flask app with instance-relative config; resolves `templates/` and `static/` from the project root.
2. `db.init_app`, `login_manager.init_app`, and `migrate.init_app` (if Flask-Migrate is importable).
3. Registers `validate_csrf_request` as a **`before_request`** hook (see §7).
4. Exposes `csrf_token` as a Jinja global so templates can render `{{ csrf_token() }}`.
5. Registers the `main_bp` blueprint.
6. Registers an `init-db` CLI command.

A module-level `app = create_app()` is created so `main:app` (gunicorn) and `from app import app` (main.py) both resolve.

---

## 3. Routes

| Endpoint | Methods | Auth | Purpose |
|---|---|---|---|
| `main.homepage` `/` | GET | public | Marketing landing (`homepage.html`) |
| `main.login` `/login` | GET, POST | public | Combined sign-in / sign-up (`form_type` field) |
| `main.onboarding` `/onboarding` | GET, POST | login req. | Capture profile; set `onboarding` flag |
| `main.logout` `/logout` | GET/POST | login req. | Log out |
| `/test` | GET | public | Legacy — **redirects to homepage** |
| `dashboard.dashboard` `/dashboard` | GET | login req. | fiT-X app screen (`dashboard_bp`, logic in `app/dashboard.py`) |
| `dashboard.coach_message` `/coach/message` | POST | login req. | Handle a coach chat message (`dashboard_bp`) |
| `dashboard.meals_add` `/meals/add` | POST | login req. | Add a MealEntry (name, type, kcal, P/C/F) |
| `dashboard.meals_delete` `/meals/<id>/delete` | POST | login req. | Remove a MealEntry |
| `dashboard.water_add` `/water/add` | POST | login req. | Log a WaterLog amount in ml |
| `dashboard.workout_done` `/workouts/<id>/done` | POST | login req. | Mark a ScheduledWorkout completed |

`dashboard._build_dashboard_context()` (in `app/dashboard.py`) assembles the dashboard payload: identity/phase labels, calorie target (`estimate_calorie_target`), BMI summary (`build_bmi_summary`), weight stats (delta, 7-day average, chart series from `WeightLog` rows), logging streak, goal progress % toward `target_weight`, upcoming `ScheduledWorkout`s + a 7-day week strip, and recent `CoachMessage`s for the chat console.

---

## 4. Data model (`app/models.py`)

| Model | Key fields | Notes |
|---|---|---|
| **User** (`UserMixin`) | name, email, password hash, `onboarding_complete`; profile: gender, age, height, weight, start_weight, target_weight, activity_level, diet, goal | `set_password` / `check_password` via Werkzeug. Relationships to the three below. |
| **WeightLog** | weight, date, user_id | Time series powering the weight chart. |
| **ScheduledWorkout** | title, scheduled_for, duration_minutes, status (default `"scheduled"`), notes, user_id | Created by the coach parser or onboarding. |
| **CoachMessage** | role, content, created_at, user_id | Chat transcript (`role` = user / assistant). |
| **MealEntry** | name, meal_type (breakfast/lunch/dinner/snack), calories, protein, carbs, fats, logged_at, user_id | Powers nutrition tab, macro donut, calorie net, daily intake ring. |
| **WaterLog** | amount_ml, logged_at, user_id | Powers the hydration card. |

**Workout philosophy:** sessions are tracked as *activity type + duration + date* (`ScheduledWorkout`, status scheduled/completed) — deliberately gym-agnostic so yoga, running, combat sports, and pilates count the same as lifting. There are no set/rep/1RM/muscle-recovery models by design.

**Gaps vs. the product wishlist (brief §11):** body measurements (waist/chest/arms/body-fat) still have no model — the Progress card shows an honest "coming soon" state.

---

## 5. Data flow

**Onboarding →** POST `/onboarding` is validated by `validation.validate_onboarding_form`, writes profile fields onto the current `User`, sets `onboarding = True`, and redirects to the dashboard.

**Dashboard →** `_build_dashboard_context()` in `app/dashboard.py` reads the user + their logs, derives calorie target and BMI, builds the weight series, and renders `dashboard.html` (the fiT-X UI).

**Coach →** POST `/coach/message` (in `app/dashboard.py`) passes the text to `process_coach_message`, which **rule-parses** it (regex/keyword) to optionally log a weight, schedule a workout, or capture a meal/workout note, then persists both the user message and the assistant reply as `CoachMessage` rows and redirects back to `/dashboard#coach`, where the chat console auto-opens.

---

## 6. Health / domain logic (inside `app/dashboard.py`)

Self-contained, standards-based, and worth preserving (now housed in the dashboard module):
- **Calorie target** — Mifflin-St Jeor BMR × activity multiplier (TDEE), adjusted by the pace the user chose on the onboarding slider (`User.pace`: ±kg/week × 7700 kcal / 7 days). This is intentionally the same math as the onboarding preview, so the number shown at signup is the number on the dashboard.
- **Macro targets** — protein 2.2 / 1.8 / 2.0 g/kg (cut / gain / maintain), fats 25% of calories, carbs fill the remainder — same as the onboarding preview.
- `calculate_bmi` and `build_bmi_summary` (returns label + Tailwind color class).
- **CDC BMI-for-age percentiles** looked up from `app/data/bmiagerev.csv`, cached with `@lru_cache`.

This logic is domain-correct — treat changes here as higher-risk and verify against the source equations. If you change one side (onboarding JS or backend), change both.

---

## 7. Authentication & security

- **Sessions:** Flask-Login; `login_manager.login_view = "main.login"`. Passwords hashed with Werkzeug.
- **Cookies (`config.py`):** `SESSION_COOKIE_HTTPONLY = True`, `SAMESITE = "Lax"` (env-overridable), `SECURE` on in production. Remember-cookie mirrors these.
- **CSRF (custom, not Flask-WTF):** `security.generate_csrf_token()` stores a `secrets.token_urlsafe(32)` in the session; `validate_csrf_request()` runs as a `before_request` on `POST/PUT/PATCH/DELETE`, compares with `secrets.compare_digest`, and `abort(400)` on mismatch. Every form posts a `{{ csrf_token() }}` hidden field.

---

## 8. Database & deployment

- **Dev:** SQLite at `instance/userdata.db`. **Prod:** PostgreSQL via `DATABASE_URL` (Render; `psycopg2`).
- **Schema creation:** `db.create_all()` (in `main.py` and the `init-db` CLI command). **Flask-Migrate is installed but there is no `migrations/` directory** — schema changes are not versioned. This is fine for greenfield dev but risky for evolving a production schema (see §10).
- **Serving:** `Procfile` → `gunicorn main:app --bind 0.0.0.0:$PORT`.

---

## 9. External services & dependencies

- **Active at runtime:** none. No third-party API is called during a normal request.
- **The AI coach runs on Gemini** (`GEMINI_API_KEY` / `GEMINI_MODEL` in `.env`, loaded via `app/config.py`). The model returns strict JSON actions (log_weight / log_meal / log_water / schedule_workout / complete_workout) which `app/dashboard.py` applies to the DB. Without a key — or on any API failure — the original rule-based parser takes over, so the coach never breaks. The old `OPENAI_*` config entries were replaced.
- **Declared-but-unused libraries:** `reportlab` + `pdfcrowd` (PDF export — no route uses them), `Authlib` (OAuth — no route uses it). These hint at planned/abandoned features.
- **Frontend libraries (CDN):** Tailwind, Flowbite 2.5.2, ApexCharts (dashboard chart), PureCounter (homepage stats).

---

## 10. Known technical debt

1. **No template inheritance.** There is no `base.html`; each template re-declares its `<head>`, Tailwind config, and `.glass-card` CSS (with subtly different values). This is the single biggest maintainability drag. *(Detail in [`DESIGN_SYSTEM.md`](./DESIGN_SYSTEM.md).)*
2. **Three conflicting color systems** across pages (product green/yellow vs. marketing orange vs. a stray blue). Branding is inconsistent between landing and app.
3. **`Inter` is configured but never loaded** (no Google Fonts `<link>`), so it silently falls back to system sans everywhere.
4. **`test.html` is broken dead code** — a stub onboarding prototype full of placeholder comments, fields that don't match the real flow (`fitness_level`, `equipment[]`, `protein_goal`), and inline scripts that reference non-existent DOM IDs (`age-slider`, `onboarding-form`, `step1`) and will throw on load. Its `/test` route only redirects home. Safe to delete or quarantine.
5. **Flask-Migrate installed but unused.** No migration history; schema evolves only via `create_all()`.
6. **No migrations.** New columns ship as guarded `ALTER TABLE` statements in `create_app()` (e.g. `scheduled_workout.calories_burned`); consider adopting Alembic before the schema grows further.
7. **Unused dependencies & config** (`reportlab`, `pdfcrowd`, `Authlib`, OpenAI config) add confusion and supply-chain surface.
8. **Unpinned deps:** `flask_login` and `flask_sqlalchemy` have no version pins in `requirements.txt`.
9. **`homepage.html` and `test.html` are missing `<!DOCTYPE html>`.**
10. **Hotlinked hero images** from Pexels/Unsplash (external availability risk).
11. **Security items (flagged, not modified — brief §14):**
    - Live `ANTHROPIC_AUTH_TOKEN` in `.claude/settings.json`, and `.claude/` is **not** in `.gitignore` → would leak if committed.
    - A **real Postgres password** is committed in `.env.example` (rotate + replace with a placeholder).
    - `SECRET_KEY` falls back to `"dev-secret-key-change-me"` when unset.
    - `instance/userdata.db` is tracked in git (contains user data).
    - `ANTHROPIC_BASE_URL` routes API traffic through the third-party `agentrouter.org`.

---

## 11. Areas that should NOT be rewritten

Per the brief, and confirmed by the audit, preserve these:

- **The Flask app-factory + two-blueprint architecture** (`main_bp` for auth/pages, `dashboard_bp` for the app screen). It is clean and correct.
- **Server-rendered Jinja templates.** Do not introduce a SPA/React frontend to "modernize" UI.
- **The custom CSRF mechanism.** It works; don't swap in Flask-WTF without a concrete reason.
- **The health/EER + CDC-percentile logic** (now in `app/dashboard.py`). It is domain-accurate; change only with care and verification.
- **Flask-Login authentication.** Keep it.
- **The working login / onboarding / dashboard / coach flows.** Improve them incrementally (§13 of the brief) — don't replace them wholesale.

---

## 12. Suggested improvement order (non-binding)

1. **Foundation:** add `base.html` + one shared design-token/Tailwind config; unify the palette; load Inter. *(Highest leverage, zero backend risk.)*
2. **Hygiene:** rotate the two exposed secrets; gitignore `.claude/` + `*.stackdump`; decide on tracking `instance/userdata.db`; remove `test.html`.
3. **Then features (need new models first):** nutrition/macros, set/rep/RPE workout tracking, personal records, richer progress charts.
4. **Verify UI in-browser** with Playwright (desktop/tablet/mobile) before marking anything done.
