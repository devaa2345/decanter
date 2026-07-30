-- Sovereign Scents dashboard — initial schema
-- Run this once in the Supabase SQL Editor (Project → SQL Editor → New query → paste → Run).

-- Every inbound WhatsApp query + how the 3-layer matcher resolved it.
-- This is what the analytics dashboard is built on.
create table if not exists message_events (
  id bigint generated always as identity primary key,
  message_id text,
  sender text,
  message_text text,
  perfume_id text,
  layer text,                 -- 'exact' | 'fuzzy' | 'llm' | null
  confidence numeric,
  ambiguous boolean not null default false,
  reply_sent boolean,
  created_at timestamptz not null default now()
);

create index if not exists message_events_created_at_idx on message_events (created_at);
create index if not exists message_events_perfume_id_idx on message_events (perfume_id);

-- One row per catalog sheet upload. 'pending' = awaiting owner review in the
-- dashboard, 'published' = went live at some point, 'discarded' = rejected
-- without ever going live. is_active marks whichever *one* published version
-- is currently live — every publish/rollback flips the old active row to
-- false and the new one to true in the same operation, so history of every
-- past published version is kept (for rollback) while staying unambiguous
-- about what's live right now.
create table if not exists catalog_versions (
  id bigint generated always as identity primary key,
  status text not null default 'pending' check (status in ('pending', 'published', 'discarded')),
  is_active boolean not null default false,
  source_filename text,
  storage_path text not null,   -- path in the 'catalog-versions' Storage bucket to the full JSON blob
  perfume_count int,
  added_count int,
  updated_count int,
  removed_count int,
  diff jsonb,                    -- structured diff for the review UI
  parse_warnings jsonb,          -- rows that didn't parse cleanly — surfaced to the owner, never silently guessed
  created_at timestamptz not null default now(),
  published_at timestamptz
);

create index if not exists catalog_versions_status_idx on catalog_versions (status);
create index if not exists catalog_versions_is_active_idx on catalog_versions (is_active);

-- RLS on: the dashboard backend talks to Postgres with the service_role key,
-- which bypasses RLS by design. This just ensures nothing is reachable with
-- the public anon key (used only for Supabase Auth login in the browser).
alter table message_events enable row level security;
alter table catalog_versions enable row level security;
