# Remote-Access Gate (Site Auth)

Off-LAN clients must clear three checks before any `/api/*` route responds:

1. **LAN-only admin**: `/admin`, `/admin.html`, `/admin/*`, and `/api/admin/*` are 404 from any non-LAN IP.
2. **HTTPS forced**: all remote HTTP requests are 301-redirected to `https://`. Admin is HTTPS-forced from any origin (LAN included).
3. **Site password**: remote `/api/*` (other than the auth bootstrap below) requires a valid `streamlink_site` cookie. Without it → 401.

All three are enforced by a single FastAPI middleware: `network_access_gate` in `main.py`.

The admin panel also exposes a **Remote Access tab** that lets an admin flip the gate on/off without editing `.env`. Disable kicks every active remote session immediately. The same tab surfaces a requirements checklist (password set, cert present, HTTPS process listening on 443) and refuses to let the toggle flip to *enable* until everything's green.

## Settings (`.env`)

| Key | Default | Notes |
|-----|---------|-------|
| `SITE_PASSWORD` | `""` | If empty, **remote `/api/*` access is blocked entirely** (503). LAN access is unaffected. |
| `SITE_SESSION_MINUTES` | `60` | TTL of the session cookie / token, in minutes. |

## "Local" definition

`_is_local_request(request)` returns true when `request.client.host` is loopback, RFC 1918 private (10/8, 172.16/12, 192.168/16), link-local (169.254/16), or the IPv6 equivalents (`::1`, `fe80::/10`, `fc00::/7`). `X-Forwarded-For` is **deliberately ignored** — the server has no trusted reverse proxy, so honouring that header would let a remote client spoof a LAN address.

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/site/status` | Always reachable. Returns `{enabled, is_local, authenticated, session_minutes}`. `enabled` reflects both `SITE_PASSWORD` and the admin toggle. |
| POST | `/api/site/login` | Body: `{password}`. Sets `streamlink_site` cookie (HttpOnly, Secure when over HTTPS, SameSite=Lax, `Path=/`, `Max-Age=session_minutes*60`). Brute-force protected. 503 if admin has disabled the gate. |
| POST | `/api/site/logout` | Revokes the token server-side and clears the cookie. |
| GET | `/api/admin/remote-access` | **Admin (LAN-only).** `{admin_enabled, ready, active, session_minutes, requirements:{password_set, cert_present, https_listening}}`. |
| POST | `/api/admin/remote-access` | **Admin (LAN-only).** Body: `{enabled: bool}`. Persists the toggle to `library.json` → `settings.admin_overrides.remote_access_enabled`. Disable also clears every active site session immediately. |

## Brute-force protection

Per-IP sliding window. After **5 failed attempts in 15 minutes**, the IP receives 429 with `Retry-After` for the remainder of the 15-minute window. The lockout state lives in `_site_login_attempts: dict[str, list[float]]` — process-local; restart clears it. Password comparison uses `secrets.compare_digest` for constant-time equality.

## Token transport

A single HttpOnly cookie (`streamlink_site`). Cookies are the only mechanism that auto-applies to every request type including `EventSource` (SSE) and static fetches — picking a header-based scheme would have forced a re-architecture of `/api/events`. The cookie is also accepted via `X-Site-Token` for non-browser clients.

Server-side store: `_site_sessions: dict[token → expiry_unix]`. Tokens are 32 hex chars (`secrets.token_hex(32)`). Like `_admin_sessions`, this is process-local and lost on restart.

## Frontend flow

`static/index.html`:

1. On `DOMContentLoaded`, `ensureSiteAuth()` calls `/api/site/status`.
2. If `is_local || authenticated` → init proceeds normally.
3. Otherwise the `#siteLoginOverlay` is shown. On successful login the page reloads (the cookie is set by the server response, so the full init flow then runs with auth in place).
4. If `enabled=false` (no `SITE_PASSWORD` configured), the overlay shows "Remote Access Disabled" — the dashboard cannot be used from off-LAN until an admin sets the password.

## Gotchas

- **Service worker / `/sw.js`**: not under `/api/*`, so it's reachable to off-LAN clients pre-login. That's fine — it's a static asset with no data. The SW's fetches go through the cookie automatically.
- **Cookie `Secure` flag**: only set when the login request itself came in over HTTPS. Since remote HTTP is force-redirected to HTTPS before login can happen, this works out — the login that actually completes is always HTTPS.
- **LAN-IP misclassification**: a host configured with a public IP on a NAT-less network would be treated as remote. Set up RFC 1918 addressing on the trusted LAN, or treat all access as off-LAN.

## See also

- [ADMIN.md](ADMIN.md) — admin is LAN-only, layered on top of this gate.
- [GOTCHAS.md](GOTCHAS.md) — entry on `X-Forwarded-For` and the cookie scheme.
