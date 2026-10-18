# 계정별 운영 (PLAN 규칙 1, 2026-10-18)

한 저장소·한 폴더에서 계정마다 따로 실행한다. 한 번의 실행은 계정 하나만 다룬다.

| | openbrain7777 (기존) | sabol.kka |
|---|---|---|
| `ACCOUNT` | 비어 있음 | `sabolkka` |
| 설정 | `config.yaml`, `data/`, `prompts/` | `accounts/sabolkka/config.yaml` (필수), 같은 폴더에 둔 `data/*`·`prompts/*` 만 기존 것 대신 사용 |
| 로컬 비밀값 | `.env` | `.env.sabolkka` + 공용 `.env`의 LLM 키·텔레그램·앱 시크릿 (Threads·Supabase·DRY_RUN 은 읽지 않음) |
| GitHub Secrets | `THREADS_USER_ID` 등 기존 이름 | 같은 이름 + `_SABOLKKA` (`THREADS_USER_ID_SABOLKKA`, `THREADS_ACCESS_TOKEN_SABOLKKA`, `THREADS_TOKEN_ISSUED_AT_SABOLKKA`, `SUPABASE_URL_SABOLKKA`, `SUPABASE_SERVICE_ROLE_KEY_SABOLKKA`) |
| GitHub Variables | `AUTO_PUBLISH_DISABLED`, `DRY_RUN` | `AUTO_PUBLISH_DISABLED_SABOLKKA`, `DRY_RUN_SABOLKKA` |
| Supabase | 기존 프로젝트 | 새 프로젝트 (`db/schema.sql`, `migrate_v3`, `migrate_v4` 실행 후 `accounts/sabolkka/db/*_cron.sql`) |

- 예약 실행은 저장소 변수 `ACCOUNTS_JSON` (예: `["openbrain","sabolkka"]`)에 적힌 계정만 돈다. 없으면 기존 계정만.
- 수동 실행은 `account` 입력으로 계정을 고른다 (기본 openbrain).
- Supabase pg_cron 은 계정마다 따로 깨운다. sabolkka 쪽은 `accounts/sabolkka/db/` 의 SQL 이 `account=sabolkka` 로 깨운다.
- 알림(텔레그램) 앞에는 `[sabolkka]` 가 붙는다. 기존 계정 알림은 그대로다.
- 시작은 `DRY_RUN_SABOLKKA` 를 만들지 않거나 `true` 로 둔다. 실제 발행은 사람이 `false` 로 바꿀 때만.
