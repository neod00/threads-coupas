-- 발행 깨우기 예약 (Supabase pg_cron → GitHub publish.yml 수동 실행)
-- GitHub 예약 실행(schedule)이 4~5시간씩 밀려서, Supabase 가 30분마다(매시 12분·42분, UTC 기준이지만 분은 같다)
-- 할 일이 있는지 DB 를 보고, 있을 때만 publish.yml 을 workflow_dispatch 로 실행시킨다.
--
-- 안전장치는 그대로다: dry_run=false 로 실행해도 저장소 변수 DRY_RUN 이 정확히 'false' 일 때만 실제 발행하고,
-- AUTO_PUBLISH_DISABLED=true 면 작업이 아예 돌지 않는다 (publish.yml).
--
-- 실행 방법: Supabase 대시보드 → SQL Editor 에 붙여 넣고 Run.
-- __GH_DISPATCH_TOKEN__ 자리에 토큰이 들어간 사본(.tmp/publish_cron_with_token.sql)을 쓴다. 저장소에는 토큰을 올리지 않는다.
-- 토큰: GitHub fine-grained token, 저장소 neod00/threads-coupas 하나, 권한 Actions: Read and write 만.

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

-- 토큰은 Vault 에 암호화해 둔다. 다시 실행하면 값만 바꾼다.
do $$
declare
  sid uuid;
begin
  select id into sid from vault.secrets where name = 'gh_dispatch_token';
  if sid is null then
    perform vault.create_secret('__GH_DISPATCH_TOKEN__', 'gh_dispatch_token', 'threads-coupas publish.yml dispatch');
  else
    perform vault.update_secret(sid, '__GH_DISPATCH_TOKEN__');
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
