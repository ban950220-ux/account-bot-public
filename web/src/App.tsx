import { ChangeEvent, useCallback, useEffect, useMemo, useState } from "react";

type View = "dashboard" | "portfolio" | "daily-buy" | "scenarios";

type Position = {
  instrument: { market: string; ticker: string; assetType: string; currency: string };
  category: string;
  quantity: string;
  totalCostBasisUSD: string;
  importMarketValueUSD: string;
  averageCostPerShareUSD: string | null;
  importCurrentPriceUSD: string | null;
  importProfitUSD: string;
  calculatedImportReturnPercent: string | null;
  latestCurrentPriceUSD: string | null;
  currentMarketValueUSD: string;
  currentMarketValueKRW: string | null;
  currentProfitUSD: string;
  currentReturnPercent: string | null;
  priceStatus: "LIVE" | "STALE" | "IMPORT_SNAPSHOT" | "UNAVAILABLE";
  priceProvider: string | null;
  priceAsOf: string | null;
  configuredContributionKRW: string;
  effectiveContributionKRW: string;
  dailyBuyEnabled: boolean;
  sourceCount: number;
  warningCodes: string[];
};

type Portfolio = {
  dataState: "READY" | "EMPTY";
  asOf: string | null;
  priceSource: string;
  fx: { status: string; rate: string | null; provider?: string | null; asOf?: string | null; message: string };
  summary: {
    totalCostBasisUSD: string;
    totalMarketValueUSD: string;
    totalMarketValueKRW: string | null;
    totalCostBasisKRW: string | null;
    totalProfitUSD: string;
    totalReturnPercent: string | null;
    dailyContributionKRW: string;
    monthlyAverageContributionKRW: string;
    annualContributionKRW: string;
    holdingCount: number;
    investmentTargetCount: number;
  };
  positions: Position[];
  disclaimer: string;
};

type Performance = {
  status: "READY" | "UNAVAILABLE";
  dataStatus?: "LIVE" | "STALE";
  lookbackYears: number;
  seriesKind: string | null;
  asOf?: string;
  coverageStart?: string;
  coverageEnd?: string;
  dataCoveragePercent?: string;
  totalReturnPercent?: string;
  cagrPercent?: string | null;
  maxDrawdownPercent?: string;
  points: { date: string; index: string; cumulativeReturnPercent: string }[];
  warnings: string[];
  disclaimer?: string;
  message?: string;
};

type ProjectionHorizon = {
  tradingDays: number;
  startingValueUSD: string;
  addedPrincipalUSD: string;
  investmentGainUSD: string;
  endingValueUSD: string;
  startingValueKRW: string;
  addedPrincipalKRW: string;
  investmentGainKRW: string;
  endingValueKRW: string;
  positions: ProjectionPosition[];
};

type ProjectionPosition = {
  ticker: string;
  startingValueUSD: string;
  addedPrincipalUSD: string;
  investmentGainUSD: string;
  endingValueUSD: string;
  startingValueKRW: string;
  addedPrincipalKRW: string;
  investmentGainKRW: string;
  endingValueKRW: string;
  annualReturnPercent: string;
  historyYears: string;
  warnings: string[];
};

type Projection = {
  status: "READY" | "UNAVAILABLE";
  dataStatus?: "LIVE" | "STALE";
  projectionMethod?: string;
  dataCoveragePercent?: string;
  horizons?: Record<"1Y" | "3Y" | "5Y" | "7Y" | "10Y", ProjectionHorizon>;
  wealthTimeline?: {date: string; startingValueKRW: string; addedPrincipalKRW: string; investmentGainKRW: string; endingValueKRW: string}[];
  milestones?: {targetKRW: string; reachedDate: string | null; status: "REACHED" | "NOT_REACHED_WITHIN_10Y"}[];
  income?: {
    status: "READY" | "UNAVAILABLE";
    method: string;
    label: string;
    reinvested: boolean;
    dataStatus: "LIVE" | "STALE";
    dataCoveragePercent: string;
    current: IncomePoint;
    horizons: Record<"1Y" | "3Y" | "5Y" | "7Y" | "10Y", IncomePoint>;
    disclaimer: string;
  };
  disclaimer?: string;
  message?: string;
};

type IncomePoint = {
  annualDistributionUSD: string;
  annualDistributionKRW: string;
  monthlyAverageDistributionUSD: string;
  monthlyAverageDistributionKRW: string;
};

type ScenarioComparison = {
  status: "READY" | "UNAVAILABLE";
  baseDailyContributionKRW?: string;
  modifiedDailyContributionKRW?: string;
  deltas?: Record<"1Y" | "3Y" | "5Y" | "7Y" | "10Y", {baseEndingValueKRW: string; modifiedEndingValueKRW: string; deltaKRW: string; deltaPercent: string | null; addedPrincipalDeltaKRW: string; investmentGainDeltaKRW: string; annualDistributionDeltaKRW: string}>;
  modifiedProjection?: Projection;
  message?: string;
  scenario?: SavedScenario;
};

type SavedScenario = {
  id: number;
  name: string;
  dailyContributionKRW: string;
  annualContributionKRW: string;
  createdAt: string;
  updatedAt: string;
  plans: {ticker: string; configuredAmountKRW: string; effectiveContributionKRW: string; enabled: boolean}[];
};

type MonteCarloPoint = {
  endingValueUSD: string;
  endingValueKRW: string;
  investmentGainUSD: string;
  investmentGainKRW: string;
};

type MonteCarlo = {
  status: "READY" | "UNAVAILABLE";
  projectionMethod?: string;
  runs?: number;
  blockDays?: number;
  seed?: number;
  historicalObservationCount?: number;
  dataCoveragePercent?: string;
  horizons?: Record<"1Y" | "3Y" | "5Y" | "7Y" | "10Y", {tradingDays: number; startingValueKRW: string; addedPrincipalKRW: string; percentiles: Record<"P10" | "P25" | "P50" | "P75" | "P90", MonteCarloPoint>}>;
  warnings?: string[];
  disclaimer?: string;
  message?: string;
};

type AllocationRow = {ticker?: string; category: string; currentValueKRW: string; currentPercent: string; projected10YValueKRW: string; projected10YPercent: string};
type Allocation = {status: "READY" | "UNAVAILABLE"; dataCoveragePercent?: string; tickers?: AllocationRow[]; categories?: AllocationRow[]; disclaimer?: string; message?: string};

type ETFDetail = {
  status: "READY";
  ticker: string;
  position: Position;
  portfolioWeightPercent: string;
  history: {status: "READY" | "UNAVAILABLE"; provider?: string; asOf?: string; stale?: boolean; seriesKind?: string; availableHistoryYears?: string; return1YPercent?: string | null; cagr3YPercent?: string | null; cagr5YPercent?: string | null; cagr10YPercent?: string | null; annualizedVolatilityPercent?: string | null; maxDrawdownPercent?: string | null; coverageStart?: string; coverageEnd?: string};
  distribution: {status: "READY" | "UNAVAILABLE"; provider?: string; asOf?: string; stale?: boolean; trailing12MPerShareUSD?: string; trailingYieldPercent?: string | null; points: {exDate: string; amountPerShareUSD: string; classification: string}[]};
  projection: {status: "READY" | "UNAVAILABLE"; horizons: Record<string, {endingValueKRW: string; endingValueUSD: string; annualDistributionKRW: string | null}>};
  warnings: string[];
  disclaimer: string;
};

type ImportPreview = {
  import_id: string;
  status: "READY" | "BLOCKED";
  marker_rows: number[];
  warnings: { code: string; severity: string; message: string; row_number?: number }[];
  summary: {
    position_count: number;
    unique_ticker_count: number;
    total_cost_basis_usd: string;
    total_import_market_value_usd: string;
    daily_contribution_krw: string;
  };
};

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: "요청을 처리하지 못했습니다." }));
    throw new Error(payload.detail ?? "요청을 처리하지 못했습니다.");
  }
  if (response.status === 204) return undefined as T;
  return response.json();
};

const usd = (value: string | null, digits = 2) =>
  value === null
    ? "—"
    : new Intl.NumberFormat("ko-KR", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: digits,
      }).format(Number(value));

const krw = (value: string | null) =>
  value === null ? "—" : `${new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 0}).format(Number(value))}원`;

const pct = (value: string | null) =>
  value === null ? "—" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;

function Mark({ name }: { name: "grid" | "wallet" | "repeat" | "bookmark" }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></>,
    wallet: <><path d="M4 7.5h15a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h12"/><path d="M16 12h5v4h-5a2 2 0 0 1 0-4Z"/></>,
    repeat: <><path d="M20 7h-9a5 5 0 0 0-5 5v1"/><path d="m17 4 3 3-3 3"/><path d="M4 17h9a5 5 0 0 0 5-5v-1"/><path d="m7 20-3-3 3-3"/></>,
    bookmark: <><path d="M6 4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18l-6-4-6 4Z"/><path d="M9 7h6"/></>,
  };
  return <svg viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}

function App() {
  const [view, setView] = useState<View>("dashboard");
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [file, setFile] = useState<File | null>(null);

  const loadPortfolio = useCallback((refresh = false) =>
    api<Portfolio>(`/api/portfolio${refresh ? "?refresh=true" : ""}`).then(setPortfolio).catch((reason) => setError(reason.message)), []);

  useEffect(() => {
    void loadPortfolio(true);
    const timer = window.setInterval(() => void loadPortfolio(true), 10 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [loadPortfolio]);

  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    const registrations = [
      context.registerTool({
        name: "read_portfolio_summary",
        title: "포트폴리오 요약 읽기",
        description: "현재 화면과 동일한 통합 포트폴리오 요약을 읽습니다.",
        inputSchema: {type: "object", properties: {}, additionalProperties: false},
        annotations: {readOnlyHint: true, untrustedContentHint: false},
        async execute() {
          const snapshot = await api<Portfolio>("/api/portfolio");
          setPortfolio(snapshot);
          return {dataState: snapshot.dataState, priceSource: snapshot.priceSource, summary: snapshot.summary};
        },
      }, {signal: lifecycle.signal}),
      context.registerTool({
        name: "configure_daily_buy",
        title: "매일모으기 설정",
        description: "미국 ETF의 거래일별 원화 적립금과 활성 상태를 저장하고 화면을 갱신합니다.",
        inputSchema: {
          type: "object",
          properties: {ticker: {type: "string", pattern: "^[A-Z][A-Z0-9.-]{0,31}$"}, configuredAmountKRW: {type: "string", pattern: "^[0-9]+(?:\\.[0-9]+)?$"}, enabled: {type: "boolean"}},
          required: ["ticker", "configuredAmountKRW", "enabled"],
          additionalProperties: false,
        },
        annotations: {readOnlyHint: false, untrustedContentHint: false},
        async execute(input) {
          if (!input || typeof input !== "object") throw new Error("입력 형식이 올바르지 않습니다.");
          const value = input as Record<string, unknown>;
          if (typeof value.ticker !== "string" || typeof value.configuredAmountKRW !== "string" || typeof value.enabled !== "boolean") throw new Error("ticker, configuredAmountKRW, enabled가 필요합니다.");
          const result = await api<{ticker: string; configuredAmountKRW: string; effectiveContributionKRW: string; enabled: boolean}>(`/api/daily-buy/${value.ticker}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({configuredAmountKRW: value.configuredAmountKRW, enabled: value.enabled})});
          await loadPortfolio();
          return result;
        },
      }, {signal: lifecycle.signal}),
    ];
    for (const registration of registrations) void Promise.resolve(registration).catch(() => undefined);
    return () => lifecycle.abort();
  }, [loadPortfolio]);

  const onPreview = async () => {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<ImportPreview>("/api/import/preview?sheetName=Sheet1", {
        method: "POST",
        headers: { "Content-Type": file.type || "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "X-Filename": "portfolio.xlsx" },
        body: file,
      });
      setPreview(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Excel을 확인하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  const onCommit = async () => {
    if (!preview) return;
    setBusy(true);
    try {
      await api(`/api/import/${preview.import_id}/commit`, { method: "POST" });
      await loadPortfolio();
      setPreview(null);
      setFile(null);
      setView("dashboard");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "가져오기를 확정하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">F</span><span><b>Forward</b><small>ETF PORTFOLIO</small></span></div>
        <nav aria-label="주요 메뉴">
          <button className={view === "dashboard" ? "active" : ""} onClick={() => setView("dashboard")}><Mark name="grid"/>대시보드</button>
          <button className={view === "portfolio" ? "active" : ""} onClick={() => setView("portfolio")}><Mark name="wallet"/>포트폴리오</button>
          <button className={view === "daily-buy" ? "active" : ""} onClick={() => setView("daily-buy")}><Mark name="repeat"/>매일모으기</button>
          <button className={view === "scenarios" ? "active" : ""} onClick={() => setView("scenarios")}><Mark name="bookmark"/>시나리오</button>
        </nav>
        <div className="sidebar-note"><span className="status-dot"/>로컬 전용<br/><small>주문 기능 없음</small></div>
      </aside>
      <main>
        <header className="topbar">
          <div><p className="eyebrow">MY INVESTMENT ROUTINE</p><h1>{view === "dashboard" ? "자산 대시보드" : view === "portfolio" ? "통합 포트폴리오" : view === "daily-buy" ? "매일모으기" : "저장된 시나리오"}</h1></div>
          <div className="asof"><span>가격 기준</span><b>{portfolio?.asOf ? new Date(portfolio.asOf).toLocaleString("ko-KR") : "데이터 없음"}</b><button onClick={() => void loadPortfolio(true)}>현재가·환율 새로고침</button></div>
        </header>
        {error && <div className="alert" role="alert">{error}<button onClick={() => setError("")} aria-label="닫기">×</button></div>}
        {!portfolio ? <Loading/> : portfolio.dataState === "EMPTY" ? (
          <ImportWorkspace file={file} preview={preview} busy={busy} onFile={(event) => setFile(event.target.files?.[0] ?? null)} onPreview={onPreview} onCommit={onCommit}/>
        ) : view === "dashboard" ? <Dashboard portfolio={portfolio}/> : view === "portfolio" ? <PortfolioView portfolio={portfolio}/> : view === "daily-buy" ? <DailyBuy portfolio={portfolio} onReload={loadPortfolio} onError={setError}/> : <Scenarios onError={setError}/>}
      </main>
      <nav className="mobile-nav" aria-label="모바일 메뉴">
        <button className={view === "dashboard" ? "active" : ""} onClick={() => setView("dashboard")}><Mark name="grid"/><span>대시보드</span></button>
        <button className={view === "portfolio" ? "active" : ""} onClick={() => setView("portfolio")}><Mark name="wallet"/><span>포트폴리오</span></button>
        <button className={view === "daily-buy" ? "active" : ""} onClick={() => setView("daily-buy")}><Mark name="repeat"/><span>매일모으기</span></button>
        <button className={view === "scenarios" ? "active" : ""} onClick={() => setView("scenarios")}><Mark name="bookmark"/><span>시나리오</span></button>
      </nav>
    </div>
  );
}

function Loading() { return <div className="loading"><span/><p>자산 정보를 불러오는 중입니다.</p></div>; }

function ImportWorkspace({ file, preview, busy, onFile, onPreview, onCommit }: { file: File | null; preview: ImportPreview | null; busy: boolean; onFile: (event: ChangeEvent<HTMLInputElement>) => void; onPreview: () => void; onCommit: () => void }) {
  return <section className="import-layout">
    <div className="import-card">
      <p className="eyebrow">FIRST STEP</p><h2>내 포트폴리오 불러오기</h2>
      <p>두 증권사의 ETF가 담긴 Excel을 먼저 검증합니다. 파일은 로컬 API에서 처리되며 원본 경로는 저장하지 않습니다.</p>
      <label className="dropzone"><input type="file" accept=".xlsx" onChange={onFile}/><span className="upload-icon">↑</span><b>{file?.name ?? "Excel 파일을 선택하세요"}</b><small>.xlsx · 최대 20MB</small></label>
      <button className="primary" disabled={!file || busy} onClick={onPreview}>{busy ? "검증 중…" : "가져오기 미리보기"}</button>
    </div>
    <div className="preview-card">
      <div className="preview-head"><span>IMPORT PREVIEW</span><b className={preview?.status === "BLOCKED" ? "bad" : "good"}>{preview?.status ?? "대기"}</b></div>
      {!preview ? <div className="empty-preview"><span>63</span><p>행 수, 구분선, 합계와 중복 종목을<br/>확정 전에 검증합니다.</p></div> : <>
        <div className="preview-stats"><div><span>원본 Position</span><b>{preview.summary.position_count}</b></div><div><span>통합 ETF</span><b>{preview.summary.unique_ticker_count}</b></div><div><span>매일모으기</span><b>{krw(preview.summary.daily_contribution_krw)}</b></div></div>
        <p className="marker-check">구분선 {preview.marker_rows.join(", ")}행 확인</p>
        {preview.warnings.length > 0 && <div className="warning-list">{preview.warnings.slice(0, 4).map((warning, index) => <p key={`${warning.code}-${index}`}>{warning.severity === "BLOCKING" ? "확인 필요" : "주의"} · {warning.code}{warning.row_number ? ` (${warning.row_number}행)` : ""}</p>)}</div>}
        <button className="primary" disabled={preview.status !== "READY" || busy} onClick={onCommit}>검증 결과로 확정</button>
      </>}
    </div>
  </section>;
}

function Dashboard({ portfolio }: { portfolio: Portfolio }) {
  const top = portfolio.positions.slice(0, 5);
  const max = Math.max(...top.map((item) => Number(item.currentMarketValueUSD)), 1);
  return <>
    <section className="summary-grid">
      <article className="hero-card"><span>현재 총자산 · KRW</span><strong>{krw(portfolio.summary.totalMarketValueKRW)}</strong><p>{usd(portfolio.summary.totalMarketValueUSD)} · {portfolio.disclaimer}</p><div className="hero-metrics"><div><small>총 매입원금</small><b>{usd(portfolio.summary.totalCostBasisUSD)}</b></div><div><small>평가손익</small><b className={Number(portfolio.summary.totalProfitUSD) >= 0 ? "gain" : "loss"}>{usd(portfolio.summary.totalProfitUSD)}</b></div><div><small>수익률</small><b className={Number(portfolio.summary.totalProfitUSD) >= 0 ? "gain" : "loss"}>{pct(portfolio.summary.totalReturnPercent)}</b></div></div></article>
      <article className="metric-card"><span>매일모으기</span><strong>{krw(portfolio.summary.dailyContributionKRW)}</strong><p>미국시장 거래일 기준 · 월평균 {krw(portfolio.summary.monthlyAverageContributionKRW)}</p><div className="metric-foot"><span>연 예상 투자금</span><b>{krw(portfolio.summary.annualContributionKRW)}</b></div></article>
      <article className={`metric-card fx-card ${portfolio.fx.status.toLowerCase()}`}><span>USD/KRW</span><strong>{portfolio.fx.rate ? `₩${Number(portfolio.fx.rate).toLocaleString("ko-KR", {maximumFractionDigits: 2})}` : "조회 실패"}</strong><p>{portfolio.fx.message}</p><div className="metric-foot"><span>{portfolio.fx.provider ?? "provider 없음"}</span><b>{portfolio.fx.asOf ? new Date(portfolio.fx.asOf).toLocaleTimeString("ko-KR") : "—"}</b></div></article>
    </section>
    <section className="dashboard-grid">
      <article className="panel allocation"><div className="panel-title"><div><p className="eyebrow">CURRENT ALLOCATION</p><h2>가장 큰 자산</h2></div><button className="text-button">평가금액순</button></div><div className="bars">{top.map((item, index) => <div className="bar-row" key={item.instrument.ticker}><span className="rank">{String(index + 1).padStart(2, "0")}</span><b>{item.instrument.ticker}</b><div className="bar-track"><i style={{width: `${Number(item.currentMarketValueUSD) / max * 100}%`}}/></div><span>{usd(item.currentMarketValueUSD, 0)}</span></div>)}</div></article>
      <article className="panel future-panel"><p className="eyebrow">DATA PRINCIPLE</p><h2>가격상승이 아닌 Total Return</h2><div className="future-axis"><span>현재</span><i/><span>10년</span></div><p>배당·분배금과 분할이 반영된 조정 시계열만 장기 계산에 사용합니다. 역사가 짧은 ETF는 자동 대체하지 않습니다.</p><div className="future-lock">미래 수익과 분배금을 보장하지 않습니다</div></article>
    </section>
    <MonteCarloPanel/>
    <ProjectionPanel/>
    <PerformancePanel/>
    <Motivation portfolio={portfolio}/>
  </>;
}

function ProjectionPanel({ refreshKey = "", dataOverride = null, title = "지금처럼 계속 투자한다면" }: { refreshKey?: string; dataOverride?: Projection | null; title?: string }) {
  const [fetchedData, setFetchedData] = useState<Projection | null>(null);
  useEffect(() => {
    if (dataOverride) return;
    api<Projection>("/api/projection").then(setFetchedData).catch((reason) => setFetchedData({status: "UNAVAILABLE", message: reason.message}));
  }, [refreshKey, dataOverride]);
  const data = dataOverride ?? fetchedData;
  if (!data || data.status !== "READY" || !data.horizons) return <article className="panel projection-panel"><p className="eyebrow">HISTORICAL-RETURN PROJECTION</p><h2>미래자산 계산 준비 중</h2><p>{data?.message ?? "10년 Total Return 데이터를 불러오고 있습니다."}</p></article>;
  const periods = ["1Y", "3Y", "5Y", "7Y", "10Y"] as const;
  const ten = data.horizons["10Y"];
  return <article className="panel projection-panel"><div className="panel-title"><div><p className="eyebrow">HISTORICAL-RETURN PROJECTION</p><h2>{title}</h2></div><span className="coverage">{data.dataStatus === "STALE" ? "STALE · " : ""}데이터 커버리지 {Number(data.dataCoveragePercent).toFixed(1)}%</span></div><div className="period-cards">{periods.map((period) => <div key={period} className={period === "10Y" ? "focus" : ""}><span>{period.replace("Y", "년")}</span><b>{krw(data.horizons![period].endingValueKRW)}</b><small>{usd(data.horizons![period].endingValueUSD)} · {data.horizons![period].tradingDays.toLocaleString("ko-KR")} 거래일</small></div>)}</div><div className="projection-breakdown"><span><small>현재 보유자산</small><b>{krw(ten.startingValueKRW)}</b></span><i>+</i><span><small>10년 추가 납입</small><b>{krw(ten.addedPrincipalKRW)}</b></span><i>+</i><span><small>투자수익</small><b className={Number(ten.investmentGainKRW) >= 0 ? "gain" : "loss"}>{krw(ten.investmentGainKRW)}</b></span><i>=</i><span><small>10년 최종자산</small><b>{krw(ten.endingValueKRW)}</b></span></div><FutureValueTable horizons={data.horizons}/>{data.wealthTimeline && <WealthGrowth timeline={data.wealthTimeline}/>} {data.milestones && <Milestones items={data.milestones}/>} {data.income && <FutureIncome income={data.income}/>}<p className="data-note">{data.disclaimer}</p></article>;
}

function FutureValueTable({ horizons }: { horizons: NonNullable<Projection["horizons"]> }) {
  const periods = ["1Y", "3Y", "5Y", "7Y", "10Y"] as const;
  const tickers = horizons["10Y"].positions.map((item) => item.ticker);
  const byPeriod = Object.fromEntries(periods.map((period) => [period, new Map(horizons[period].positions.map((item) => [item.ticker, item]))])) as Record<typeof periods[number], Map<string, ProjectionPosition>>;
  return <section className="future-value-block"><div className="subhead"><div><p className="eyebrow">ETF FUTURE VALUE</p><h3>종목별 미래 예상금액</h3></div><small>현재 환율 유지 · Total Return 재투자</small></div><div className="future-value-table"><table><thead><tr><th>ETF</th><th>현재</th>{periods.map((period) => <th key={period}>{period}</th>)}</tr></thead><tbody>{tickers.map((ticker) => <tr key={ticker}><td><b>{ticker}</b></td><td>{krw(byPeriod["1Y"].get(ticker)?.startingValueKRW ?? null)}</td>{periods.map((period) => <td key={period}>{krw(byPeriod[period].get(ticker)?.endingValueKRW ?? null)}</td>)}</tr>)}</tbody></table></div></section>;
}

function MonteCarloPanel() {
  const [data, setData] = useState<MonteCarlo | null>(null);
  useEffect(() => {
    api<MonteCarlo>("/api/projection/monte-carlo?runs=1000&blockDays=20&seed=20260912")
      .then(setData)
      .catch((reason) => setData({status: "UNAVAILABLE", message: reason.message}));
  }, []);
  if (!data) return <article className="panel monte-panel"><p className="eyebrow">MONTE CARLO · BLOCK BOOTSTRAP</p><h2>확률적 예상범위를 계산 중입니다</h2><p>1,000개 경로를 별도 계산하며 다른 화면 조작은 계속할 수 있습니다.</p></article>;
  if (data.status !== "READY" || !data.horizons) return <article className="panel monte-panel"><p className="eyebrow">MONTE CARLO · BLOCK BOOTSTRAP</p><h2>확률적 예상범위 데이터 없음</h2><p>{data.message ?? "공통 Total Return 이력이 부족합니다."}</p></article>;
  const ten = data.horizons["10Y"].percentiles;
  return <article className="panel monte-panel"><div className="panel-title"><div><p className="eyebrow">MONTE CARLO · BLOCK BOOTSTRAP</p><h2>10년 후 예상 범위</h2></div><span className="coverage">{data.runs?.toLocaleString("ko-KR")}회 · {data.blockDays}거래일 block · seed {data.seed}</span></div><div className="monte-range"><div><span>보수적 · P25</span><b>{krw(ten.P25.endingValueKRW)}</b></div><div className="median"><span>기준 · Median P50</span><strong>{krw(ten.P50.endingValueKRW)}</strong><small>{usd(ten.P50.endingValueUSD)}</small></div><div><span>낙관적 · P75</span><b>{krw(ten.P75.endingValueKRW)}</b></div></div><div className="monte-horizons">{(["1Y", "3Y", "5Y", "7Y", "10Y"] as const).map((period) => <span key={period}><small>{period} Median</small><b>{krw(data.horizons![period].percentiles.P50.endingValueKRW)}</b></span>)}</div>{data.warnings?.includes("SHORT_COMMON_HISTORY") && <div className="warning-note">주의 · 여러 ETF가 함께 겹치는 과거 이력이 3년 미만이어서 10년 확률분포의 신뢰도가 낮습니다.</div>}<p className="data-note">자산 데이터 커버리지 {Number(data.dataCoveragePercent).toFixed(1)}% · 공통 과거 거래일 {data.historicalObservationCount?.toLocaleString("ko-KR")}개<br/>{data.disclaimer}</p></article>;
}

function WealthGrowth({ timeline }: { timeline: NonNullable<Projection["wealthTimeline"]> }) {
  const width = 900, height = 260;
  const endings = timeline.map((point) => Number(point.endingValueKRW));
  const principals = timeline.map((point) => Number(point.startingValueKRW) + Number(point.addedPrincipalKRW));
  const max = Math.max(1, ...endings, ...principals);
  const line = (values: number[]) => values.map((value, index) => `${index * width / Math.max(1, values.length - 1)},${height - value / max * height}`).join(" ");
  return <section className="wealth-block"><div className="subhead"><div><p className="eyebrow">WEALTH GROWTH</p><h3>내가 넣은 돈과 투자수익</h3></div><div className="chart-legend"><span className="total">총자산</span><span className="principal">현재자산 + 추가원금</span></div></div><div className="wealth-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="10년 자산성장 추이"><polyline className="principal-line" points={line(principals)}/><polyline className="total-line" points={line(endings)}/></svg><div><span>{timeline[0]?.date}</span><span>{timeline[timeline.length - 1]?.date}</span></div></div></section>;
}

function Milestones({ items }: { items: NonNullable<Projection["milestones"]> }) {
  return <section className="milestone-block"><div className="subhead"><div><p className="eyebrow">ASSET MILESTONES</p><h3>목표자산 예상 도달일</h3></div><small>미국 실제 거래일 · 현재 환율 유지 가정</small></div><div className="milestone-list">{items.map((item) => <div key={item.targetKRW}><b>{krw(item.targetKRW)}</b><span>{item.reachedDate ? new Date(`${item.reachedDate}T00:00:00`).toLocaleDateString("ko-KR", {year: "numeric", month: "long", day: "numeric"}) : "10년 내 미도달"}</span></div>)}</div></section>;
}

function FutureIncome({ income }: { income: NonNullable<Projection["income"]> }) {
  if (income.status !== "READY") return <section className="income-block"><p className="eyebrow">FUTURE DISTRIBUTION</p><h3>배당/분배금 데이터 없음</h3></section>;
  const points: [string, IncomePoint][] = [["현재", income.current], ...(["1Y", "3Y", "5Y", "7Y", "10Y"] as const).map((period) => [period.replace("Y", "년"), income.horizons[period]] as [string, IncomePoint])];
  return <section className="income-block"><div className="subhead"><div><p className="eyebrow">FUTURE DISTRIBUTION</p><h3>예상 배당/분배금 현금흐름</h3></div><small>{income.dataStatus === "STALE" ? "STALE · " : ""}데이터 커버리지 {Number(income.dataCoveragePercent).toFixed(1)}%</small></div><div className="income-list">{points.map(([label, point]) => <div key={label}><span>{label}</span><small>연간</small><b>{krw(point.annualDistributionKRW)}</b><small>월평균 {krw(point.monthlyAverageDistributionKRW)}</small></div>)}</div><p className="data-note">{income.disclaimer}</p></section>;
}

function PerformancePanel() {
  const [range, setRange] = useState(5);
  const [data, setData] = useState<Performance | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setLoading(true);
    api<Performance>(`/api/performance?lookbackYears=${range}`)
      .then(setData)
      .catch((reason) => setData({status: "UNAVAILABLE", lookbackYears: range, seriesKind: null, points: [], warnings: ["TOTAL_RETURN_UNAVAILABLE"], message: reason.message}))
      .finally(() => setLoading(false));
  }, [range]);
  const values = data?.points.map((point) => Number(point.cumulativeReturnPercent)) ?? [];
  const width = 760, height = 220;
  const min = Math.min(0, ...values), max = Math.max(1, ...values);
  const polyline = values.map((value, index) => `${index * width / Math.max(1, values.length - 1)},${height - (value - min) / Math.max(1, max - min) * height}`).join(" ");
  return <article className="panel performance-panel"><div className="panel-title"><div><p className="eyebrow">CUMULATIVE TOTAL RETURN</p><h2>실제 포트폴리오 수익률 추이</h2></div><div className="range-tabs">{[1,3,5,10].map((year) => <button key={year} className={range === year ? "active" : ""} onClick={() => setRange(year)}>{year}Y</button>)}</div></div>{loading ? <div className="performance-empty">Total Return을 불러오는 중입니다.</div> : data?.status === "READY" ? <div className="performance-layout"><div className="performance-metrics"><span>누적 Total Return</span><strong className={Number(data.totalReturnPercent) >= 0 ? "gain" : "loss"}>{pct(data.totalReturnPercent ?? null)}</strong><small>연환산 {pct(data.cagrPercent ?? null)}</small><div><span>최대 낙폭</span><b className="loss">{pct(data.maxDrawdownPercent ?? null)}</b></div><div><span>자산 커버리지</span><b>{Number(data.dataCoveragePercent).toFixed(1)}%</b></div></div><div className="performance-chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${range}년 포트폴리오 Total Return 그래프`}><polyline points={polyline}/></svg><div><span>{data.coverageStart}</span><span>{data.coverageEnd}</span></div></div></div> : <div className="performance-empty"><b>수익률 시계열을 표시할 수 없습니다.</b><span>{data?.message ?? "데이터 없음"}</span></div>}<p className="data-note">{data?.disclaimer ?? "데이터가 없을 때 임의 수익률을 만들지 않습니다."}</p></article>;
}

function Motivation({ portfolio }: { portfolio: Portfolio }) { return <section className="motivation"><div><p className="eyebrow">KEEP THE ROUTINE</p><h2>오늘의 꾸준함이 장기 자산의 출발점입니다.</h2></div><div className="motivation-numbers"><span><small>보유 ETF</small><b>{portfolio.summary.holdingCount}개</b></span><span><small>투자 대상</small><b>{portfolio.summary.investmentTargetCount}개</b></span><span><small>연간 추가 원금</small><b>{krw(portfolio.summary.annualContributionKRW)}</b></span></div></section>; }

function PortfolioView({ portfolio }: { portfolio: Portfolio }) {
  const [allocation, setAllocation] = useState<Allocation | null>(null);
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  useEffect(() => {
    api<Allocation>("/api/allocation").then(setAllocation).catch((reason) => setAllocation({status: "UNAVAILABLE", message: reason.message}));
  }, [portfolio.asOf, portfolio.summary.dailyContributionKRW]);
  return <><AllocationPanel data={allocation}/><Holdings portfolio={portfolio} onSelect={setSelectedTicker}/>{selectedTicker && <ETFDetailModal ticker={selectedTicker} onClose={() => setSelectedTicker(null)}/>}</>;
}

function AllocationPanel({ data }: { data: Allocation | null }) {
  if (!data || data.status !== "READY" || !data.categories) return <section className="panel allocation-compare"><p className="eyebrow">ALLOCATION</p><h2>현재 vs 10년 예상 비중</h2><p>{data?.message ?? "비중을 계산 중입니다."}</p></section>;
  const rows = data.categories;
  return <section className="panel allocation-compare"><div className="panel-title"><div><p className="eyebrow">CATEGORY ALLOCATION</p><h2>현재 vs 10년 예상 비중</h2></div><span className="coverage">Projection 커버리지 {Number(data.dataCoveragePercent).toFixed(1)}%</span></div><div className="allocation-rows">{rows.map((row) => <div key={row.category}><b>{row.category}</b><div className="allocation-track"><i className="current" style={{width: `${Math.min(100, Number(row.currentPercent))}%`}}/><i className="projected" style={{width: `${Math.min(100, Number(row.projected10YPercent))}%`}}/></div><span><small>현재 {Number(row.currentPercent).toFixed(1)}%</small><small>10년 {Number(row.projected10YPercent).toFixed(1)}%</small></span></div>)}</div><p className="data-note">{data.disclaimer}</p></section>;
}

function ETFDetailModal({ ticker, onClose }: { ticker: string; onClose: () => void }) {
  const [detail, setDetail] = useState<ETFDetail | null>(null);
  useEffect(() => {
    api<ETFDetail>(`/api/etf/${ticker}`).then(setDetail);
  }, [ticker]);
  const periods = ["1Y", "3Y", "5Y", "7Y", "10Y"];
  return <div className="modal-backdrop" role="presentation" onClick={onClose}><section className="etf-modal" role="dialog" aria-modal="true" aria-label={`${ticker} 상세`} onClick={(event) => event.stopPropagation()}><button className="modal-close" onClick={onClose} aria-label="닫기">×</button>{!detail ? <div className="performance-empty">{ticker} 데이터를 불러오는 중입니다.</div> : <><div className="detail-head"><div><p className="eyebrow">ETF DETAIL</p><h2>{ticker}</h2><span>{detail.position.category} · Portfolio {Number(detail.portfolioWeightPercent).toFixed(2)}%</span></div><div><small>평가금액</small><b>{krw(detail.position.currentMarketValueKRW)}</b><span className={Number(detail.position.currentProfitUSD) >= 0 ? "gain" : "loss"}>{pct(detail.position.currentReturnPercent)}</span></div></div><div className="detail-grid"><span><small>보유수량</small><b>{Number(detail.position.quantity).toLocaleString("ko-KR", {maximumFractionDigits: 6})}</b></span><span><small>평균매입가</small><b>{usd(detail.position.averageCostPerShareUSD, 4)}</b></span><span><small>현재가</small><b>{usd(detail.position.latestCurrentPriceUSD, 4)}</b></span><span><small>매일모으기</small><b>{krw(detail.position.effectiveContributionKRW)}</b></span></div><div className="freshness-grid"><span><b>현재가</b>{detail.position.priceStatus} · {detail.position.priceProvider ?? "provider 없음"}<small>{detail.position.priceAsOf ? new Date(detail.position.priceAsOf).toLocaleString("ko-KR") : "조회시각 없음"}</small></span><span><b>Total Return</b>{detail.history.status === "READY" ? `${detail.history.stale ? "STALE" : "LIVE"} · ${detail.history.provider ?? "provider 없음"}` : "UNAVAILABLE"}<small>{detail.history.asOf ? new Date(detail.history.asOf).toLocaleString("ko-KR") : "조회시각 없음"}</small></span><span><b>Distribution</b>{detail.distribution.status === "READY" ? `${detail.distribution.stale ? "STALE" : "LIVE"} · ${detail.distribution.provider ?? "provider 없음"}` : "UNAVAILABLE"}<small>{detail.distribution.asOf ? new Date(detail.distribution.asOf).toLocaleString("ko-KR") : "조회시각 없음"}</small></span></div>{detail.history.status === "READY" ? <div className="detail-metrics"><span><small>1Y Total Return</small><b>{pct(detail.history.return1YPercent ?? null)}</b></span><span><small>3Y CAGR</small><b>{pct(detail.history.cagr3YPercent ?? null)}</b></span><span><small>5Y CAGR</small><b>{pct(detail.history.cagr5YPercent ?? null)}</b></span><span><small>10Y CAGR</small><b>{pct(detail.history.cagr10YPercent ?? null)}</b></span><span><small>연환산 변동성</small><b>{pct(detail.history.annualizedVolatilityPercent ?? null)}</b></span><span><small>최대 낙폭</small><b className="loss">{pct(detail.history.maxDrawdownPercent ?? null)}</b></span></div> : <div className="warning-note">Total Return history가 없습니다.</div>}{detail.distribution.status === "READY" && <div className="detail-distribution"><b>최근 12개월 배당/분배금</b><span>주당 {usd(detail.distribution.trailing12MPerShareUSD ?? null, 4)} · Yield {pct(detail.distribution.trailingYieldPercent ?? null)}</span><small>구성정보가 없는 지급금은 Distribution으로만 표시합니다.</small></div>}{detail.projection.status === "READY" && <div className="detail-future">{periods.map((period) => <span key={period}><small>{period}</small><b>{krw(detail.projection.horizons[period].endingValueKRW)}</b><em>연 분배금 {krw(detail.projection.horizons[period].annualDistributionKRW)}</em></span>)}</div>}<p className="data-note">{detail.disclaimer}</p></>}</section></div>;
}

function Holdings({ portfolio, onSelect }: { portfolio: Portfolio; onSelect: (ticker: string) => void }) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("ALL");
  const [sort, setSort] = useState("value");
  const categories = useMemo(() => [...new Set(portfolio.positions.map((item) => item.category))].sort(), [portfolio.positions]);
  const filtered = useMemo(() => portfolio.positions.filter((item) => (item.instrument.ticker.includes(query.trim().toUpperCase()) || item.category.includes(query.trim())) && (category === "ALL" || item.category === category)).sort((a, b) => sort === "ticker" ? a.instrument.ticker.localeCompare(b.instrument.ticker) : Number(sort === "return" ? b.currentReturnPercent : sort === "cost" ? b.totalCostBasisUSD : sort === "daily" ? b.effectiveContributionKRW : b.currentMarketValueUSD) - Number(sort === "return" ? a.currentReturnPercent : sort === "cost" ? a.totalCostBasisUSD : sort === "daily" ? a.effectiveContributionKRW : a.currentMarketValueUSD)), [portfolio.positions, query, category, sort]);
  return <section className="panel table-panel"><div className="panel-title"><div><p className="eyebrow">CONSOLIDATED POSITIONS</p><h2>통합 보유종목</h2></div><div className="table-controls"><label className="search"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ticker 또는 분류 검색"/></label><select value={category} onChange={(event) => setCategory(event.target.value)} aria-label="분류 필터"><option value="ALL">전체 분류</option>{categories.map((name) => <option key={name}>{name}</option>)}</select><select value={sort} onChange={(event) => setSort(event.target.value)} aria-label="정렬"><option value="value">평가금액순</option><option value="return">수익률순</option><option value="cost">매입원금순</option><option value="daily">매일모으기순</option><option value="ticker">Ticker순</option></select></div></div><div className="table-wrap"><table><thead><tr><th>종목</th><th>분류</th><th>수량</th><th>평균매입가</th><th>현재가</th><th>매입원금</th><th>평가금액</th><th>평가손익</th><th>수익률</th><th>매일모으기</th><th>가격 상태</th></tr></thead><tbody>{filtered.map((item) => <tr key={item.instrument.ticker} role="button" tabIndex={0} onClick={() => onSelect(item.instrument.ticker)} onKeyDown={(event) => {if (event.key === "Enter" || event.key === " ") onSelect(item.instrument.ticker);}}><td><b>{item.instrument.ticker}</b><small>{item.instrument.market} · {item.instrument.assetType} · {item.sourceCount}개 계좌행</small></td><td><span className="category">{item.category}</span></td><td>{Number(item.quantity).toLocaleString("ko-KR", {maximumFractionDigits: 6})}</td><td>{usd(item.averageCostPerShareUSD, 4)}</td><td>{usd(item.latestCurrentPriceUSD, 4)}</td><td>{usd(item.totalCostBasisUSD)}</td><td>{usd(item.currentMarketValueUSD)}<small className="krw-sub">{krw(item.currentMarketValueKRW)}</small></td><td className={Number(item.currentProfitUSD) >= 0 ? "gain" : "loss"}>{usd(item.currentProfitUSD)}</td><td className={Number(item.currentProfitUSD) >= 0 ? "gain" : "loss"}>{pct(item.currentReturnPercent)}</td><td>{krw(item.effectiveContributionKRW)}</td><td><span className={`freshness ${item.priceStatus.toLowerCase()}`}>{item.priceStatus}</span></td></tr>)}</tbody></table></div></section>;
}

function DailyBuy({ portfolio, onReload, onError }: { portfolio: Portfolio; onReload: () => Promise<void>; onError: (value: string) => void }) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [enabledDrafts, setEnabledDrafts] = useState<Record<string, boolean>>({});
  const [comparison, setComparison] = useState<ScenarioComparison | null>(null);
  const [newTicker, setNewTicker] = useState("");
  const [newAmount, setNewAmount] = useState("3000");
  const [adding, setAdding] = useState(false);
  const [scenarioName, setScenarioName] = useState("");
  const [savingScenario, setSavingScenario] = useState(false);
  const changes = useMemo(() => portfolio.positions.filter((item) => item.instrument.ticker in drafts || item.instrument.ticker in enabledDrafts).map((item) => ({ticker: item.instrument.ticker, configuredAmountKRW: drafts[item.instrument.ticker] || item.configuredContributionKRW, enabled: enabledDrafts[item.instrument.ticker] ?? item.dailyBuyEnabled})), [portfolio.positions, drafts, enabledDrafts]);
  const changesKey = JSON.stringify(changes);
  useEffect(() => {
    if (!changes.length) { setComparison(null); return; }
    const timer = window.setTimeout(() => {
      api<ScenarioComparison>("/api/projection/compare", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({changes})}).then(setComparison).catch((reason) => onError(reason.message));
    }, 200);
    return () => window.clearTimeout(timer);
  }, [changesKey]);
  const save = async (item: Position) => {
    const ticker = item.instrument.ticker;
    const amount = drafts[ticker] || item.configuredContributionKRW;
    const enabled = enabledDrafts[ticker] ?? item.dailyBuyEnabled;
    try {
      await api(`/api/daily-buy/${ticker}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({configuredAmountKRW: amount, enabled})});
      setDrafts((current) => { const next = {...current}; delete next[ticker]; return next; });
      setEnabledDrafts((current) => { const next = {...current}; delete next[ticker]; return next; });
      await onReload();
    } catch (reason) { onError(reason instanceof Error ? reason.message : "저장하지 못했습니다."); }
  };
  const addTarget = async () => {
    if (!newTicker.trim()) return;
    setAdding(true);
    try {
      await api(`/api/daily-buy/${newTicker.trim().toUpperCase()}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({configuredAmountKRW: newAmount, enabled: true})});
      setNewTicker("");
      await onReload();
    } catch (reason) { onError(reason instanceof Error ? reason.message : "투자 대상을 추가하지 못했습니다."); }
    finally { setAdding(false); }
  };
  const saveScenario = async () => {
    if (!scenarioName.trim() || !changes.length) return;
    setSavingScenario(true);
    try {
      await api<SavedScenario>("/api/scenarios", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: scenarioName.trim(), changes})});
      setScenarioName("");
    } catch (reason) { onError(reason instanceof Error ? reason.message : "시나리오를 저장하지 못했습니다."); }
    finally { setSavingScenario(false); }
  };
  const periods = ["1Y", "3Y", "5Y", "7Y", "10Y"] as const;
  return <section className="daily-layout"><div className="daily-summary"><div><p className="eyebrow">BASE STRATEGY</p><h2>{krw(portfolio.summary.dailyContributionKRW)} <small>/ 거래일</small></h2></div><div><span>월평균 {krw(portfolio.summary.monthlyAverageContributionKRW)}</span><span>연 예상 투자금</span><b>{krw(portfolio.summary.annualContributionKRW)}</b></div></div>{comparison?.status === "READY" && comparison.deltas && <article className="scenario-preview"><div><p className="eyebrow">WORKING SCENARIO · 즉시 미리보기</p><h2>Base {krw(comparison.baseDailyContributionKRW ?? null)} → Modified {krw(comparison.modifiedDailyContributionKRW ?? null)}</h2></div><div className="scenario-deltas">{periods.map((period) => <span key={period}><small>{period} 차이</small><b className={Number(comparison.deltas![period].deltaKRW) >= 0 ? "gain" : "loss"}>{krw(comparison.deltas![period].deltaKRW)}</b></span>)}</div><div className="scenario-detail"><span>10년 추가원금 차이 <b>{krw(comparison.deltas["10Y"].addedPrincipalDeltaKRW)}</b></span><span>10년 투자수익 차이 <b>{krw(comparison.deltas["10Y"].investmentGainDeltaKRW)}</b></span><span>10년 후 연간 분배금 차이 <b>{krw(comparison.deltas["10Y"].annualDistributionDeltaKRW)}</b></span></div><div className="scenario-save"><input value={scenarioName} maxLength={80} onChange={(event) => setScenarioName(event.target.value)} placeholder="예: QQQ 강화" aria-label="시나리오 이름"/><button disabled={!scenarioName.trim() || savingScenario} onClick={() => void saveScenario()}>{savingScenario ? "저장 중…" : "이 전략 저장"}</button></div></article>}<ProjectionPanel refreshKey={portfolio.summary.dailyContributionKRW} dataOverride={comparison?.modifiedProjection} title={comparison?.modifiedProjection ? "수정 중인 전략으로 계속 투자한다면" : "현재 저장된 전략으로 계속 투자한다면"}/><div className="add-target"><div><p className="eyebrow">ADD INVESTMENT TARGET</p><h2>새 ETF 매일모으기</h2><small>보유수량이 0이어도 투자 대상으로 추가할 수 있습니다.</small></div><label><span>Ticker</span><input value={newTicker} maxLength={32} onChange={(event) => setNewTicker(event.target.value.toUpperCase().replace(/[^A-Z0-9.\-]/g, ""))} placeholder="VT"/></label><label><span>거래일 금액</span><input inputMode="numeric" value={newAmount} onChange={(event) => setNewAmount(event.target.value.replace(/[^0-9]/g, ""))}/></label><button className="primary" disabled={adding || !newTicker || Number(newAmount) <= 0} onClick={addTarget}>{adding ? "추가 중…" : "ETF 추가"}</button></div><div className="daily-list">{portfolio.positions.map((item) => {const ticker = item.instrument.ticker; const enabled = enabledDrafts[ticker] ?? item.dailyBuyEnabled; const changed = ticker in drafts || ticker in enabledDrafts; return <article key={ticker}><div className="ticker-cell"><span>{ticker.slice(0, 2)}</span><div><b>{ticker}</b><small>{item.category}</small></div></div><div className="preset-row">{[0, 1000, 2000, 3000, 5000, 10000].map((amount) => <button key={amount} onClick={() => setDrafts({...drafts, [ticker]: String(amount)})}>{amount === 0 ? "0" : `${amount / 1000}천`}</button>)}</div><label className="amount-input"><input inputMode="numeric" value={drafts[ticker] ?? item.configuredContributionKRW} onChange={(event) => setDrafts({...drafts, [ticker]: event.target.value.replace(/[^0-9]/g, "")})}/><span>원</span></label><label className="switch"><input type="checkbox" checked={enabled} onChange={(event) => setEnabledDrafts({...enabledDrafts, [ticker]: event.target.checked})}/><span/><em>{enabled ? "ON" : "OFF"}</em></label><button className="save-button" disabled={!changed} onClick={() => void save(item)}>{changed ? "저장" : "저장됨"}</button></article>})}</div></section>;
}

function Scenarios({ onError }: { onError: (value: string) => void }) {
  const [items, setItems] = useState<SavedScenario[]>([]);
  const [comparison, setComparison] = useState<ScenarioComparison | null>(null);
  const load = useCallback(() => api<{status: string; scenarios: SavedScenario[]}>("/api/scenarios").then((result) => setItems(result.scenarios)).catch((reason) => onError(reason.message)), [onError]);
  useEffect(() => { void load(); }, [load]);
  const compare = async (item: SavedScenario) => {
    try { setComparison(await api<ScenarioComparison>(`/api/scenarios/${item.id}/comparison`)); }
    catch (reason) { onError(reason instanceof Error ? reason.message : "시나리오를 비교하지 못했습니다."); }
  };
  const remove = async (item: SavedScenario) => {
    if (!window.confirm(`'${item.name}' 시나리오를 삭제할까요?`)) return;
    try { await api(`/api/scenarios/${item.id}`, {method: "DELETE"}); setComparison(null); await load(); }
    catch (reason) { onError(reason instanceof Error ? reason.message : "시나리오를 삭제하지 못했습니다."); }
  };
  const periods = ["1Y", "3Y", "5Y", "7Y", "10Y"] as const;
  return <section className="scenario-library"><div className="scenario-intro"><div><p className="eyebrow">SAVED STRATEGIES</p><h2>투자 전략을 저장하고 현재 전략과 비교합니다</h2><p>매일모으기 화면에서 금액을 바꾼 뒤 이름을 붙여 저장할 수 있습니다.</p></div><b>{items.length}개 저장됨</b></div>{items.length === 0 ? <div className="scenario-empty"><b>저장된 시나리오가 없습니다.</b><span>매일모으기 금액을 수정하면 Working Scenario 아래에 저장 버튼이 나타납니다.</span></div> : <div className="scenario-library-grid"><div className="scenario-cards">{items.map((item) => <article key={item.id}><div><small>SAVED SCENARIO</small><h3>{item.name}</h3><span>{krw(item.dailyContributionKRW)} / 거래일</span><em>연 {krw(item.annualContributionKRW)}</em></div><div><button onClick={() => void compare(item)}>현재 전략과 비교</button><button className="danger-text" onClick={() => void remove(item)}>삭제</button></div></article>)}</div><div className="scenario-result">{comparison?.status === "READY" && comparison.deltas ? <><p className="eyebrow">BASE VS SAVED</p><h2>{comparison.scenario?.name}</h2><p>Base {krw(comparison.baseDailyContributionKRW ?? null)} → Saved {krw(comparison.modifiedDailyContributionKRW ?? null)}</p><div>{periods.map((period) => <span key={period}><small>{period}</small><b className={Number(comparison.deltas![period].deltaKRW) >= 0 ? "gain" : "loss"}>{krw(comparison.deltas![period].deltaKRW)}</b><em>{pct(comparison.deltas![period].deltaPercent)}</em></span>)}</div><footer><span>10년 추가원금 차이 <b>{krw(comparison.deltas["10Y"].addedPrincipalDeltaKRW)}</b></span><span>10년 투자수익 차이 <b>{krw(comparison.deltas["10Y"].investmentGainDeltaKRW)}</b></span><span>10년 후 연 분배금 차이 <b>{krw(comparison.deltas["10Y"].annualDistributionDeltaKRW)}</b></span></footer></> : <div className="scenario-empty"><b>비교할 전략을 선택하세요.</b><span>미래 금액은 현재 시장데이터와 환율을 기준으로 다시 계산됩니다.</span></div>}</div></div>}</section>;
}

export default App;
