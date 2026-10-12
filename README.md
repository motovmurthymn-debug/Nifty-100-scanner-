# Nifty 100 Multi-Timeframe Structure Scanner

Scans the Nifty 100 after the Indian market close and sends a short WhatsApp report for:

1. **Weekly + 1H**: weekly CHoCH/BOS bias and same-direction 1-hour CHoCH/BOS with an opposite-candle Order Block plus aggressive FVG.
2. **Monthly + 4H**: monthly CHoCH/BOS bias and same-direction 4-hour CHoCH/BOS with an Order Block plus aggressive FVG.

It uses completed candles only and runs from GitHub Actions (no always-on computer required).

## Important limitations

- This is a starter implementation, not a validated trading system. “External swing” is made repeatable through ATR-filtered confirmed pivots; that is an approximation and must be checked against your own chart examples.
- Yahoo Finance data via `yfinance` is unofficial and may be delayed, incomplete, rate-limited, or change without notice. Intraday history availability can vary. For a trading-grade system, use a licensed broker/data API.
- GitHub Actions scheduled jobs are best-effort, not guaranteed to start at an exact time. A run can be delayed. Do not interpret an incomplete-data result as “no setups.”
- The script is not financial advice and does not place trades. Verify each signal on your chart.
- Meta WhatsApp Cloud API requires a business setup and an approved message template for proactive daily messages. Message charges may apply. A free, personal WhatsApp account cannot simply be used as an official automated sender.

## 1. Create a GitHub repository

1. Sign in to GitHub and create a **private** repository, for example `nifty100-structure-scanner`.
2. Upload the files in this ZIP, preserving the `.github/workflows/daily-scan.yml` path.
3. Ensure the workflow is on the repository's default branch (usually `main`).

## 2. Configure WhatsApp Cloud API

Use Meta's official WhatsApp Business Platform / Cloud API:
https://developers.facebook.com/docs/whatsapp/cloud-api/overview

You will need:
- Meta developer account and a Meta business portfolio.
- WhatsApp Business Account and sender phone-number ID.
- Access token with permission to send messages.
- Recipient number in international format without `+` (India example format: `91XXXXXXXXXX`).
- An approved template, for example `nifty100_daily_scan`, language `en_US`, with body text like:

  `Nifty 100 daily structure scan:\n{{1}}`

The daily stock report is sent as the value of `{{1}}`. Template approval/category and pricing are determined by Meta. Daily proactive financial/market alerts may be classified as Marketing; don't assume they are free.

Meta API reference:
https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages

## 3. Add GitHub Actions secrets

In the repository go to **Settings → Secrets and variables → Actions → New repository secret**.

Create these secrets:

| Secret | Value |
|---|---|
| `WA_TOKEN` | Meta access token |
| `WA_PHONE_NUMBER_ID` | WhatsApp sender phone-number ID |
| `WA_TO` | Your recipient number, country code included, no `+` |
| `WA_TEMPLATE_NAME` | Approved template name, e.g. `nifty100_daily_scan` |
| `WA_TEMPLATE_LANGUAGE` | Template language, e.g. `en_US` |
| `WA_GRAPH_API_VERSION` | Optional; defaults to `v23.0`. Use a currently supported Graph API version from Meta if this version is retired. |

Never put tokens or phone numbers directly in the Python file or commit them to GitHub.

## 4. Run a manual test

1. Open the repository's **Actions** tab.
2. Select **Nifty 100 daily scan**.
3. Click **Run workflow**.
4. Open the run and inspect the logs.
5. Check WhatsApp delivery. If the scan fails, inspect the log before relying on any list.

## 5. Schedule

The workflow is scheduled Monday–Friday at **16:17 IST** (10:47 UTC), after the 15:30 IST market close. Exchange holidays may still trigger the workflow; the script uses the latest available candles and may send a scan-status warning. GitHub Actions can be delayed, so exact delivery time is not guaranteed.

## Signal definitions implemented

- External structure: confirmed local swing pivots filtered by minimum swing size relative to ATR.
- CHoCH/BOS: candle close beyond a previously confirmed swing level; wick-only breaks do not qualify.
- Bullish OB: last bearish candle before bullish displacement/FVG.
- Bearish OB: last bullish candle before bearish displacement/FVG.
- FVG: standard three-candle gap definition.
- Aggressive displacement: middle candle body at least 1.2 × 14-period ATR.
- Lower-timeframe alignment: latest lower-timeframe structural break must match the higher-timeframe directional bias.
- The current weekly/monthly candle is excluded until its close is confirmed by the scanner's conservative calendar rule.

These thresholds are explicit defaults, not universal SMC definitions. Validate and tune them against your chart screenshots before using the output for decisions.
