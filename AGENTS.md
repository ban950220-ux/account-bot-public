# PORTFLOW / account-bot — 프로젝트 작업 규칙

이 저장소는 PORTFLOW의 private backend/reference와 기존 `account_bot` Telegram runtime을 함께 보유한다. `account-bot` package/repository 이름은 호환성을 위해 유지하며, 사용자-facing 프로젝트 이름은 PORTFLOW로 취급한다. 공통 repository 작업, Git, secret, 외부 작업, 검증, model routing 절차는 Codex 전역 `AGENTS.md`와 `~/.codex/rules/`를 따른다.

현재 구현 사실은 `PROJECT_CONTEXT.md`, 시스템 구조와 invariant는 `ARCHITECTURE.md`, 미완료 작업은 `TASKS.md`, 설치·실행·사용법은 `README.md`가 canonical source다.

## 제품 안전 경계

- 최종 사용자 제품 표면은 기존 owner-only PORTFLOW ChatGPT Site다. 별도의 대체 localhost 사이트를 새로 만들지 않으며, 현재 local FastAPI/React는 계산·기능 reference와 private backend 후보로 보존한다. hosted Site source/projection과 이 Git repository는 별도 version boundary이므로 `web/`를 hosted Site source로 간주하거나 자동 덮어쓰지 않는다.
- 이 봇은 읽기 전용 계좌 통합·분석 도구다. 주문·이체·주문 정정/취소·자동 매매를 구현하거나 실행하지 않는다.
- 원문 계좌번호 대신 fingerprint/alias만 저장하고 금액·수량·환율 계산은 `Decimal`을 유지한다.
- 부분 실패를 0원 또는 완전한 포트폴리오로 바꾸지 않는다.
- broker HTTP 공식 origin·method·path/TR allowlist와 Telegram fail-closed 인증을 완화하지 않는다.
- `ARCHITECTURE.md`에 기록된 DB/snapshot schema, idempotency, `UNKNOWN` 전송 상태, 구형 instrument 고유키 호환성 invariant를 보존한다.
- LLM은 정량 계산을 변경하거나 입력 JSON 밖 사실·숫자를 만들 수 없다. 현재 비활성 narrative provider를 요청 없이 활성화하지 않는다.

## 운영 데이터와 외부 작업

- `.env`, 운영 DB, snapshot, log, report, cache, attachment는 개인 운영 데이터다. 현재 작업에 꼭 필요한 별도 승인과 안전한 범위가 없으면 내용을 읽거나 출력하지 않는다.
- 새 dependency, live broker 호출, 실제 Telegram 전송, 데이터 migration, 봇 재시작은 현재 작업 범위와 명시적 사용자 승인이 없으면 추가·실행하지 않는다. 유료 API에는 전역 비용 승인 규칙도 적용한다.
- 운영 DB에 `init-db`, migration, 데이터 수정 script를 테스트 목적으로 실행하지 않는다.

## 프로젝트 검증 조건

- 저장·인증·스케줄·증권사 경계 변경은 전체 `pytest`와 `ruff check .`를 실행한다.
- 테스트는 네트워크, 주문 API, 실제 Telegram 전송 없이 수행한다.
