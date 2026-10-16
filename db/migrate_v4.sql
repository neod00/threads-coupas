-- 2026-10-16: 시즌 모음(bundle)·참여형(talk) 글 유형 추가. posts.type 제약을 바꾼다.
-- 실행 방법: Supabase 대시보드 → SQL Editor 에 붙여 넣고 Run. 여러 번 실행해도 된다.
alter table posts drop constraint if exists posts_type_check;
alter table posts add constraint posts_type_check check (type in ('calc', 'compare', 'pick', 'bundle', 'talk', 'tip'));

-- 확인용
select conname, pg_get_constraintdef(oid) from pg_constraint where conname = 'posts_type_check';
