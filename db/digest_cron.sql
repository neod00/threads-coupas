-- 일일 요약 깨우기 예약 (Supabase pg_cron → GitHub digest.yml 수동 실행)
-- GitHub 예약 실행이 몇 시간씩 밀려서, Supabase 가 매일 12:00 UTC(21:00 KST)에 digest.yml 을 직접 실행시킨다.
-- 요약이 두 번 가지 않도록 digest.yml 의 GitHub 예약(schedule)은 없앴다. config.yaml 의 digest_time_kst 를 바꾸면 아래 cron 도 같이 바꾼다 (UTC).
-- 토큰은 db/publish_cron.sql 을 실행할 때 Vault 에 저장한 gh_dispatch_token 을 그대로 쓴다. 이 파일에는 토큰이 없다.
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
  'threads-coupas-digest',
  '0 12 * * *',
  $job$
  select net.http_post(
    url := 'https://api.github.com/repos/neod00/threads-coupas/actions/workflows/digest.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'gh_dispatch_token'),
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'threads-coupas-cron',
      'Content-Type', 'application/json'
    ),
    body := '{"ref": "main", "inputs": {"dry_run": "false"}}'::jsonb
  );
  $job$
);

-- 확인용: 예약 목록
select jobid, jobname, schedule, active from cron.job where jobname like 'threads-coupas-%' order by jobname;

-- 끄려면: select cron.unschedule('threads-coupas-digest');
