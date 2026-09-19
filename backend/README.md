# Tracer Backend

FastAPI cloud service. Validates device JWTs minted by the Next.js frontend
and proxies LLM calls to Gemini using `agent.py`.

Stateless — Supabase owns all persistent state (users, subscriptions,
device_codes, active-device jti, usage). This service only reaches Supabase
via its REST API to check jti freshness on each request.

## Run locally

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

Copy-Item .env.example .env
# Then edit .env:
#   TRACER_DEVICE_JWT_SECRET  — same value as tracer-frontend/.env.local
#   TRACER_SUPABASE_URL       — https://<proj>.supabase.co
#   TRACER_SUPABASE_SERVICE_ROLE_KEY — from Supabase → Project Settings → API
#   TRACER_GEMINI_API_KEY     — https://aistudio.google.com/apikey

uvicorn app.main:app --reload --port 8100
```

Then:

```bash
curl http://127.0.0.1:8100/health
# → {"ok": true, "version": "0.1.0",
#    "supabase_configured": true, "jwt_secret_configured": true,
#    "gemini_configured": true}
```

`false` on any of the flags means that env var isn't set.

## Endpoints

All `/v1/*` routes require `Authorization: Bearer <device_jwt>`.

| Method | Path | What it does |
|---|---|---|
| GET  | `/health`      | Config sanity + liveness |
| GET  | `/version`     | App version |
| GET  | `/v1/me`       | Returns `{id, tier, jti}` from the JWT. First test after login. |
| POST | `/v1/generate` | Agent mode. Body: `{prompt, history?, attachments?}`. Runs full generate + backend build + auto-repair. |
| POST | `/v1/ask`      | Ask mode. Body: `{prompt, history?}`. Plain-text Q&A. |
| POST | `/v1/plan`     | Plan mode. Body: `{prompt, history?}`. Design outline, no build. |

## Smoke test with a real device JWT

Grab one via the frontend flow (mint code → activate → poll for JWT — see
tracer-frontend/README or SETUP.md), then:

```powershell
$JWT = "eyJhbGci..."   # your token
Invoke-RestMethod -Uri http://127.0.0.1:8100/v1/me `
  -Headers @{ Authorization = "Bearer $JWT" }
# → @{ id = "b73eb2b3-…"; tier = "hobby"; jti = "…" }
```

If you get `{"error": "revoked"}`, someone activated another plugin on the
same account and rotated the jti — expected behavior. Re-mint a token.

## Layout

```
backend/
├── app/
│   ├── main.py         # FastAPI app + CORS + Gemini env bridge
│   ├── settings.py     # pydantic-settings from .env
│   ├── auth.py         # require_user dependency — JWT verify + jti check
│   ├── agent.py        # copied from ../agent.py; source-of-truth for LLM logic
│   └── routers/
│       └── v1.py       # /v1/generate|ask|plan|me
└── pyproject.toml
```

`app/agent.py` is a copy of `../agent.py` (the plugin's engine). In an
upcoming slice the plugin drops its copy and calls this backend instead.

## Phase status

- [x] **Slice C — Trim scaffold** — dropped local DB, Alembic, SQLAlchemy.
- [x] **Slice D — JWT validation** — HS256 verify + Supabase jti freshness check.
- [x] **Slice E — Mount agent** — `/v1/generate|ask|plan|me` behind auth.
- [ ] **Slice F — Quota enforcement** — count tokens per request, 402 when over.
- [ ] **Slice G+H+I+J — Plugin integration** — login button, JWT storage,
      swap `agent.py` for backend HTTP, 401/402 handling.
