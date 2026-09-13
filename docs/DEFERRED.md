# Deferred features

Items that were scoped, understood, and consciously deferred. Not "TODO".
Each has a **decision date** so we know when to reopen it.

## Autoroute via Freerouting

**Deferred:** 2026-09-13.
**Trigger to reopen:** boards routinely hit >50 nets, or manual routing
becomes a repeated pain point.

**Scope when we build it (~2 days work + ongoing maintenance):**

1. Install `kicad-python` (PyPI).
2. Spike `KiCad.run_action("...")` with candidate names — likely
   `ExportSpecctra` and `ImportSpecctra` — until a headless DSN export
   lands a file. **The API is marked unstable by KiCad, so no name is
   guaranteed.** If the spike fails, Freerouting is dead here.
3. Bundle `freerouting.jar` (GPL), detect Java runtime, call as
   subprocess with `--input <dsn> --output <ses>`, parse crash logs.
4. Wire the SES import back through `run_action` — same unstable
   name-hunt as step 2.
5. Add UI: a "Route" button in the dock, gated by "pcb exists" +
   "not already routing"; progress in the Activity log.

**Known risks that will not go away:**

- Every KiCad point release can silently rename actions.
- Freerouting's cost function over-vias on small boards (adds worse
  results than manual for anything under ~30 nets).
- Java bundling adds an install-size and runtime-dep burden.

**What we do instead until then:**

Route interactively inside KiCad. The push-shove router does 4-net
clusters in ~90 seconds and produces cleaner results than Freerouting
on small boards.
