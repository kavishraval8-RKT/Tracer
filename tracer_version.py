"""Single source of truth for the plugin version.

Bumped by hand at release. Read by:
- sidecar_app.py — logged on startup and shown in the Account panel
- tracer_api.py — sent as X-Tracer-Version on every backend request
                  so the backend log tells you which plugin version
                  a bug report came from
"""
TRACER_VERSION = "0.1.0"
