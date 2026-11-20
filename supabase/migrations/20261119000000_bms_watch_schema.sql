-- Dedicated schema for BMS Watch in the shared Supabase project (ismyhkcsxrswqsfzfrxe)
-- Keeps tables isolated from public and evenly schemas.
-- In Supabase Dashboard: Go to Project Settings > API > PostgREST > Exposed Schemas and ensure `bms_watch` is added.

create schema if not exists bms_watch;
grant usage on schema bms_watch to authenticated, service_role;

-- 1. Watches Table
create table if not exists bms_watch.watches (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    movie_title text not null,
    event_code text not null,
    movie_slug text not null,
    region_code text not null,
    region_slug text not null,
    language text default 'english',
    dates jsonb not null default '[]'::jsonb,
    cinemas jsonb default '[]'::jsonb,
    formats jsonb default '[]'::jsonb,
    prime_seats_only boolean default false,
    min_seats_together integer default 1,
    email text not null,
    is_active boolean default true,
    created_at timestamptz default now(),
    last_checked_at timestamptz,
    last_alert_at timestamptz
);

-- 2. State Cache Table (For deduplicated polling & diffs)
create table if not exists bms_watch.state_cache (
    key text primary key, -- event_code:region_code:date
    data jsonb not null,
    updated_at timestamptz default now()
);

-- 3. Alert Logs Table
create table if not exists bms_watch.alert_logs (
    id uuid primary key default gen_random_uuid(),
    watch_id uuid references bms_watch.watches(id) on delete cascade,
    user_id uuid references auth.users(id) on delete cascade,
    title text not null,
    details text,
    sent_to text not null,
    created_at timestamptz default now()
);

-- Enable Row Level Security (RLS)
alter table bms_watch.watches enable row level security;
alter table bms_watch.alert_logs enable row level security;

-- Policies for bms_watch.watches: Users can only see and manage their own watches
drop policy if exists "Users can view own watches" on bms_watch.watches;
create policy "Users can view own watches" on bms_watch.watches
    for select to authenticated using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own watches" on bms_watch.watches;
create policy "Users can create own watches" on bms_watch.watches
    for insert to authenticated with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update own watches" on bms_watch.watches;
create policy "Users can update own watches" on bms_watch.watches
    for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own watches" on bms_watch.watches;
create policy "Users can delete own watches" on bms_watch.watches
    for delete to authenticated using ((select auth.uid()) = user_id);

-- Policies for bms_watch.alert_logs
drop policy if exists "Users can view own alert logs" on bms_watch.alert_logs;
create policy "Users can view own alert logs" on bms_watch.alert_logs
    for select to authenticated using ((select auth.uid()) = user_id);

-- Permissions for authenticated users & service role
grant select, insert, update, delete on bms_watch.watches, bms_watch.alert_logs to authenticated;
grant all on bms_watch.watches, bms_watch.alert_logs, bms_watch.state_cache to service_role;
