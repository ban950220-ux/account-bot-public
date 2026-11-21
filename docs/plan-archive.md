# 모으기 계획 보관(삭제) — 설계

상태: 설계 확인 대기 · 작성 2026-11-21

## 배경

`US:ROK` 계획이 `user-candidate:US:ROK`와 `kiwoom-photo:US:ROK` 두 개로 중복 등록돼 있다. 계획은 변경 이력이 쌓이는 구조라 삭제 기능이 없다. 그래서 OFF로 꺼도 종목 이름으로 찾으면 계속 "둘 중 고르라"는 응답이 나온다.

## 목표

필요 없는 계획을 **보관**해서 목록, 합계, 보고서, 추천, 종목 이름 찾기에서 빼낸다. 행과 변경 이력은 지우지 않으므로 언제든 되살릴 수 있다.

## 범위

명령:

| 명령 | 동작 |
|---|---|
| `/모으기삭제 <계획키 또는 종목>` (`/recurring_archive`) | 계획을 OFF로 바꾸고 보관한다. 두 가지를 한 트랜잭션으로 처리한다. 종목 이름이 보관 안 된 계획 여러 개와 맞으면 계획키 목록을 보여주고 아무것도 바꾸지 않는다. |
| `/모으기복원 <계획키>` (`/recurring_restore`) | 보관을 해제한다. 계획은 OFF 상태 그대로 돌아오므로 켜려면 `/모으기설정`으로 ON한다. |

- 보관 응답에는 되살리는 명령(`/모으기복원 계획키`)을 함께 적는다.
- 보관된 계획은 `listCurrentPlans`에서 빠진다. 그래서 `/모으기`, `/추천`, 일간보·주간보, `/모으기설정`(한 줄·여러 줄) 어디에도 나오지 않는다.

DB 변경(추가만 하고 기존 테이블은 건드리지 않음):

```sql
CREATE TABLE IF NOT EXISTS cloud_plan_archive_events (
  event_no INTEGER PRIMARY KEY,
  plan_key TEXT NOT NULL REFERENCES cloud_plans(plan_key),
  action TEXT NOT NULL CHECK (action IN ('ARCHIVE','RESTORE')),
  effective_at TEXT NOT NULL,
  mutation_key TEXT NOT NULL UNIQUE
);
-- 쓰기 fence(ACTIVE + edits_enabled)와 수정·삭제 금지 trigger를 기존 테이블과 같은 방식으로 둔다.
```

- 현재 보관 여부는 계획별 마지막 이벤트로 정한다. 이벤트는 추가만 하고 고치거나 지우지 않는다.
- 테이블이 없으면 "보관 기능 미설치"로 보고 기존 동작을 그대로 유지한다. `cloud_policy`와 같은 방식이다. 그래서 코드 배포와 DB 적용 순서가 바뀌어도 안전하다.
- 같은 텔레그램 메시지가 다시 와도 `mutation_key`가 같아 한 번만 반영된다.

운영 DB 적용(migration):

- 파일은 `deploy/cloudflare/migrations/2026-11-21-plan-archive.sql`이다. 같은 내용을 `schema.sql` 끝에도 넣는다.
- Actions workflow에 `migrate` 동작을 추가한다. 순서는 아래와 같다.
  1. D1 Time Travel 복원 지점(bookmark)을 기록한다.
  2. 위 SQL 파일을 실행한다.
  3. 테이블과 trigger가 생겼는지, 기존 테이블 행 수가 그대로인지 확인한다.
- 백업은 Time Travel 복원 지점으로 한다. D1 export는 개인 운영 데이터를 공개 저장소의 Actions 산출물로 남기게 되므로 쓰지 않는다.

## 제약

- 기존 테이블 구조, `cloud_plan_lifecycle`, idempotency 규칙은 바꾸지 않는다. `cloud_plan_lifecycle`은 이전(cutover) 이후 잠겨 있고, 되돌리기 점검 스크립트가 변경을 막으므로 재사용하지 않는다.
- 행을 실제로 지우지 않는다.
- 주문·증권사 호출은 없다.
- migration과 운영 배포는 각각 실행 직전에 사용자 승인을 받는다.

## 단계와 완료 기준

1. **코드와 테스트**
   - 완료 기준: 테스트로 아래를 확인한다.
     - 보관하면 목록·합계·종목 찾기·여러 줄 설정에서 빠지고, 보관하면서 OFF가 된다.
     - 같은 메시지를 다시 보내도 한 번만 반영된다.
     - 같은 종목이 여러 개면 목록을 보여주고 아무것도 바꾸지 않는다.
     - 복원하면 OFF 상태로 돌아온다.
     - 테이블이 없으면 기존 동작 그대로다.
     - fence가 닫혀 있으면 쓰기가 거부된다.
     - 이벤트를 수정·삭제하면 거부된다.
   - 전체 JS·Python 테스트, ruff, 이관 점검이 통과해야 한다.
2. **migration 동작 추가 후 운영 DB 적용** (승인 후)
   - 완료 기준: 복원 지점이 기록되고, 테이블과 trigger가 생기고, 기존 테이블 행 수가 그대로다.
3. **코드 배포** (승인 후)
   - 완료 기준: 운영 100%, webhook 403
4. **실사용**
   - 완료 기준: `/모으기삭제 user-candidate:US:ROK` 다음에 `/모으기설정 ROK OFF`가 바로 적용되고, 둘 중 고르라는 응답이 나오지 않는다.

## 되돌리기

- 기능: `/모으기복원`으로 개별 계획을 되살린다.
- 코드: 직전 Worker 버전으로 `rollback`한다. 새 테이블이 남아 있어도 이전 코드는 무시한다.
- DB: 2단계에서 기록한 Time Travel 복원 지점으로 D1 전체를 되돌릴 수 있다. 이 경우 그 뒤의 모든 변경이 사라지므로 최후 수단으로만 쓴다.

## 남은 이슈

- Windows 로컬 봇으로 되돌리는 점검 스크립트(`scripts/cloud/rollback_reconcile.py`)는 새 테이블을 모른다. 되돌리면 보관했던 계획이 OFF 상태로 다시 보인다. 합계에는 영향이 없다.
