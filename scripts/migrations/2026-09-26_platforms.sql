-- Multi-platform posts (publisher layer, phase 1). Non-destructive: only adds columns
-- with defaults, so every existing row reads as a LinkedIn post and nothing is rewritten.
alter table posts add column if not exists platform         text   not null default 'linkedin';
alter table posts add column if not exists targets          text[] not null default array['linkedin']::text[];
alter table posts add column if not exists platform_post_id text;
alter table posts add column if not exists platform_data    jsonb  not null default '{}'::jsonb;

-- Backfill the LinkedIn id of rows already posted (post_url ends with the share URN).
update posts
   set platform_post_id = substring(post_url from 'urn:li:[a-zA-Z]+:[0-9]+')
 where platform_post_id is null and post_url like 'https://www.linkedin.com/feed/update/urn:li:%';
