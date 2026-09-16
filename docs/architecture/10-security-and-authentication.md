# Security & Authentication

ContextQuilt follows a **server-authoritative** security model. The server is the authority on all access decisions — clients are untrusted.

## Authentication Model

CQ authenticates **applications**, not end users. Apps are responsible for their own user authentication (Apple Sign-In, OAuth, etc.) and pass `user_id` to CQ. CQ trusts the app to provide correct user identities.

### App Registration

```bash
# Register your app
POST /v1/auth/register
{"app_name": "my-app"}

# Response — store client_secret securely, it's shown once
{
  "app_id": "930824d3-2ccb-4869-b3f0-0ed2693f183f",
  "app_name": "my-app",
  "client_secret": "cs_..."
}
```

### JWT Authentication (Required)

```bash
# Exchange credentials for a JWT
POST /v1/auth/token
Content-Type: application/x-www-form-urlencoded
username={app_id}&password={client_secret}

# Response
{"access_token": "eyJ...", "token_type": "bearer", "expires_in": 3600}

# Use the token on all API calls
curl -H "Authorization: Bearer eyJ..." https://your-cq-host/v1/recall
```

JWTs expire after 60 minutes. Your app should refresh tokens before expiry.

### Auth Enforcement

Apps with `enforce_auth: true` (default for new registrations) **require JWT** on every request. The legacy `X-App-ID` header fallback is disabled.

```bash
# Enable strict auth (recommended)
PATCH /v1/auth/apps/{app_id}
{"enforce_auth": true}
```

Unregistered app IDs are rejected with 401.

### Admin Dashboard

The admin dashboard at `/dashboard/` is protected by a separate `X-Admin-Key` header (set via the `CQ_ADMIN_KEY` environment variable). This is independent of app authentication.

### Token Endpoint Rate Limiting

`POST /v1/auth/token` verifies with pbkdf2_sha256, so a WRONG credential
costs CQ one password hash per attempt, and a caller that retries turns
its own request rate into hashing load on us. Until 2026-09-16 there was
no limit of any kind.

`services/auth_rate_limit` counts FAILURES per `client_id` in Redis and
refuses with **429 + `Retry-After`** once a client passes
`CQ_AUTH_MAX_FAILURES` (default 10) inside
`CQ_AUTH_FAILURE_WINDOW_SECONDS` (default 900). Kill switch:
`CQ_AUTH_RATELIMIT_ENABLED=0`.

Four properties, each load bearing:

- **It runs before the lookup and the verify.** A refusal that still pays
  the hash removes the feature while keeping every symptom of having it,
  so a unit test asserts the ORDER in `main.py`'s own source.
- **Failures only; a success clears the counter.** Minting is never
  throttled, so an honest caller cannot be locked out by using the API.
- **It fails OPEN.** Redis unreachable means allowed. A closed limiter
  converts a cache outage into a total auth outage, which is worse than
  the load it prevents.
- **The window starts at the first failure** and is not pushed forward by
  later ones, so a steady trickle cannot hold a caller out indefinitely.
  The counter lives in Redis because prod runs four uvicorn workers, and
  four in-process counters would each carry the full budget.

Found from the other side: GhostPour had built a client-side cooldown for
rejected credentials, because a wrong secret is permanent and retrying it
is pure cost. That is a caller compensating for a missing server limit,
and it only protects CQ for as long as every caller has built one.

### Admin Key Rate Limiting

Every admin-gated route answers 403 or 200, so every one of them is an
oracle for `CQ_ADMIN_KEY`, and that key is a SINGLE long-lived operator
secret gating every admin surface CQ has.
`GET /api/dashboard/verify-key` is simply the politest oracle, being
unauthenticated by design so the dashboard login can check a typed key.

Reachability, measured 2026-09-16: the edge IP-gates exactly two literal
prefixes, `cz.shouldersurf.com/admin` and `cq.shouldersurf.com/dashboard`.
CQ mounts the dashboard UI at `/dashboard` and its API at
`/api/dashboard/`, which that prefix never matches, and CQ does not check
the `Host` header anywhere, so the same routes answer on
`contextquilt.com` too. The oracle was internet-reachable with no limit.

So the counter lives in `verify_admin_key` (`contextquilt/api_deps.py`),
the check every admin route shares. Limiting one endpoint would have
moved the guessing to `/api/dashboard/stats`.

Two buckets, because one of them can be weaponised:

- **Per source**, `CQ_ADMIN_MAX_FAILURES` (10) in
  `CQ_ADMIN_FAILURE_WINDOW_SECONDS` (900).
- **Global**, `CQ_ADMIN_GLOBAL_MAX_FAILURES` (50) in
  `CQ_ADMIN_GLOBAL_WINDOW_SECONDS` (3600), to catch a rotating source at
  a threshold an operator's own typos will never reach.

A correct key clears the SOURCE bucket only: one operator typing the right
key must not reset a distributed campaign's budget.

**The source identity is the delicate part.** A counter keyed on a header
anyone can set is worse than no counter: it can be rotated to evade, and
it can be AIMED at the operator's address to lock them out of the
dashboard. GhostPour reaches CQ container to container at
`http://contextquilt:8000` and never traverses the proxy, so traffic with
no forwarding header is normal, and anything on that docker network can
send one saying whatever it likes. Therefore a forwarding header is read
ONLY from a peer listed in `CQ_TRUSTED_PROXY_IPS`; every other caller is
counted as the address CQ actually sees (`request.client.host`).

`CQ_TRUSTED_PROXY_IPS` is **empty by default**, which means no header is
ever believed and all edge traffic shares the proxy's bucket. That is the
safe direction, and it costs per-client precision until an operator sets
it to the proxy's address on the docker network. From the proxy,
`X-Real-IP` is preferred (nginx sets it from `$remote_addr`, replacing any
client value); `X-Forwarded-For` is the fallback and only its LAST entry
is used, because nginx APPENDS there and everything earlier is
client-supplied.

Fails OPEN, like the token limiter: no Redis, or none bound, means
allowed. A limiter in front of the login check that fails closed turns a
cache blip into "nobody can reach the dashboard", which is the same
lockout by another route.

## Threat Model

### What CQ Protects

| Protection | How |
|-----------|-----|
| App identity | JWT with HS256 signing — can't be forged without `JWT_SECRET_KEY` |
| User data isolation | All queries scoped by `user_id` — apps can only access users they submit |
| Admin access | `CQ_ADMIN_KEY` required for dashboard and management APIs |
| LLM API keys | Stored server-side only — never exposed to clients or in API responses |
| Database credentials | Environment variables, never in code or API responses |
| Unregistered apps | Rejected at the auth layer — only registered UUID apps accepted |

### What CQ Delegates to the Calling App

| Responsibility | Why |
|---------------|-----|
| End-user authentication | CQ doesn't know your users — your app verifies identity (Apple Sign-In, OAuth, etc.) and passes `user_id` |
| User-to-user isolation | CQ trusts the `user_id` your app provides. If your app sends the wrong `user_id`, CQ returns the wrong user's data |
| Rate limiting | CQ does not rate-limit API calls. Your gateway should handle this |
| TLS termination | CQ runs behind a reverse proxy (Nginx, Caddy, etc.) that handles HTTPS |

### Defense in Depth Recommendations

For production deployments:

1. **Enable `enforce_auth: true`** on all registered apps
2. **Use short-lived JWTs** — the default 60-minute expiry is reasonable; don't extend it
3. **Run CQ behind a reverse proxy** — never expose port 8000 directly
4. **Set strong secrets** — `JWT_SECRET_KEY` and `CQ_ADMIN_KEY` should be long random strings
5. **SSL certificate pinning** — if your client app is native (iOS/Android), pin the certificate to prevent MITM proxy inspection
6. **Rotate `client_secret`** periodically — re-register the app and update your gateway config
7. **Monitor access patterns** — unusual spikes in recall or memory writes may indicate abuse
8. **Never hardcode your production CQ URL in public repositories** — use environment variables (e.g., `CQ_BASE_URL`)

## Environment Variables (Security-Related)

```bash
# Required — set to strong random values
JWT_SECRET_KEY=...          # HS256 signing key for app JWTs
CQ_ADMIN_KEY=...            # Admin dashboard access key
CQ_LLM_API_KEY=...          # LLM provider API key (never exposed to clients)
POSTGRES_PASSWORD=...       # Database credential
REDIS_PASSWORD=...          # Cache credential
```

## Data Privacy

### GDPR Support

```bash
# Delete all data for a user (right to erasure)
DELETE /v1/quilt/{user_id}

# Returns: {"status": "deleted", "patches_deleted": N, "entities_deleted": N}
# Removes: all patches, entities, relationships, Redis caches
```

### Data Residency

CQ stores all data in your PostgreSQL and Redis instances. You control where these run. No data is sent to external services except the configured LLM provider for extraction (cold path only).

### What Gets Sent to the LLM

Only the cold path (extraction) calls the LLM. It sends:
- Meeting transcripts or conversation logs (for fact extraction)
- The system extraction prompt

The hot path (recall) **never calls an LLM** — it's pure database queries.
