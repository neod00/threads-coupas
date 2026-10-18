# 계정별 운영 (PLAN 규칙 1, 2026-10-18)

한 저장소·한 폴더에서 계정마다 따로 실행한다. 한 번의 실행은 계정 하나만 다룬다.

| | openbrain7777 (기존) | sabol.kka |
|---|---|---|
| `ACCOUNT` | 비어 있음 | `sabolkka` |
| 설정 | `config.yaml`, `data/`, `prompts/` | `accounts/sabolkka/config.yaml` (필수), 같은 폴더에 둔 `data/*`·`prompts/*` 만 기존 것 대신 사용 |
| 로컬 비밀값 | `.env` | `.env.sabolkka` + 공용 `.env`의 LLM 키·텔레그램·앱 시크릿 (Threads·Supabase·DRY_RUN 은 읽지 않음) |
| GitHub Secrets | `THREADS_USER_ID` 등 기존 이름 | 같은 이름 + `_SABOLKKA` (`THREADS_USER_ID_SABOLKKA`, `THREADS_ACCESS_TOKEN_SABOLKKA`, `THREADS_TOKEN_ISSUED_AT_SABOLKKA`, `SUPABASE_URL_SABOLKKA`, `SUPABASE_SERVICE_ROLE_KEY_SABOLKKA` — 값은 기존 계정과 같다) |
| GitHub Variables | `AUTO_PUBLISH_DISABLED`, `DRY_RUN` | `AUTO_PUBLISH_DISABLED_SABOLKKA`, `DRY_RUN_SABOLKKA` |
| Supabase | 기존 프로젝트의 `public` 스키마 | **같은 프로젝트**의 `sabolkka` 스키마 (`accounts/sabolkka/db/schema.sql` 실행 → Exposed schemas 에 `sabolkka` 추가 → `*_cron.sql`). URL·키는 기존과 같은 값 |

- 예약 실행은 저장소 변수 `ACCOUNTS_JSON` (예: `["openbrain","sabolkka"]`)에 적힌 계정만 돈다. 없으면 기존 계정만.
- 수동 실행은 `account` 입력으로 계정을 고른다 (기본 openbrain).
- Supabase pg_cron 은 계정마다 따로 깨운다. sabolkka 쪽은 `accounts/sabolkka/db/` 의 SQL(예약 이름 `threads-coupas-sabolkka-*`)이 `account=sabolkka` 로 깨운다. GitHub 토큰(Vault)은 기존 `db/publish_cron.sql` 이 넣어 둔 것을 같이 쓴다.
- 코드는 `ACCOUNT` 가 있으면 그 이름의 스키마로 접근한다 (`store.py`). 키가 같아서 한 키가 새면 두 계정 데이터가 같이 노출된다.
- 알림(텔레그램) 앞에는 `[sabolkka]` 가 붙는다. 기존 계정 알림은 그대로다.
- 시작은 `DRY_RUN_SABOLKKA` 를 만들지 않거나 `true` 로 둔다. 실제 발행은 사람이 `false` 로 바꿀 때만.

## 계정별 설정 항목 (config.yaml)
- `tone`: `polite`(기본, 해요체) 또는 `casual`(친근한 반말). 생성 말투는 `accounts/<계정>/prompts/` 의 프롬프트가 정하고, `tone` 은 검수자 R11 기준과 링크 답글 안내문 말투를 고른다.
- `talk_topics`: 참여형(talk) 글 주제 목록. 비우면 기본 목록(겨울 난방 주제).
- `reply_delay_hours`: 링크 답글을 본문 발행 몇 시간 뒤에 달지 (0~12, 기본 0 = 바로). 답글은 발행기가 30분마다 확인하다가 때가 되면 단다. sabolkka 는 3.
- 검수자 시험 기록은 계정마다 따로다 (`state/` 또는 `accounts/<계정>/state/`). 검수 프롬프트(tone 포함)가 바뀌면 그 계정은 `ACCOUNT=<계정> python exam.py` 로 다시 시험을 본다.
