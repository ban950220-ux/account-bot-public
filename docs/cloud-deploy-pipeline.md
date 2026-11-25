# 운영 Worker 클라우드 배포 (GitHub Actions) — 설계

상태: 2단계 완료, 3단계 대기 · 작성 2026-11-16

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
- 배포 시점은 수동 실행만 쓴다. master 푸시만으로는 배포되지 않는다. (사용자 결정 2026-11-16)
- Environment `production`은 사용자가 만들었다. 배포 브랜치는 master로 제한하고 Secret 2개를 넣었다.
- workers.dev 주소는 workflow가 Cloudflare API(`/workers/subdomain`)로 읽는다.
- "Daily 모으기 Report" 제목은 유지한다. (사용자 결정 2026-11-16)
- 전환 후 PC `prod-ops`에서는 배포하지 않는다. 비상용으로만 남긴다. (사용자 확인 2026-11-16)

## 단계별 기록

### 1단계

- 추가: `package.json`, `package-lock.json`(wrangler 4.156.1, 38 packages), `deploy/cloudflare-js/wrangler.production.json`, `.github/workflows/deploy-worker.yml`, `.gitignore`에 `/node_modules/`, `.wrangler/`
- 로컬 `wrangler deploy --dry-run` 번들 성공(279.55 KiB, gzip 67.24 KiB). 바인딩은 D1 1개와 변수 7개이며, Worker secret 4개와 합쳐 기존 12개와 일치한다.
- workflow는 `workflow_dispatch` 전용이다. 실행 파일이 기본 브랜치에 있어야 버튼이 생기므로 master에 올린다. 운영에는 영향이 없다.
- Actions `check` run #1 성공: 토큰 유효, 현재 운영 `9d81836d` 100%, Worker URL `https://account-bot-production.ban950220.workers.dev`, 버전 목록 조회됨.
- 이슈: `wrangler whoami`와 `versions list`의 Author가 계정 이메일을 공개 로그에 출력했다. run #1 로그는 삭제했다. 토큰 확인을 `/user/tokens/verify`로 바꾸고, 모든 wrangler 출력에 이메일 가리기 필터를 넣었다(`6f53821`).
- Actions `check` run #2 성공: `토큰 상태: active`, Author가 `[email]`로 가려짐, Account ID `***`. **1단계 완료.**
- run #2가 잠깐 `waiting`으로 보였던 것은 Environment job이 runner를 기다리는 상태였다. 승인 대기가 아니었다(Required reviewers 미설정).

### 2단계

- Actions `deploy` run #3 (master `194058c`): Worker 테스트 통과, 새 버전 `fd4b7b24-7102-4aa2-a2e7-7f7d888472e3` 업로드, wrangler가 `SUCCESS ... at 100%`로 배포했다.
- 이슈: 확인 단계가 배포 0.1초 뒤 Cloudflare 배포 목록을 읽어 아직 `9d81836d`가 보였고, run이 실패로 끝났다. 배포 자체는 성공했다. 확인 단계를 최신 `created_on` 기준으로 읽고 최대 1분 재시도하도록 고쳤다(`e228743`).
- Actions `check` run #4: `현재 배포: 2026-11-16T12:25:18Z · fd4b7b24 100%`. 이 세션에서 webhook 무인증 POST를 보내 `403`을 받았다.
- **2단계 완료.** 남은 확인: 다음 평일(11/17) 08:00 일간보가 `Daily 모으기 Report`로 오는지 사용자가 확인한다.
- 되돌릴 직전 버전: `9d81836d-7a3d-48e1-add8-3a5e1832a7e6`

## 배포 이력

| 날짜 | 커밋 | 버전 | 내용 | 결과 |
|---|---|---|---|---|
| 2026-11-16 | `194058c` | `fd4b7b24` | 일간보 0원 블록 제거 | 배포 성공, 확인 단계 지연 문제로 run 실패 표시(이후 수정) |
| 2026-11-21 | `bda78ef` | `d7806194-1380-4d35-86cf-6558a91bcf27` | `/모으기설정` 여러 줄 한 번에 반영 | run 성공, 운영 100%, webhook 403. 직전 버전 `fd4b7b24-7102-4aa2-a2e7-7f7d888472e3` |
| 2026-11-21 | `cae3ca3` | `b403a75e-62b7-4bee-935d-fe0d7168193d` | 모으기 계획 보관(`/모으기삭제`·`/모으기복원`), 운영 D1 테이블 추가는 run #12 | run 성공, 운영 100%, webhook 403. 직전 버전 `d7806194-1380-4d35-86cf-6558a91bcf27` |
| 2026-11-21 | `63f7ea2` | `33d05e5a-7461-41ca-81de-2bc7b3999846` | `/모으기시점`·`/모으기이력` | run 성공, 운영 100%, webhook 403. 직전 버전 `b403a75e-62b7-4bee-935d-fe0d7168193d` |
| 2026-11-25 | `e810ddc` | `7e32e657-ff4b-4eb3-ab23-8a7915458217` | Free CPU 대응 1단계: 한국 시간 산술 계산 | run 성공, 운영 100%, webhook 403. 직전 버전 `33d05e5a-7461-41ca-81de-2bc7b3999846` |
| 2026-11-25 | `7304378` | `6f1fcf2d-53b6-4b59-9db6-15c01251d246` | `/start`·`/help` 최신화 | run 성공, 운영 100%, webhook 403. 직전 버전 `7e32e657-ff4b-4eb3-ab23-8a7915458217` |
