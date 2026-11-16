# 운영 Worker 클라우드 배포 (GitHub Actions) — 설계

상태: 설계 확인 대기 · 작성 2026-11-16

## 목표

PC가 꺼져 있어도 운영 Worker `account-bot-production`을 GitHub Actions로 배포·확인·되돌리기할 수 있게 한다.
지금은 PC의 `prod-ops/wrangler.json`과 로컬 wrangler 로그인으로만 배포할 수 있다.

## 범위

추가하는 파일:

| 파일 | 내용 |
|---|---|
| `package.json`, `package-lock.json` | `wrangler` 4.156.1 개발 의존성 하나만 (버전 고정, lockfile 포함) |
| `deploy/cloudflare-js/wrangler.production.json` | PC `prod-ops/wrangler.json`과 같은 설정. `main`은 저장소 경로로 바꾸고, `account_id`는 빼서 Secret에서 읽음 |
| `.github/workflows/deploy-worker.yml` | 확인(check)·배포(deploy)·되돌리기(rollback) 수동 실행 workflow |

GitHub 설정(사용자 완료): Environment `production`, Secret `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`.

## 제약

- 배포는 `wrangler versions upload` → `wrangler versions deploy <id>@100%`만 쓴다. `wrangler deploy`는 cron trigger를 덮어쓸 수 있으므로 쓰지 않는다.
- Worker secret(Telegram token 등), cron trigger, D1 데이터·schema는 건드리지 않는다. D1 migration은 하지 않는다.
- 배포 job은 `production` environment에서만 Secret을 받는다. 배포 전에 기존 JS 테스트(`tests/cloud_js_*.test.mjs`, 네트워크 차단)를 다시 통과해야 한다.
- 배포 확인은 webhook 경로에 비밀값 없는 POST를 보내 `403 forbidden`을 받는 것으로 한다. 실제 Telegram 메시지는 보내지 않는다.
- PC `prod-ops` 경로는 지우지 않고 예비 배포 경로로 남긴다. 두 곳에서 동시에 배포하지 않는다.

## workflow 동작

`workflow_dispatch` 입력 `action`:

- `check`: `wrangler whoami`, `wrangler versions list` (읽기 전용, 운영 영향 없음)
- `deploy`: 테스트 → `versions upload`(메시지에 커밋 SHA) → `versions deploy <새 ID>@100%` → `deployments status`로 새 ID 100% 확인 → webhook 403 확인
- `rollback`: 입력 `version_id`를 `versions deploy <id>@100%`로 되돌림 → 같은 확인

## 단계와 완료 기준

1. **설정 파일과 확인 모드**
   - 완료 기준: 로컬에서 `wrangler deploy --dry-run`으로 번들 생성 성공(인증 불필요), 기존 JS·Python 테스트와 ruff 통과, Actions `check` 실행이 성공하고 Worker 목록에 운영 버전 `9d81836d`가 보임.
2. **배포 모드**
   - 완료 기준: Actions `deploy`로 master `3eae73d` 이상을 배포, `deployments status`에 새 버전 100%, webhook 무인증 POST가 403. 다음 평일 08:00 일간보가 `Daily 모으기 Report`로 오는지 사용자가 확인.
3. **되돌리기 모드**
   - 완료 기준: 현재 운영 버전 ID로 `rollback`을 실행해 성공하고 운영이 그대로임(같은 버전 재지정이라 화면 변화 없음).
4. **문서화**
   - 완료 기준: `deploy/cloudflare-js/README.md`에 배포·되돌리기 방법 기록, 이 문서에 단계별 결과·결정·남은 이슈 기록.

## 되돌리기

- 배포 문제: Actions `rollback`에 직전 버전 ID 입력. 첫 배포 직전 버전은 `9d81836d`.
- Actions 자체 문제: PC `prod-ops`에서 기존 방식으로 배포.
- 파이프라인 제거: 추가한 세 파일을 삭제하는 커밋. 운영 Worker에는 영향 없음.

## 결정 기록

- D1 database ID는 공개 저장소에 둔다. 접근에는 토큰이 필요하다. (사용자 결정 2026-11-16)
- wrangler 개발 의존성 추가 승인. (사용자 결정 2026-11-16)
- Account ID는 Actions 로그에서 가려지도록 Secret에 둔다.
- Secret은 Repository가 아닌 Environment `production`에 둔다. master에서만 배포되게 제한할 수 있고, 승인 버튼을 걸 수 있다.

## 미정

- 배포 시점: 수동 버튼만 쓸지, master 푸시 때 자동으로 배포할지.
- 배포 확인용 workers.dev 주소.
