# PORTFLOW / 통합 계좌관리 Backend

PORTFLOW는 owner-only ChatGPT Site를 최종 사용자 제품 표면으로 사용합니다. 이 repository는 PORTFLOW의 private backend/reference와 기존 `account_bot` Telegram runtime을 함께 보유하며, package·CLI 이름은 호환성을 위해 유지합니다. hosted Site source/projection은 이 repository와 별도 version boundary이므로 `web/`는 hosted Site의 직접 source가 아닙니다.

키움증권·토스증권·메리츠증권에 흩어진 자산을 하나의 포트폴리오로 조회하고, 정량 분석·적립 계획·신규 자금 리밸런싱 의견·일간/주간 보고서를 제공하는 읽기 전용 Telegram runtime도 함께 유지합니다.

전체 보유종목·비중·집중도·리밸런싱·정기 계좌 평가는 키움 메인 계좌만 사용합니다. 키움 모으기 계좌와 메리츠는 `/모으기` 계획 평가에만 포함하며 전체계좌 평가 분모에서는 제외합니다.

이 프로젝트의 증권사 표면은 **READ ONLY**입니다. 매수·매도·주문 정정·주문 취소 메서드가 공통 인터페이스에 없으며, HTTP 요청도 조회 endpoint allowlist를 통과해야 합니다.

## 운영 목적

1. 매일모으기에서 불필요한 종목을 걸러냅니다.
2. 등록된 모으기 설정으로 하루·주간 매수금액을 확인합니다.
3. 검증 가능한 분류 데이터로 지나친 섹터 쏠림을 방지합니다.
4. 매일모으기 종목과 금액의 균형 조정 의견을 냅니다.
5. 매일모으기를 제외한 정수주·온주를 주식봇 기준으로 정기 분석해 기계적인 차익실현 판단을 지원합니다.

모든 기능은 조회·분석 전용이며 실제 주문을 실행하지 않습니다.

## 지원 범위

| 영역 | 현재 구현 |
|---|---|
| 키움증권 | 공식 REST API의 국내/미국 잔고·현금·당일 체결 조회 |
| 토스증권 | 공식 Open API의 계좌·보유·구매가능금액·체결 완료 주문 조회 |
| 메리츠증권 | 공식 개인용 공개 REST API를 확인할 수 없어 엄격한 정규화 JSON snapshot만 지원 |
| 통합 포트폴리오 | `market + ticker + asset type + currency` 식별, 수량 합산, 원가가중 평균단가, KRW/USD 환산, 현금 포함 비중 |
| 장애 처리 | 실패 증권사를 0원으로 간주하지 않고 부분합계만 표시; 전체 금액·비중·리밸런싱은 보류 |
| 매일모으기 | 로컬 계획·revision·일별 occurrence·Thesis revision 저장; 주문은 실행하지 않음 |
| 시장데이터 | provider interface와 선택적 yfinance 구현, dataset별 TTL, bounded last-good fallback |
| 분석 | Company Score와 Portfolio Score 분리, coverage gate, hard-warning override, 정량 계산은 `Decimal` 기반 Python |
| 보고서 | 모바일 HTML 형식 일간/주간 보고서, PTB JobQueue, 전송 idempotency/불확실 상태 관리 |
| DB | async SQLAlchemy + SQLite(WAL, FK, busy timeout); 모델 계층은 향후 PostgreSQL port를 고려 |
| Excel import | marker 기반 TOTAL/PER_SHARE 정규화, 원본/정규화 snapshot, 복합 instrument key 집계, 차단형 preview |
| Migration | schema version 기록, 기존 SQLite 사전 backup 및 integrity check, 정상 시작의 자동 migration 금지 |
| Local web | FastAPI import/portfolio/daily-buy/scenario API와 React/Vite Dashboard·통합잔고·매일모으기·Saved Scenario 화면 |

공식 자료: [키움 REST API 안내](https://openapi.kiwoom.com/guide/apiguide), [키움 공식 예제/명세](https://github.com/Kiwoom-Securities/Kiwoom-REST-API), [토스증권 Open API](https://developers.tossinvest.com/docs), [토스증권 OpenAPI JSON](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json).

토스증권은 Open API 사용 자격/사전 신청 상태를 먼저 확인해야 합니다. 메리츠증권은 앱 scraping이나 RPA를 사용하지 않습니다.

## Architecture

컴포넌트, data flow, storage, 외부 service 경계와 system invariant는 [`ARCHITECTURE.md`](ARCHITECTURE.md)를 참조하십시오. 현재 구현·검증 상태와 알려진 한계는 [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md), 미완료 작업과 우선순위는 [`TASKS.md`](TASKS.md)에 둡니다.

## Directory structure

```text
src/account_bot/
  analysis/       deterministic scores and new-money rebalancing
  brokers/        read-only adapters and HTTP allowlist
  config/         dotenv/settings validation
  domain/         immutable Decimal models and result envelopes
  market/         provider contracts, cache, yfinance, news/LLM guardrails
  portfolio/      aggregation and recurring-buy reconciliation
  portfolio_import/ XLSX parsing, validation, normalization and import snapshots
  reports/        Telegram-safe formatters and periodic reports
  scheduler/      PTB JobQueue jobs, snapshots, idempotent delivery
  services/       broker/analysis/storage orchestration
  storage/        async SQLAlchemy models and repository
  telegram_ui/    authorization and commands
  web_api/        localhost FastAPI import/portfolio/daily-buy API
tests/             network-free unit/integration tests
web/               React/Vite/TypeScript dashboard
examples/          normalized snapshot example
```

## 설치

Python 3.11 이상을 권장합니다. Windows PowerShell 예시:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[market,dev]"
Copy-Item .env.example .env
```

`market` extra는 선택적 yfinance provider, `dev` extra는 테스트/정적 검사 도구입니다. Telegram JobQueue가 사용하는 APScheduler는 `python-telegram-bot[job-queue]`의 호환 범위로 설치됩니다.

여러 PC에서는 source와 위 네 문서만 동일한 private Git remote로 동기화합니다. `.env`, 가상환경, DB, snapshot, log, report, cache, 첨부 이미지는 PC별 local 상태로 유지하며 Git으로 공유하지 않습니다.

## 환경변수

실제 값은 `.env`에만 두고 commit하지 마십시오.

| 변수 | 용도 | 필수 여부 |
|---|---|---|
| `ACCOUNT_BOT_TELEGRAM_TOKEN` | 새 계좌관리봇 전용 Bot token | 실행 시 필수 |
| `ALLOWED_TELEGRAM_USER_IDS` | 쉼표로 구분한 허용 사용자 ID | 실행 시 필수, 빈 값은 전원 거부 |
| `ACCOUNT_BOT_FINGERPRINT_PEPPER` | 계좌/Telegram ID HMAC fingerprint용 별도 random secret | snapshot/scheduler 사용 시 필수 |
| `KIWOOM_MAIN_APPKEY`, `KIWOOM_MAIN_SECRET` | 키움 메인 계좌 REST 인증 | 한 쌍 필수 |
| `KIWOOM_ACCUM_APPKEY`, `KIWOOM_ACCUM_SECRET` | 키움 모으기 계좌 REST 인증 | 한 쌍 필수 |
| `KIWOOM_API_BASE_URL`, `TOSS_API_BASE_URL` | 공식 API origin override; 설정 validator가 허용 host를 제한 | 선택 |
| `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` | 토스 Open API 인증 | 선택; 없으면 토스를 관리 대상에서 제외 |
| `MERITZ_SNAPSHOT_PATH` | 정규화된 메리츠 JSON 파일 | 메리츠 조회 시 필요 |
| `MERITZ_ACCUMUL_SNAPSHOT_PATH` | 정규화된 메리츠 모으기 계획 JSON | 모으기 목록 조회 시 필요 |
| `KIWOOM_SNAPSHOT_PATH`, `TOSS_SNAPSHOT_PATH` | 실 API credential이 없을 때 normalized snapshot fallback | 선택 |
| `ACCOUNT_BOT_ENV_FILE` | CLI argument가 없을 때 사용할 dotenv path | 선택, 기본 `.env` |
| `ACCOUNT_BOT_DATA_DIR`, `ACCOUNT_BOT_LOG_DIR`, `ACCOUNT_BOT_REPORT_DIR`, `ACCOUNT_BOT_CACHE_DIR` | machine-local runtime directory | 선택 |
| `DATABASE_URL` | async SQLAlchemy URL | 기본 SQLite 제공 |
| `SQLITE_BUSY_TIMEOUT_MS` | SQLite lock 대기 시간(1~120000ms) | 기본 `5000` |
| `ACCOUNT_BOT_TIMEZONE` | 보고서 날짜 경계 | 기본 `Asia/Seoul` |
| `DAILY_REPORT_TIME` | 평일 Daily report 시간 | 기본 `08:00` |
| `WEEKLY_REPORT_TIME`, `WEEKLY_REPORT_WEEKDAY` | Weekly report 시간/Python weekday | 기본 토요일 `09:00`, `5` |
| `SCHEDULER_ENABLED` | 예약 보고서 사용 | 기본 true; pepper 없으면 강제 비활성 |
| `COMPANY_WEIGHT_*`, `FINAL_WEIGHT_COMPANY`, `FINAL_WEIGHT_PORTFOLIO` | 정량 점수 가중치; 각 group 합은 1이어야 함 | 선택 |

`ACCOUNT_BOT_FINGERPRINT_PEPPER`는 최소 16자이며 Telegram token이나 broker credential을 재사용하면 설정 단계에서 거부됩니다. 충분히 긴 별도 random 값으로 생성하고 화면, 로그, README에 붙여 넣지 마십시오.

키움/토스는 계좌 조회 endpoint에서 계정을 발견하므로 원문 계좌번호 환경변수를 받지 않습니다. 키움 MAIN과 ACCUM은 서로 다른 논리 소스로 수집하여 한쪽 실패를 다른 쪽의 0원으로 숨기지 않습니다. DB에는 원문 account ID 대신 HMAC fingerprint와 일반 표시명만 저장합니다.

## 실행

설정 상태만 확인하며 실제 값을 출력하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m account_bot doctor
.\.venv\Scripts\python.exe -m account_bot init-db
.\.venv\Scripts\python.exe -m account_bot run
```

기존 SQLite schema를 올릴 때만 별도의 backup directory를 지정해 명시적으로 실행합니다. 먼저 Bot을 중지하고 DB와 backup 경로를 다시 확인해야 합니다.

```powershell
.\.venv\Scripts\python.exe -m account_bot migrate-db --backup-dir .\data\migration-backups
```

`migrate-db`는 변경 전 SQLite online backup을 만들고 무결성을 확인합니다. 정상 `run`/`init-db` 경로는 기존 unversioned 또는 pending DB를 자동 migration하지 않습니다. 운영 rollback은 생성된 backup 복원을 사용하며, 개발 중에는 반드시 임시 DB로 먼저 rehearsal하십시오.

Schema v2는 이름을 붙인 매일모으기 전략과 종목별 활성 상태를 Decimal 문자열 기반으로 저장합니다. 같은 이름으로 다시 저장하면 해당 시나리오의 전체 전략 snapshot을 원자적으로 교체합니다.

### 로컬 ETF Dashboard

Backend와 frontend를 각각 localhost에서 실행합니다. 기존 봇 DB를 실수로 선택하지 않도록 웹 실행기는 별도의 명시적 `ACCOUNT_BOT_WEB_DATABASE_URL`이 없으면 시작을 거부합니다.

```powershell
$env:ACCOUNT_BOT_WEB_DATABASE_URL = "sqlite+aiosqlite:///data/account_bot_web.db"
$env:ACCOUNT_BOT_WEB_LIVE_MARKET_DATA = "true"
account-bot-web
Set-Location .\web
pnpm install
pnpm dev
```

브라우저에서 `http://127.0.0.1:5173`을 엽니다. Excel 수량·원가는 보유자산 source로 유지하고, 공개 Yahoo/yfinance 현재가격과 USD/KRW를 우선 적용합니다. 정상값은 SQLite에 last-good cache와 가격 snapshot으로 보존되며 실패 시 stale 또는 Excel snapshot 상태를 표시합니다. 배당·분할 조정 Total Return 수익률 추이, XNYS 실제 거래일 기반 1/3/5/7/10년 Historical CAGR projection, 월별 자산성장 graph와 1/2/3/5/10억원 milestone을 제공합니다. 최근 12개월 실제 지급 이력 기반 연간·월평균 배당/분배금 참고값도 별도로 표시하며 Total Return에 중복 가산하지 않습니다. 모든 미래 결과는 과거 데이터와 현재 환율 유지 가정이며 수익 또는 지급을 보장하지 않습니다.

Dashboard의 확률적 범위는 동일 날짜 ETF 수익률 벡터를 함께 재표본하는 20거래일 historical block bootstrap 1,000회 결과다. P25/P50/P75와 기간별 P50을 표시하며 fixed seed로 재현한다. 개인 데이터의 현재 공통 이력이 3년 미만이면 장기 분포의 신뢰도가 낮다는 경고가 함께 표시된다. 최초 계산은 수십 초가 걸릴 수 있고 같은 입력은 서버 memory cache에서 재사용한다.

Portfolio 화면은 통합 보유종목 표와 함께 category별 현재 비중 및 10년 Historical CAGR 예상 비중을 비교한다. 예상 비중은 수익률이나 평균가격을 단순평균하지 않고 종목별 미래금액을 category로 합산한 뒤 다시 계산한다.

보유종목 행을 누르면 ETF 상세 modal이 열린다. 현재 수량·평균원가·live 가격·비중, 1Y Total Return, 3/5/10Y CAGR, 변동성, 최대낙폭, trailing 배당/분배금과 종목별 1/3/5/7/10Y 예상금액을 확인할 수 있다. 역사가 부족한 기간은 임의로 채우지 않고 값 없음 또는 warning으로 남긴다.

개발 로그:

```powershell
.\.venv\Scripts\python.exe -m account_bot --verbose run
```

다른 env 파일은 subcommand 앞에 지정합니다.

```powershell
.\.venv\Scripts\python.exe -m account_bot --env-file .env.development doctor
```

## Telegram commands

| 한글 | 영문 | 설명 |
|---|---|---|
| `/자산` | `/assets` | 키움 메인 전체자산·지역·집중도 |
| `/포트폴리오` | `/portfolio` | 키움 메인 종목·비중·수익률 |
| `/모으기` | `/recurring` | 모으기 종목 평가·주간금액·섹터·배분·최종 요약 |
| `/모으기추가 AMD 20USD 8%` | `/recurring_add AMD 20USD 8%` | 로컬 계획 저장, 주문 없음 |
| `/종목 AMD` | `/stock AMD` | 현재가 |
| `/분석 AMD` | `/analyze AMD` | 키움 메인 기준 기업/포트폴리오 분리 분석 |
| `/리밸런싱 500000` | `/rebalance 500000` | 원화 신규 자금 배분 의견, 매도 없음 |
| `/주간리포트` | `/weekly` | 키움 메인·모으기 주간 심화 보고서 |
| - | `/thesis AMD 논리...` | append-only Investment Thesis revision |

Ticker 충돌을 피하려면 `NASDAQ:AMD`, `KOSPI:005930`, `KOSDAQ:247540`처럼 시장을 명시하십시오.

## Scoring model

Company Score 기본 가중치는 Fundamental 35%, Valuation 25%, Growth 20%, Moat 10%, Safety 10%입니다. `*_WEIGHT_*` 환경변수로 변경할 수 있으며 합계가 정확히 1이어야 합니다.

Final Score는 Company 70% + Portfolio 30%입니다. 두 점수는 별도로 유지됩니다. 각 점수는 필요한 가중 데이터의 70% 미만만 확인되면 계산하지 않습니다. 불완전한 증권사 분모에서는 Portfolio Score와 리밸런싱을 항상 보류합니다.

매일모으기에는 기업점수와 별도로 장기추세 적합성 점수를 제공합니다. 수정주가 5년 이력에서 3년 CAGR, 1년 수익률, 3년 최대낙폭, 1년 연환산 변동성, 200일 이동평균 상·하회를 같은 기준으로 정규화합니다. 이 점수는 ETF처럼 기업 재무지표가 맞지 않는 종목도 비교하기 위한 후행 지표이며 미래 수익을 보장하는 예측값이 아닙니다. `/모으기`는 같은 투자 통화별 일일금액 합계에서 각 종목의 모으기 비중과 균등 비중을 함께 표시하고, 장기추세 적합성과 집중도를 조합해 증액·유지·감액 테스트 의견을 냅니다.

키움 ACCUM의 기존 보유종목·보유수량은 모으기 계획 평가에 사용하지 않습니다. `/모으기`는 2026-08-12 사진으로 확인한 키움 설정, 메리츠 로컬 계획, 사용자 추가 후보를 기준으로 현황을 보여주며, 종목별 1회 매수금액 조정안은 개별 종목 Valuation만을 근거로 제시합니다. 원화 모으기 최소 금액은 키움 2,000원, 메리츠 1,000원입니다. 권장안은 자동 적용되지 않습니다. 소수점 체결 내역은 수집하거나 표시하지 않으며 주문도 실행하지 않습니다.

모으기 Valuation은 주식봇의 정량 보강 기준(EV/EBITDA, FCF Yield, 주식수 변화)을 포함하며, 미국 금요일 정규장 종가로 P/E와 기업가치 배수를 다시 계산합니다. 매주 토요일 08:30 KST에 전체 활성 모으기 종목을 갱신하고, 미국 휴장 시에는 해당 주의 마지막 정상 거래일 종가를 사용합니다.

USD/KRW 환율은 Yahoo Finance의 `KRW=X`를 사용합니다. 가벼운 Yahoo chart 응답을 우선 조회하고 yfinance를 보조 경로로 사용하며 30분 동안 캐시합니다. 두 경로가 모두 실패하면 최대 3일 이내의 마지막 정상값만 `마지막 정상값`으로 표시해 사용하며, 그보다 오래되거나 정상값이 없으면 키움 메인 원화 총액과 비중을 보류합니다.

판정 구간은 다음과 같습니다.

- 85–100: 적극 매수
- 70–84.99: 계속 매수
- 55–69.99: 현재 수준 유지
- 40–54.99: 매수 축소
- 0–39.99: 매수 중단 검토

Accounting issue, liquidity crisis, going concern, fraud, major customer loss, structural deterioration, thesis breakdown은 점수와 별개인 hard warning입니다. 뉴스는 정량 점수에 직접 더하지 않고 Fundamental 영향 검토 대상으로만 표시합니다.

현재 yfinance provider가 제공하지 못하는 과거 5년 valuation median, peer median, 수주·TAM·market share, 검증된 moat 자료는 임의 생성하지 않습니다. 데이터가 부족하면 점수를 보류합니다.

## Scheduler와 snapshot

### 창 없는 로컬 봇의 상태와 정상 종료 요청

새 버전으로 **정상 기동한 이후** 같은 PC의 계좌관리봇 디렉터리에서 다음 명령을
사용할 수 있습니다. 운영 봇을 종료하거나 재시작하는 승인 없이 명령을 실행하지
마십시오. `status`는 읽기 전용이며 `stop`은 현재 실행본에 종료 요청만 남깁니다.

```powershell
.\.venv\Scripts\python.exe -m account_bot --env-file .\.env status
.\.venv\Scripts\python.exe -m account_bot --env-file .\.env stop
```

기존 단일 인스턴스 OS 잠금으로 실행 여부를 판단하고 실행본별 두 번째 OS 잠금으로
상태 파일이 현재 프로세스의 것인지 확인합니다. 상태에는 PID, 기동 시각, 패키지
버전, **기동 시점의 디스크 파일**인 주간 리포트와 핵심 런타임 소스 SHA-256만
기록하며 인증정보나 계좌 데이터는 기록하지 않습니다. 해시만으로 실제 로드된
바이트코드나 운영 적용을 증명하지는 않습니다. `stop`은 해당 실행본 ID만 대상으로
하며, 구버전 프로세스처럼 검증 가능한 상태와 실행본 잠금이 없는 경우 요청을
보내지 않습니다. 종료가 시작되면
새 명령은 처리 대신 종료 중임을 알리고 새 예약 작업은 기록·전송 전에 건너뜁니다.
PTB의 `run_polling` 종료 경로가 polling을 중지하고 이미 받은 업데이트와 실행 중
작업을 마친 뒤 broker 연결과 DB 연결을 정리합니다. 상태가 `stopped`이고 OS 잠금이
풀렸을 때 정상 종료를 확인할 수 있습니다. 잠금이 아직 유지된 `stopped` 상태는
`finalizing`으로 표시합니다. 잠금은 풀렸지만 마지막 상태가 `stopped`가 아니면
정상 종료 여부를 확인할 수 없습니다. 실행본별 종료 요청 파일은 다른 실행본이
재사용하지 않으며 이전 요청을 자동 삭제하지 않습니다.

이 기능은 기존에 이미 실행 중인 `pythonw`에 소급 적용되지 않습니다. 디스크의
코드를 바꿔도 실행 중 Python 모듈은 자동 갱신되지 않으며, 구버전 프로세스의 종료나
재시작을 이 기능으로 강제하지 않습니다. 네트워크 listener, Telegram 관리 명령,
새 key 또는 계정은 추가하지 않습니다.

별도 scheduler process를 만들지 않고 Telegram application의 단일 PTB JobQueue를 사용합니다.

- Daily: 평일, `DAILY_REPORT_TIME`
- Weekly: `WEEKLY_REPORT_WEEKDAY`, `WEEKLY_REPORT_TIME`
- timezone: `ACCOUNT_BOT_TIMEZONE`
- 동일 기간/수신자는 DB idempotency key로 한 번만 claim
- Telegram 전송 성공 여부가 불확실하면 `UNKNOWN`으로 기록하고 자동 재전송하지 않음
- 예약 실행 시 complete/partial source 상태와 함께 portfolio snapshot 저장

Telegram API에는 원격 idempotency key가 없으므로 프로세스가 전송 직후 비정상 종료되는 극단적 상황에서 exactly-once를 보장할 수 없습니다. 이 구현은 중복 계좌정보 전송을 피하는 방향을 우선합니다.

## 메리츠 normalized snapshot

[`examples/normalized_snapshot.example.json`](examples/normalized_snapshot.example.json)을 복사해 값과 `as_of`를 갱신하고 `MERITZ_SNAPSHOT_PATH`로 지정합니다. 경로가 폴더이면 그 안의 `snapshot.json`을 읽습니다. `MERITZ_ACCUMUL_SNAPSHOT_PATH`가 폴더이면 `recurring.json`을 읽습니다.

- `account_id`에는 실제 계좌번호가 아닌 `meritz-primary` 같은 로컬 alias만 사용
- 숫자로 보이는 긴 계좌번호는 거부
- token, secret, password, authorization, account number key가 있으면 전체 파일 거부
- raw API response나 앱 export를 그대로 넣지 말고 예제 구조로 정규화
- 앱 캡처의 원문 계좌번호·이름은 JSON에 옮기지 않으며 이미지 파일은 gitignored 로컬 자료로만 유지
- 원화로 해외주식을 모으는 계획은 원화 일일금액과 USD 종목통화를 분리해 표시하고, 체결별 환전금액이 없으면 체결 여부를 임의 확정하지 않음
- 현재 메리츠 모으기는 실시간 API가 없으므로 로컬 계획의 종목·금액·섹터 평가만 제공
- 메리츠 모으기 목록은 계획 데이터이며 별도 계좌 잔고로 중복 합산하지 않음
- `accounts`, `positions`, `cash`, `transactions` 배열이 빠지면 빈 잔고가 아니라 조회 실패로 처리
- 상단/행 `as_of`와 파일 수정시간으로 freshness를 확인하며, 가장 오래된 기준이 3일을 넘으면 `STALE`로 표시하고 전체 금액·비중·점수를 보류
- 파일은 runtime `/data` 등 gitignored 경로에 저장

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe -m pytest
```

검증 범위는 broker Position 정규화, 동일 ticker 합산, KRW/USD 환산, 현금 포함 비중, 평균단가/수익률, 부분 실패, cache single-flight/stale fallback, 점수/coverage, recurring 판정, 신규자금 리밸런싱, Telegram authorization, scheduler idempotency, SQLite 제약을 포함합니다. 또한 XLSX marker/두 입력방식/금액 기반 집계/validation, 빈 DB·legacy DB migration, backup, rollback, exact Decimal persistence를 검증합니다. 테스트는 실제 네트워크나 주문 API를 호출하지 않습니다.

개인 workbook regression은 파일이나 기대값을 Git에 넣지 않고 `ACCOUNT_BOT_PRIVATE_IMPORT_XLSX`와 `ACCOUNT_BOT_PRIVATE_IMPORT_EXPECTED`를 현재 shell에만 설정했을 때 선택적으로 실행됩니다. 설정하지 않으면 해당 테스트는 skip됩니다.

## Security

- 새 bot token은 기존 Bot과 분리
- Telegram allowlist는 fail-closed
- 계좌 명령은 허용 사용자와 chat ID가 같은 Telegram 개인 채팅에서만 허용
- credential/token/account number를 출력·메시지·DB에 저장하지 않음
- adapter error는 상태/type만 노출하고 provider body는 폐기
- broker origin은 공식 hostname으로 고정하고 HTTP method/path/TR allowlist로 조회 이외 요청 차단
- 모든 금액·수량·환율은 binary float가 아닌 `Decimal`
- `.env`, DB, snapshot, logs, reports는 gitignored
- log filter가 Bearer/token/secret/account-number 형태를 추가로 redact
- verbose 모드는 `account_bot` namespace만 DEBUG로 바꾸며 Telegram/DB wire payload 로그는 WARNING으로 고정
- LLM guardrail은 입력 JSON에 없는 숫자/사실 생성을 금지하며 정량 계산에는 LLM을 사용하지 않음

## Troubleshooting

`doctor`에서 `not configured`가 나오면 `.env`의 변수 이름과 실행 위치를 확인하십시오. 실제 값은 출력되지 않습니다.

- `Telegram allowlist: not configured`: `ALLOWED_TELEGRAM_USER_IDS`가 비었음. Bot은 모든 사용자를 거부합니다.
- `SCHEDULER: disabled`: `SCHEDULER_ENABLED=false`이거나 fingerprint pepper가 없음.
- 키움 인증 실패: App Key/Secret, 등록 IP, 모의/실전 서비스 조건을 공식 포털에서 확인.
- 토스 인증 실패: Open API 신청/활성화 여부와 client credential을 확인.
- 메리츠 조회 실패: normalized snapshot 경로·JSON 구조·금지 key·local account alias를 확인.
- 전체 금액이 아닌 부분합계: 한 증권사/현금/FX 조회가 실패한 상태입니다. 누락 계좌는 0원으로 계산되지 않습니다.
- 분석 보류: 시장데이터 extra가 없거나 정량 coverage가 기준보다 낮습니다.
- SQLite lock: 긴 handler session을 공유하지 말고 기본 WAL/busy timeout을 유지하십시오.

투자판단은 정보 제공용이며 실제 주문은 반드시 사용자가 별도로 검토하고 실행해야 합니다.

## Offline CI

The canonical repository includes `.github/workflows/ci.yml`. It runs credential-free validation on pull requests, `master` pushes, and manual dispatch. Python 3.11–3.14 run `pytest`, `ruff check .`, and `pip check`; Python 3.11 also verifies the wheel build. A separate Node 22 job installs the locked `web/` dependencies and runs the TypeScript/Vite production build. A Windows Python 3.12 job checks local locks, status/stop control, and shutdown hooks using temporary fixtures only. The workflow must not access broker credentials, Telegram credentials, local operational databases, snapshots, logs, or reports.

## GPT cloud setup / offline smoke

Linux cloud checkout에서도 PC 파일이나 API secret 없이 수정·검증할 수 있습니다.
이 절차는 기존 private backend/reference 검증용이며 hosted PORTFLOW Site를
배포하거나 Telegram polling을 시작하지 않습니다.

1. 저장된 account-bot 환경이 `ready`가 된 후 repository checkout과 `AGENTS.md`를
   확인하고 작업 브랜치를 만듭니다. shell/checkout 자체가 실패하면 repository
   테스트 실패와 구분해 플랫폼 시작 오류를 기록합니다.
2. Python 3.11+, Node 22.12+와 pnpm이 설치된 이미지에서 실행합니다.
   이번 검증 조합은 Python 3.12.14 / Node 24.19.0 / pnpm 11.19.0입니다.

   ```bash
   bash scripts/cloud/setup.sh
   .venv/bin/python scripts/cloud/validate.py
   # 빠른 독립 시작 검사 (설치 완료 후)
   .venv/bin/python scripts/cloud/validate.py smoke
   ```

   스크립트는 자신의 위치로 repository를 찾아 다른 working directory에서도 동작합니다.
   setup은 `.venv`를 재사용하며 반복 실행 가능합니다. Python dependency는
   `pyproject.toml`의 범위로 설치되고 frontend는 기존 `pnpm-lock.yaml`을 고정해
   설치합니다. Python의 전체 transitive dependency lock은 아직 없으므로 설치
   재현성을 byte-for-byte 고정한다고 보장하지 않습니다.
3. 저장된 cloud 환경의 setup 명령에는 `bash scripts/cloud/setup.sh`를 사용합니다.
   시작/maintenance 검사에는 `.venv/bin/python scripts/cloud/validate.py smoke`를
   사용합니다. 자동 시작에 `account-bot run` 또는 운영 `init-db`를 넣지 않습니다.
   이 repository 변경만으로 플랫폼에 저장된 환경 설정이 변경되지는 않습니다.

Setup에는 package registry 연결이 필요합니다. 캐시는 쓰기 가능한 checkout의
`.cloud-cache/`와 `.pnpm-store/`에 저장하므로 read-only HOME에서도 설치 가능합니다.
`EAI_AGAIN`, registry timeout, 다운로드 거부는 설치 환경/네트워크 오류로 기록합니다.
인증이나 네트워크 정책을 자동 변경하지 않습니다. pnpm 다운로드는 재시도 없이
요청당 15초 제한을 사용합니다. 네트워크 허용 또는 사전 설치된 image/cache가
준비된 후 같은 setup 명령을 재실행하십시오.

검사는 credential·운영 DB URL·개인 workbook 경로를 자식 프로세스에 전달하지
않습니다. `src`, `tests`, 공개 `examples`만 임시 디렉터리로 복사하므로 root `.env`를
자동 탐색하는 테스트도 운영 설정을 읽지 않습니다. pytest, Python compile,
ruff, pip check, 기존 TypeScript/Vite build를 실행합니다. 테스트와 smoke 서버는
Python socket outbound 연결도 차단합니다. 이는 native library나 임의 subprocess까지
차단하는 OS network sandbox는 아니며, 실제 검증 대상은 기존 offline fixture입니다.
프런트엔드 검사는 설치된 도구를 직접 실행하여 pnpm의 암묵적 재설치를 피합니다.

Smoke는 기존 FastAPI app factory를 live market provider 없이 시작하고, 매번 새
임시 SQLite와 예약된 임시 loopback 포트를 사용합니다. `/api/health`의 READ_ONLY
응답을 확인한 후 서버를 종료하고 DB를 삭제합니다. 외부 공개 주소나 CORS,
broker allowlist, 인증 정책은 변경하지 않습니다. 고정 포트 8000을 쓰는 기존
운영 entry point와 별개인 한정된 cloud 검사 진입점입니다.

결과는 다음처럼 구분합니다.

- **Fresh start:** container ready, shell, checkout, dependency install 성공 여부.
- **App validation:** compile/lint/offline fixture 및 임시 DB HTTP smoke 성공 여부.
- **미검증:** 실제 Telegram, broker 인증/계좌 조회, live market data, 운영 scheduler,
  hosted Site 배포. Offline 통과를 이 기능들의 live 검증 성공으로 취급하지 않습니다.

Cloud setup/smoke에 필요한 secret은 **없습니다**. 실제 기능의 변수 이름·용도는
위 환경변수 표를 참조하십시오. Telegram의 `ACCOUNT_BOT_TELEGRAM_TOKEN`,
`ALLOWED_TELEGRAM_USER_IDS`, fingerprint용 `ACCOUNT_BOT_FINGERPRINT_PEPPER`,
키움의 `KIWOOM_MAIN_APPKEY`/`KIWOOM_MAIN_SECRET` 및
`KIWOOM_ACCUM_APPKEY`/`KIWOOM_ACCUM_SECRET`, 토스의
`TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`을 이 검증을 위해 입력하지 마십시오.
