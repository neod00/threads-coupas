-- 2026-10-30: 앱 홍보 계정(openbrain, PLAN 1-2) 글 유형 추가 — launch(앱 소개)·feature(기능)·devlog(고친 것)·ask(질문).
-- posts.type 제약을 바꾼다. 실행 방법: Supabase 대시보드 → SQL Editor 에 붙여 넣고 Run. 여러 번 실행해도 된다.
alter table public.posts drop constraint if exists posts_type_check;
alter table public.posts add constraint posts_type_check
  check (type in ('calc', 'compare', 'pick', 'bundle', 'talk', 'tip', 'launch', 'feature', 'devlog', 'ask'));
alter table sabolkka.posts drop constraint if exists posts_type_check;
alter table sabolkka.posts add constraint posts_type_check
  check (type in ('calc', 'compare', 'pick', 'bundle', 'talk', 'tip', 'launch', 'feature', 'devlog', 'ask'));

-- 확인용
select conrelid::regclass, pg_get_constraintdef(oid) from pg_constraint where conname = 'posts_type_check';
