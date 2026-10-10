# ENV_TEMPLATE.md - SAMePOS environment variable inventory

STATUS: Draft, assembled from the Sam repo plus Mr LSG's setup pack. The real .env files live in the product repos, not here. To finish this file: open each real .env, diff it against the matching block below, fix any names that differ, and add anything missing. **Every value below is a placeholder by design. Zero real secrets, ever.**

## How to read this file

Each block is one component of the SAMePOS stack. Inside each block, the comment above a variable gives its purpose and a provenance tag:

| Tag | Meaning |
|-----|---------|
| [repo] | Verified in the Sam repo (file named in the comment). |
| [pack] | Stated by Mr LSG in the setup pack. Authoritative context. |
| [draft] | Inferred or industry-standard guess. Confirm the real name before trusting it. |

A [pack] tag on a block means the component exists per the pack. A [draft] tag on a variable means the exact variable NAME is a guess even if the component is real.

The Supabase project ref `kpjqfsbjksnmxogmmcrb` appears in the pack itself, so hostnames using it are safe to keep here. Keys are not.

---

## 1. POS node / local database

Context: the pack says the local cache is PostgreSQL 16 on port 5433 with CDC triggers [pack]. The repo says the node runs an embedded PostgreSQL and a FastAPI app [repo: db/README.md]. Those two facts fit together (embedded PG serving on 5433), but the repo never states the port. Confirm 5433 is what the embedded instance actually listens on.

```bash
# --- POS node / local PostgreSQL ---

# Local DB connection. Either one URL or the PG* set below.
# Component is real [pack]; these exact variable names are [draft].
DATABASE_URL=postgresql://<LOCAL_DB_USER>:<LOCAL_DB_PASSWORD>@localhost:5433/<LOCAL_DB_NAME>

# Split form, standard libpq names. [draft]
PGHOST=localhost
PGPORT=5433
PGDATABASE=<LOCAL_DB_NAME>
PGUSER=<LOCAL_DB_USER>
PGPASSWORD=<LOCAL_DB_PASSWORD>

# Standard trial length in days. Default 14. The licensing core honors this
# via licence_ensure_trial(:days) or licence_config.standard_trial_days.
# [repo: db/README.md, "Trial length also honours the product's
# SAMEPOS_TRIAL_STANDARD_DAYS env var"]
SAMEPOS_TRIAL_STANDARD_DAYS=<TRIAL_DAYS>
```

Open question for the node: the repo proves a FastAPI app serves `/api/licence/status` and `POST /api/licence/activate` on the node [repo: db/README.md]. That app almost certainly has its own settings (bind port, vendor public key location, log path). None are named in this repo. Add them from the real node .env. [draft]

---

## 2. Sync engine (Python, ~900 lines)

Context: the sync engine reads the local PG (CDC triggers) and writes to Supabase cloud [pack]. It is a trusted backend writer, so per pack rule 6 it uses the service role key, never the anon key.

```bash
# --- Sync engine ---

# Local source database, same instance as block 1.
# Component real [pack]; name [draft].
LOCAL_DATABASE_URL=postgresql://<LOCAL_DB_USER>:<LOCAL_DB_PASSWORD>@localhost:5433/<LOCAL_DB_NAME>

# Supabase project URL. The ref is from the pack's own pg_dump command.
# Component real [pack]; name [draft].
SUPABASE_URL=https://kpjqfsbjksnmxogmmcrb.supabase.co

# Service role key. TRUSTED BACKEND WRITER ONLY. Bypasses RLS.
# Never ship this to a terminal, dashboard, or browser. [pack rule 6]
# Name [draft].
SUPABASE_SERVICE_KEY=<SUPABASE_SERVICE_KEY>

# Terminal identity, if the engine stamps terminal_id on events itself.
# The (terminal_id, sale_ref) unique constraint is load bearing [pack].
# Whether this arrives via env or config file is unknown. [draft]
TERMINAL_ID=<TERMINAL_ID>
```

---

## 3. Supabase Edge Function - invoice scanning

Context: AI supplier invoice scanning runs in a Deno/TypeScript Edge Function calling Claude [pack]. Edge Function secrets are set with `supabase secrets set NAME=value`, not a .env file in the repo.

```bash
# --- Edge Function secrets (set via: supabase secrets set) ---

# Claude API key for invoice scanning. Component real [pack];
# this exact name is the Anthropic SDK default, but still [draft] here.
ANTHROPIC_API_KEY=<ANTHROPIC_API_KEY>

# Injected automatically by the Supabase platform into every Edge Function.
# Do not set these yourself; listed so you know they exist. [draft, standard
# Supabase behavior]
# SUPABASE_URL=<provided by platform>
# SUPABASE_ANON_KEY=<provided by platform>
# SUPABASE_SERVICE_ROLE_KEY=<provided by platform>
# SUPABASE_DB_URL=<provided by platform>
```

---

## 4. Dashboard (React + TypeScript, dual connection)

Context: the dashboard connects to both Supabase cloud and the local PG [pack]. Browser-exposed variables need a framework prefix. **Which prefix is real depends on the build tool, and the pack does not say.** Next.js uses `NEXT_PUBLIC_`. Vite uses `VITE_`. Plain CRA uses `REACT_APP_`. Confirm which one the dashboard uses and delete the other spellings.

```bash
# --- Dashboard (pick ONE prefix family, delete the rest) ---

# Supabase URL, browser-safe. Component real [pack]; names [draft].
NEXT_PUBLIC_SUPABASE_URL=https://kpjqfsbjksnmxogmmcrb.supabase.co
# VITE_SUPABASE_URL=https://kpjqfsbjksnmxogmmcrb.supabase.co

# Anon key, browser-safe by design. RLS scoped by venue does the real
# protection [pack rule 6]. Names [draft].
NEXT_PUBLIC_SUPABASE_ANON_KEY=<SUPABASE_ANON_KEY>
# VITE_SUPABASE_ANON_KEY=<SUPABASE_ANON_KEY>

# Local connection for the dual-connection fallback. How the browser
# reaches local PG is not stated; likely via the node's local API, not
# raw Postgres. Confirm the mechanism and the real name. [draft]
NEXT_PUBLIC_LOCAL_API_URL=http://localhost:<NODE_API_PORT>
# VITE_LOCAL_API_URL=http://localhost:<NODE_API_PORT>
```

---

## 5. Billing app (Next.js App Router + Stripe + Prisma + Supabase)

Context: billing is Stripe plus Prisma plus Supabase on Next.js App Router with an idempotent webhook handler [pack]. Variable names below are the standard ones for that exact stack, but all are [draft] until diffed against the real .env.

```bash
# --- Billing app ---

# Stripe server-side secret key. Server only, never NEXT_PUBLIC_. [draft]
STRIPE_SECRET_KEY=<STRIPE_SECRET_KEY>

# Stripe webhook signing secret. Verifies webhook authenticity; the
# idempotent handler depends on trusting only signed events. [draft]
STRIPE_WEBHOOK_SECRET=<STRIPE_WEBHOOK_SECRET>

# Stripe publishable key, browser-safe. [draft]
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=<STRIPE_PUBLISHABLE_KEY>

# Prisma connection string to the billing database (Supabase Postgres).
# DATABASE_URL is Prisma's default name. Supabase's pooler setup usually
# adds DIRECT_URL for migrations; confirm if you use it. [draft]
DATABASE_URL=postgresql://<DB_USER>:<DB_PASSWORD>@<DB_HOST>:5432/<DB_NAME>
DIRECT_URL=postgresql://<DB_USER>:<DB_PASSWORD>@<DB_HOST>:5432/<DB_NAME>

# Supabase for auth / provisioning writes. Service role stays server-side
# only [pack rule 6]. Names [draft].
NEXT_PUBLIC_SUPABASE_URL=https://kpjqfsbjksnmxogmmcrb.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<SUPABASE_ANON_KEY>
SUPABASE_SERVICE_ROLE_KEY=<SUPABASE_SERVICE_KEY>
```

---

## 6. The Sam repo's own .env (Sema4.ai action server)

Context: this is the one .env whose names are fully verified, copied from `.env.example` in the Sam repo [repo: .env.example, README.md]. It powers the repo's Snowflake actions and `ask_claude` action. It is not part of the SAMePOS product runtime.

```bash
# --- Sam repo action server (all names [repo: .env.example]) ---

# Snowflake account identifier, e.g. myorg-myaccount.
SNOWFLAKE_ACCOUNT=<SNOWFLAKE_ACCOUNT>
SNOWFLAKE_USER=<SNOWFLAKE_USER>

# Password auth (option A).
SNOWFLAKE_PASSWORD=<SNOWFLAKE_PASSWORD>

# Key-pair auth (option B). Takes precedence over the password when set.
SNOWFLAKE_PRIVATE_KEY_PATH=<PATH_TO_PKCS8_KEY>
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=<KEY_PASSPHRASE>

# Optional session defaults. Prefer the read-only role.
SNOWFLAKE_WAREHOUSE=<WAREHOUSE>
SNOWFLAKE_DATABASE=<DATABASE>
SNOWFLAKE_SCHEMA=<SCHEMA>
SNOWFLAKE_ROLE=<READONLY_ROLE>

# Claude API key for the ask_claude action.
ANTHROPIC_API_KEY=<ANTHROPIC_API_KEY>
```

---

## Known tension: the Azure cloud app

The Sam repo contains a GitHub Actions workflow configured to build an ASP.NET Core app named SAMePOS and deploy it to Azure App Service, though the repo itself holds no .NET source [repo: .github/workflows/main_samepos.yml]. The pack says the cloud layer is Supabase [pack]. Both can be true (a legacy or parallel cloud app), but this file cannot resolve it. If the Azure app is live, its settings live in Azure App Service configuration, not a .env, and are not inventoried here. **Tell Claude which cloud layer is current so it stops guessing.**

---

## Rules

1. **Never commit a .env file.** The Sam repo already gitignores `.env` [repo: README.md]. Every product repo must do the same. Check with `git check-ignore .env` in each repo.
2. If a real key ever lands in a chat, a commit, or a knowledge file: **rotate the key first**, then clean up the leak. A deleted message is not a rotation. [pack rule 7]
3. Service role vs anon key separation [pack rule 6]:

| Key | Who holds it | RLS |
|-----|--------------|-----|
| Service role key | Sync engine, billing server, Edge Functions | Bypassed. Full write access. |
| Anon key | Dashboard, terminals, anything in a browser | Enforced. Scoped by venue. |

Never mix the two. A service key in browser code is a full database leak.

4. Every value in this file is a placeholder. When you copy a block into a real .env, replace every `<PLACEHOLDER>` and delete the comment tags you no longer need.
5. When you diff this file against a real .env and find a variable not listed here, add it here with its purpose and mark it [repo-of-truth: that .env]. This file is only useful if it stays complete.

## Next action

Open the sync engine's real .env first. Diff it against block 2. It holds the service role key, so it is the highest-risk file to get right.
