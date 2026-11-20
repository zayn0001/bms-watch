-- Restore prime_seats_only and min_seats_together columns on watches table
alter table bms_watch.watches add column if not exists prime_seats_only boolean default false;
alter table bms_watch.watches add column if not exists min_seats_together integer default 1;
