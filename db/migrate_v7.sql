-- 2026-11-18: 선물지니 글 유형(gift, 2026-11-14 PLAN 1-4) 추가. 코드에만 넣고 DB 제약을 안 바꿔서
-- 11-17 sabol.kka 생성이 posts_type_check 위반으로 멈췄다 (11-18 글 0개).
-- posts.type 제약을 바꾼다. 실행 방법: Supabase 대시보드 → SQL Editor 에 붙여 넣고 Run. 여러 번 실행해도 된다.
alter table public.posts drop constraint if exists posts_type_check;
alter table public.posts add constraint posts_type_check
  check (type in ('calc', 'compare', 'pick', 'bundle', 'talk', 'tip', 'gift', 'launch', 'feature', 'devlog', 'ask'));
alter table sabolkka.posts drop constraint if exists posts_type_check;
alter table sabolkka.posts add constraint posts_type_check
  check (type in ('calc', 'compare', 'pick', 'bundle', 'talk', 'tip', 'gift', 'launch', 'feature', 'devlog', 'ask'));
alter table openbrain_en.posts drop constraint if exists posts_type_check;
alter table openbrain_en.posts add constraint posts_type_check
  check (type in ('calc', 'compare', 'pick', 'bundle', 'talk', 'tip', 'gift', 'launch', 'feature', 'devlog', 'ask'));

-- 확인용: 세 줄 모두 'gift' 가 들어 있어야 한다
select conrelid::regclass, pg_get_constraintdef(oid) from pg_constraint where conname = 'posts_type_check';
