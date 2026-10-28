-- 발행 깨우기 예약 (Supabase pg_cron → GitHub publish.yml 수동 실행)
-- GitHub 예약 실행(schedule)이 4~5시간씩 밀려서, Supabase 가 30분마다(매시 12분·42분, UTC 기준이지만 분은 같다)
-- 할 일이 있는지 DB 를 보고, 있을 때만 publish.yml 을 workflow_dispatch 로 실행시킨다.
--
-- 안전장치는 그대로다: dry_run=false 로 실행해도 저장소 변수 DRY_RUN 이 정확히 'false' 일 때만 실제 발행하고,
-- AUTO_PUBLISH_DISABLED=true 면 작업이 아예 돌지 않는다 (publish.yml).
--
-- 실행 방법: Supabase 대시보드 → SQL Editor 에 붙여 넣고 Run.
-- 아래 do 블록의 토큰 자리표시자(대문자 밑줄 이름)를 진짜 토큰으로 바꾼 사본(.tmp/publish_cron_with_token.sql)을 쓴다. 저장소에는 토큰을 올리지 않는다.
-- 토큰: GitHub fine-grained token, 저장소 neod00/threads-coupas 하나, 권한 Actions: Read and write 만.
-- 자리표시자가 그대로 남은 원본을 실행하면 아래 검사가 오류로 멈춘다 (2026-10-27: 원본을 실행해 Vault 의 토큰이 자리표시자 글자로
-- 덮어써지고, 이후 모든 깨우기가 401 Bad credentials 로 거절돼 이틀 가까이 발행이 멈췄다).
-- 토큰만 바꾸려면 파일 전체를 다시 실행하지 말고 이 한 줄만 실행한다:
--   select vault.update_secret((select id from vault.secrets where name = 'gh_dispatch_token'), '새 토큰');

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

-- 토큰은 Vault 에 암호화해 둔다. 다시 실행하면 값만 바꾼다.
do $$
declare
  sid uuid;
  new_token text := '__GH_DISPATCH_TOKEN__';
begin
  -- 진짜 GitHub 토큰(fine-grained github_pat_… 또는 classic ghp_…)이 아니면 Vault 를 건드리지 않고 멈춘다.
  if new_token !~ '^(github_pat_|ghp_)[A-Za-z0-9_]{20,}$' then
    raise exception 'gh_dispatch_token 이 진짜 GitHub 토큰이 아닙니다. 자리표시자를 토큰으로 바꾼 사본(.tmp/publish_cron_with_token.sql)을 실행하세요. Vault 는 바꾸지 않았습니다.';
  end if;
  select id into sid from vault.secrets where name = 'gh_dispatch_token';
  if sid is null then
    perform vault.create_secret(new_token, 'gh_dispatch_token', 'threads-coupas publish.yml dispatch');
  else
    perform vault.update_secret(sid, new_token);
  end if;
end $$;

-- 같은 이름으로 다시 실행하면 예약이 덮어써진다.
select cron.schedule(
  'threads-coupas-publish',
  '12,42 * * * *',
  $job$
  select net.http_post(
    url := 'https://api.github.com/repos/neod00/threads-coupas/actions/workflows/publish.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'gh_dispatch_token'),
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'threads-coupas-cron',
      'Content-Type', 'application/json'
    ),
    body := '{"ref": "main", "inputs": {"dry_run": "false"}}'::jsonb
  )
  -- 할 일이 있을 때만 깨운다 (2026-10-27: 30분마다 무조건 깨우다 GitHub Actions 무료 시간을 다 써서 이틀 멈춤).
  -- 1) 예약 시각이 지난 approved 글  2) 때가 된 링크 답글(답글만 실패한 글 포함)
  -- 자동 정지 중이면 깨우지 않는다. 답글 감시는 publish.yml 의 4시간 예비 실행이 맡는다.
  where (
      exists (select 1 from public.posts where status = 'approved' and scheduled_at <= now())
      or exists (select 1 from public.posts
                 where status = 'published' and variant = 'link_in_reply'
                   and threads_media_id is not null and threads_reply_id is null
                   and published_at > now() - interval '1 day')   -- 답글 지연 없음 (reply_delay_hours 0). 하루 넘게 못 단 답글로는 깨우지 않는다
    )
    and not coalesce((select (value->>'active')::boolean from public.system_state where key = 'auto_stop'), false);
  $job$
);

-- 확인용: 예약 목록
select jobid, jobname, schedule, active from cron.job where jobname = 'threads-coupas-publish';

-- 끄려면: select cron.unschedule('threads-coupas-publish');
