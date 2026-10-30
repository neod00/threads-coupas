-- 2026-10-30: 게시물 이미지·영상 (규칙 2 개정, media.py). posts 에 media 열(data/media/ 파일 이름 목록)을 더한다.
-- 실행 방법: Supabase 대시보드 → SQL Editor 에 붙여 넣고 Run. 여러 번 실행해도 된다.
-- 열이 없어도 기존 글(이미지·영상 없음)은 그대로 발행된다. 이미지·영상 글을 저장하기 전에만 실행하면 된다.
alter table public.posts add column if not exists media text[] not null default '{}';
alter table sabolkka.posts add column if not exists media text[] not null default '{}';

-- 확인용
select table_schema, column_name, data_type from information_schema.columns
where table_name = 'posts' and column_name = 'media';
