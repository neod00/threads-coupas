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
- **GitHub 깨우기 토큰(Vault `gh_dispatch_token`)을 바꿀 때는 이 한 줄만 실행한다** (파일 전체를 다시 실행하지 않는다):
  `select vault.update_secret((select id from vault.secrets where name = 'gh_dispatch_token'), '새 토큰');`
  토큰은 fine-grained, 저장소 `neod00/threads-coupas` 하나, Actions: Read and write. 만료되면 모든 깨우기가 401 Bad credentials 로 거절된다. 확인: `select status_code from net._http_response order by created desc limit 5;` (204 가 정상)
  `db/publish_cron.sql` 을 자리표시자가 남은 채 실행하면 토큰 모양 검사가 오류로 멈추고 Vault 는 바뀌지 않는다 (2026-10-27 사고 후 추가).
- 발행 예약은 30분마다 그 계정 스키마를 보고 **할 일이 있을 때만** 깨운다 (2026-10-27, Actions 무료 시간 절약). 계정의 `reply_delay_hours` 를 바꾸면 그 계정의 `publish_cron.sql` 답글 대기 시간도 같이 바꾸고 Supabase 에 다시 실행한다 (테스트가 둘이 다르면 실패한다).
- 코드는 `ACCOUNT` 가 있으면 그 이름의 스키마로 접근한다 (`store.py`). 키가 같아서 한 키가 새면 두 계정 데이터가 같이 노출된다.
- 알림(텔레그램) 앞에는 `[sabolkka]` 가 붙는다. 기존 계정 알림은 그대로다.
- 시작은 `DRY_RUN_SABOLKKA` 를 만들지 않거나 `true` 로 둔다. 실제 발행은 사람이 `false` 로 바꿀 때만.

## openbrain_en (영어 앱 홍보 계정, 2026-11-07 준비, 아직 발행 안 함)
openbrain 의 영어판. 같은 앱(PdfMonster)을 영어권에 알린다. `config.yaml` 에 `content: app`, `lang: en`.
- 언어 설정 `lang: en` 이 바꾸는 것: 링크 답글 첫 줄에 "I built" 필수(`disclosure_text: "I built this app."`), 영어 필수 금지어·금지 훅 문구(`settings.REQUIRED_BANNED_WORDS_EN`)를
  대소문자 없이 낱말 단위로 검사, 훅 60자(가드 G7), 본문·답글에 한글이 있으면 막음(가드 G10), 영어 생성 지시문, 영어 검수 프롬프트(`prompts/reviewer_app_en.md`),
  영어 시험지(`tests/review_cases_app_en/cases.json`, 2026-11-07 합격 20/20·10/10), 영어 화면 캡처(`en-US`). 기존 계정은 모두 `ko` 라 바뀐 것이 없다.
- 글감은 `accounts/openbrain_en/data/apps.yaml` (영어 사실·기능, 링크 `https://pdfmonster.xyz/en/`, 한글·HWP 이야기 없음). 캡처는 PdfMonster 영어판과 지어낸 내용의 `sample-en.pdf` 로 찍는다.
  두 쪽짜리 PDF 는 쪽 목록이 열린 채 시작해 휴대폰 너비에서 본문을 덮으므로 캡처 단계에서 `#btnThumbs` 로 닫는다.
- 발행: 하루 최대 2개(링크 글 1개 + 개발 일지), 슬롯 `00:40`·`22:40` KST = 미국 동부 오전. 링크 답글은 바로 단다(`reply_delay_hours` 0).
- 앞으로 앱 데이터 `updates` 를 적을 때는 루트 `data/apps.yaml`(한국어)과 이 계정 파일(영어) 양쪽에 적는다.

### 켜기 전에 사람이 할 일 (순서대로, 자세한 것은 `.claude/skills/threads-new-account/SKILL.md` 2단계·4단계)
1. 영어용 Instagram·Threads 계정을 새로 만들고 영어 프로필(이름·소개·사진)을 채운다. 인사글 1개는 직접 올린다
2. Meta 개발자 앱 → Threads 테스터에 새 계정 추가 → 새 계정에서 초대 수락(시크릿 창) → 액세스 토큰 생성 → `.env.openbrain_en` 의 `THREADS_ACCESS_TOKEN=` 에 붙여 넣기
3. `$env:ACCOUNT="openbrain_en"; python threads_token.py` (사용자 ID·발급일 기록)
4. Supabase SQL Editor 에서 `accounts/openbrain_en/db/schema.sql` 실행 → Exposed schemas 에 `openbrain_en` 추가 → `.env.openbrain_en` 에 기존 `SUPABASE_URL`·`SUPABASE_SERVICE_ROLE_KEY` 복사
5. `python tools/set_github_secrets.py openbrain_en --dry-run` 후 실제 등록, 코드 push, Actions dry_run 두 번 (`account=openbrain_en`)
6. `accounts/openbrain_en/db/{publish,generate,digest}_cron.sql` 실행, `ACCOUNTS_JSON` 에 `openbrain_en` 추가
7. **마지막에 사용자만:** `gh variable set DRY_RUN_OPENBRAIN_EN --body false --repo neod00/threads-coupas`

## 계정별 설정 항목 (config.yaml)
- `tone`: `polite`(기본, 해요체) 또는 `casual`(친근한 반말). 생성 말투는 `accounts/<계정>/prompts/` 의 프롬프트가 정하고, `tone` 은 검수자 R11 기준과 링크 답글 안내문 말투를 고른다.
- `talk_topics`: 참여형(talk) 글 주제 목록. 비우면 기본 목록(겨울 난방 주제).
- `reply_delay_hours`: 링크 답글을 본문 발행 몇 시간 뒤에 달지 (0~12, 기본 0 = 바로). 답글은 발행 예약이 30분마다 확인하다가 때가 되면 발행기를 깨워 단다. sabolkka 는 3.
- 검수자 시험 기록은 계정마다 따로다 (`state/` 또는 `accounts/<계정>/state/`). 검수 프롬프트(tone 포함)가 바뀌면 그 계정은 `ACCOUNT=<계정> python exam.py` 로 다시 시험을 본다.

## 운영 알림 (`ops_advice: true` 인 계정, 일일 요약에 표시)
- **상품 링크 보충 필요:** 소개 글에 쓸 상품 ÷ 하루 링크 글 수 < 10일이면 알림. → Claude 에게 "상품 10개 추가해줘".
- **발행 수 늘려도 돼요:** 첫 발행 7일 뒤 하루 4개(상품 20개 이상), 14일 뒤 하루 6개(상품 30개 이상)로, 최근 7일 보류 20% 이하이고 자동 정지가 없을 때만. → Claude 에게 "발행 수 N개로 올려줘" (슬롯 시각도 같이 늘림).
- 알림만 한다. 설정과 상품은 사람이 승인한 뒤 바꾼다.
