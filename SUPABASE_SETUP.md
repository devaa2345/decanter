# Supabase setup checklist (owner dashboard)

The dashboard and the catalog "retrain" (upload → regenerate `catalog_data.json`)
feature need a Supabase project for storage, analytics, and login. Do this once.

## 1. Get your API keys

Supabase dashboard → your project → **Project Settings → API**. Copy three values:

- **Project URL** (e.g. `https://xxxxxxxx.supabase.co`)
- **anon / public key**
- **service_role key** — this one is secret, treat it like a password. It bypasses
  all database security rules, so it only ever goes in server-side config
  (your local `.env` and Render's environment variables), never in the browser
  and never committed to git.

## 2. Create your login

**Authentication → Providers** — confirm "Email" is enabled (it is by default).

**Authentication → Users → Add user** — create the one owner login you'll use to
sign into `/dashboard`: an email + password. That's the only account you need;
this dashboard is single-owner, there's no separate sign-up flow.

## 3. Create the storage bucket

**Storage → New bucket** — name it exactly `catalog-versions`, and leave it
**private** (not public). This holds every catalog version ever uploaded, so
you can roll back if a sheet upload goes wrong.

## 4. Run the schema migration

**SQL Editor → New query** — paste the entire contents of
[`supabase/migrations/0001_init.sql`](supabase/migrations/0001_init.sql) and click
**Run**. This creates two tables: `message_events` (powers the analytics
dashboard) and `catalog_versions` (powers upload/review/publish/rollback).

## 5. Set environment variables

Create a `.env` file in the project root (it's already gitignored) with:

```env
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_ANON_KEY=<anon public key from step 1>
SUPABASE_SERVICE_ROLE_KEY=<service_role key from step 1>
OWNER_EMAIL=<the email you used in step 2>
```

Then set the same four values in **Render → your service → Environment** so the
deployed bot has them too (mark `SUPABASE_SERVICE_ROLE_KEY` as a secret).

## 6. Seed the catalog (one-time)

Once the above is done, run:

```bash
python scripts/seed_catalog.py
```

This pushes your current 1,207-perfume `catalog_data.json` into Supabase as the
first **published** catalog version, so the dashboard's version history and the
"Catalog upload" diff view have something real to compare against instead of
starting from nothing.

## What you never need to give Claude / paste into chat

The `service_role` key and your login password are secrets for *your* Supabase
project — fill them into `.env` and Render's dashboard yourself. There's no step
in this setup that requires pasting either into the chat.

## If you'd rather I run the migration for you

This session's Supabase connector isn't authorized for your new project (it only
sees a few unrelated ones). If you connect the right Supabase organization/project
to the connector, I can run the migration and inspect the schema directly instead
of you using the SQL Editor — but it's entirely optional, the steps above work
fine on their own.
