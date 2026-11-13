// Static, read-only command replies. Cross-runtime fixture checks prevent drift.
export const START = `🤖 <b>통합 계좌관리봇</b>

전체자산 분석은 키움 메인 계좌만 사용합니다.
키움 모으기·메리츠는 모으기 점검에서만 사용합니다.
<b>READ ONLY</b> — 매수·매도·정정·취소 주문은 실행하지 않습니다.

<code>/help</code>에서 명령어를 확인하세요.`;

export const HELP = `🤖 <b>통합 계좌관리봇 도움말</b>

<b>운영 목적</b>
1. 매일모으기에서 불필요한 종목 걸러내기
2. 모으기 설정을 기준으로 하루·주간 매수금액 확인
3. 지나친 섹터 쏠림 방지
4. 매일모으기 종목·금액 밸런싱
5. 모으기 제외 온주를 정기 분석해 기계적인 차익실현 판단 지원

🌱 <b>매일모으기 관리</b>
/recurring · /모으기
키움·메리츠·추가 후보의 종목 평가, 주간 금액, 섹터 분산, 근거 있는 금액 조정안
권장 금액은 개별 종목 Valuation을 기준으로 산정

📊 <b>키움 메인·온주 관리</b>
/assets · /자산 — 키움 메인 자산과 지역·집중도
/portfolio · /포트폴리오 — 키움 메인 보유종목·비중·수익률
/analyze AMD · /분석 AMD — 키움 메인 기준 종목 정량 분석
/stock AMD · /종목 AMD — 현재가 확인
/weekly · /주간리포트 — 키움 메인과 모으기 주간 점검

🔧 <b>상태·고급 명령</b>
/recurring_add AMD 2000KRW 8% — 분석용 로컬 계획 추가
/recurring_update ETF +1000 — ETF 전체 금액 변경
/모으기변경 개별주 NVDA,AMD 빼고 -1000 — 일부 제외 후 변경
/모으기변경 ETF 전부 모으기중단 — 해당 그룹 중단
/rebalance 500000 · /리밸런싱 500000 — 목표비중 기반 신규자금 의견
/thesis AMD 투자논리 — 종목 Thesis 저장

메리츠와 키움 모으기 보유자산은 전체계좌 평가에서 제외됩니다.
한국 종목은 <code>KOSPI:005930</code>처럼 시장을 지정합니다.
<b>READ ONLY</b> — 매수·매도·정정·취소 주문을 실행하지 않습니다.
섹터·온주 자동 선별은 검증 가능한 데이터만 사용하며 부족하면 판단을 보류합니다.`;
