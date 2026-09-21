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

## 3. Tech stack — in depth

The stack splits into three parts: the **plugin** (runs on the user's
Windows PC inside KiCad), the **backend** (a small FastAPI server we
host), and **data & auth** (Supabase + a Next.js frontend for account
management). Each layer is explained with what it does, why we picked
it, and what we considered instead.

---

### 3.1 Plugin (client-side, runs on user's PC)

#### KiCad 10 IPC Plugin format

**What it is.** KiCad ships two plugin models: the older *Action
Plugin* (in-process wxPython, one script per action) and the newer
*IPC Plugin* (out-of-process, KiCad talks to your process over a socket
and via file conventions). We use the IPC form.

**What it does for us.** Registers Tracer in KiCad's `Tools → External
Plugins` menu. When the user clicks it, KiCad spawns
`sidecar_launcher.py`, which detaches a Python sidecar and returns
control to KiCad immediately.

**Why this, not Action Plugin.** Action plugins run in KiCad's own
Python interpreter, which is wxPython-based. They can't create the
frameless-translucent overlays we need, they die when KiCad reloads a
project, and any exception crashes KiCad. IPC decouples us from KiCad's
lifecycle entirely.

**Version:** KiCad 8 and 9 both work; KiCad 10 is what we test against.

#### Python 3.11+ sidecar

**What it is.** A standalone Python process (`sidecar_app.py`, ~1550
lines) that owns all our UI, workers, and state.

**Why Python.** KiCad's own scripting is Python; users who install
plugins tend to have Python installed already. FastAPI needs Python on
the backend anyway; sharing the language means we can share type
definitions (`agent.py` compiles unchanged on both sides).

**Why 3.11+.** `run_in_threadpool` semantics, structural pattern
matching, better error tracebacks. Anything older doesn't buy us
anything at this project's size.

**Alternatives considered.** Node.js sidecar (would let us reuse the
frontend team's TS skills, but no LLM SDK parity with backend);
Electron (much bigger install footprint, users would notice); pure
Rust (fast but no LLM ecosystem, and we'd have to hand-write the
schematic parser).

#### PySide6 (Qt for Python) for UI

**What it is.** Official Python bindings for the Qt UI framework. Same
library that powers KiCad itself under the hood.

**What it does for us.** Two frameless translucent windows (chat
panel + dock pill) glued to KiCad's window. Real graphics acceleration,
proper input handling, a `QRegion` mask so clicks in the transparent
areas of our pill fall through to KiCad instead of being swallowed.

**Why this, not wxPython.** wxPython is KiCad's own toolkit — using
the same one seemed obvious at first. But wxPython on Windows can't
reliably render translucent frameless windows and its input-region
masking is broken. Qt does both natively. Bonus: `QWebEngineView` is
a real Chromium — we render our chat UI as HTML/CSS/JS instead of
hand-drawn Qt widgets.

**Alternatives considered.** Tkinter (ships with Python, no
transparency); wxPython (see above); pywebview (Chromium wrapper —
but no reliable way to glue it to KiCad's window position);
Electron (huge install).

**License note.** Qt/PySide6 is LGPL v3. We link dynamically (which is
what pip installing does), so LGPL doesn't infect Tracer's own source.
No commercial-license fee needed.

#### QWebEngineView + QWebChannel

**What it is.** `QWebEngineView` is Qt's Chromium widget — a real
browser inside your app. `QWebChannel` is Qt's built-in bridge that
lets JavaScript in that browser call Python methods and vice-versa.

**What it does for us.** The whole chat panel and dock are HTML +
Tailwind CSS + vanilla JS — no Qt-widget bespoke UI. Python's
`PyBridge` class exposes methods like `sendMessage`,
`copyToClipboard`, `login`; the JS calls `pyBridge.sendMessage(text)`
and Python receives it. Signals go the other way: Python emits
`assistantMessage` → JS's `renderAssistant` renders it.

**Why this, not native Qt widgets.** Iteration speed. Changing the
chat bubble style is one CSS line, not a widget subclass. Onboarding
someone with web experience is one afternoon, not one week of Qt
concepts.

**Alternatives considered.** Pure `QLabel`/`QTextEdit` widgets (slow
to iterate, ugly); embedding React via WebView (works, but adds a
build step to the plugin distribution which is a nightmare with KiCad's
plugin packaging).

#### pywin32 for window detection

**What it is.** Python bindings for the Win32 API — the low-level
Windows system calls.

**What it does for us.** Two jobs. First: `EnumWindows` finds KiCad's
main window handle so we can glue Tracer's overlays to it and follow
its geometry as the user moves KiCad around. Second: `keybd_event`
sends a synthetic Ctrl+F5 to KiCad after we edit a `.kicad_sch` so
KiCad reloads the file.

**Why not something cross-platform.** There isn't a good one. KiCad
doesn't expose its window handle through any API. Every OS has its
own tricks: Windows uses `EnumWindows`, macOS uses AppleScript /
`osascript`, Linux uses `xdotool` or `wmctrl` depending on the WM.
Roadmap phase 4.1 wraps all three behind a `platform_win.py` /
`platform_mac.py` / `platform_linux.py` split.

#### `requests` for HTTP to backend

**What it is.** The de-facto sync HTTP client in Python.

**Why this, not httpx or aiohttp.** The plugin is already spending
most of its wall-clock time waiting on the LLM. Async on the client
side buys us nothing — we call one endpoint, we wait for the reply,
we show it. `requests` is smaller, more stable, and every Python dev
alive knows how to debug it.

**Where it's used.** `tracer_auth.py` for the device-flow polls,
`tracer_api.py` for the `/v1/*` calls. Timeouts per-endpoint
(`_TIMEOUTS` dict): 300s for `/v1/generate` (Agent runs a repair
loop), 45s for Ask/Plan, 10s for `/v1/me` and `/v1/usage`.

---

### 3.2 Backend (our FastAPI server)

#### FastAPI 0.115+

**What it is.** A modern Python web framework. Type-hints define the
request/response shape; Pydantic validates payloads automatically;
async is first-class.

**What it does for us.** Exposes `/v1/ask`, `/v1/plan`, `/v1/generate`,
`/v1/me`, `/v1/usage`, `/health`, `/version`. Handles the JWT check as
a FastAPI dependency (`AuthedUser = Annotated[CurrentUser,
Depends(require_user)]`) so any route that adds `user: AuthedUser`
gets the check automatically.

**Why this, not Flask or Django.** Django is too heavy for a
stateless proxy. Flask needs Flask-Pydantic, Flask-JWT, Flask-CORS,
Flask-async — five plugins to get what FastAPI gives out of the box.
Also: FastAPI's docs (`/docs`) generate an OpenAPI schema for free,
which will matter if we ever build a public API tier.

**Alternatives considered.** Litestar (newer, smaller ecosystem);
Starlette raw (FastAPI is Starlette + Pydantic + DI, so this is what
we already use); Node.js/Express (would fragment the language stack).

#### uvicorn

**What it is.** An ASGI server — the thing that actually accepts HTTP
connections and hands them to FastAPI's async code.

**Why this.** FastAPI's default. Two-line command in prod:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Render, Fly, DO,
and every other Python host recognizes it.

**Not:** hypercorn (HTTP/2 support we don't need), gunicorn+uvicorn
workers (only matters when you scale past one dyno).

#### google-generativeai (Gemini SDK)

**What it is.** Google's official Python SDK for the Gemini API.

**What it does for us.** Everything under `agent.query_gemini` and
`agent.ask_gemini` uses it. Handles model routing, streaming (which
we don't use yet), and safety-setting configuration.

**Why Gemini right now.** Free tier is generous enough for beta
(25 Pro req/day, 250 Flash/day — per-key, so shared across our users).
Google gives new accounts real free credits without a card. No credit
card on file = no bill shock.

**Post-beta plan.** `agent.py` already has a `MODEL_CANDIDATES`
fallback chain and a `_working_model` register. Swapping to
Anthropic's `anthropic` SDK is a `_chat_gemini` → `_chat_claude`
function swap; the caller signature (`(prompt, history, attachments)
→ str`) doesn't change. Same for OpenAI's `openai` SDK.

#### `fastapi.concurrency.run_in_threadpool`

**What it is.** A helper that runs a sync function on FastAPI's
thread pool, freeing the async event loop.

**Why we need it.** Our `agent.build_with_auto_repair` is synchronous
(it makes blocking calls to the Gemini SDK and to the local schematic
validator). If we called it directly from an `async def` route, the
whole event loop would freeze during the 30-120s Agent turn — nothing
else in the process could respond, including `/v1/usage` requests
from the same user.

**How.** `result = await run_in_threadpool(agent.build_with_auto_repair,
prompt, history, ...)`. The route stays `async`, the event loop
stays free, other requests process normally.

#### PyJWT (JWT verify)

**What it is.** The mainstream Python JWT library.

**What it does for us.** Decodes the `Authorization: Bearer <token>`
header using our `DEVICE_JWT_SECRET` (HS256) and checks all the
required claims (`exp`, `iat`, `sub`, `jti`) exist. Raises
`ExpiredSignatureError` / `InvalidTokenError` on failure, which our
`auth.py` maps to 401.

**Why this over python-jose.** Fewer transitive deps; actively
maintained; we don't need JOSE's RSA/ES support because our JWTs are
HS256.

**Security note.** We explicitly pass
`algorithms=["HS256"]` on decode — never trust the JWT header's
`alg` field alone, that's the "algorithm confusion" attack path.

#### httpx (async → Supabase REST)

**What it is.** A modern Python HTTP client — sync and async.

**Why we use it on the backend.** Supabase's REST endpoint is what we
call for the `jti` freshness check and the `usage_ledger` reads /
writes. Those calls happen inside FastAPI's async request handler,
so they need to be async themselves. `httpx.AsyncClient` fits
cleanly. We time out at 5 seconds per call — if Supabase is slow, we
fail fast rather than pile up requests.

**Not `supabase-py`.** The official Python client is sync-only and
brings in `postgrest-py`, `gotrue-py`, `realtime-py`, `storage3` — 4
transitive libraries we don't need for the two REST calls we make.
`httpx + hand-written URLs` is 20 lines and zero magic.

---

### 3.3 Data & auth

#### Supabase Auth

**What it is.** A managed identity service. Sign-up, sign-in,
password reset, email verification, magic links, OAuth providers —
all behind a REST API.

**What it does for us.** Handles the *web-side* login flow. When a
user creates a Tracer account on the Next.js site, Supabase Auth
handles the email verification, password hashing (bcrypt), and issues
a session cookie. Our device-flow code then reads
`supabase.auth.getUser()` to know who's approving a plugin's
activation code.

**Why this, not Auth0 / Clerk / rolling our own.** Auth0 charges
$0.023/MAU past the free tier — economically hostile at scale. Clerk
is nicer UX but $0.02/MAU and vendor-locks us to their JS SDK.
Rolling our own means writing password-reset flows, email
verification, session management, and OAuth in-house — a
security-critical afternoon that turns into a permanent maintenance
burden. Supabase Auth is free up to 50k MAU, hosted on the same
Supabase project as our DB, and speaks standard OAuth 2.

#### Supabase Postgres

**What it is.** A managed Postgres 15 database.

**What it does for us.** Stores everything persistent: `profiles`
(one per user, tier + jti + Stripe fields), `usage_ledger` (one row
per LLM request), `device_codes` (pending activation codes),
`waitlist` and `contact_messages` (marketing forms). Schema is
`tracer-frontend/supabase/schema.sql`.

**Why Postgres, not Mongo/DynamoDB/etc.** Every relationship we have
is naturally relational (`usage_ledger` FK to `auth.users`;
`profiles` PK identical to `auth.users.id`). Constraints and RLS are
enforced in the DB, not the app. We're never going to outgrow it at
beta scale.

**Free tier limits.** 500MB DB, 2GB bandwidth/mo, unlimited API
requests. Enough for a few thousand users. Supabase Pro ($25/mo)
raises to 8GB DB + 50GB bandwidth — the trigger to upgrade is when
`usage_ledger` starts pushing 500MB, which won't happen this year.

#### Row-Level Security (RLS)

**What it is.** Postgres' native "policies on rows" feature. Each
policy says which rows a role can `SELECT` / `INSERT` / `UPDATE` /
`DELETE`.

**What it does for us.** Every table has RLS *enabled*. Only two
policies are actually permissive:

- `profiles`: users can `SELECT` their own row (`auth.uid() = id`)
- `usage_ledger`: users can `SELECT` their own row (`auth.uid() = user_id`)

Everything else (writes to `profiles`, all access to `device_codes`,
`waitlist`, `contact_messages`) is service-role-only — the app has to
use the admin client (which bypasses RLS with the service-role key)
to write those.

**Why this matters.** Even if we make a bug in a Next.js route that
queries the wrong table, RLS is a second wall: an anon or authenticated
user's key can only ever see rows they're explicitly allowed to. It's
defense in depth.

#### Next.js 16 + React 19 + TypeScript

**What it is.** Meta's/Vercel's React framework. Handles SSR
(server-side rendering), routing, server actions, and image
optimization.

**What it does for us.** Serves the marketing site (`/`, `/pricing`,
`/contact`, `/privacy`, `/refund`), the auth pages (`/signup`,
`/signin`), the device-flow confirmation page (`/activate`), and the
billing dashboard (`/billing`). Server actions (`lib/actions/*.ts`)
handle form POSTs without needing a separate API layer.

**Why Next.js.** SSR gives our public pages proper SEO (a plain SPA
would fail here — engineers googling "KiCad AI plugin" won't find us).
Server actions mean the device-flow mint code runs on Vercel's servers
with the service-role key, never in the browser. And it's the framework
Vercel is designed around, so deploys are `git push` and done.

**Alternatives considered.** SvelteKit (smaller community for hiring),
Remix (less traction), plain React (no SSR = SEO problem).

#### Vercel (frontend hosting)

**What it is.** A managed PaaS for Next.js and static sites.
GitHub-connected — every push to `main` deploys automatically.

**Why this.** Free tier is generous (100GB bandwidth / month,
unlimited requests). Global edge network makes the landing page load
fast worldwide. Zero configuration for a Next.js app — Vercel *is*
Next.js's home.

**When we outgrow it.** Vercel's pricing model gets aggressive if we
serve API-heavy traffic (100 GB-hours of function execution / month
free, then $$$). For us that's fine — our expensive traffic (LLM
calls) doesn't go through Vercel at all; the plugin talks straight to
the FastAPI backend.

#### Render (backend hosting — planned)

**What it is.** A managed PaaS specifically for backend services
(Docker containers, Python, Node, etc.). Also GitHub-connected.

**Why this.** Free tier for web services (750h/mo — enough for one
always-on service, minus a 15min-idle sleep). One-click custom
domains + free TLS via Let's Encrypt. `[req]` log lines from our
`main.py` middleware appear in Render's dashboard automatically.

**Alternatives considered.**
- **Fly.io** — no sleep on free tier, but needs a Dockerfile.
- **Railway** — $5/mo credit runs out fast under real traffic.
- **DigitalOcean App Platform** — removed from GitHub Education pack.
- **Oracle Always Free** — Oracle's UI is painful; account reclamation
  risk.
- **Azure App Service** — Education credit ($100) is enough, but
  Azure's Python deploy story is genuinely bad.

Render is the smallest-friction option that still runs 24/7 for free
during beta.

**Cold-start note.** Render free-tier sleeps after 15 min idle; wake-up
adds ~30-45s to the first request. Acceptable for beta traffic
patterns. When it becomes a real complaint, Render Starter is $7/mo
and always-on.

#### Stripe (payments, deferred)

**What it is.** The standard for online payments.

**What it does for us (once wired).** Handles credit-card checkout for
Basic/Pro subscriptions. On successful subscription, Stripe fires a
webhook to our backend; we update `profiles.plan` accordingly. On
cancellation, same thing in reverse.

**Why deferred.** No paying users to test with. The schema is
Stripe-ready (`profiles.stripe_customer_id`,
`profiles.stripe_subscription_id`, `profiles.current_period_end`
already exist and are indexed) so wiring it is a ~1-day task when the
first user asks how to pay us.

**Why Stripe, not Razorpay / Paddle.** Razorpay is India-specific and
harder to test from outside India. Paddle handles VAT for you but
takes a 5% cut vs. Stripe's 2.9% + $0.30. If we go India-first with
UPI, we'll revisit — but for a global SaaS, Stripe wins.

---

### 3.4 One non-obvious design choice: two-window quotas

Almost every SaaS meters usage as a **monthly** number. We use **two
rolling windows** — a 5-hour "session" cap and a 7-day "weekly" cap
— and both must pass for a request to succeed.

**Why.** Our upstream (Google's Gemini free tier) has a hard
25-request-per-day cap **shared across every user of our service**.
If we only had a monthly budget, a single hobby-tier user could burn
their entire allocation in one afternoon and kill Gemini access for
everyone else. The 5-hour session cap prevents that — even a
fully-drained user can only ruin 5 hours of the day, and other users
keep working.

**Why 5 hours and 7 days.** Directly modeled on Anthropic's own Claude
quota shape (a proven pattern with real-world tuning); it maps well to
"a normal workday of engineering" without being punishing.

**Where the numbers come from.** `backend/app/settings.py` and its
mirror `tracer-frontend/lib/quotas.ts`. See §7 for details.

---

### 3.5 Quick-reference summary

For anyone skimming — the same information as §3.1–3.3 in one screen.

#### Plugin (client)

| Layer | Tech | Version | Why |
|---|---|---|---|
| KiCad integration | IPC Plugin | KiCad 8/9/10 | Out-of-process, survives KiCad restarts, can render translucent overlays |
| Sidecar runtime | Python | 3.11+ | Same language as backend; type hints; `run_in_threadpool` semantics |
| UI framework | PySide6 (Qt) | 6.6+ | Frameless transparency + click-through mask; LGPL, dynamically linked |
| Chat / dock UI | QWebEngineView + QWebChannel | Qt 6 built-in | Real Chromium; iteration by CSS not widget subclass; two-way Py↔JS bridge |
| Window discovery | pywin32 | 306+ | KiCad exposes no window API — `EnumWindows` is the fallback |
| HTTP → backend | `requests` | 2.31+ | Sync is fine here (one call, wait, render); smaller than httpx |
| Async workers | Qt `QThread` | Qt 6 built-in | Keeps the chat UI responsive during long Agent turns |

#### Backend (our server)

| Layer | Tech | Version | Why |
|---|---|---|---|
| Web framework | FastAPI | 0.115+ | Pydantic validation, async, deps-based auth, OpenAPI for free |
| ASGI server | uvicorn | 0.30+ | FastAPI's default; one-line prod command on Render |
| Sync-wrapping | `run_in_threadpool` | FastAPI built-in | Keeps event loop free while sync Agent code runs |
| LLM SDK (Gemini) | google-generativeai | 0.8+ | Free tier for beta; `agent.py` provider layer allows Claude/OpenAI swap |
| JWT | PyJWT | 2.9+ | HS256 with explicit `algorithms=["HS256"]` (algorithm-confusion safe) |
| Supabase REST | httpx (async) | 0.27+ | Non-blocking calls inside async routes; 20 lines vs supabase-py's 4 transitive libs |
| Validation | Pydantic | 2.9+ | Request bodies + `Field(max_length=...)` for payload caps |
| Config | pydantic-settings | 2.4+ | `.env`-driven, env-var overrides, list types auto-parsed from JSON |

#### Data & auth (Supabase + Next.js)

| Layer | Tech | Version | Why |
|---|---|---|---|
| Identity | Supabase Auth | managed | Free ≤50k MAU; email verification + password hashing + OAuth for free |
| Database | Supabase Postgres | 15 | Relational fits our data; RLS is server-enforced defense in depth |
| Row security | Postgres RLS | native | Even a buggy app query can't leak another user's data |
| Frontend | Next.js + React + TS | 16 / 19 / 5 | SSR for SEO on landing; server actions for form handling |
| CSS | Tailwind CSS | 3 | Component-locality; no CSS-in-JS runtime; JIT small bundle |
| Frontend host | Vercel | free tier | Zero-config Next.js deploys; global edge; free TLS |
| Backend host (planned) | Render | free tier | GitHub-connect deploy; stdout log aggregation; free custom domain + TLS |
| Payments (deferred) | Stripe | — | Industry standard; schema already Stripe-ready; wire when a user asks |

#### Cross-cutting

| Concern | Tech | Where |
|---|---|---|
| Auth transport | Device Flow (RFC 8628) + HS256 JWT | Plugin ↔ frontend for pairing; plugin → backend for every call |
| Single-plugin-per-user | `jti` freshness check against `profiles.active_device_jti` | `backend/app/auth.py:98` |
| Quotas | Two rolling windows (5h session + 7d weekly) | `backend/app/quota.py` |
| History cap | Hard truncate at 12 turns | `backend/app/agent.py::_cap_history` |
| Repair loop | `MAX_REPAIRS=1` (2 once on paid API) | `backend/app/agent.py::build_with_auto_repair` |
| Testing | pytest, no Qt/win32/Gemini in tests | `tests/` — 35 cases, ~2s |
| Access logging | one line per request → stdout → Render dashboard | `backend/app/main.py::_access_log` |

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
