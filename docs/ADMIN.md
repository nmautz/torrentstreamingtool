# Admin Panel

`/admin` — served by `static/admin.html` ([main.py:3167](../main.py#L3167)). Disabled if `ADMIN_PASSWORD` is empty in `.env`.

## Auth flow

1. `GET /api/admin/status` returns `{enabled: bool}`. If false, the login overlay shows "Admin disabled" and the dashboard hides the admin link
2. `POST /api/admin/login {password}` → returns `{token}` (32 hex chars, `secrets.token_hex(32)`)
3. Token stored client-side in `sessionStorage.admin_token`. Sent on every request via `Authorization: Bearer <token>`
4. Server-side store: `_admin_sessions: dict[str, float]` — token → Unix-timestamp expiry. TTL is 24 h ([main.py:3184](../main.py#L3184))
5. `_check_admin(request)` accepts token from `Authorization: Bearer`, `X-Admin-Token` header, or `?admin_token=` query param. The query-param form is needed for SSE because EventSource can't set headers

## HTTPS redirect ([main.py:1772](../main.py#L1772))

`admin_https_redirect` middleware: any HTTP request to `/admin*` or `/api/admin/*` returns a 301 to the same path on `https://<host>/`. Assumes the HTTPS process is listening on port 443 — `run.py` only launches it when `cert.pem`+`key.pem` exist.

Browsers will show a warning until `ca.pem` is added to the system trust store. `setup.py` prints the platform-specific command:
- macOS: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.pem`
- Linux: `sudo cp ca.pem /usr/local/share/ca-certificates/streamlink-ca.crt && sudo update-ca-certificates`
- Windows: `Import-Certificate -FilePath ca.pem -CertStoreLocation Cert:\\LocalMachine\\Root`

## Tabs

### 1. Indexers ([static/admin.html:95](../static/admin.html#L95))

Lists configured Jackett indexers. Each row shows the indexer name + test result + delete button. Add button opens a modal that:
1. Calls `GET /api/admin/indexers/available` for the full Jackett catalog
2. Renders a filterable list. Selecting one calls `GET /api/admin/indexers/{id}/config` for the config form schema (Jackett returns field types: text, password, checkbox, select)
3. Save POSTs back to `/api/admin/indexers/{id}/config`

Also a small form to override `INDEXER_CATEGORIES` at the top of the tab. This writes to `library.json` → `settings.admin_overrides.indexer_categories` rather than touching `.env`, so it can be changed without a restart. The `/api/search` endpoint reads this override at query time.

#### Jackett authentication

If `JACKETT_PASSWORD` is set in `.env`, `_jackett_admin()` ([main.py:200](../main.py#L200)) calls `/UI/Login` (POSTing `{password}`) and caches the `Jackett` session cookie for 1 hour. All admin indexer endpoints use this cookie. If the password is wrong, returns 502 with "Could not authenticate with Jackett".

### 2. Content Lock ([static/admin.html:142](../static/admin.html#L142))

Lists all library items with an "Admin only" toggle. Calls `POST /api/library/{id}/admin-lock {admin_only}`. When `admin_only=true`:
- `GET /api/library` excludes the item unless the requester is admin OR the requesting `profile_id` has `elevated=true`
- Other endpoints (`/files`, `/play`, `/download`, etc.) currently do not check `admin_only`; the gate is at list-time only

To grant a profile elevated access without making them an admin, use the Profile PINs tab.

### 3. Smart Skip ([static/admin.html:155](../static/admin.html#L155))

For each item:
- **Series key**, file count, "X/Y files have skip data"
- If an analysis job is running for the series, shows a live progress bar (driven by `analysis_status` SSE events)
- **Analyze** button → `POST /api/admin/library/{id}/analyze` — force re-run for the entire series
- **Edit** button → opens inline editor with three numeric fields per file (intro start, intro end, credits start). Empty → clear. Save calls `PATCH /api/admin/library/{id}/skip-data`. Manual edits set `analysis.source="manual"` so they survive future analyzer runs

Admin SSE: `ensureAdminSSE()` opens `/api/events?admin_token=…` so the progress bars live-update.

### 4. Profile PINs ([static/admin.html:182](../static/admin.html#L182))

For each profile:
- **Set PIN** — admin overrides the usual current-PIN check
- **Clear PIN** — same
- **Elevated** toggle → `POST /api/profiles/{id}/set-elevated {elevated}` — grants view of `admin_only` items

## Server endpoints (admin)

All require admin auth (`_require_admin`). See [API.md](API.md#admin) for the full table.

## See also

- [FRONTEND.md](FRONTEND.md) — admin.html structure
- [API.md](API.md) — admin endpoint signatures
- [BACKEND.md](BACKEND.md) — `_check_admin`, `_jackett_admin`, `_admin_sessions`
