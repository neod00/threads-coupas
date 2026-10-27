-- sabolkka 계정용. 기존 계정과 같은 Supabase 프로젝트의 SQL Editor 에서 실행한다. 예약 이름이 sabolkka 전용이고 account=sabolkka 로 깨운다.
-- 발행 깨우기 예약 (Supabase pg_cron → GitHub publish.yml 수동 실행, 매시 12분·42분에 할 일이 있을 때만)
-- 토큰은 db/publish_cron.sql 을 실행할 때 Vault 에 저장한 gh_dispatch_token 을 그대로 쓴다. 이 파일에는 토큰이 없다.
-- 실제 발행 여부는 저장소 변수 DRY_RUN_SABOLKKA·AUTO_PUBLISH_DISABLED_SABOLKKA 가 정한다.
--
-- 실행 방법: Supabase 대시보드 → SQL Editor 에 이 파일 전체를 붙여 넣고 Run.

do $$
begin
  if not exists (select 1 from vault.secrets where name = 'gh_dispatch_token') then
    raise exception 'Vault 에 gh_dispatch_token 이 없습니다. 먼저 db/publish_cron.sql 을 실행하세요.';
  end if;
end $$;

-- 같은 이름으로 다시 실행하면 예약이 덮어써진다.
select cron.schedule(
  'threads-coupas-sabolkka-publish',
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
    body := '{"ref": "main", "inputs": {"account": "sabolkka", "dry_run": "false"}}'::jsonb
  )
  -- 할 일이 있을 때만 깨운다 (2026-10-27: 30분마다 무조건 깨우다 GitHub Actions 무료 시간을 다 써서 이틀 멈춤).
  -- 1) 예약 시각이 지난 approved 글  2) 때가 된 링크 답글(답글만 실패한 글 포함)
  -- 자동 정지 중이면 깨우지 않는다. 답글 감시는 publish.yml 의 4시간 예비 실행이 맡는다.
  where (
      exists (select 1 from sabolkka.posts where status = 'approved' and scheduled_at <= now())
      or exists (select 1 from sabolkka.posts
                 where status = 'published' and variant = 'link_in_reply'
                   and threads_media_id is not null and threads_reply_id is null
                   and published_at > now() - interval '1 day'   -- 하루 넘게 못 단 답글로는 깨우지 않는다 (계속 실패하면 무료 시간만 씀)
                   and published_at <= now() - interval '2 hours 45 minutes'   -- reply_delay_hours 3시간 - 15분 여유 (publisher.REPLY_DUE_GRACE)
                 )
    )
    and not coalesce((select (value->>'active')::boolean from sabolkka.system_state where key = 'auto_stop'), false);
  $job$
);

-- 확인용: 예약 목록
select jobid, jobname, schedule, active from cron.job where jobname like 'threads-coupas-%' order by jobname;

-- 끄려면: select cron.unschedule('threads-coupas-sabolkka-publish');
