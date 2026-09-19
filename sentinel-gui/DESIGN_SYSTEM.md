# Sentinel GUI — design system notes

Frontend-only. No backend contract, route, or API call changed, and no new dependencies were added.

## Structure
- `src/styles/tokens.css` — colour, type, spacing, radius, elevation, motion (the only place values are defined)
- `src/styles/base.css` — reset, typography, focus ring, `.sr-only`, reduced-motion
- `src/styles/layout.css` — app shell, sidebar (drawer ≤960px), topbar, page scaffolding
- `src/styles/components.css` — buttons, badges, forms, tables, alerts, dialog, toast, skeleton
- `src/styles/pages.css` — page-specific layouts
- `src/components/ui/` — primitives: `Button`, `Card`, `PageHeader`, `StatusPill`, `Tag`, `SeverityBadge`,
  `Timestamp`, `AlertBanner`, `EmptyState`/`ErrorState`, `Loading` (skeletons), `ConfirmDialog`, `Toast`,
  `Field`, `Pagination`, `LastUpdated`, `Icon`
- `src/utils/status.js` — the single status vocabulary (state → tone → label)

## Rules the UI follows
- **Status is never colour alone.** Every pill/dot has a text label; only genuinely in-progress states pulse.
- **Now vs. history are labelled.** Dashboard KPIs are tagged NOW / ALL TIME.
- **Freshness is honest.** `LastUpdated` measures from the last successful fetch; on a failed refresh the last
  data stays on screen with a warning instead of being replaced by an error.
- **Loading = skeletons** shaped like the content; **empty** and **error** states say what happened and what to do.
- **Consequential actions** use `ConfirmDialog` (native `<dialog>`; Cancel is focused first). Config changes keep
  the existing edit → review → confirm flow, now with a sticky "N unsaved changes" bar.
- **Accessibility:** skip link, visible `:focus-visible` ring, labelled fields (`Field`), `aria-current` on nav,
  `aria-expanded` on disclosures, table captions, focus-trapped drawer on small screens, `prefers-reduced-motion`.
- **Responsive:** sidebar becomes a drawer ≤960px; tables scroll inside their own container with the first column
  pinned; grids collapse 3→2→1.
- Contrast: all text tokens pass WCAG AA on every surface (`--text-3` was ~3.0:1 before, now ≥4.8:1).
