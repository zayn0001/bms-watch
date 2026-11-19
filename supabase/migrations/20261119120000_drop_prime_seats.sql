-- Prime-seat feature removed from BMS Watch.
alter table bms_watch.watches drop column if exists prime_seats_only;
alter table bms_watch.watches drop column if exists min_seats_together;
