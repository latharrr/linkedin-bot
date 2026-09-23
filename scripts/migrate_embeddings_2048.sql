-- ONE-TIME migration for databases created with the old 1536-dim schema
-- (OpenAI text-embedding-3-small). Fresh installs: just run schema.sql.
--
-- Vectors from different models are not comparable, and a 1536-dim column
-- cannot hold 2048-dim vectors, so every stored embedding is cleared here and
-- rebuilt afterwards by:  python scripts/reembed_corpus.py
-- Rows, text, engagement and post history are untouched.

begin;

-- Old RPCs take vector(1536) arguments; drop them (typmods are not part of the signature).
drop function if exists match_past_posts(vector, int);
drop function if exists find_similar_topic(vector, float);

alter table past_posts alter column embedding       type vector(2048) using null;
alter table posts      alter column topic_embedding type vector(2048) using null;

create or replace function match_past_posts(q vector(2048), k int default 3)
returns setof past_posts
language sql stable
as $$
  select * from past_posts
  where embedding is not null
  order by (embedding <=> q) - least(0.1 * log(1 + engagement), 0.1)
  limit k;
$$;

create or replace function find_similar_topic(q vector(2048), threshold float default 0.85)
returns table (id uuid, topic text, similarity float)
language sql stable
as $$
  select p.id, p.topic, 1 - (p.topic_embedding <=> q) as similarity
  from posts p
  where p.topic_embedding is not null
    and p.status = 'posted'
    and p.posted_at > now() - interval '30 days'
    and 1 - (p.topic_embedding <=> q) > threshold
  order by p.topic_embedding <=> q
  limit 1;
$$;

commit;
