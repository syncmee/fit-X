# fiT — Design System v2 (landing page)

> Documents the system introduced by the new `homepage.html` (replaces the marketing section of `DESIGN_SYSTEM.md` §1–§9). Written the same way, so it can be diffed against the old doc directly.

---

## 1. TL;DR — what changed

The landing page no longer runs its own orange `accent` palette. It now runs the **Product** system — same family as `dashboard.html` / `login.html` / `onboarding.html` — plus three additions that the old Product pages don't have yet: a second background shade (`bg2`), a monospace type layer for data/labels, and a full motion system (scroll reveal, float, marquee, animated gradient).

**Net effect on the "two-and-a-half identities" problem from v1:** down to one core palette. The marketing/Product split is gone. What's left to converge is the **v1 Product pages catching up** to what the landing page now does (see §9).

---

## 2. Color

```js
colors: {
  bg:     '#0A0A0A',   // page background
  bg2:    '#0E0E0E',   // alternating section background (NEW — not in v1)
  yellow: '#fef08a',
  green:  '#4ade80',
}
```

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0A0A0A` | Base page background |
| `--bg-2` | `#0E0E0E` | Alternate band for every other full-width section (`bg-bg2`) — new device for visually separating sections without hairlines alone |
| `--text` | `#F5F5F5` | Primary text |
| `--muted` | `#9CA3AF` | Secondary text |
| `--dim` | `#6B7280` | Tertiary / timestamp-level text |
| `--surface` / `--surface-2` | `rgba(255,255,255,.03)` / `.06` | Flat fill layers |
| `--border` / `--border-strong` | `rgba(255,255,255,.08)` / `.14` | Hairlines |
| `--grad` | `linear-gradient(135deg, #fef08a 0%, #4ade80 100%)` | Signature gradient — buttons, gradient text, chart accents, progress fills |
| `--grad-soft` | same stops at 15% opacity | Ambient background glow blobs behind section headers |

**Unofficial red:** `text-red-400` / `bg-red-400/70` shows up ad hoc (BMI "underweight" state, danger cues) with no named token — same gap the v1 doc flagged for `fitGreen`/`fitYellow`. Worth adding a `--red` token before this spreads further.

**Retired:** the marketing `accent` orange ramp (`#fb580f` and friends) is gone. Nothing in the new landing page references it.

---

## 3. Typography

Two type layers now, not one:

- **Inter** — headings and body copy. `text-4xl`–`8xl font-black tracking-tighter` for display type, `text-sm/base text-gray-400` for body. **Actually loaded this time** via a Google Fonts `<link>` in `<head>` — the v1 bug ("Inter referenced but never loaded, silently falls back to system sans") is fixed on this page specifically. The Product pages (`login.html`, `onboarding.html`, `dashboard.html`) still need the same `<link>` added — they weren't touched here.
- **JetBrains Mono** (`.font-mono`) — new layer, used for everything numeric or technical: section eyebrows (`/ 01 — Coach console`), stat values, badges, timestamps, chart labels, form inputs, footer build info. Convention: mono + `uppercase` + `tracking-wider` + 9–11px, almost always in `--muted` or `--dim`. This is what gives the page its "product datasheet" voice rather than a generic marketing tone — worth carrying into the dashboard's own stat labels for consistency.

---

## 4. Surfaces — `.glass` / `.glass-strong`

Replaces v1's `.glass-card`, which was independently redefined three times. Now two deliberate tiers instead of one ambiguous one:

| Class | Fill | Blur | Border | Used for |
|---|---|---|---|---|
| `.glass` | `rgba(20,20,20,.55)` | `blur(20px) saturate(140%)` | `--border` | Content cards — program cards, capability cards, calculator strip |
| `.glass-strong` | `rgba(15,15,15,.75)` | `blur(24px) saturate(160%)` | `--border-strong` | Chrome — nav bar, floating hero overlay cards, coach console frame |

Rule of thumb: **chrome and anything floating over imagery gets `.glass-strong`; in-flow content cards get `.glass`.**

---

## 5. Background treatments

- **`.grid-bg`** — 64px hairline grid (`rgba(255,255,255,.025)` lines), used behind the hero as ambient texture. Cheap, no image dependency.
- **`.hero-img`** — the "graded photo" pattern: a radial vignette + horizontal gradient + vertical gradient stacked over a hotlinked photo, then `contrast(1.05) brightness(.85) saturate(.7)`. Used on the hero and repeated (as `grayscale contrast-125` + a top-fade overlay) on each program card's background image. This is now a reusable pattern, not a one-off — extract it as a named utility if a third instance shows up.
- **Hotlinked images** (Unsplash) — same external-dependency risk v1 flagged. Now three additional hotlinks (one per program card) on top of the hero, so the exposure has grown, not shrunk.

---

## 6. Motion system (new — v1 had none)

| Name | Effect | Where |
|---|---|---|
| `.reveal` / `.reveal.in` | Fade + rise on scroll via `IntersectionObserver` | Every section header and card grid |
| `.float-1/2/3` | Slow independent vertical drift, staggered delay | The four floating overlay cards in the hero |
| `.pulse-dot` | Expanding-ring ping | Live-status indicators |
| `.grad-anim` | Animated gradient position shift | (available; not yet used on this page) |
| Marquee (`.marquee-track`) | Continuous horizontal scroll, 40s linear loop, duplicated content for seamlessness | Stats ticker band |
| `.tick` | Small rise-and-fade on value change | BMI calculator output numbers |
| `.btn-shine` | Diagonal light sweep on hover | Primary buttons |
| `.dot` (chat) | Staggered opacity blink | AI coach "typing…" indicator |

None of this exists on `login.html` / `onboarding.html` / `dashboard.html` today. That's a real tone gap now: the landing page feels alive, the product screens are static. Worth a deliberate call on whether motion migrates inward or stays a marketing-only device.

---

## 7. Data visualization (ApexCharts)

Three chart shapes, all dark-themed (`tooltip: { theme: 'dark' }`, mono font in tooltips/axes):

1. **Sparkline area** — hero weight trend, no axes, gradient fill fading to transparent.
2. **Full area with end marker** — the three member-progress charts (`makeWeightChart()`), smooth curve, gradient fill, a single marked point at the latest value. This is the same shape as the dashboard's own `#sexy-chart` — good, that consistency already exists.
3. **Horizontal distributed bar** — aggregate member breakdown, no axis labels, color carries the categorical meaning (green = on track, yellow = behind, translucent white = off track).

**Duplication flag:** `makeWeightChart()` is called three times with near-identical option objects, each ~20 lines. Same smell as v1's triple-defined `.glass-card` — worth factoring into one shared chart-options module if a fourth chart shows up anywhere in the app.

---

## 8. Page anatomy

Numbered-eyebrow convention (`/ 01 — Coach console`, `/ 02 — Programs`, `/ 03 — fiT-X profile`) gives the page a spec-sheet rhythm instead of a typical marketing scroll. Section order:

1. **Nav** — logo, in-page anchors (`#coach` `#programs` `#capabilities` `#progress`), sign in / start free.
2. **Hero** — headline + CTA + trust strip, with four floating `.glass-strong` cards over the graded photo (weight trend sparkline, calorie ring, streak, AI chat snippet).
3. **Stats marquee** — continuous ticker band.
4. **AI Coach console** — copy on the left, a mocked chat + action-button console on the right.
5. **Programs** — Lose / Gain / Maintain cards, each with a graded background photo, spec grid, and a sample day/week; closes with a **live BMI/TDEE calculator** wired to the same Mifflin-St Jeor math as `health.py`.
6. **Capabilities** — four cards, one per Coach Console action (log weight, log meal, log workout, schedule workout), framed as a "product datasheet" rather than credentials.
7. **Member progress** — three real-chart case studies (each with its own weight-trend chart) + one aggregate horizontal-bar summary.
8. **CTA** — full-bleed graded-photo band, single "Start free" button.
9. **Footer** — four link columns (Product / Account / Resources / Legal) + a version/build/geo strip (`v2.4.18` · `SHA 8f2a19x` · lat/lon).

---

## 9. Open decisions (not resolved by this doc)

- **Fake numbers.** Session counter (14,827), member counts (48,210 etc.), and the aggregate chart's data are hardcoded illustrative figures, not live queries. Decide before launch whether these stay as stylized marketing copy or get wired to real counts.
- **Product-page catch-up.** `login.html` / `onboarding.html` / `dashboard.html` still use the old single-tier `.glass-card`, Inter-only type (unloaded), and no motion system. Recommend porting `bg2`, JetBrains Mono, the two-tier glass system, and `.reveal` into a shared stylesheet so the whole app — not just the landing page — reads as one product.
- **Build metadata.** Footer's `v2.4.18` / `SHA 8f2a19x` are placeholders. Decide whether to template these from the real git SHA at deploy time or drop them.
- **No `base.html` still.** Same structural gap v1 called out — this page is another fully self-contained `<html>` document. The shared-layout fix remains unstarted.

---

## 10. Retired / confirmed dead

- Flowbite (CSS + JS) — not referenced anywhere in the new landing page. Only remaining reference is `test.html`, already flagged dead in v1.
- Marketing orange `accent` ramp — fully retired, no remaining references.
- Missing `<!DOCTYPE html>` — fixed on this page (was flagged in v1 as missing on `homepage.html`).
