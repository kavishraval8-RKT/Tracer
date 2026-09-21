# Tracer Copilot — Technical Overview

> **Purpose of this document.** Internal reference. Everything you need to
> answer a mentor's or evaluator's question about the project without
> hunting through the codebase. Written for someone who *already* has git
> access — file paths are clickable and named for `grep`-ability.
>
> Companion reads: `README.md` (installation), `SETUP.md` (dev machine),
> `docs/SAAS_ARCHITECTURE.md` (cloud design), `docs/ROADMAP.md` (what's
> next), `docs/CHANGELOG.md` (what shipped when).

Last updated: 2026-09-21.

---

## 1. What Tracer is (one paragraph)

Tracer Copilot is a **KiCad plugin** that puts an AI chat panel next to
your open schematic. You describe the circuit in plain English; Tracer
designs it, wires it up, saves the `.kicad_sch`, and explains what it
did. The plugin runs on the user's Windows PC and talks to our cloud
backend, which handles authentication, quota, and the LLM. **No API
key is ever handled by the user.**

Three interaction modes (`Ask` for questions, `Plan` for design
outlines, `Agent` for actually building the circuit) plus a fourth
`Debug` mode that surfaces the raw activity log.

---

## 2. The product surface — what users see

- **Chat panel** on the right of the KiCad schematic editor, transparent,
  frameless, always-on-top.
- **Dock pill** at the bottom holding the mode selector, model dropdown,
  target-file indicator, and undo button.
- **Activity feed** streaming per-step logs (`[Act]`, `[Obs]`, `[Err]`,
  `[Sys]` categories) while an Agent turn runs.
- **Changes panel** showing which schematic lines got added / modified /
  removed. Post-Agent `Keep changes` / `Discard` buttons.
- **Undo modal** with a timestamped list of every Agent turn's snapshot;
  one click reverts.
- **Copy button** on every assistant message → raw markdown to clipboard.

None of this is a browser overlay — it's real PySide6 windows glued to
KiCad's window with pywin32-based window detection.

---

## 3. Tech stack (with why-this-not-that)

### Plugin (client)

| Layer | Tech | Why this |
|---|---|---|
| KiCad plugin API | KiCad 10 IPC plugin | Native plugin format; can spawn processes |
| Sidecar process | Python 3.11+ | KiCad's Python; avoids version mismatch |
| UI | PySide6 (Qt) | Frameless overlays, transparency, `QRegion` mask for click-through — wxPython (KiCad's own toolkit) can't do that reliably |
| Chat & dock HTML | QWebEngineView + QWebChannel | Real browser rendering; two-way Python↔JS bridge for signals |
| Window detection | pywin32 | KiCad doesn't publish window handles; we EnumWindows |
| HTTP to backend | `requests` | Stdlib-adjacent; no async needed on client side |

**Why Windows-only right now:** pywin32 window discovery + Ctrl+F5 reload
keystroke are Windows-specific. Ports to macOS (AppleScript) and Linux
(xdotool/wmctrl) are documented in `docs/ROADMAP.md` Phase 4.1.

### Backend

| Layer | Tech | Why this |
|---|---|---|
| Server framework | **FastAPI** | Pydantic-native validation, async, first-class type hints |
| Async runtime | uvicorn | FastAPI's default; standalone-friendly for Render |
| LLM SDK | google-generativeai (Gemini) | Currently free-tier; swappable via `agent.py` provider layer |
| Sync wrapping | `fastapi.concurrency.run_in_threadpool` | Agent code is sync; keeps FastAPI's event loop unblocked |
| JWT verify | PyJWT | HS256 with `jti` freshness against Supabase |
| HTTP → Supabase | httpx (async) | Non-blocking Supabase REST calls |

### Data & auth

| Layer | Tech | Why this |
|---|---|---|
| Auth | **Supabase Auth** | Email/password, JWT, RLS out of the box — cheaper than Auth0/Clerk |
| DB | Supabase PostgreSQL | Same project, one login, no cross-service secrets |
| RLS | Postgres row-level security | Users can only ever see their own rows; enforced in the DB, not the app |
| Frontend framework | Next.js 16 + React 19 + TypeScript | SSR for landing/legal pages; server actions for form handling |
| Frontend hosting | **Vercel** | Zero-config Next.js deploys; free tier fits us for beta |
| Backend hosting (planned) | **Render** | Free tier, GitHub-connect deploy, stdout log aggregation |
| Payments (deferred) | Stripe | Industry standard; webhook mints subscription → tier upgrade in `profiles` |

### The one non-obvious choice

**Two-window quota model** (Claude-style) instead of the usual "monthly
budget". Every request has to fit under **both** a rolling 5-hour
"session" cap **and** a rolling 7-day "weekly" cap. Reason: prevents a
single user from draining our shared free-tier Gemini quota (25 req/day
for ALL users of the service) in an afternoon. See §7 for numbers.

---

## 4. Repositories & how they fit

The project is split across **two repos**:

```
kavishraval8-RKT/Tracer            (engine — plugin + backend)
├── tracer_plugin/                 KiCad plugin entrypoint
├── sidecar_app.py                 The PySide6 sidecar (1550 lines)
├── sidecar_launcher.py            Spawns sidecar detached from KiCad
├── tracer_auth.py                 Device-flow client (RFC 8628)
├── tracer_api.py                  HTTP client to the backend
├── tracer_helpers.py              Pure logic (testable, stdlib-only)
├── tracer_version.py              TRACER_VERSION = "0.1.0"
├── backend/                       FastAPI cloud backend
│   └── app/
│       ├── main.py                Uvicorn entrypoint, CORS, access log
│       ├── settings.py            Env-driven config (Pydantic)
│       ├── auth.py                JWT verify + jti freshness
│       ├── quota.py               Two-window token accounting
│       ├── agent.py               LLM orchestration (Gemini + repair loop)
│       └── routers/v1.py          /v1/ask, /v1/plan, /v1/generate, /v1/me
├── assets/
│   ├── chat.html                  Chat panel (Tailwind + QWebChannel)
│   └── dock.html                  Dock pill
├── tests/                         35 pytest cases (~2s)
└── docs/                          You are here

kavishraval8-RKT/tracer_frontend   (frontend — Next.js on Vercel)
├── app/
│   ├── page.tsx                   Landing
│   ├── activate/                  Device-code confirmation UI
│   ├── billing/                   Account + usage dashboard
│   ├── api/device/                Device-flow REST endpoints
│   └── (legal, contact, etc.)
├── lib/
│   ├── supabase/                  Server + admin clients
│   ├── actions/                   Server actions (auth, activate, forms)
│   ├── device-flow.ts             JWT minting for the plugin
│   └── quotas.ts                  Tier caps (mirror of backend/settings.py)
└── supabase/schema.sql            All tables + RLS policies
```

---

## 5. End-to-end architecture — how a user prompt travels

```
  KiCad Schematic Editor (user's PC, Windows)
         │
         │ 1. User opens plugin, types "Add a bypass cap to U1"
         ▼
  ┌──────────────────────────┐
  │ sidecar_app.py           │ PySide6 chat panel; QWebChannel bridge to
  │ (OverlayShell.on_send)   │ Python. Routes to Ask/Plan/Agent worker.
  └────┬─────────────────────┘
       │ 2. GenerateWorker runs in a QThread; calls tracer_api
       ▼
  ┌──────────────────────────┐
  │ tracer_api.build_with_   │ HTTP POST /v1/generate with the prompt,
  │ auto_repair              │ history, and Bearer <device JWT>.
  └────┬─────────────────────┘
       │ 3. HTTPS
       ▼
  ┌──────────────────────────┐    ┌────────────────────────────┐
  │ FastAPI backend          │ ──►│ Supabase (Postgres + Auth) │
  │ (uvicorn on Render)      │    │  - profiles (tier, jti)    │
  │                          │    │  - usage_ledger (append)   │
  │ 4a. auth.require_user    │◄── │  - device_codes            │
  │     verifies JWT, checks │    └────────────────────────────┘
  │     jti freshness        │
  │ 4b. quota.check_quota    │
  │     sums 5h + 7d windows │
  │ 4c. agent.build_with_    │    ┌────────────────────────────┐
  │     auto_repair          │ ──►│ Gemini API                 │
  │     (LLM + repair loop)  │    │ (free tier, model fallback)│
  │ 4d. Write usage_ledger   │    └────────────────────────────┘
  │     row with tokens spent│
  └────┬─────────────────────┘
       │ 5. JSON {status, circuit, backend, history}
       ▼
  ┌──────────────────────────┐
  │ InjectWorker             │ Takes the returned circuit spec and edits
  │ (tracer_inject.py)       │ the user's .kicad_sch file (text-block
  │                          │ manipulation for now — AST rewrite in
  │                          │ roadmap phase 2.3).
  └────┬─────────────────────┘
       │ 6. .kicad_sch on disk updated + snapshot saved to .tracer-bak
       ▼
       Ctrl+F5 sent to KiCad → schematic reloads → user sees the change.
```

**Auth is a separate one-time flow** (before step 1):

```
Plugin                 Frontend (Next.js)      Supabase
  │                        │                      │
  │ 1. POST /api/device/   │                      │
  │    code                │                      │
  ├───────────────────────►│                      │
  │                        │ 2. INSERT into       │
  │                        │    device_codes      │
  │                        ├─────────────────────►│
  │◄───────────────────────┤                      │
  │  {device_code,         │                      │
  │   user_code: ABCD1234} │                      │
  │                        │                      │
  │ 3. webbrowser.open(    │                      │
  │  /activate?code=ABCD…) │                      │
  │  User confirms in      │                      │
  │  browser after signing │                      │
  │  in to Supabase.       │                      │
  │                        │ 4. UPDATE            │
  │                        │    device_codes SET  │
  │                        │    approved_user_id  │
  │                        ├─────────────────────►│
  │                        │                      │
  │ 5. Poll POST /api/     │                      │
  │    device/token every  │                      │
  │    5s                  │                      │
  ├───────────────────────►│ 6. mint HS256 JWT    │
  │                        │    with fresh jti,   │
  │                        │    save to profiles. │
  │                        │    active_device_jti │
  │                        ├─────────────────────►│
  │◄───────────────────────┤                      │
  │  {access_token: eyJ…}  │                      │
  │                        │                      │
  │ Plugin saves token to  │                      │
  │ %APPDATA%/tracer/      │                      │
  │ token.json (mode 600). │                      │
```

RFC 8628 Device Authorization Grant. The JWT holds `sub` (user id),
`tier` (hobby/basic/pro), `jti` (unique id for revocation), signed
HS256 with a secret that lives on **both** frontend and backend
(`DEVICE_JWT_SECRET`).

---

## 6. Authentication in depth

### Why device flow instead of OAuth code + PKCE?

The plugin isn't a web app; it has no callback URL. Device flow is the
standard pattern for "shell app on your PC needs auth from a browser
session on the same machine." Same pattern GitHub CLI, Netflix on your
TV, Spotify on a smart speaker all use. RFC 8628.

### Single-plugin-per-user enforcement

Each JWT carries a `jti` claim (a UUID). On mint (`/api/device/token`
in the frontend), we write that `jti` into `profiles.active_device_jti`
for that user.

On every backend request, `auth.require_user` re-reads the current
`active_device_jti` from Supabase and rejects with 401 if it doesn't
match the JWT's `jti`.

A second plugin activating on another machine → new jti minted → the
first plugin's next call returns 401 → sidecar drops to the sign-in
overlay automatically. The user cannot run two plugins at once.

### Sign-out clears the plugin too (fixed 2026-09-20)

The frontend's `signOut` server action now nulls `active_device_jti`
via the service-role admin client **before** calling
`supabase.auth.signOut()`. That way the plugin's next request 401s the
same way as a re-activation would. See `lib/actions/auth.ts:87` and the
matching backend fix in `backend/app/auth.py:98`.

**Failure mode this fixed:** user signed out on the web, plugin kept
working because its JWT was never rotated. Caught in beta testing on
2026-09-20, patched same day.

### Token storage on disk

`tracer_auth.save_token` writes to
`%APPDATA%/tracer/token.json` with `chmod 600` (via `os.chmod` after
`os.O_CREAT | os.O_EXCL`). Anyone reading the file needs the same OS
user. On corporate machines with disk encryption at rest this is
adequate for beta.

### What we haven't done yet (honest list)

- **JWT secret rotation** — no plan for what happens if
  `DEVICE_JWT_SECRET` leaks. Needs a rollover mechanism (dual-key
  window, then retire old key). Non-trivial. Deferred until public
  launch.
- **Refresh tokens** — current JWTs expire after ~30 days and the
  plugin re-runs the full device flow. For beta that's fine (they
  activate maybe twice); for prod we want a refresh token so they
  never think about it.

---

## 7. Quota system

Two rolling windows, both must pass.

### The tiers

```python
# backend/app/settings.py
tier_session_hours: int = 5
tier_session_quotas: dict[str, int] = {
    "hobby":   30_000,   # ~30 short chats before you have to wait
    "basic":  300_000,
    "pro":  1_300_000,
}
tier_weekly_quotas: dict[str, int] = {
    "hobby":  100_000,
    "basic": 1_000_000,
    "pro":   4_000_000,
}
```

Mirrored in `tracer-frontend/lib/quotas.ts` so the UI can render
progress bars without a round-trip.

### How the check works

`backend/app/quota.py::check_quota(user_id, tier)`:

1. Sum `prompt_tokens + completion_tokens` from `usage_ledger` where
   `created_at > now() - 7 days` (weekly window).
2. If sum ≥ weekly cap for tier → raise `HTTPException(402, {window: "weekly", …})`.
3. Sum same over the last 5 hours (session window).
4. If sum ≥ session cap → raise `HTTPException(402, {window: "session", reset_hours: 5})`.
5. Otherwise pass.

Both queries hit Supabase REST (`select prompt_tokens,
completion_tokens from usage_ledger where user_id=eq.<uuid> and
created_at=gte.<iso>`).

**Fail-open on ledger read errors.** If Supabase is unreachable, we
let the request through and log the error. Reason: we'd rather cost a
few tokens than break every user during a network blip. See
`quota._sum_since`.

### Why two windows

- **Weekly** = economic bound. What we can afford to pay upstream at
  that tier.
- **Session** = fairness bound. Even a pro user shouldn't be able to
  burn 4M tokens in an afternoon and page us at 3am because Gemini
  free-tier for everyone else is dead.

Same idea as Anthropic's own two-window quota on Claude.

---

## 8. LLM orchestration

Lives in `backend/app/agent.py`.

### Model layer

```python
MODEL_CANDIDATES        = ["gemini-2.5-pro", "gemini-1.5-pro", ...]
FAST_MODEL_CANDIDATES   = ["gemini-2.5-flash", ...]  # for Ask/Plan
HISTORY_CAP_TURNS       = 12    # env override: TRACER_HISTORY_CAP
MAX_REPAIRS             = 1     # will bump to 2 once on paid Claude/OpenAI
MAX_CLARIFICATIONS      = 4
```

Fallback chain: if `gemini-2.5-pro` returns rate-limit or 5xx, the
`_working_model` register moves to the next candidate. Sticky per
process — once a model works, we keep using it until it fails.

Ask and Plan use Flash by default (cheaper, faster, quality delta is
imperceptible for Q&A). Agent uses Pro because it has to output
structured circuit specs and repair on validator errors.

### The auto-repair loop

`agent.build_with_auto_repair` runs up to `MAX_REPAIRS + 1` LLM calls
per Agent turn:

1. Call the LLM with the prompt + history + schematic context.
2. Parse the returned JSON circuit spec.
3. Run the local schematic backend validator (`backend.build_schematic`).
4. If validator passes → return.
5. If validator rejects → append the error message to history, call
   again. Repeat up to `MAX_REPAIRS` times.
6. Also handles clarifications (up to `MAX_CLARIFICATIONS`) —
   assistant asks a question, plugin surfaces it to the user, user
   answers, loop continues without burning a repair budget.

### History cap

Every LLM turn re-sends the full conversation. A 20-turn user is
paying for 20 turns of input context on every new turn. `_cap_history`
truncates to the last N turns before the call — hard cap, no
summarization. Ships in the polish pass (2026-09-20).

---

## 9. Schematic mutation pipeline

Runs on the user's PC after the backend returns a circuit spec.

```
backend response
    │
    ▼
InjectWorker (QThread)                        agent.py's local backend
    │                                         validates the spec against
    │  1. Snapshot current .kicad_sch          KiCad rules BEFORE injection
    │     to .tracer-bak.<TS>                  is even attempted.
    │
    │  2. tracer_inject.py applies text-block
    │     mutations to the .kicad_sch source
    │
    │  3. save
    │
    │  4. Emit `changesFinalized` → the UI
    │     shows Keep/Discard buttons.
    │
    ▼
send_reload_to_kicad()   ← Ctrl+F5 keystroke to KiCad's window handle
                            so the editor rereads the file.
```

### Known fragility

`tracer_inject.py` does **text-block** manipulation of `.kicad_sch`
(s-expression source). This works today; it will break the day
KiCad 10.1 changes a token or reindents. The fix is
`tracer_ast/` — a real s-expression parser and typed mutator suite —
scoped as roadmap Phase 2.3, deferred until the failure actually
surfaces (see §12).

### Undo ledger

Every successful inject writes a `.tracer-bak.<YYYYMMDD_HHMMSS>` file
next to the schematic and appends an entry to `_undo_ledger` on the
`OverlayShell`. The undo modal in the UI is a list-view of these
entries; clicking one restores that snapshot and sends Ctrl+F5.

---

## 10. Frontend responsibilities

The Next.js app on Vercel does **five things**:

1. **Auth pages** — sign up, sign in, password reset. Thin wrappers
   over Supabase Auth.
2. **Device-flow endpoints** — `/api/device/code` mints a code,
   `/api/device/token` polls and returns the JWT once approved.
   Uses the admin client because they write to a service-role-only
   table (`device_codes`).
3. **`/activate` page** — where users confirm the plugin's user code
   after signing in. Includes CSRF-safe server action.
4. **Billing dashboard** — reads `usage_ledger` (RLS-scoped), computes
   the two window bars, shows the breakdown by endpoint.
5. **Landing + legal + contact** — public marketing surface, waitlist
   form, contact form (rate-limited by IP).

No LLM traffic touches the frontend. The plugin talks straight to the
backend. The frontend never sees prompts.

### Rate limits already in place

`tracer-frontend/lib/guard.ts::rateLimit` — in-memory sliding window
keyed by IP. Used on:

- `signup`: 5 per IP per 10 min
- `signin`: 10 / 10 min
- `activate`: 10 / 10 min
- `waitlist` form: 5 / 10 min
- `contact` form: 3 / 10 min

Frontend-only. Backend `/v1/*` is unprotected against per-user request
flooding right now — flagged as a pre-launch hardening item.

---

## 11. Testing

Extracted in roadmap Phase 2.1, landed 2026-09-20.

- 35 pytest cases, ~2s runtime.
- No Qt, no win32, no Gemini in the test path.
- Pure logic lives in `tracer_helpers.py` so tests can import without
  pulling PySide6, pywin32, or google-generativeai.
- Coverage:
  - `_project_from_title` — window-title parser
  - `parse_drc_json` — kicad-cli DRC JSON → summary
  - `resolve_sch_from_recent` / `resolve_sch_from_file_history` —
    schematic-path resolvers (with injectable filesystem probe)
  - `_cap_history` — history truncation
  - `_to_contents` — Gemini SDK shape converter

Run: `backend/.venv/Scripts/python -m pytest tests/ -q`

### What's still uncovered

- `KiCadWatcher.find_open_schematic` EnumWindows callback loop
- `_undo_ledger` snapshot + restore round-trip
- `_pending_clarification` state transitions
- `build_with_auto_repair` (needs mocked Gemini + validator)

Each of these needs a small mocking shim. Batched into "second 2.1
pass" — worth doing before any refactor of `sidecar_app.py`.

---

## 12. Known limitations & how we're addressing each

Ordered by severity.

### 12.1 Model quality on free-tier Gemini

**Problem:** Complex circuits sometimes wander. Free-tier Pro is capped
at 25 requests per day *across all users*.

**Address:** Waiting on funding (SSIP application) to buy Anthropic
API credits and swap the Agent path to Claude Sonnet 5. Multi-model
routing (Flash for Ask/Plan, Sonnet for Agent) keeps unit costs
tractable. Ask/Plan is already on Flash.

**Fallback if no funding:** Move to paid Gemini 2.5 Pro (~3× cheaper
than Claude, 15% quality delta on our workload). Runs indefinitely on
even a modest budget.

### 12.2 Text-template inject fragility

**Problem:** `tracer_inject.py` string-manipulates `.kicad_sch` source.
A KiCad 10.1 format change will break silently.

**Address:** Roadmap Phase 2.3 — a real s-expression parser and typed
mutator suite in `tracer_ast/`. Scoped, not started. Trigger to start:
first tester's schematic breaks and the diff shows text-surgery was
the cause. Doing it preemptively is 2 weeks of solo work with no user
feedback.

### 12.3 Windows-only

**Problem:** pywin32 + Ctrl+F5 keystroke are Windows-specific.

**Address:** Roadmap Phase 4.1. macOS uses AppleScript
`System Events`; Linux uses `xdotool`/`wmctrl`. Same interface behind
a platform-selection module. Ship after Windows beta stabilizes.

### 12.4 No streaming

**Problem:** User waits 30-120s for an Agent turn with no visible
progress except the activity feed.

**Address:** Server-Sent Events on `/v1/generate` and a JS-side event
handler. Deferred because it doubles the backend complexity and the
activity feed already gives *some* real-time signal.

### 12.5 Attachments (images) UI-only

**Problem:** The chat lets users attach reference schematic
screenshots but the backend ignores the image bytes.

**Address:** Needs a decision on transport (base64 in JSON vs.
multipart vs. S3 upload → URL). Once decided, `agent._to_contents`
already knows how to attach PIL images to the last user turn.

### 12.6 No public deployment yet

**Problem:** Everything runs on localhost. No cloud backend.

**Address:** Deploy plan documented — Render for backend, Vercel
already live for frontend, custom domain `tracerai.dev` planned.
Gated on completing the pre-launch hardening batch (rate limit,
payload caps, `/health` sanitize, `banned_at` column, service kill
switch, log enrichment).

### 12.7 No summarization at history-cap boundary

**Problem:** Hard truncation past turn 12 loses context. If a user
brings up something from turn 3 at turn 20, we've forgotten it.

**Address:** Add an LLM summarization pass at cap boundary that
condenses turns 1-12 into one system message. Deferred until a beta
tester actually reports lost context — hard cap captures ~90% of the
cost/latency win without the extra call.

### 12.8 No usage-abuse detection

**Problem:** A malicious user could hammer `/v1/ask` 1000×/min with
tiny prompts and stay under token quota while draining our shared
Gemini free-tier request-per-day quota.

**Address:** Per-user request-rate limit on `/v1/*` (10 req/min),
plus `profiles.banned_at` column for one-SQL kill switch, plus
`TRACER_SERVICE_DISABLED` env for global emergency shutdown. All
scoped, none implemented yet. Bundled into pre-launch hardening.

### 12.9 Solo dev, no external validation

**Problem:** We've built for a while without a real user touching it.
Every day of pre-launch polish is a bet on what will matter.

**Address:** Localhost beta with 3-5 known testers this week using
the `docs/BETA_TESTING.md` guide. Decisions on model swap, quota
tuning, and public deploy blocked on their feedback. Explicitly named
in ROADMAP.

---

## 13. What's shipped (versioned)

Version `0.1.0` — everything on `main` today. Full commit-by-commit
timeline in `docs/CHANGELOG.md`.

**Working end-to-end:**

- Device-flow auth with `jti` revocation on new plugin + on web sign-out
- Two-window quota with 402 responses distinguishing session vs weekly
- FastAPI backend on localhost with async threadpool wrapping of the
  sync agent code
- Gemini free-tier LLM path with model fallback + per-endpoint client
  timeouts (300s Agent, 45s Ask/Plan)
- History cap at 12 turns
- Full plugin UI: chat, dock, activity, changes, undo modal, copy
  button, account page
- 35 pytest cases
- Localhost beta guide
- Sign-out fix (2026-09-20)

**Not yet shipped:**

- Cloud deployment
- Domain
- Rate limiting on `/v1/*`
- Kill switches
- Attachments
- Streaming
- macOS / Linux
- Paid-model swap
- Stripe checkout (schema is Stripe-ready; UI is not)

---

## 14. Team

- **Kavish Raval** — everything currently.
- **[Second team member]** — role TBD.
- **Mentor at VGEC** — light-touch advisory.

---

## 15. Cheat sheet — where to point when someone asks…

| Question | File / doc |
|---|---|
| "How does auth work?" | `docs/SAAS_ARCHITECTURE.md` + this doc §6 |
| "How is quota computed?" | `backend/app/quota.py` + this doc §7 |
| "What did you ship?" | `docs/CHANGELOG.md` |
| "What's next?" | `docs/ROADMAP.md` |
| "How do I install and test it?" | `docs/BETA_TESTING.md` + `SETUP.md` |
| "How is the tech split between plugin and cloud?" | This doc §4 + §5 |
| "What's fragile?" | This doc §12 (this section) |
| "How does the AI actually build circuits?" | `backend/app/agent.py::build_with_auto_repair` + this doc §8-9 |
| "Why Supabase?" | This doc §3 (Data & auth) |
| "How much would it cost to run for 100 users?" | Session convo — ask Claude for the burn-rate table |

---

## Reading order if you're new to the project

1. This document.
2. `README.md` (5 min — public-facing overview).
3. `docs/CHANGELOG.md` — reverse-chronological, gets you the last month.
4. Skim `backend/app/routers/v1.py` — smallest file, shows the whole
   request lifecycle.
5. Skim `sidecar_app.py::OverlayShell` — the state machine at the
   heart of the plugin.
6. `docs/ROADMAP.md` — what's next and why.

Everything else is reachable from the cheat sheet above.
