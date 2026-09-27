# OR-Engine — Docker Compose

Each service in the stack gets its own `compose.yml` under a dedicated subdirectory.
The top-level `compose.yml` uses `include:` to merge them into one project.

## Layout

```
compose/
├── .env                  ← secrets (POSTGRES_PASSWORD, AMAP_API_KEY, ACR vars)
├── .env.example
├── compose.yml            ← top-level orchestrator (include: all sub-files)
│
├── postgis/
│    └── compose.yml        ← PostgreSQL + PostGIS extension
│
├── postgrest/
│    └── compose.yml        ← REST API proxy over Postgres
│
├── or-engine/
│    └── compose.yml        ← REST + MCP server  (image pulled from ACR)
│
├── janux/
│    ├── compose.yml        ← auth server + OIDC provider  (image pulled from ACR)
│    ├── base.toml          ← server config   (copy from base.example.toml)
│    ├── base.example.toml
│    ├── seed.toml          ← tenant / RBAC seed  (copy from seed.example.toml)
│    └── seed.example.toml
│
└── caddy/
     ├── compose.yml        ← TLS reverse-proxy front-end
     └── Caddyfile          ← routing + forward-auth
```

## Usage

### Start the full stack

The primary entry point. Merges all sub-files via `include:`.

```bash
cd compose

# First time — copy janux config from the examples and fill in your details:
cp janux/base.example.toml  janux/base.toml    # then edit: set a real encryption_key
cp janux/seed.example.toml  janux/seed.toml    # then edit: set admin email
cp .env.example .env                           # then fill in POSTGRES_PASSWORD, AMAP_API_KEY

docker compose up -d
```

### Start a single component

Sub-files are definition units — they resolve cross-file dependencies
(`depends_on`, network, volumes) via the top-level orchestrator.
For infrastructure-only services (no deps) you can run standalone,
but Docker Compose looks for `.env` in the compose file's own
directory, so pass `--project-directory .` to find `compose/.env`:

```bash
cd compose
docker compose --project-directory . -f postgis/compose.yml  up -d
```

### Tear down

```bash
cd compose
docker compose down               # stop and remove containers
docker compose down --volumes     # also remove named volumes (WARNING: deletes data)
```

## Network diagram

```
Browser
   │
   ▼
 Caddy   (:80 / :443)
   │
   ├── /optimize, /mcp *          forward_auth → janux /api/v1/auth/verify → or-engine
   ├── /health                    → or-engine   (public, monitoring)
   └── *                          → janux       (login UI, OIDC, SCIM, admin, verify)

    janux   (:8080)      ←  Caddy  (auth + all public API)
    or-engine (:8000)    ←  Caddy  /  → postgrest (:3000) → postgis (:5432)
```

## Image sources

| Service  | Image                                                              |
|----------|-------------------------------------------------------------------|
| postgis  | `postgis/postgis:16-3.4`                                          |
| postgrest | `postgrest/postgrest:v12.2.0`                                    |
| or-engine | `${ACR_REGISTRY}/${ACR_NAMESPACE}/or-engine:latest`  (Aliyun ACR) |
| janux     | `${ACR_REGISTRY}/${ACR_NAMESPACE}/janux:latest`    (Aliyun ACR)   |
| caddy    | `caddy:2.8-alpine`                                              |

`ACR_REGISTRY` and `ACR_NAMESPACE` are in `.env` (defaults match the CI pipeline).

## Janux config

`janux/base.toml` and `janux/seed.toml` are bind-mounted into the container's
`/app/` directory at compile time. They are NOT committed with real secrets —
use the `*.example.toml` files as a template and keep your real
`base.toml` / `seed.toml` out of version control.

Key files to adjust before production use:

- `base.toml` → `encryption_key` (generate: `openssl rand -hex 32`)
- `seed.toml` → `users[].email` (a real inbox you control)
- `seed.toml` → `[seed.resend].resend_key` (to enable magic-link email)
