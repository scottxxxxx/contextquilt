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
