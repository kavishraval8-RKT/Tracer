# CHANGELOG

Authoritative timeline of Tracer's development. Updated on every commit.

Two naming schemes appeared during the SaaS pivot and this doc reconciles
them:

- **"Slice A–J"** are the small implementation units of the SaaS pivot as
  we planned them mid-session (~1–2 hrs each).
- **"SaaS-1 … SaaS-5"** are the phases in [`ROADMAP.md`](ROADMAP.md).

The mapping is:

| Slice   | Roadmap phase            | What it delivers |
|---------|--------------------------|------------------|
| A       | SaaS-1 + SaaS-2 frontend | Device Code Flow (RFC 8628), `/activate` page, JWT minting |
| B       | (was BYOK, dropped)      | Never built — BYOK dropped as an MVP requirement |
| C, D, E | SaaS-1 + SaaS-3 backend  | FastAPI scaffold + JWT dependency + `/v1/*` LLM proxy |
| F       | SaaS-3 quota half        | Quota + usage_ledger + 402 enforcement |
| G, H, I | SaaS-3 plugin cutover    | Plugin Login UI + JWT storage + `agent.py` → HTTP swap |
| J       | SaaS-3 UX polish         | 401/402/5xx error surfaces in the plugin |
| —       | SaaS-4                   | (Not yet started) Deploy to Render + Vercel |
| —       | SaaS-5                   | (Not yet started) Stripe subscription tiers |

Two repositories are in play:
- **`tracer-engine`** — the KiCad plugin + Python cloud backend (this repo).
- **`tracer-frontend`** — the Next.js web app + Supabase.

Commit hashes below are `engine/frontend` when both are involved.

---

## Unreleased

_(Moves here as work lands. Empty means the working tree matches the last
tagged commit.)_

**Phase-2.1 test harness (initial pass):**

- `tracer_helpers.py` — stdlib-only home for pure logic that previously
  lived inline inside `sidecar_app.py`. `sidecar_app.py` re-exports:
  - `_project_from_title` (moved verbatim — the inline asserts moved
    to `tests/test_project_title.py`)
  - `parse_drc_json` (extracted from `PcbDrcWorker.run` — the roadmap
    called this out explicitly as "extract from run() first")
  - `resolve_sch_from_recent` / `resolve_sch_from_file_history`
    (extracted from `KiCadWatcher.find_open_schematic`; the `exists`
    predicate is injectable so tests never touch the filesystem)
- `tests/` at repo root — 35 pytest cases, ~2s runtime:
  - `test_project_title.py` (8 cases)
  - `test_drc_parse.py` (7 cases)
  - `test_sch_resolver.py` (11 cases)
  - `test_agent_helpers.py` (9 cases) — covers `_cap_history` + the
    attachment-free path of `_to_contents`, reloading the module under
    a scoped `TRACER_HISTORY_CAP` env var.
- `tests/conftest.py` — one-liner that puts the repo root on
  `sys.path` so `import tracer_helpers` and `from backend.app import
  agent` both work without a package install.

**Roadmap updates:**
- 2.1 marked ✅ with a "still uncovered" list (KiCadWatcher title loop,
  undo ledger, clarification state transitions, agent auto-repair).
- 2.5 marked ✅ partially — the hard cap shipped in the polish pass and
  now has tests; summarization is deferred until a tester actually
  reports lost context across the cap boundary.
- "Test coverage: one 15-line focus-window test" line replaced with the
  actual current state.

**Pre-beta hardening + beta doc:**

- `tracer_version.py` — single source of truth for plugin version
  (`TRACER_VERSION = "0.1.0"`). Logged on sidecar startup and sent as
  `X-Tracer-Version` on every backend request so server logs can be
  matched against bug reports.
- Backend `app/main.py` — one-line access log middleware per request:
  `[req] POST /v1/ask -> 200 3421ms plugin=0.1.0 ip=1.2.3.4`. Writes
  to stdout, so Render aggregates it automatically.
- `sidecar_app.py` — dedicated `CloudUnreachableError` branch. Beta
  users now see *"Backend unreachable — check your internet…"* instead
  of a raw `HTTPSConnectionPool(...)` traceback.
- `assets/chat.html` — Copy button rewritten a second time. Per-button
  `addEventListener` with closure-captured markdown + hidden textarea
  fallback. `pyBridge.copyToClipboard` is now tried FIRST since
  `navigator.clipboard` fails silently in `file://` QWebEngine pages.
  (Diagnostic stderr traces from the previous attempt confirmed the
  delegated handler never fired.)
- `docs/BETA_TESTING.md` — long-form beta tester guide, **localhost
  edition**: testers clone both repos, apply the Supabase schema,
  run `uvicorn` on `:8100` + `npm run dev` on `:3000`, drop a
  `.tracer.env` next to `sidecar_app.py` to point the plugin at
  local backends. Covers install, test matrix (Ask / Plan / Agent
  / quota / offline / re-link), architecture diagram, bug-report
  checklist with which files/lines to attach. No prod URL is
  referenced — nothing is deployed yet.

**Pre-beta audit (documented, no code change):**

- Secrets: `grep -rEn "AIza…|sk-…|eyJhbGciOi…"` outside `.venv` and
  `node_modules` — no keys committed. Only hits are the local `.env`
  files, which `.gitignore` excludes and `git ls-files` confirms are
  not tracked.
- Supabase RLS: `schema.sql` has `enable row level security` on
  every table. Only `profiles` and `usage_ledger` expose select-own
  policies (`(select auth.uid()) = id/user_id`). `waitlist`,
  `contact_messages`, `device_codes` have RLS on with no policies →
  service-role-only, as intended.
- Plugin URL: `tracer_auth.DEFAULT_API_URL = "https://api.tracer.dev"`
  and `tracer_api._api_url()` reads that default. Localhost only via
  `TRACER_API_URL` env override or `.tracer.env`.

**Still TODO before flipping traffic to prod:**

- Pin `TRACER_CORS_ORIGINS` to the prod Vercel URL in Render env.
- Deploy backend (Render) + frontend (Vercel), run one full E2E on
  prod URLs before inviting testers.

Bumped agent client timeouts + halved MAX_REPAIRS (from earlier in this session):

- `tracer_api.py` per-endpoint timeouts — `/v1/generate` 300s (was 120s),
  `/v1/ask` and `/v1/plan` 45s, `/v1/me` and `/v1/usage` 10s.
- `agent.MAX_REPAIRS: 2 → 1`. Bump back to 2 once we're on paid
  Anthropic/OpenAI.

---

## 2026-09-20 — Polish pass

**engine `547f55e` + frontend `7908052`**

Four items shipped as one integrated pass, plus session/weekly quota
model (Claude-style).

### Copy button rewrite

- Replaced inline `onclick` with JSON-stringified content with a
  delegated click handler using `data-copy-idx`.
- Raw markdown stashed in a `_copyStore` array, button carries the index.
- Robust to messages with quotes, backslashes, emoji, backticks, or
  newlines.
- `clearAll()` empties the store on New Conversation.

### Flash for Ask/Plan

- New `FAST_MODEL_CANDIDATES` list ordered `gemini-2.5-flash` first.
- `_chat_gemini` takes `model_candidates` param; `ask_gemini` and
  `plan_gemini` pass Flash.
- Agent (`query_gemini`) stays on Pro — needs structured JSON.
- Free-tier Flash quota is 250 req/day (10× Pro).

### History cap

- `HISTORY_CAP_TURNS = 12` (env override `TRACER_HISTORY_CAP`).
- `_cap_history()` applied in both Ask/Plan and Agent code paths.
- Long chats no longer accumulate token cost linearly; also cuts
  latency since Gemini scans less input.

### Session + weekly quotas (Claude-style two-window)

Replaces the flat monthly quota. Every request checks BOTH windows:

| Tier | 5-hr session cap | 7-day weekly cap |
|---|---|---|
| Hobby (free) | 30k tokens | 100k tokens |
| Basic ($10/mo) | 300k tokens | 1M tokens |
| Pro ($20/mo) | 1.3M tokens | **4M tokens** |

- `quota.check_quota` checks weekly first, then session, surfaces
  distinct error shapes (`window: "weekly" | "session"` + `reset_hours`).
- Plugin 402 bubble reads e.g. *"hobby tier: used 30,000 / 30,000 tokens
  (5-hour session). Try again in a couple of hours."*
- `/v1/usage` returns both windows: `{session_used, session_quota,
  weekly_used, weekly_quota, session_remaining, weekly_remaining,
  session_hours}`.
- `/billing` page renders both bars stacked with independent 85%/100%
  warning states.

---

## 2026-09-19 — Docs rewrite for SaaS reality

**engine `f60dddf`**

- README + SETUP fully rewritten around cloud auth. Old GEMINI_API_KEY
  requirement removed from install steps.
- New "First run — sign in" walkthrough (Sign in → browser opens →
  paste 8-char code → welcome).
- New "Account & usage" section pointing at the top-right icon and
  the `/billing` page.
- Troubleshooting rewritten around the sign-in flow: overlay stuck,
  "Signed out" mid-session (jti rotation), "Quota exceeded" bubble.
- Local development section explains `.tracer.env` for pointing the
  plugin at localhost during dev.

---

## 2026-09-19 — Slice F: quota + usage_ledger

**engine `c0c281c` + frontend `5f29092`**

### Backend (Slice F)

- `backend/app/quota.py`: `check_quota` sums usage_ledger for the
  current month via Supabase REST, raises HTTP 402 with `{tier,
  used_tokens, quota_tokens}`.
- `backend/app/settings.py`: `TIER_QUOTAS` dict (hobby=200k,
  basic=1M, pro=10M — later replaced by the two-window model).
- `backend/app/routers/v1.py`: every route runs
  `quota.check_quota → agent call → write_ledger`. Failures still
  write a 500 status row for triage.
- Sync `agent.*` calls wrapped in `run_in_threadpool` — was
  blocking the async event loop and causing 120s plugin timeouts.
- Structured exception body: `{error, message[:500], type}`. Was
  previously bare "Internal Server Error".
- New `GET /v1/usage` for the dashboard.

### Plugin

- `tracer_api.py`: 402 body parsed into a human message.
- `sidecar_app.py`: 402 chat bubble reads *"hobby tier: used
  210,000 / 200,000 tokens this month"* and points at the Account
  icon.

### Frontend dashboard

- `/billing` page: `UsageCard` with a monthly progress bar +
  by-mode breakdown, reading `usage_ledger` directly via Supabase
  (RLS-gated). Later replaced by the two-window card.

---

## 2026-09-19 — Plugin cutover to cloud (Slice G+H+I+J)

**engine `8894343`**

The plugin now routes every LLM call through the backend. No local
`GEMINI_API_KEY` required.

### Slice G — Login UI

- `tracer_auth.py`: RFC 8628 client. `DeviceFlow` runs on a daemon
  thread, cancellable, callbacks marshalled through the Qt bridge.
  Uses `webbrowser.open` to launch the `/activate` page.
- Chat panel gains a full-window overlay for `logged_out` (Sign in
  button) and `pending` (user_code + Open browser + Cancel + pulsing
  waiting indicator). `logged_in` hides it.

### Slice H — JWT storage

- `%APPDATA%\Tracer\token.json` on Windows,
  `~/.config/tracer/token.json` on POSIX (0600 perms). Loaded on
  shell init so next launch skips the login flow.

### Slice I — LLM proxy client

- `tracer_api.py` mirrors `agent.build_with_auto_repair` /
  `ask_gemini` / `plan_gemini` signatures as HTTP POSTs to
  `TRACER_API_URL/v1/*` with `Authorization: Bearer <jwt>`.
- `GenerateWorker` and `AskWorker` call `tracer_api.*` instead of
  `agent.*`.

### Slice J — Error taxonomy

- `NotAuthenticatedError` (401): clears local token, overlay
  reappears with "Signed out. Please sign in again."
- `QuotaExceededError` (402): chat bubble points at the Account icon.
- `CloudUnreachableError` (network / 5xx / non-JSON): full body
  streamed into Activity.

### Bonuses in this commit

- **Settings icon** in the chat header — direct
  `pyBridge.openAccountPage()` opens `<TRACER_WEB_URL>/billing`.
- **Removed drag from the chat header.** The KiCadWatcher re-anchors
  the panel every 500ms so drag rubber-banded. Also fixed a subtle
  bug where the header's `onmousedown` drag handler was swallowing
  the account button's clicks.
- **`.tracer.env` file fallback** for URL config. Solves the Windows
  Explorer-stale-env-snapshot problem: processes launched by
  Explorer inherit its env from Explorer startup, so User-scope env
  vars set later don't reach the KiCad-spawned sidecar. Precedence
  is `process env → .tracer.env → prod default`.

---

## 2026-09-18 — Cloud backend (Slice C+D+E)

**engine `45d7d7f` + frontend `ab3fb85`**

The auth backbone. Cloud backend runs on port 8100 with JWT-gated
`/v1/*` endpoints backed by `agent.py`.

### Frontend (Slice A)

- `supabase/schema.sql`: `device_codes` (opaque device_code +
  8-char user_code + TTL + poll interval + approved_user_id) and
  `usage_ledger` (RLS: read-own, service-role write). Added
  `active_device_jti` + `active_device_linked_at` columns on
  `profiles` — the load-bearing column for single-plugin
  enforcement.
- `lib/device-flow.ts`: `newDeviceCode` (base64url), `newUserCode`
  (Crockford b32 minus IOLU → ABCD-1234), `mintDeviceJwt` (HS256,
  30-day TTL, claims `{sub, tier, jti, iss, aud, iat, exp}`),
  `newJti` (16-byte base64url).
- `POST /api/device/code`: RFC 8628 §3.2 authorization response.
- `POST /api/device/token`: RFC 8628 §3.5 access-token endpoint.
  Returns `authorization_pending` / `expired_token` /
  `invalid_grant` per spec. On approval: mints jti, atomically
  rotates `profiles.active_device_jti` (single-plugin enforcement
  lives here), mints JWT, deletes the row (single-use).
- `/activate` page + `ActivateForm` client component: signed-in
  user pastes the user_code, sees green "Linked" message. Signed-
  out users get bounced to `/login?next=/activate?code=...`.
- `lib/actions/auth.ts` + `app/login/page.tsx`: `signIn` and
  `signUp` honor `?next=` param.

### Backend (Slice C+D+E)

- New `backend/` directory: FastAPI + async SQLAlchemy + Alembic
  scaffold (later simplified when Supabase became the single source
  of truth).
- `app/settings.py`: `TRACER_DEVICE_JWT_SECRET` (must match
  frontend), `TRACER_SUPABASE_URL`, `TRACER_SUPABASE_SERVICE_ROLE_KEY`,
  `TRACER_GEMINI_API_KEY`.
- `app/auth.py`: `require_user` dependency. HS256 verify + iss/aud/
  exp check, then Supabase REST for `active_device_jti` — 401
  `revoked` when the JWT's jti no longer matches.
- `app/agent.py`: copied from the plugin's `agent.py` (canonical
  copy while the plugin still has its own; the plugin's copy goes
  away once Slice I is stable).
- `app/routers/v1.py`: `POST /v1/generate|ask|plan` behind auth,
  plus `GET /v1/me` for post-login sanity.

### Docs

- New `docs/SAAS_ARCHITECTURE.md`: end-to-end architecture reference.
- `docs/ROADMAP.md`: added the SaaS distribution track.

---

## 2026-09-13 — Modes, markdown, undo modal, custom dropdowns, attachments

**engine `f433aec`**

Big features PR before the SaaS pivot started.

- **Ask/Plan lightweight LLM path** via `agent._chat_gemini`. Plan
  outputs a design outline (components/nets/notes) as text — no
  backend build, no schematic gen, no inject.
- **Debug mode**: verbose Activity lines at send time.
- **Clarification-aware routing**: while Agent has a pending
  clarification, the next Send is pinned to the Agent worker
  regardless of the mode dropdown. Dropdown greys out, send button
  turns amber.
- **Undo history**: timestamped `.tracer-bak.<ts>` snapshots per
  successful inject (sch + sibling pcb). Modal opens in the chat
  panel (620px) — was originally in the 130px dock and couldn't
  render the list.
- **Custom dropdowns** (mode / model / attach): replaced native
  `<select>` so the open list stays inside the design system. Zero
  geometry churn (dock permanently 300px + `QRegion` input mask).
  Setting geometry on a translucent frameless window forced a
  Chromium repaint every time — that was the source of the flicker.
- **Attachments** (images only for MVP): `+` opens a menu with
  Attach file… / Screenshot KiCad. Chips above the textarea, max 3,
  click × to remove.
- **DRC + schematic-parity**: post-inject
  `kicad-cli pcb drc --schematic-parity` streams violations into
  Activity.
- **Markdown rendering** in assistant bubbles (headers, bold, code,
  lists) + bridge-routed clipboard copy (later regressed and rewired
  in the Polish pass above).
- **Auto-target resolver** improvements — parses window titles +
  walks KiCad's `system.file_history` for `.kicad_sch` even when
  only PCB Editor is open.

---

## 2026-09-13 — Reorganize: legacy_plugin split

**engine `c3058df`**

Moved the wxPython in-process route + the original sidecar
architecture doc + dev-only scripts to `legacy_plugin/`, so the root
reflects only the current PySide6 IPC architecture.

- Root now contains the working stack: `sidecar_app.py`,
  `assets/{chat,dock}.html`, `tracer_plugin/` (IPC entry),
  `tracer_ast/`, `deploy.py`.
- `legacy_plugin/` carries: the original 26KB README, the sidecar
  architecture blueprint, the wxPython UI, dev diagnostics,
  deployment session artifacts. `WHY_LEGACY.md` inside explains
  what each file was and what replaced it.

---

## Earlier (pre-session)

- **`672a65a`** — Expand README into a full setup and first-run guide.
- **`135e4ad`** — Document API key setup for testers, add `agent.py --check-key`.
- **`ea24d7d`** — Initial release of Tracer Copilot.

---

## How to update this file

- **Every commit** that ships a user-visible or architectural change
  adds an entry to `## Unreleased`.
- **At tag time** the `Unreleased` block moves down under a dated
  heading with the commit hash(es).
- **Two hashes** (`engine/frontend`) when the commit touches both.
- Keep it concrete — what shipped, why, and any breaking notes.
- Deleted work also gets a line (see BYOK under Slice B above).
