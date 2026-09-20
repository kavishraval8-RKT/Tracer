# Tracer Copilot — Beta Testing Guide

> **Welcome, beta tester.** You are one of the first people to run Tracer against real KiCad projects. This document explains what Tracer is, how to install it, every feature we want you to exercise, and exactly what to send us when something breaks.
>
> Read this end-to-end **once** before you start. Bug reports without the info from the "How to report a bug" section at the bottom cost us hours of back-and-forth.

---

## 1. What Tracer is

Tracer Copilot is a KiCad plugin that puts an AI chat panel next to your schematic. You describe the circuit you want in plain English; Tracer designs it, wires it up in your open schematic, and explains what it did.

There are three modes:

| Mode  | What it does                                                         | Try it with                                            |
|-------|----------------------------------------------------------------------|--------------------------------------------------------|
| Ask   | Answers questions. Never touches your schematic.                     | *"What's a bypass cap for?"*                           |
| Plan  | Proposes a step-by-step design plan. Also read-only.                 | *"Plan a 5V→3.3V regulator for a 200mA load."*         |
| Agent | Actually builds the circuit — places parts, draws wires, saves it.   | *"Add a bypass capacitor to U1."*                       |

Under the hood Tracer talks to our cloud backend, which talks to Google's Gemini API. Your API key is on our side — you never handle one.

---

## 2. Before you install

You need:

- **KiCad 8 or 9** (nightly builds are fine)
- **Windows 10 / 11** — macOS and Linux builds come after the beta
- **A Tracer account** — sign up at [tracer.dev](https://tracer.dev) if you don't have one
- **A test schematic you don't mind Tracer editing** — start with a blank `.kicad_sch`; do not point it at anything you care about

You do **not** need:

- An OpenAI, Anthropic, or Google API key
- A credit card (the beta runs on the free `hobby` tier)
- Python installed globally — the plugin ships its own runtime

---

## 3. Install (5 minutes)

1. Download `tracer_plugin.zip` from the link we sent you.
2. In KiCad: **Tools → Plugin and Content Manager → Install from File** → pick the zip.
3. Restart KiCad.
4. Open any `.kicad_sch` file.
5. In the schematic editor: **Tools → External Plugins → Tracer Copilot**.
6. A chat panel appears on the right, with a **Sign in** button.
7. Click Sign in. Your browser opens to `tracer.dev/activate` with an 8-character code pre-filled. Confirm it.
8. Back in KiCad, the chat panel now shows the composer instead of the sign-in overlay. You're in.

If step 7 fails ("token rejected") the plugin drops you back to the sign-in overlay automatically — click Sign in again and it will start a fresh device code.

---

## 4. What to test (in order)

Do these in this order. Each one exercises a specific slice of the system.

### 4.1 Ask mode — the fast path

**Why:** proves the round-trip works.

1. Set the mode selector (top-left of chat) to **Ask**.
2. Type: `In one sentence, what does a bypass capacitor do?`
3. Press Enter.

You should see a reply in ~3-8 seconds. If it takes longer than 45s or shows a `CloudUnreachableError`, note the exact error text and skip to §6.

### 4.2 Plan mode — the medium path

1. Switch mode to **Plan**.
2. Type: `Plan a 5V to 3.3V linear regulator for a 200mA load.`
3. Expect a structured plan back (bullet-point steps, parts list). ~5-15s.

### 4.3 Agent mode — the slow, interesting path

**This is the main event.** Agent can take 30-120s per turn because it iterates: propose → validate in KiCad → repair if the schematic backend rejects it.

1. Switch mode to **Agent**.
2. Make sure a **blank** `.kicad_sch` is open in KiCad and Tracer's status shows `Target: <yourfile>.kicad_sch`.
3. Type: `Add an LM7805 5V regulator with input and output bypass caps.`
4. Watch the **Activity** panel on the right — you'll see `[Act]` and `[Obs]` lines as the agent works.
5. When it finishes, KiCad should show new components on your schematic. The chat panel will summarize what it added.

Test these follow-ups:

- **Undo:** Click the ↶ button under the chat header, pick a step from the list, confirm. The schematic should revert.
- **Copy:** Click the `Copy` button under any assistant message. Paste into a text file — you should get the raw markdown source.
- **Keep / Discard:** After the agent makes changes, look for the "Keep changes / Discard" buttons under the activity feed.

### 4.4 Quota enforcement

**Why:** proves the two-window quota system works and the error is readable.

You're on the `hobby` tier: **30,000 tokens per 5-hour session** and **100,000 tokens per rolling 7 days**.

1. Fire off 5-10 Agent-mode prompts back to back until you hit the session quota.
2. The next request should fail with a message like:
   > *Quota exceeded — hobby tier: used 31,240 / 30,000 tokens (5-hour session). Try again in a couple of hours.*
3. Wait a few hours, try again — it should work.
4. To exercise the weekly cap, ask us to bump your usage manually in Supabase, or run through hobby's 100k over a week.

### 4.5 Sign-out and re-link

1. Click your avatar (top-right of chat) → the account page opens in your browser.
2. Click **Sign out** in the account page.
3. Back in the plugin, next request → the chat overlay reappears with the Sign in button.

If you sign in on a *second* KiCad instance while the first is still open, the first instance's next request will fail with `NotAuthenticatedError` and drop it to signed-out. That's expected — **one plugin per account, always**.

### 4.6 Offline / backend down

1. Turn off Wi-Fi.
2. Try any prompt.
3. You should see:
   > *Backend unreachable — check your internet, then try again.*
4. Turn Wi-Fi back on, retry, it should work.

### 4.7 The account & billing page

Visit `tracer.dev/billing` in a browser while signed in. You should see:

- Two usage bars — one for the current 5-hour session, one for the current 7-day window
- Each bar shows `used / total tokens` with the tier's cap
- Bars go **green → amber → red** at 85% and 100%
- A breakdown of tokens by endpoint (`/v1/generate`, `/v1/ask`, `/v1/plan`)

If the numbers here don't match your gut feeling of how much you've used, that's a bug — report it.

---

## 5. How the pieces fit together

You do not need to read this to test. But if a bug feels architectural, it helps to know:

```
  ┌────────────────┐     device flow      ┌────────────────┐
  │  KiCad plugin  │  <──────────────────>│   tracer.dev   │
  │   (Python)     │      device JWT      │  (Next.js on   │
  │                │  <──────────────────>│    Vercel)     │
  │                │                      └────────┬───────┘
  │                │                               │
  │                │       /v1/{ask,plan,          │  Supabase Auth
  │                │        generate,me,usage}     │  (users, JWTs,
  │                │  ────────────────────────>    │   usage_ledger)
  │                │      Authorization:           │
  │                │      Bearer <device JWT>      │
  └────────┬───────┘                               │
           │                                       │
           ▼                                       ▼
  ┌────────────────┐                      ┌────────────────┐
  │  api.tracer    │  ── service role ──> │    Supabase    │
  │  (FastAPI on   │  writes usage_ledger,│   PostgREST    │
  │    Render)     │  checks jti, reads   │                │
  │                │  tier from profiles  │                │
  │                │                      └────────────────┘
  │  ├─> Gemini API                                       
  └────────────────┘
```

- **Authentication:** device flow. The plugin opens a browser to `tracer.dev/activate?code=XXXXXXXX`. You confirm in the browser; the plugin polls until approved, then gets a device JWT signed with a shared secret.
- **Single-plugin-per-user:** the JWT has a `jti` (unique id). The backend checks `JWT.jti == profiles.active_device_jti` on every call. Activating a new plugin rotates it and boots the old one out.
- **Quotas:** every LLM response's token usage is written to `usage_ledger`. Before each request, the backend sums tokens over the last 5h (session) and 7d (weekly) and rejects with 402 if either is over the tier cap.
- **Data flow:** your prompts and Tracer's replies transit our backend and Gemini. Your `.kicad_sch` file **never leaves your machine** — the plugin extracts a JSON summary of the schematic to send along, not the file itself.

---

## 6. How to report a bug

We can't fix what we can't reproduce. Every bug report should include:

### Always

1. **What you typed / did** — exact prompt, exact mode (Ask / Plan / Agent), what you clicked
2. **What you expected** — one sentence
3. **What actually happened** — one sentence
4. **The plugin version** — first line of `logs/sidecar.log` after you launch the plugin, e.g. `[tracer] sidecar v0.1.0 starting`
5. **When it happened** — timestamp with timezone, so we can grep our server log

### If the plugin misbehaved

- **The last ~100 lines of `logs/sidecar.log`.** On Windows the log lives at:
  ```
  <plugin install dir>\logs\sidecar.log
  ```
  where `<plugin install dir>` is under `%APPDATA%\kicad\<version>\3rdparty\plugins\tracer_plugin\`.
  Zip and attach it. Do **not** paste more than 40 lines inline.

- **Any red `[Err]` lines** from the Activity panel (right-hand sidebar of the chat).

### If the schematic looks wrong after Agent mode

- **The `.kicad_sch` file** *before* and *after* the Agent turn. Yes, both. The plugin keeps `.tracer-bak` backups next to the file — the one with the timestamp just before your prompt is the "before".
- **A screenshot** of the schematic editor showing the bad state.

### If your quota / usage numbers look wrong

- **A screenshot of the billing page.**
- **Your best guess of what you actually did** in the current session and week.

### Where to send it

Email `beta@tracer.dev` with subject `[bug] <one-line summary>`. Or drop it in the `#beta-bugs` channel of our Discord if we've invited you there.

---

## 7. Known limitations (please don't report these)

- **First Agent turn can take 60-120 seconds.** Free-tier Gemini is slow and our repair loop runs up to twice per turn. Timeout budget is 300s.
- **Attachments (image uploads) are UI-only right now.** The plugin sends the filename but the backend doesn't yet forward image bytes to Gemini. Text prompts work end-to-end.
- **Streaming isn't wired.** You get the whole reply at once, not token-by-token. It's not broken — just deferred.
- **No macOS / Linux build.** Coming after the beta.
- **`hobby` is the only tier open.** `basic` and `pro` are wired in the code but Stripe checkout isn't live yet.

---

## 8. What we most want feedback on

Rank of interest, highest first:

1. **Agent mode circuit quality.** Does it actually build what you asked for? File the `.kicad_sch` before/after and tell us.
2. **Prompt ergonomics.** What did you have to say twice before Tracer got it? What phrasing worked first try?
3. **Latency perception.** Which mode felt "fine", "slow", "unusable"?
4. **Error clarity.** When something failed, did the message tell you what to do next?
5. **Quota fairness.** Did the 5-hour and 7-day caps feel reasonable for a real work session, or did they cut you off mid-thought?

---

## 9. Getting help

- **Something's on fire:** `beta@tracer.dev`
- **Feature request:** same address, subject `[fr] ...`
- **General chat / questions:** Discord `#beta-general`
- **This document is unclear:** tell us — file it as a bug against the doc.

Thanks for testing. Every session you run makes the shipped version measurably less bad.
