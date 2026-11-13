# Recurring exposure classification

The recurring report and allocation policy group each plan into one exposure
group. The mapping is a hand-maintained table in
`deploy/cloudflare-js/recurring-report.mjs`, mirrored in
`src/account_bot/reports/formatter.py`.

## Additions on 2026-11-13

Source: Yahoo Finance public search metadata (`quoteType`, `sector`, `industry`,
fund name), read on 2026-11-13. Equities use the provider's sector/industry.
The provider publishes no sector for funds, so funds are grouped by their
published name and type; that is a judgement, not provider data.

| Ticker | Provider data | Group |
|---|---|---|
| CRWD, PANW | Technology / Software - Infrastructure | 소프트웨어 |
| AMAT | Technology / Semiconductor Equipment & Materials | 반도체 |
| COIN | Financial Services / Financial Data & Stock Exchanges | 금융 |
| LUMN | Communication Services / Telecom Services | 커뮤니케이션 |
| PBR.A | Energy / Oil & Gas Integrated | 에너지 |
| IEF, TLT | ETF, Treasury bond fund (by name) | 미국 국채 |
| JEPI, FEPI | ETF, equity premium income fund (by name) | 옵션인컴·배당 ETF |
| VIG | ETF, dividend appreciation index fund (by name) | 옵션인컴·배당 ETF |
| GRNY | ETF, US large-cap fund (by name) | 미국 광범위 지수 |

A fund still counts as a single group. Its holdings are not looked through, so
overlap with directly held stocks is not measured.
