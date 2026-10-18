-- sabolkka 계정용 스키마. 기존 프로젝트(기존 계정과 같은 프로젝트)의 SQL Editor 에서 한 번 실행한다.
-- 테이블은 db/schema.sql 과 같고(최신 posts_type_check 포함) public 이 아니라 sabolkka 스키마에 만든다.
-- 실행 후 Project Settings → API → Exposed schemas 에 sabolkka 를 추가해야 서버 코드가 접근할 수 있다.

create schema if not exists sabolkka;
set search_path to sabolkka;


create table if not exists posts (
  id               uuid primary key,
  type             text not null check (type in ('calc', 'compare', 'pick', 'bundle', 'talk', 'tip')),
  product_ids      text[] not null default '{}',
  text             text not null,
  reply_text       text,
  has_link         boolean not null,
  link_url         text,
  variant          text check (variant in ('link_in_body', 'link_in_reply')),
  topic_tag        text,                     -- PLAN 표에 없음: 발행 시 topic_tag 파라미터용
  calc_payload     jsonb not null,
  hook_candidates  jsonb not null default '[]',
  hook_style       text check (hook_style in ('question', 'result', 'compare')),
  status           text not null check (status in ('draft', 'approved', 'held', 'cancelled', 'published', 'failed')),
  guard_log        jsonb not null default '{}',
  review_result    jsonb,
  review_model     text,
  review_attempts  int not null default 0,
  scheduled_at     timestamptz,
  published_at     timestamptz,
  threads_media_id text unique,              -- 멱등성 키
  threads_reply_id text,
  created_at       timestamptz not null default now()
);
create index if not exists posts_status_scheduled on posts (status, scheduled_at);
create index if not exists posts_created on posts (created_at);

create table if not exists metrics_post (
  threads_media_id    text not null,
  collected_at        timestamptz not null default now(),
  hours_since_publish int not null,
  views   int, likes int, replies int, reposts int, quotes int, shares int,   -- views·shares 는 API 에서 비어 올 수 있어 null 허용
  primary key (threads_media_id, hours_since_publish)
);

create table if not exists metrics_coupang_daily (
  date       date primary key,
  clicks     int not null,
  orders     int not null,
  commission int not null
);

-- 자동 정지 상태, 연속 실패 카운터 같은 운영 상태 (key-value)
create table if not exists system_state (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

-- 자동 정지·해제·취소 같은 운영 기록 (해제 사유 기록용)
create table if not exists events (
  id         bigint generated always as identity primary key,
  kind       text not null,
  detail     jsonb not null default '{}',
  created_at timestamptz not null default now()
);

alter table posts enable row level security;
alter table metrics_post enable row level security;
alter table metrics_coupang_daily enable row level security;
alter table system_state enable row level security;
alter table events enable row level security;

-- service role 만 접근한다. anon·authenticated 에는 아무 권한도 주지 않는다 (RLS 도 켜져 있다).
grant usage on schema sabolkka to service_role;
grant all on all tables in schema sabolkka to service_role;
grant all on all sequences in schema sabolkka to service_role;
alter default privileges in schema sabolkka grant all on tables to service_role;
alter default privileges in schema sabolkka grant all on sequences to service_role;

reset search_path;
