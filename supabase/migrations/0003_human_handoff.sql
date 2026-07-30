-- Human handoff: once the owner personally messages a customer directly
-- (through Chat Mitra's own dashboard/app, not through this bot), the bot
-- backs off from that conversation for a configurable window so it
-- doesn't talk over a human agent already handling the order — see
-- app/handoff.py and app/main.py's message.sent webhook handling.

create table if not exists human_takeovers (
  sender text primary key,
  paused_until timestamptz not null,
  created_at timestamptz not null default now()
);

create index if not exists human_takeovers_paused_until_idx on human_takeovers (paused_until);

-- Small key/value config store for settings the owner can change live from
-- the dashboard (Settings page) without a redeploy — currently just the
-- human-handoff pause duration, in hours.
create table if not exists bot_settings (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

insert into bot_settings (key, value)
values ('human_handoff_pause_hours', '24')
on conflict (key) do nothing;

alter table human_takeovers enable row level security;
alter table bot_settings enable row level security;
