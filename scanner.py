import os
import sys
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
NIFTY100_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"
LOG = logging.getLogger("nifty100_scanner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Explicit, repeatable starting rules. Tune only after chart-by-chart validation.
PIVOT_LTF = 3
PIVOT_HTF = 2
MIN_SWING_ATR = 1.0
DISPLACEMENT_ATR = 1.2
LTF_RECENT_BARS = 12


def fetch_symbols():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Nifty100Scanner/1.0)",
        "Accept": "text/csv,*/*",
        "Referer": "https://www.niftyindices.com/",
    }
    response = requests.get(NIFTY100_CSV_URL, headers=headers, timeout=25)
    response.raise_for_status()
    from io import StringIO
    constituents = pd.read_csv(StringIO(response.text))
    symbol_col = next((c for c in constituents.columns if c.strip().lower() == "symbol"), None)
    if not symbol_col:
        raise RuntimeError(f"Could not find Symbol column in official constituent CSV: {list(constituents.columns)}")
    symbols = sorted({str(s).strip().upper() for s in constituents[symbol_col].dropna() if str(s).strip()})
    if len(symbols) < 90:
        raise RuntimeError(f"Expected around 100 Nifty 100 constituents; received only {len(symbols)}.")
    return symbols


def download_chunks(symbols, period, interval, chunk_size=10):
    """Download in small batches to reduce Yahoo throttling. Returns symbol -> OHLCV frame."""
    output = {}
    tickers = [s + ".NS" for s in symbols]
    for start in range(0, len(tickers), chunk_size):
        chunk = tickers[start:start + chunk_size]
        LOG.info("Downloading %s %s bars for symbols %d-%d/%d",
                 interval, period, start + 1, min(start + len(chunk), len(tickers)), len(tickers))
        data = yf.download(
            tickers=chunk,
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            prepost=False,
            threads=True,
            progress=False,
            timeout=25,
        )
        if data is None or data.empty:
            LOG.warning("Empty batch for %s", chunk)
            continue
        for ticker in chunk:
            symbol = ticker[:-3]
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    if ticker in data.columns.get_level_values(0):
                        frame = data[ticker].copy()
                    elif ticker in data.columns.get_level_values(1):
                        frame = data.xs(ticker, axis=1, level=1).copy()
                    else:
                        continue
                else:
                    if len(chunk) != 1:
                        continue
                    frame = data.copy()
                needed = ["Open", "High", "Low", "Close"]
                if not all(c in frame.columns for c in needed):
                    continue
                frame = frame.dropna(subset=needed)
                if frame.empty:
                    continue
                frame.index = pd.to_datetime(frame.index)
                if frame.index.tz is None:
                    frame.index = frame.index.tz_localize("Asia/Kolkata")
                else:
                    frame.index = frame.index.tz_convert("Asia/Kolkata")
                frame = frame[~frame.index.duplicated(keep="last")].sort_index()
                output[symbol] = frame
            except Exception as exc:
                LOG.warning("Could not parse %s: %s", symbol, exc)
        time.sleep(1.0)
    return output


def clean_closed_intraday(df, now=None):
    """Drop any intraday bars that could still be forming. Yahoo timestamps are bar starts."""
    if df is None or df.empty:
        return pd.DataFrame()
    now = now or datetime.now(IST)
    out = df.copy()
    idx = out.index.tz_convert("Asia/Kolkata")
    # NSE's final hourly bar starts at 15:15 and closes at 15:30.
    # Do not use same-day bars until after 15:35 IST; then keep bars no later than 15:15.
    keep = []
    for ts in idx:
        if ts.date() < now.date():
            keep.append(True)
        elif ts.date() > now.date():
            keep.append(False)
        else:
            keep.append(now.time() >= datetime.strptime("15:35", "%H:%M").time()
                        and ts.time() <= datetime.strptime("15:15", "%H:%M").time())
    out = out.loc[keep]
    return out


def atr(df, period=14):
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=max(5, period // 2)).mean()


def pivots(df, left, right, min_swing_atr=MIN_SWING_ATR):
    """Confirmed local pivots filtered by minimum distance from the prior opposite pivot."""
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()
    atrs = atr(df).to_numpy()
    raw = []
    for i in range(left, len(df) - right):
        if highs[i] >= max(highs[i-left:i+right+1]) and highs[i] > max(highs[i-left:i]) and highs[i] >= max(highs[i+1:i+right+1]):
            raw.append((i, "H", float(highs[i]), i + right))
        if lows[i] <= min(lows[i-left:i+right+1]) and lows[i] < min(lows[i-left:i]) and lows[i] <= min(lows[i+1:i+right+1]):
            raw.append((i, "L", float(lows[i]), i + right))
    raw.sort(key=lambda x: (x[0], x[1]))
    alternating = []
    for p in raw:
        if not alternating:
            alternating.append(p)
            continue
        last = alternating[-1]
        if p[1] == last[1]:
            if (p[1] == "H" and p[2] > last[2]) or (p[1] == "L" and p[2] < last[2]):
                alternating[-1] = p
            continue
        # Require enough distance between opposite swing points to filter internal noise.
        local_atr = atrs[p[0]]
        if pd.isna(local_atr):
            local_atr = abs(p[2] - last[2])
        if abs(p[2] - last[2]) >= max(float(local_atr) * min_swing_atr, 0.0):
            alternating.append(p)
    return alternating


def structure_events(df, left, right):
    """Find candle-close breaks of confirmed external pivots; wick-only breaches do not count."""
    if df is None or len(df) < max(35, left + right + 20):
        return [], []
    ps = pivots(df, left, right)
    highs = [p for p in ps if p[1] == "H"]
    lows = [p for p in ps if p[1] == "L"]
    events = []
    previous_direction = None
    close = df["Close"].to_numpy()
    for i in range(1, len(df)):
        known_highs = [p for p in highs if p[3] < i]
        known_lows = [p for p in lows if p[3] < i]
        if not known_highs and not known_lows:
            continue
        last_high = known_highs[-1] if known_highs else None
        last_low = known_lows[-1] if known_lows else None
        direction = None
        level = None
        pivot = None
        if last_high and close[i] > last_high[2] and close[i-1] <= last_high[2]:
            direction, level, pivot = "Bullish", last_high[2], last_high
        elif last_low and close[i] < last_low[2] and close[i-1] >= last_low[2]:
            direction, level, pivot = "Bearish", last_low[2], last_low
        if direction:
            kind = "BOS" if previous_direction == direction else "CHoCH"
            events.append({
                "index": i, "direction": direction, "kind": kind,
                "level": float(level), "pivot_index": int(pivot[0]),
                "time": df.index[i].isoformat()
            })
            previous_direction = direction
    return events, ps


def resample_ohlc(df, rule):
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    # Daily bars are indexed at local midnight; resample using NSE-local calendar.
    result = work.resample(rule, label="right", closed="right").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last",
        "Volume": "sum" if "Volume" in work.columns else "first"
    })
    return result.dropna(subset=["Open", "High", "Low", "Close"])


def resample_4h_nse(df):
    """Build session-anchored 4h bars from hourly bars; second daily bar is the short close-of-session bar."""
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    local = work.index.tz_convert("Asia/Kolkata")
    minutes = local.hour * 60 + local.minute - (9 * 60 + 15)
    valid = (minutes >= 0) & (minutes <= 360) & (local.hour < 16)
    work = work.loc[valid].copy()
    local = work.index.tz_convert("Asia/Kolkata")
    mins = local.hour * 60 + local.minute - (9 * 60 + 15)
    work["_session_date"] = [x.date().isoformat() for x in local]
    work["_bucket"] = (mins // 240).astype(int)
    rows = []
    for (day, bucket), g in work.groupby(["_session_date", "_bucket"], sort=True):
        g = g.sort_index()
        if len(g) < 2:
            continue
        rows.append({
            "time": g.index[-1],
            "Open": float(g["Open"].iloc[0]),
            "High": float(g["High"].max()),
            "Low": float(g["Low"].min()),
            "Close": float(g["Close"].iloc[-1]),
            "Volume": float(g["Volume"].sum()) if "Volume" in g.columns else 0.0,
        })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).set_index("time").sort_index()
    return out


def detect_ob_fvg(df, event, recent_bars=LTF_RECENT_BARS):
    """Require a directional FVG, a large displacement candle, and the last opposite candle as OB."""
    if df is None or df.empty or not event:
        return None
    end_i = int(event["index"])
    start_i = max(2, end_i - recent_bars)
    end_limit = min(len(df) - 1, end_i + 2)
    if end_limit < start_i:
        return None
    a = atr(df, 14).to_numpy()
    direction = event["direction"]
    for i in range(end_limit, start_i - 1, -1):
        # FVG is formed by candles i-2, i-1, i; middle candle must show aggressive displacement.
        if i < 2:
            continue
        c1, c2, c3 = df.iloc[i-2], df.iloc[i-1], df.iloc[i]
        av = a[i-1]
        if pd.isna(av) or av <= 0:
            continue
        body = abs(float(c2["Close"]) - float(c2["Open"]))
        if body < DISPLACEMENT_ATR * av:
            continue
        if direction == "Bullish":
            fvg_ok = float(c3["Low"]) > float(c1["High"])
            displacement_ok = float(c2["Close"]) > float(c2["Open"])
        else:
            fvg_ok = float(c3["High"]) < float(c1["Low"])
            displacement_ok = float(c2["Close"]) < float(c2["Open"])
        if not (fvg_ok and displacement_ok):
            continue
        # OB is the last opposite-colour candle before the displacement candle.
        ob_idx = None
        for j in range(i-2, max(-1, i-8), -1):
            candle = df.iloc[j]
            if direction == "Bullish" and float(candle["Close"]) < float(candle["Open"]):
                ob_idx = j
                break
            if direction == "Bearish" and float(candle["Close"]) > float(candle["Open"]):
                ob_idx = j
                break
        if ob_idx is None:
            continue
        ob = df.iloc[ob_idx]
        fvg_low = float(c1["High"]) if direction == "Bullish" else float(c3["High"])
        fvg_high = float(c3["Low"]) if direction == "Bullish" else float(c1["Low"])
        # Require the FVG to be temporally close to the structural break.
        if abs(i - end_i) > 3:
            continue
        return {
            "ob_low": min(float(ob["Open"]), float(ob["Close"]), float(ob["Low"])),
            "ob_high": max(float(ob["Open"]), float(ob["Close"]), float(ob["High"])),
            "fvg_low": min(fvg_low, fvg_high),
            "fvg_high": max(fvg_low, fvg_high),
            "fvg_time": df.index[i].isoformat(),
            "displacement_atr": round(body / av, 2),
        }
    return None


def latest_event(events, max_age=None, length=None):
    if not events:
        return None
    event = events[-1]
    if max_age is not None and length is not None and (length - 1 - event["index"]) > max_age:
        return None
    return event


def analyse_symbol(symbol, daily, hourly):
    if daily is None or hourly is None or len(daily) < 150 or len(hourly) < 80:
        return [], "insufficient_data"
    daily = daily.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    hourly = clean_closed_intraday(hourly).dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if len(hourly) < 80:
        return [], "insufficient_intraday"

    weekly = resample_ohlc(daily, "W-FRI")
    monthly = resample_ohlc(daily, "ME")
    four_hour = resample_4h_nse(hourly)

    # Use only fully closed weekly/monthly candles for higher-timeframe bias.
    # On a Friday after close, include the current week only if Friday data exists.
    now = datetime.now(IST)
    daily_dates = [ts.date() for ts in daily.index.tz_convert("Asia/Kolkata")]
    latest_daily_date = max(daily_dates) if daily_dates else None
    if now.weekday() == 4 and now.time() >= datetime.strptime("15:35", "%H:%M").time() and latest_daily_date == now.date():
        week_cutoff = now.date()
    else:
        this_friday = now.date() + timedelta(days=(4 - now.weekday()) if now.weekday() <= 4 else -(now.weekday() - 4))
        week_cutoff = this_friday - timedelta(days=7)
    weekly = weekly[[ts.date() <= week_cutoff for ts in weekly.index]]

    # The month is considered complete on its last weekday only when that day's
    # daily candle is available and the market close has passed.
    first_next_month = (now.date().replace(day=28) + timedelta(days=4)).replace(day=1)
    calendar_month_end = first_next_month - timedelta(days=1)
    last_weekday = calendar_month_end
    while last_weekday.weekday() >= 5:
        last_weekday -= timedelta(days=1)
    if (now.date() == last_weekday and now.time() >= datetime.strptime("15:35", "%H:%M").time()
            and latest_daily_date == now.date()):
        month_cutoff = calendar_month_end
    else:
        previous_month_end = now.date().replace(day=1) - timedelta(days=1)
        month_cutoff = previous_month_end
    monthly = monthly[[ts.date() <= month_cutoff for ts in monthly.index]]

    candidates = []
    # Setup A: Weekly bias + 1h alignment.
    w_events, _ = structure_events(weekly, PIVOT_HTF, PIVOT_HTF)
    w_event = latest_event(w_events)
    h_events, _ = structure_events(hourly, PIVOT_LTF, PIVOT_LTF)
    h_event = latest_event(h_events, max_age=LTF_RECENT_BARS, length=len(hourly))
    if w_event and h_event and w_event["direction"] == h_event["direction"]:
        obfvg = detect_ob_fvg(hourly, h_event)
        if obfvg:
            candidates.append({
                "symbol": symbol, "setup": "Weekly + 1H",
                "bias": w_event["direction"], "htf_event": w_event["kind"],
                "ltf_event": h_event["kind"], "price": float(hourly["Close"].iloc[-1]),
                **obfvg
            })

    # Setup B: Monthly bias + 4h alignment.
    m_events, _ = structure_events(monthly, PIVOT_HTF, PIVOT_HTF)
    m_event = latest_event(m_events)
    f_events, _ = structure_events(four_hour, PIVOT_LTF, PIVOT_LTF)
    f_event = latest_event(f_events, max_age=6, length=len(four_hour))
    if m_event and f_event and m_event["direction"] == f_event["direction"]:
        obfvg = detect_ob_fvg(four_hour, f_event, recent_bars=6)
        if obfvg:
            candidates.append({
                "symbol": symbol, "setup": "Monthly + 4H",
                "bias": m_event["direction"], "htf_event": m_event["kind"],
                "ltf_event": f_event["kind"], "price": float(hourly["Close"].iloc[-1]),
                **obfvg
            })
    return candidates, "ok"


def format_message(results, scanned, failed):
    now = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")
    lines = [f"NIFTY 100 STRUCTURE SCAN | {now}", ""]
    for setup in ("Weekly + 1H", "Monthly + 4H"):
        lines.append(setup)
        for bias in ("Bullish", "Bearish"):
            matches = sorted(r["symbol"] for r in results if r["setup"] == setup and r["bias"] == bias)
            lines.append(f"{bias}: {', '.join(matches) if matches else 'None'}")
        lines.append("")
    lines.append(f"Data coverage: {scanned - failed}/{scanned} stocks")
    if failed:
        lines.append("WARNING: incomplete data; results are provisional.")
    lines.append("Signals use closed candles; verify charts before trading.")
    return "\n".join(lines)


def send_whatsapp(text):
    token = os.environ.get("WA_TOKEN", "").strip()
    phone_id = os.environ.get("WA_PHONE_NUMBER_ID", "").strip()
    recipient = os.environ.get("WA_TO", "").strip()
    template = os.environ.get("WA_TEMPLATE_NAME", "nifty100_daily_scan").strip()
    language = os.environ.get("WA_TEMPLATE_LANGUAGE", "en_US").strip()
    version = os.environ.get("WA_GRAPH_API_VERSION", "v23.0").strip()
    if not all([token, phone_id, recipient]):
        raise RuntimeError("WhatsApp secrets are missing: WA_TOKEN, WA_PHONE_NUMBER_ID, WA_TO.")
    # Proactive daily delivery requires a Meta-approved template outside the 24-hour service window.
    # Split long output into chunks conservatively for template variable limits.
    chunks = []
    current = ""
    for line in text.splitlines():
        candidate = (current + "\n" + line).strip() if current else line
        if len(candidate) > 850 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for chunk in chunks:
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": language},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": chunk}]
                }]
            }
        }
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code >= 300:
            raise RuntimeError(f"WhatsApp API error {response.status_code}: {response.text[:1200]}")
        LOG.info("WhatsApp message accepted: %s", response.json().get("messages", [{}])[0].get("id", "no-id"))


def main():
    symbols = fetch_symbols()
    LOG.info("Loaded %d Nifty 100 constituents", len(symbols))
    # Daily bars provide enough history to construct weekly/monthly structure.
    daily_data = download_chunks(symbols, period="5y", interval="1d", chunk_size=10)
    # Yahoo hourly history is limited and unofficial; 730d is requested, but availability may vary.
    hourly_data = download_chunks(symbols, period="730d", interval="1h", chunk_size=10)

    results, failed = [], 0
    for symbol in symbols:
        try:
            matches, status = analyse_symbol(symbol, daily_data.get(symbol), hourly_data.get(symbol))
            results.extend(matches)
            if status != "ok":
                failed += 1
                LOG.warning("%s skipped: %s", symbol, status)
        except Exception:
            failed += 1
            LOG.exception("Analysis failed for %s", symbol)

    message = format_message(results, len(symbols), failed)
    print(message)
    # Do not send a misleading empty scan when too much data is missing.
    if failed > max(5, int(len(symbols) * 0.10)):
        message = ("NIFTY 100 SCAN FAILED: data unavailable for too many constituents. "
                   f"Coverage {len(symbols)-failed}/{len(symbols)}. Check GitHub Actions logs.")
    send_whatsapp(message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.exception("Scanner failed")
        # Best effort failure notification, if messaging credentials are configured.
        try:
            send_whatsapp("NIFTY 100 SCAN FAILED. Please check the GitHub Actions run logs.")
        except Exception:
            LOG.exception("Could not send failure alert")
        sys.exit(1)
