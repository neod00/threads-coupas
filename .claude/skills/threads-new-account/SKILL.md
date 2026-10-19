---
name: threads-new-account
description: 새 Threads 계정(예: sabol.kka 같은 두 번째·세 번째 계정)을 이 저장소에 추가해 자동 발행 직전까지 준비한다. 계정 폴더·SQL 생성, GitHub Secrets 등록, 검수자 시험, 생성·발행 dry_run까지. "새 계정 추가", "계정 이름 줄게", "threads 계정 만들었어" 같은 요청에 쓴다. 실제 발행 스위치(DRY_RUN_<계정>)는 사람만 켠다.
---

# 새 Threads 계정 추가

기준: 2026-10-18 sabol.kka 를 실제로 올린 과정. 계정 1개 = 이 저장소의 `accounts/<계정>/` 폴더 + `.env.<계정>` + GitHub Secrets/변수에 `_<계정 대문자>` 접미사 + 기존 Supabase 프로젝트의 `<계정>` 스키마. 자세한 구조는 `ACCOUNTS.md`.

## 하지 않는 것 (사람이 직접)
- Instagram/Threads **계정 만들기**, 휴대폰 인증, 프로필(이름·소개·사진) 채우기
- Meta 개발자 앱에서 **테스터 초대 → 계정에서 수락 → 액세스 토큰 생성**
- Supabase 대시보드에서 SQL 실행, Exposed schemas 에 계정 추가
- **`DRY_RUN_<계정>=false`** (실제 발행 스위치, 항상 마지막, 사용자만. 이 스킬이 대신 켜지 않는다)
- 토큰·키를 채팅에 붙여 넣지 않는다 (`.env.<계정>`에만 넣고 값은 출력하지 않는다)

## 0. 먼저 정할 것 (사용자에게 물어본다)
1. **계정 이름**(소문자 영문, 예 `kitchenpick`)과 Threads 아이디
2. **컨셉·니치.** 기존 계정과 **겹치지 않는 다른 니치**로 한다 (같은 상품·주제 금지: Meta 가 같은 패턴의 자동 계정을 묶어 보고, 쿠팡 파트너스는 스팸성 홍보를 제재한다)
3. 말투: `casual`(반말, "사라 vs 마라" 틀) 또는 `polite`(해요체)
4. **검증 우선:** 기존 계정(sabol.kka)의 틀이 2주 안에 반응(답글·클릭·수수료)을 낸 뒤에 늘린다. 아직 검증 전이면 계정을 늘리는 대신 틀·상품을 고치자고 권한다.

## 1. 폴더 만들기
```powershell
python tools/new_account.py <계정> --tone casual
```
만들어지는 것: `accounts/<계정>/{config.yaml, prompts/, data/products.csv(머리글만), db/*.sql}`, `.env.<계정>`. 아무것도 켜지 않는다.

## 2. 사람이 할 일 안내 (순서대로)
1. 새 Instagram 계정 → Threads 가입, 공개 계정, 프로필 채우기, 인사글 1개 직접 올리기
2. Meta 개발자 앱 → Threads API 설정 → **Threads 테스터 추가**에 새 계정 → 계정 쪽 웹사이트 권한에서 초대 수락 (**시크릿 창**에서 새 계정으로 로그인)
3. 같은 화면의 사용자 토큰 생성기에서 새 계정 줄의 **액세스 토큰 생성하기** → `.env.<계정>`의 `THREADS_ACCESS_TOKEN=` 에 붙여 넣기 (팝업이 다른 계정 이름이면 취소하고 시크릿 창으로)
4. 토큰 교환·ID 확인: `$env:ACCOUNT="<계정>"; python threads_token.py` → 사용자 ID·발급일 자동 기록. 대시보드 토큰은 이미 장기(60일)라 "교환 거절" 메시지가 나와도 정상
5. Supabase SQL Editor 에 `accounts/<계정>/db/schema.sql` 실행 → **Project Settings → API → Exposed schemas 에 `<계정>` 추가**
6. `.env.<계정>`에 기존 `.env`의 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` 복사 (같은 프로젝트라 같은 값)

## 3. 에이전트가 할 일
1. **`config.yaml` 채우기:** `talk_topics` 에 이 니치의 "사라 vs 마라" 주제 20~30개 (60자 이하, 상품명·브랜드 없이 물건 종류만, 금지어 `가격·할인·안전·최고·1위` 등 제외). `test_start_date` 는 첫 발행일이 정해지면 기록.
2. **상품 선정·링크 생성**(사용자 Chrome 에 쿠팡 파트너스 로그인 필요, `chrome-browser` 스킬 먼저 읽기):
   - 기준: 의견이 갈리는 종류, 대표 상품(후기 많음), 5만~50만 원대, 건강·식품·유아용 제외, 이미 다른 계정이 쓴 상품 제외. 저가(3만 원 미만)는 수수료가 작아 제외
   - 방법: `partners.coupang.com/#affiliate/ws/link/0/<검색어>` → 상품 선택 → `.btn-generate-link` 클릭 → 페이지의 `https://link.coupang.com/a/...` 추출. **긴 반복문은 비동기로 시작해 결과를 폴링한다** (45초 JS 시간 제한). 겹쳐 실행하면 결과가 섞인다
   - 등록 전 **상품명·가격대·링크 목록을 사용자에게 보여 주고 승인**받는다. `note` 는 상품명·상세 페이지의 사실만 (후기·효능·순위 금지, 120자)
   - 최소 12개로 시작, 하루 링크 글 2개 기준 10일 분량(20개)을 목표
3. **연결 확인:** `$env:ACCOUNT="<계정>"; python settings.py` (시크릿 비어 있음 항목 없음) → `python check_connections.py` (5개 테이블 OK)
4. **GitHub Secrets 등록:** `python tools/set_github_secrets.py <계정>` (값은 출력하지 않음. 먼저 `--dry-run`)
5. **검수자 시험**(계정별 합격 기록 필요): `$env:ACCOUNT="<계정>"; python exam.py` → 합격까지 (`--help` 같은 옵션 없음. 아무 인자도 시험을 시작한다)
6. **로컬 생성 연습:** `$env:AUTO_PUBLISH_DISABLED="false"; $env:ACCOUNT="<계정>"; python pipeline.py generate --dry-run` → 글 3개 승인·말투·링크 확인
7. **push 후 Actions dry_run:**
   ```powershell
   gh workflow run publish.yml  --repo neod00/threads-coupas -f account=<계정> -f dry_run=true
   gh workflow run generate.yml --repo neod00/threads-coupas -f account=<계정> -f dry_run=true
   ```
   (코드 push 는 사용자 요청이 있을 때만)

## 4. 켜기 (사용자와 함께, 이 순서)
1. 내일 글 저장: `gh workflow run generate.yml ... -f account=<계정> -f dry_run=false` → 로그에서 `→ approved` 확인, 저장된 글을 읽어서 사용자에게 보여 준다
2. Supabase SQL Editor 에 `accounts/<계정>/db/{publish,generate,digest}_cron.sql` 하나씩 실행 (기존 계정이 Vault 에 넣어 둔 GitHub 토큰을 같이 씀)
3. `ACCOUNTS_JSON` 변수에 계정 추가: `'["openbrain","sabolkka","<계정>"]' | gh variable set ACCOUNTS_JSON --repo neod00/threads-coupas`
4. **사용자가 직접:** `gh variable set DRY_RUN_<계정 대문자> --body false --repo neod00/threads-coupas`
5. 비상 정지: `gh variable set AUTO_PUBLISH_DISABLED_<계정 대문자> --body true --repo ...`

켠 뒤 첫날 확인: 본문에 링크·광고 문구 없음, 3시간 뒤 첫 답글에 광고 문구+링크, 텔레그램 `[<계정>]` 알림, 일일 요약.

## 함정 (겪은 것)
- **GitHub 표현식에서 빈 문자열은 거짓이다.** `cond && '' || x` 는 cond 가 참이어도 x 가 된다. 기존 계정 값을 `''` 로 만들 때는 `account != 'openbrain' && x || ''` (테스트가 `&& '' ||` 를 막는다).
- 워크플로를 바꾼 뒤에는 push 직후 **기존 계정**(`account=openbrain`) dry_run 부터 돌려 확인한다. 예약 실행이 바로 깨지기 때문.
- 공용 `.env` 의 `THREADS_*`·`SUPABASE_*`·`DRY_RUN` 은 다른 계정이 물려받지 않는다 (`.env.<계정>`에만).
- 검수 프롬프트(PLAN 1장 포함)가 바뀌면 모든 계정이 재시험 대상이다. PLAN 규칙을 고치면 기존 계정 시험부터 확인.
- `exam.py` 는 옵션을 받지 않는다. `--help` 를 줘도 시험이 실행된다 (LLM 비용 발생).
- 소개(pick) 글은 **한 상품 목록에서 날짜로 돌려 쓴다**. 컨셉에 안 맞는 상품이 섞이면 그것이 먼저 나가니 목록에서 뺀다.

## 알림 (일일 요약 `ops_advice`)
- "상품 링크 보충 필요" → 사용자에게 알리고 "상품 10개 추가"를 제안
- "발행 수 늘려도 돼요" → 첫 발행 7일 뒤 하루 4개(상품 20+), 14일 뒤 6개(상품 30+). 사용자 승인 후 `max_posts_per_day`·`slots_kst` 를 함께 올리고 관련 테스트 확인

## 목표 점검 (월 50만원)
수익은 계정 수가 아니라 **터지는 글 + 고가 상품**에서 나온다. 계정 추가 전에 반드시: 글당 답글 수, 파트너스 리포트의 클릭·수수료를 주간 리포트로 확인하고, 반응이 없으면 틀부터 고친다.
