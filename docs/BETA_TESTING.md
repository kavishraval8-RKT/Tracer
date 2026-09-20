# Tracer Copilot — Beta Testing Guide (Localhost)

> **Welcome, beta tester.** Nothing is deployed yet. You will run the entire Tracer stack on your own machine — Supabase (cloud, shared), the Next.js frontend, the FastAPI backend, and the KiCad plugin — and test all the flows end-to-end.
>
> Read this document once before you start. Bug reports without the info from §7 cost hours of back-and-forth.

---

## 1. What Tracer is

Tracer Copilot is a KiCad plugin that puts an AI chat panel next to your schematic. You describe the circuit you want in plain English; Tracer designs it, wires it up in your open schematic, and explains what it did.

Three modes:

| Mode  | What it does                                                         | Try it with                                            |
|-------|----------------------------------------------------------------------|--------------------------------------------------------|
| Ask   | Answers questions. Never touches your schematic.                     | *"What's a bypass cap for?"*                           |
| Plan  | Proposes a step-by-step design plan. Also read-only.                 | *"Plan a 5V→3.3V regulator for a 200mA load."*         |
| Agent | Actually builds the circuit — places parts, draws wires, saves it.   | *"Add a bypass capacitor to U1."*                       |

Under the hood the plugin talks to a local FastAPI backend, which talks to Google's Gemini API. The Gemini key stays on the backend — you never handle one from the plugin.

---

## 2. Prerequisites

- **Windows 10 / 11** (macOS/Linux plugin builds aren't ready yet)
- **KiCad 8 or 9**
- **Python 3.11+** on PATH (for the backend)
- **Node.js 20+** and **npm** or **pnpm** (for the frontend)
- **Git**
- A **Supabase project** — either the shared team one (URL + keys will be handed to you) or your own free project at [supabase.com](https://supabase.com)
- A **Google Gemini API key** — free at [aistudio.google.com](https://aistudio.google.com/app/apikey)

---

## 3. One-time setup (≈ 20 minutes)

### 3.1 Clone both repos

```bash
git clone https://github.com/kavishraval8-RKT/Tracer.git tracer-engine
git clone https://github.com/kavishraval8-RKT/Tracer-frontend.git tracer-frontend
```

(Adjust URLs to whatever we hand you.)

### 3.2 Supabase — apply the schema

1. In your Supabase project SQL editor, paste and run `tracer-frontend/supabase/schema.sql`. Safe to re-run.
2. Grab three values from **Project Settings → API**:
   - `Project URL`
   - `anon public` key
   - `service_role` key (server-only — never commit this)

### 3.3 Backend (`tracer-engine/backend`)

```bash
cd tracer-engine/backend
python -m venv .venv
.venv\Scripts\activate           # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create `tracer-engine/backend/.env` (this file is gitignored):

```env
TRACER_DEVICE_JWT_SECRET=any-long-random-string-shared-with-frontend
TRACER_SUPABASE_URL=https://<yourproj>.supabase.co
TRACER_SUPABASE_SERVICE_ROLE_KEY=<service_role key from Supabase>
TRACER_GEMINI_API_KEY=<your Gemini key>
```

`TRACER_DEVICE_JWT_SECRET` **must** match `DEVICE_JWT_SECRET` in the frontend's `.env.local` (below). The frontend mints the JWT; the backend verifies it — same HS256 secret both sides.

Launch it:

```bash
cd tracer-engine/backend
.venv\Scripts\activate
uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
```

Sanity check in a browser: [http://127.0.0.1:8100/health](http://127.0.0.1:8100/health) should return JSON with `ok: true` and every `*_configured` field `true`.

### 3.4 Frontend (`tracer-frontend`)

```bash
cd tracer-frontend
npm install
```

Create `tracer-frontend/.env.local`:

```env
NEXT_PUBLIC_SUPABASE_URL=https://<yourproj>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon key>
SUPABASE_SERVICE_ROLE_KEY=<service_role key>
DEVICE_JWT_SECRET=any-long-random-string-shared-with-frontend
```

Launch:

```bash
npm run dev
```

Sanity check: [http://localhost:3000](http://localhost:3000) loads.

### 3.5 Plugin — point it at your local backend

The plugin defaults to production URLs. To override for localhost testing, drop a `.tracer.env` file next to `sidecar_app.py`:

```env
TRACER_API_URL=http://127.0.0.1:8100
TRACER_WEB_URL=http://localhost:3000
```

(This file is gitignored — one per machine.)

### 3.6 Install the plugin in KiCad

Two options:

**(a) Dev-mode symlink** — recommended, edits reload without repacking:

```powershell
# From an admin PowerShell:
New-Item -ItemType SymbolicLink `
  -Path "$env:APPDATA\kicad\<version>\3rdparty\plugins\tracer_plugin" `
  -Target "C:\path\to\tracer-engine"
```

**(b) Zip build** — closer to what beta shipping looks like:

```bash
cd tracer-engine
python scripts/build_plugin_zip.py     # if this exists in your checkout
```

Then **KiCad → Tools → Plugin and Content Manager → Install from File** → pick the zip.

Restart KiCad after either option.

---

## 4. First-time sign-in

1. Open any `.kicad_sch` in KiCad. **Do not point it at anything you care about** — start with a blank schematic.
2. **Tools → External Plugins → Tracer Copilot**. Chat panel appears with a **Sign in** overlay.
3. Click Sign in. Your browser opens to `http://localhost:3000/activate?code=XXXXXXXX` with an 8-char code pre-filled.
4. If you don't have a Supabase account yet, sign up there first, then reload the activate URL.
5. Click **Confirm** on the activate page.
6. The plugin polls, receives a device JWT, and drops you into the composer.

If the plugin can't reach the backend, you'll see *"Backend unreachable — check your internet, then try again."* — that means `uvicorn` isn't running on `127.0.0.1:8100`.

---

## 5. What to test (in order)

### 5.1 Ask mode — the fast path

**Why:** proves the round-trip works.

1. Set mode selector (top-left of chat) to **Ask**.
2. Type: `In one sentence, what does a bypass capacitor do?`
3. Enter. Expect a reply in ~3–8s.

Watch your backend terminal. You should see:

```
[req] POST /v1/ask -> 200 3421ms plugin=0.1.0 ip=127.0.0.1
```

### 5.2 Plan mode

1. Switch to **Plan**.
2. Type: `Plan a 5V to 3.3V linear regulator for a 200mA load.`
3. Expect a structured plan back (~5–15s).

### 5.3 Agent mode — the slow, interesting path

**This is the main event.** Agent can take 30–120s per turn because it iterates.

1. Switch to **Agent**.
2. Make sure a **blank** `.kicad_sch` is open. Tracer's status bar should show `Target: <yourfile>.kicad_sch`.
3. Type: `Add an LM7805 5V regulator with input and output bypass caps.`
4. Watch the **Activity** panel — `[Act]` and `[Obs]` lines stream as the agent works.
5. When it finishes, KiCad shows new components on your schematic and the chat summarizes them.

Test follow-ups:

- **Copy button:** click `Copy` under any assistant message, paste into a text file — you should get the raw markdown source. Check `logs/sidecar.log` for `[bridge] copied to Qt clipboard`.
- **Undo:** ↶ button under the chat header → pick a step → confirm. Schematic reverts.
- **Keep / Discard:** after Agent turns, look for the buttons under the activity feed.

### 5.4 Quota enforcement (two-window)

You start on the `hobby` tier: **30,000 tokens per 5-hour session**, **100,000 tokens per rolling 7 days**.

1. Fire 5–10 Agent prompts back to back until you hit the session cap.
2. Next request should fail with:
   > *Quota exceeded — hobby tier: used 31,240 / 30,000 tokens (5-hour session). Try again in a couple of hours.*
3. To force-drain the ledger for testing, in Supabase SQL editor:
   ```sql
   delete from public.usage_ledger where user_id = '<your-uuid>';
   ```
4. To force-fill it (test the 402 quickly), in Supabase SQL editor:
   ```sql
   insert into public.usage_ledger (user_id, endpoint, model, prompt_tokens, completion_tokens)
   values ('<your-uuid>', '/v1/generate', 'gemini-2.5-pro', 50000, 0);
   ```

### 5.5 Sign-out and re-link

1. Click your avatar (top-right of chat) → account page opens.
2. Click **Sign out** on the account page.
3. Back in the plugin, next request → chat overlay reappears with Sign in.

**Single-plugin enforcement:** if you sign in on a *second* KiCad instance while the first is still open, the first instance's next request fails with `NotAuthenticatedError` and drops it to signed-out. That's expected — one plugin per account.

### 5.6 Offline / backend down

1. Stop `uvicorn` (Ctrl-C in its terminal).
2. Try any prompt.
3. Expect *"Backend unreachable — check your internet, then try again."*
4. Restart `uvicorn`, retry — should work.

### 5.7 Billing / usage page

Visit [http://localhost:3000/billing](http://localhost:3000/billing) while signed in.

- Two usage bars — one for the current 5-hour session, one for the rolling 7-day window
- Each shows `used / total tokens` for your tier
- Bars go **green → amber → red** at 85% / 100%
- Breakdown by endpoint (`/v1/generate`, `/v1/ask`, `/v1/plan`)

If the numbers here don't match what you actually used, that's a bug — report it.

---

## 6. How the pieces fit together (localhost)

```
  ┌────────────────┐     device flow        ┌────────────────┐
  │  KiCad plugin  │  <──────────────────>  │  localhost:3000│
  │   (Python)     │        device JWT      │   (Next.js)    │
  │                │  <──────────────────>  └────────┬───────┘
  │                │                                 │
  │                │        /v1/{ask,plan,           │  Supabase Auth
  │                │         generate,me,usage}      │  (users, JWTs,
  │                │  ────────────────────────>      │   usage_ledger)
  │                │       Authorization:            │
  │                │       Bearer <device JWT>       │
  └────────┬───────┘                                 │
           │                                         │
           ▼                                         ▼
  ┌────────────────┐                        ┌────────────────┐
  │ localhost:8100 │  ── service role  ──>  │    Supabase    │
  │   (FastAPI)    │   writes usage_ledger, │   PostgREST    │
  │                │   checks jti, reads    │                │
  │                │   tier from profiles   │                │
  │  ├─> Gemini API                         └────────────────┘
  └────────────────┘
```

- **Auth:** device flow (RFC 8628). The plugin opens a browser to `localhost:3000/activate?code=XXXXXXXX`. You confirm; the plugin polls until approved, then gets a device JWT signed with the shared HS256 secret.
- **Single-plugin-per-user:** JWT carries a `jti` (unique id). The backend checks `JWT.jti == profiles.active_device_jti` on every request. Activating a new plugin rotates the value and boots the old plugin out.
- **Quotas:** every LLM response's token usage is written to `usage_ledger`. Before each request the backend sums tokens over the last 5h (session) and 7d (weekly) and rejects with 402 if either is over the tier cap.
- **Your `.kicad_sch` never leaves your machine.** The plugin extracts a JSON summary and sends that, not the file.

---

## 7. How to report a bug

We can't fix what we can't reproduce. Every report needs:

### Always

1. **What you typed / did** — exact prompt, exact mode, what you clicked
2. **What you expected** — one sentence
3. **What actually happened** — one sentence
4. **The plugin version** — first line of `logs/sidecar.log` after launch, e.g. `[tracer] sidecar v0.1.0 starting`
5. **Timestamp with timezone**

### If the plugin misbehaved

- **Last ~100 lines of `tracer-engine/logs/sidecar.log`**. Zip and attach. Don't paste more than 40 lines inline.
- **Any red `[Err]` lines** from the Activity panel.

### If the backend misbehaved

- **Last ~100 lines of the `uvicorn` terminal output** — the `[req]` lines and any traceback.

### If the schematic looks wrong after Agent mode

- **The `.kicad_sch` file** *before* and *after* the Agent turn. Both. The plugin keeps `.tracer-bak` backups next to the file — the one with the timestamp just before your prompt is the "before".
- **Screenshot** of the schematic editor showing the bad state.

### If quota / usage numbers look wrong

- **Screenshot** of the billing page.
- **Your guess** at what you actually used this session/week.
- **A query result** from Supabase SQL editor:
  ```sql
  select endpoint, sum(prompt_tokens + completion_tokens) as total, count(*) as calls
  from public.usage_ledger
  where user_id = '<your-uuid>' and created_at > now() - interval '7 days'
  group by endpoint;
  ```

### Where to send it

DM the team on whatever channel we invited you on. Include everything above in one message so we don't chase you for it.

---

## 8. Known limitations (please don't report these)

- **First Agent turn can take 60–120s.** Free-tier Gemini is slow; our repair loop runs up to twice per turn. Timeout is 300s.
- **Attachments (image uploads) are UI-only right now.** The plugin sends the filename but the backend doesn't yet forward image bytes to Gemini. Text prompts work end-to-end.
- **Streaming isn't wired.** You get the whole reply at once, not token-by-token.
- **No macOS / Linux plugin build.** Coming later.
- **`hobby` is the only tier open.** `basic` and `pro` are wired in the code but Stripe checkout is not.
- **Everything is localhost.** No prod deploy yet. Every tester runs their own backend/frontend.

---

## 9. What we most want feedback on

Highest priority first:

1. **Agent mode circuit quality.** Does it build what you asked for? Attach the `.kicad_sch` before/after.
2. **Prompt ergonomics.** What did you have to say twice? What worked first try?
3. **Latency perception.** Which mode felt "fine", "slow", "unusable"?
4. **Error clarity.** When something failed, did the message tell you what to do next?
5. **Quota fairness.** Did the 5-hour and 7-day caps fit a real work session?

Thanks for testing. Every session you run makes the shipped version measurably less bad.
