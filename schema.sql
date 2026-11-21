-- JobSimple schema for Supabase (run in SQL Editor).
-- All tables use the deutschjob_ prefix to stay separate from other projects.

create table if not exists deutschjob_users (
  tg_id         bigint primary key,
  profile       jsonb,                       -- JSON from CV parsing
  cv_path       text,
  location      text,                        -- search city, null = all of Germany
  note          text,                        -- /note: extra context for letters
  auto_on       boolean not null default false,
  daily_limit   int not null default 5 check (daily_limit between 1 and 30),
  min_score     int not null default 4,
  consent_at    timestamptz,                 -- GDPR consent for auto mode
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Job details, shared between users (not personal data).
create table if not exists deutschjob_jobs (
  refnr       text primary key,
  data        jsonb not null,
  fetched_at  timestamptz not null default now()
);

-- Search results cache: key = "<location>|<sorted titles>"
create table if not exists deutschjob_search_cache (
  k    text primary key,
  data jsonb not null,
  ts   timestamptz not null default now()
);

create table if not exists deutschjob_applications (
  id           bigint generated always as identity primary key,
  tg_id        bigint not null references deutschjob_users(tg_id) on delete cascade,
  refnr        text not null,
  employer     text,
  employer_key text,                         -- normalized employer name (30-day rule)
  title        text,
  to_email     text,
  letter       jsonb,                        -- {subject, body}
  mode         text not null default 'manual' check (mode in ('manual','auto')),
  status       text not null check (status in
               ('draft','queued','sending','sent','failed','cancelled','skipped','applied_manual')),
  send_after   timestamptz,
  sent_at      timestamptz,
  error        text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (tg_id, refnr)                      -- never apply twice to the same job
);

create index if not exists deutschjob_apps_due_idx
  on deutschjob_applications (status, send_after);
create index if not exists deutschjob_apps_employer_idx
  on deutschjob_applications (tg_id, employer_key, created_at);
create index if not exists deutschjob_cache_ts_idx
  on deutschjob_search_cache (ts);

-- Atomically claim due emails (queued -> sending). Safe even with several workers.
-- Rows stuck in 'sending' for 15+ minutes (crash) are marked failed, never re-sent.
create or replace function deutschjob_claim_due(batch int default 2)
returns setof deutschjob_applications
language plpgsql
as $$
begin
  update deutschjob_applications
     set status = 'failed', error = 'stuck in sending', updated_at = now()
   where status = 'sending' and updated_at < now() - interval '15 minutes';

  return query
  update deutschjob_applications a
     set status = 'sending', updated_at = now()
   where a.id in (
     select id from deutschjob_applications
      where status = 'queued' and send_after <= now()
      order by send_after
      limit batch
      for update skip locked)
  returning a.*;
end;
$$;

-- Lock everything down: the bot uses the service_role key (bypasses RLS).
-- With RLS on and no policies, the public anon key can read nothing.
alter table deutschjob_users         enable row level security;
alter table deutschjob_jobs          enable row level security;
alter table deutschjob_search_cache  enable row level security;
alter table deutschjob_applications  enable row level security;

revoke all on function deutschjob_claim_due(int) from public, anon, authenticated;

-- Optional cleanup (run periodically or with pg_cron):
-- delete from deutschjob_search_cache where ts < now() - interval '1 day';
-- delete from deutschjob_jobs where fetched_at < now() - interval '30 days'
--   and refnr not in (select refnr from deutschjob_applications);

-- 1. Sender columns used by db.set_sender / clear_sender
alter table deutschjob_users
  add column if not exists smtp_host          text,
  add column if not exists smtp_port          int,
  add column if not exists smtp_user          text,
  add column if not exists smtp_pass_enc      text,
  add column if not exists from_name          text,
  add column if not exists sender_verified_at timestamptz;

-- 2. Stats view used by /stats, /admin and the daily digest
drop view if exists deutschjob_client_stats;
create view deutschjob_client_stats as
select
  u.tg_id,
  u.profile->>'full_name'  as name,
  u.smtp_user              as sender,
  u.auto_on,
  count(*) filter (where a.status = 'sent'
        and a.sent_at >= date_trunc('day', now() at time zone 'Europe/Berlin')
                         at time zone 'Europe/Berlin')   as sent_today,
  count(*) filter (where a.status = 'sent')              as sent_total,
  count(*) filter (where a.status = 'queued')            as queued,
  count(*) filter (where a.status = 'failed')            as failed,
  count(*) filter (where a.status = 'applied_manual')    as web_applied,
  max(a.sent_at)                                         as last_sent
from deutschjob_users u
left join deutschjob_applications a on a.tg_id = u.tg_id
group by u.tg_id;

alter view deutschjob_client_stats set (security_invoker = on);
revoke all on deutschjob_client_stats from anon, authenticated;

-- 3. Refresh PostgREST's schema cache
notify pgrst, 'reload schema';

alter table deutschjob_applications add column if not exists sender_email text;
notify pgrst, 'reload schema';

-- 4. Daily limit up to 30 (matches MAX_DAILY in handlers.py)
alter table deutschjob_users drop constraint if exists deutschjob_users_daily_limit_check;
alter table deutschjob_users add constraint deutschjob_users_daily_limit_check
  check (daily_limit between 1 and 30);
-- 5. Reply tracking (IMAP): opt-in per user, outcome per application, processed-message log
alter table deutschjob_users
  add column if not exists track_replies   boolean not null default false,
  add column if not exists imap_host       text,          -- null = derived from smtp_host
  add column if not exists imap_checked_at timestamptz,
  add column if not exists imap_error      text;

alter table deutschjob_applications
  add column if not exists message_id    text,            -- Message-ID of the sent mail
  add column if not exists outcome       text check (outcome in
                 ('acknowledged','interview','rejected','other','bounced')),
  add column if not exists reply_at      timestamptz,
  add column if not exists reply_summary text;

create table if not exists deutschjob_replies (
  tg_id       bigint not null references deutschjob_users(tg_id) on delete cascade,
  message_id  text not null,
  app_id      bigint references deutschjob_applications(id) on delete cascade,
  category    text not null,
  received_at timestamptz,
  created_at  timestamptz not null default now(),
  primary key (tg_id, message_id)
);

-- Addresses that bounced: auto mode never sends to them again
create table if not exists deutschjob_bad_emails (
  email      text primary key,
  reason     text,
  created_at timestamptz not null default now()
);

alter table deutschjob_replies    enable row level security;
alter table deutschjob_bad_emails enable row level security;

-- Stats view with reply outcomes
drop view if exists deutschjob_client_stats;
create view deutschjob_client_stats as
select
  u.tg_id,
  u.profile->>'full_name'  as name,
  u.smtp_user              as sender,
  u.auto_on,
  count(*) filter (where a.status = 'sent'
        and a.sent_at >= date_trunc('day', now() at time zone 'Europe/Berlin')
                         at time zone 'Europe/Berlin')   as sent_today,
  count(*) filter (where a.status = 'sent')              as sent_total,
  count(*) filter (where a.status = 'queued')            as queued,
  count(*) filter (where a.status = 'failed')            as failed,
  count(*) filter (where a.status = 'applied_manual')    as web_applied,
  count(*) filter (where a.outcome = 'interview')        as interviews,
  count(*) filter (where a.outcome = 'rejected')         as rejections,
  count(*) filter (where a.outcome = 'bounced')          as bounced,
  max(a.sent_at)                                         as last_sent
from deutschjob_users u
left join deutschjob_applications a on a.tg_id = u.tg_id
group by u.tg_id;

alter view deutschjob_client_stats set (security_invoker = on);
revoke all on deutschjob_client_stats from anon, authenticated;

notify pgrst, 'reload schema';

-- 6. Extra attachments (certificates, references) sent with every application: [{id,name,key,size,mime}]
alter table deutschjob_users
  add column if not exists attachments jsonb not null default '[]'::jsonb;
notify pgrst, 'reload schema';
