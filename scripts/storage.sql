-- Public Storage bucket for the 1080x1350 PNGs (SPEC §5.1 step 9).
-- Not part of SPEC §4; run once after schema.sql. Name must match SUPABASE_BUCKET.
insert into storage.buckets (id, name, public)
values ('post-images', 'post-images', true)
on conflict (id) do nothing;
