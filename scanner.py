import os
import sys
import time
import logging
import smtplib
import csv
import io
import html
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from docx import Document
from docx.shared import Inches, Pt

IST = ZoneInfo("Asia/Kolkata")
NIFTY200_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"
LOG = logging.getLogger("nifty200_scanner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Explicit, repeatable starting rules. Tune only after chart-by-chart validation.
PIVOT_LTF = 3
PIVOT_HTF = 2
MIN_SWING_ATR = 1.0
DISPLACEMENT_ATR = 1.2
LTF_RECENT_BARS = 12


def fetch_symbols():
    """Fetch the current Nifty 200 constituents from the official index CSV."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Nifty200Scanner/2.0)",
        "Accept": "text/csv,*/*",
        "Referer": "https://www.niftyindices.com/",
    }
    response = requests.get(NIFTY200_CSV_URL, headers=headers, timeout=25)
    response.raise_for_status()
    from io import StringIO
    constituents = pd.read_csv(StringIO(response.text))
    symbol_col = next((c for c in constituents.columns if c.strip().lower() == "symbol"), None)
    if not symbol_col:
        raise RuntimeError(
            f"Could not find Symbol column in official Nifty 200 CSV: {list(constituents.columns)}"
        )
    symbols = sorted({
        str(symbol).strip().upper()
        for symbol in constituents[symbol_col].dropna()
        if str(symbol).strip()
    })
    if len(symbols) < 180:
        raise RuntimeError(
            f"Expected around 200 Nifty 200 constituents; received only {len(symbols)}."
        )
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
    """
    Detect breaks of the latest confirmed external swing high/low.

    IMPORTANT:
    - A break is valid ONLY when a candle CLOSES beyond the swing level.
    - High/Low wick breaches never count as BOS or CHoCH.
    - Swings are the alternating, ATR-filtered pivots returned by pivots().
    - The broken level must already have been confirmed before the break candle.
    - CHoCH is used when the break opposes the recent external swing trend;
      BOS is used when it continues that trend. If the trend is not clear,
      classify the first valid break as CHoCH rather than guessing.
    """
    if df is None or len(df) < max(35, left + right + 20):
        return [], []

    ps = pivots(df, left, right)
    highs = [p for p in ps if p[1] == "H"]
    lows = [p for p in ps if p[1] == "L"]
    close = df["Close"].to_numpy()
    events = []

    def trend_before(bar_index):
        # Only pivots confirmed before the candidate break candle may be used.
        known = [p for p in ps if p[3] < bar_index]
        known_highs = [p for p in known if p[1] == "H"]
        known_lows = [p for p in known if p[1] == "L"]
        if len(known_highs) < 2 or len(known_lows) < 2:
            return None

        previous_high, latest_high = known_highs[-2], known_highs[-1]
        previous_low, latest_low = known_lows[-2], known_lows[-1]

        higher_highs = latest_high[2] > previous_high[2]
        higher_lows = latest_low[2] > previous_low[2]
        lower_highs = latest_high[2] < previous_high[2]
        lower_lows = latest_low[2] < previous_low[2]

        if higher_highs and higher_lows:
            return "Bullish"
        if lower_highs and lower_lows:
            return "Bearish"
        return None

    for i in range(1, len(df)):
        # Use only external pivots whose right-side confirmation bars have closed.
        known_highs = [p for p in highs if p[3] < i]
        known_lows = [p for p in lows if p[3] < i]
        if not known_highs and not known_lows:
            continue

        last_high = known_highs[-1] if known_highs else None
        last_low = known_lows[-1] if known_lows else None

        direction = None
        level = None
        pivot = None

        # CLOSE-only crossing checks. Candle High/Low are deliberately not used
        # to trigger a structure break, so wick-only sweeps are ignored.
        if last_high and close[i - 1] <= last_high[2] and close[i] > last_high[2]:
            direction, level, pivot = "Bullish", last_high[2], last_high
        elif last_low and close[i - 1] >= last_low[2] and close[i] < last_low[2]:
            direction, level, pivot = "Bearish", last_low[2], last_low

        if direction:
            prior_trend = trend_before(i)
            # Do not guess CHoCH when the prior external trend is ambiguous.
            if prior_trend is None:
                continue
            kind = "BOS" if prior_trend == direction else "CHoCH"
            events.append({
                "index": i,
                "direction": direction,
                "kind": kind,
                "level": float(level),
                "pivot_index": int(pivot[0]),
                "pivot_confirmed_index": int(pivot[3]),
                "time": df.index[i].isoformat(),
                "prior_trend": prior_trend,
            })

    # The scanner uses the latest event; retain the full event list for diagnostics.
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
        # Find the nearest opposite-colour candle before displacement, then
        # aggregate its contiguous opposite-colour run (maximum three candles).
        ob_idx = None
        for j in range(i - 2, max(-1, i - 8), -1):
            candle = df.iloc[j]
            is_opposite = (
                float(candle["Close"]) < float(candle["Open"])
                if direction == "Bullish"
                else float(candle["Close"]) > float(candle["Open"])
            )
            if is_opposite:
                ob_idx = j
                break
        if ob_idx is None:
            continue

        ob_indices = [ob_idx]
        for j in range(ob_idx - 1, max(-1, ob_idx - 3), -1):
            candle = df.iloc[j]
            is_opposite = (
                float(candle["Close"]) < float(candle["Open"])
                if direction == "Bullish"
                else float(candle["Close"]) > float(candle["Open"])
            )
            if not is_opposite or len(ob_indices) >= 3:
                break
            ob_indices.append(j)

        ob_group = df.iloc[sorted(ob_indices)]
        fvg_low = float(c1["High"]) if direction == "Bullish" else float(c3["High"])
        fvg_high = float(c3["Low"]) if direction == "Bullish" else float(c1["Low"])
        # Require the FVG to be temporally close to the structural break.
        if abs(i - end_i) > 3:
            continue
        return {
            "ob_low": float(ob_group["Low"].min()),
            "ob_high": float(ob_group["High"].max()),
            "ob_time": df.index[min(ob_indices)].isoformat(),
            "ob_candle_count": len(ob_indices),
            "fvg_low": min(fvg_low, fvg_high),
            "fvg_high": max(fvg_low, fvg_high),
            "fvg_time": df.index[i].isoformat(),
            "fvg_candle_low": float(c3["Low"]),
            "fvg_candle_high": float(c3["High"]),
            "fvg_index": int(i),
            "ob_index": int(ob_idx),
            "displacement_atr": round(body / av, 2),
        }
    return None


def candle_touches_zone(candle, zone_low, zone_high):
    """A wick/range touch is valid for zone mitigation; it is NOT a BOS/CHoCH."""
    return float(candle["High"]) >= float(zone_low) and float(candle["Low"]) <= float(zone_high)


def evaluate_htf_retest(ltf, htf_event, zones, direction):
    """
    Replay lower-timeframe candles after the higher-timeframe break.

    State rules:
    1. Price must return to the HTF FVG, its defining candle range, or the HTF OB.
    2. The first FVG touch marks that FVG as mitigated/eliminated. The touch candle's
       directional extreme becomes the next POI (high for bearish, low for bullish).
    3. That POI remains tracked until price reaches the HTF OB.
    4. OB overlap marks mitigation; complete coverage is tracked separately.
    5. A lower-timeframe BOS/CHoCH must occur AFTER the qualifying retest.
    Candle ranges may mitigate zones; only candle CLOSES may break structure.
    """
    if ltf is None or ltf.empty or not zones or not htf_event:
        return None

    htf_time = pd.Timestamp(htf_event["time"])
    if htf_time.tzinfo is None:
        htf_time = htf_time.tz_localize("Asia/Kolkata")
    else:
        htf_time = htf_time.tz_convert("Asia/Kolkata")

    # Weekly/monthly resampled bars are labelled at local midnight. Their close
    # is not confirmed until the NSE session ends on that label date.
    htf_time = htf_time.replace(hour=15, minute=30, second=0, microsecond=0)

    frame = ltf.copy()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("Asia/Kolkata")
    else:
        frame.index = frame.index.tz_convert("Asia/Kolkata")
    original_positions = {timestamp: position for position, timestamp in enumerate(frame.index)}
    frame = frame.loc[frame.index > htf_time]
    if frame.empty:
        return None

    fvg_low, fvg_high = float(zones["fvg_low"]), float(zones["fvg_high"])
    ob_low, ob_high = float(zones["ob_low"]), float(zones["ob_high"])
    # Include the full three-candle FVG formation candle range as a POI.
    fvg_candle_low = float(zones.get("fvg_candle_low", fvg_low))
    fvg_candle_high = float(zones.get("fvg_candle_high", fvg_high))

    fvg_mitigated = False
    fvg_mitigation_time = None
    fvg_poi_level = None
    fvg_poi_high = None
    fvg_poi_low = None
    fvg_poi_active = False
    ob_mitigated = False
    ob_fully_covered = False
    ob_mitigation_time = None
    ob_poi_level = None
    ob_poi_high = None
    ob_poi_low = None
    ob_poi_active = False
    first_retest_index = None
    first_retest_timestamp = None
    retest_type = None

    for timestamp, candle in frame.iterrows():
        source_idx = original_positions[timestamp]
        touches_fvg = candle_touches_zone(candle, fvg_low, fvg_high)
        touches_fvg_candle = candle_touches_zone(candle, fvg_candle_low, fvg_candle_high)
        touches_ob = candle_touches_zone(candle, ob_low, ob_high)

        if not fvg_mitigated and (touches_fvg or touches_fvg_candle):
            fvg_mitigated = True
            fvg_mitigation_time = timestamp.isoformat()
            fvg_poi_high = float(candle["High"])
            fvg_poi_low = float(candle["Low"])
            fvg_poi_level = fvg_poi_high if direction == "Bearish" else fvg_poi_low
            fvg_poi_active = True
            first_retest_index = source_idx
            first_retest_timestamp = timestamp
            retest_type = "HTF FVG retest"

        # After FVG mitigation, retain the mitigation candle's swing extreme as POI
        # until price reaches the HTF OB zone.
        if fvg_mitigated and not touches_ob and fvg_poi_level is not None:
            if direction == "Bearish":
                poi_reached = float(candle["High"]) >= fvg_poi_level
            else:
                poi_reached = float(candle["Low"]) <= fvg_poi_level
            if poi_reached and first_retest_index is None:
                first_retest_index = source_idx
                first_retest_timestamp = timestamp
                retest_type = "FVG mitigation-candle swing POI"

        if touches_ob:
            # Reaching the OB ends the FVG-mitigation-candle POI phase.
            fvg_poi_active = False
            if not ob_mitigated:
                ob_mitigated = True
                ob_mitigation_time = timestamp.isoformat()
                ob_poi_high = float(candle["High"])
                ob_poi_low = float(candle["Low"])
                ob_poi_level = ob_poi_high if direction == "Bearish" else ob_poi_low
                ob_poi_active = True
                if first_retest_index is None:
                    first_retest_index = source_idx
                    first_retest_timestamp = timestamp
                    retest_type = "HTF OB retest"

            # A bearish OB is fully covered when price reaches its far/top boundary;
            # a bullish OB is fully covered when price reaches its far/bottom boundary.
            if direction == "Bearish" and float(candle["High"]) >= ob_high:
                ob_fully_covered = True
                ob_poi_active = False
            elif direction == "Bullish" and float(candle["Low"]) <= ob_low:
                ob_fully_covered = True
                ob_poi_active = False

        # Once OB has been touched, its mitigation candle extreme is tracked as POI
        # until full coverage is observed. This is diagnostic state, not a break signal.
        if ob_mitigated and not ob_fully_covered and ob_poi_level is not None:
            if direction == "Bearish":
                _ob_poi_reached = float(candle["High"]) >= ob_poi_level
            else:
                _ob_poi_reached = float(candle["Low"]) <= ob_poi_level

    if first_retest_index is None:
        return None

    return {
        "retest_index": int(first_retest_index),
        "retest_time": first_retest_timestamp.isoformat(),
        "retest_type": retest_type or "HTF zone retest",
        "fvg_mitigated": fvg_mitigated,
        "fvg_mitigation_time": fvg_mitigation_time or "not yet mitigated",
        "fvg_mitigation_poi": fvg_poi_level,
        "fvg_poi_high": fvg_poi_high,
        "fvg_poi_low": fvg_poi_low,
        "fvg_poi_active": fvg_poi_active,
        "ob_mitigated": ob_mitigated,
        "ob_mitigation_time": ob_mitigation_time or "not yet mitigated",
        "ob_mitigation_poi": ob_poi_level,
        "ob_poi_high": ob_poi_high,
        "ob_poi_low": ob_poi_low,
        "ob_poi_active": ob_poi_active,
        "ob_fully_covered": ob_fully_covered,
    }


def latest_event(events, max_age=None, length=None):
    if not events:
        return None
    event = events[-1]
    if max_age is not None and length is not None and (length - 1 - event["index"]) > max_age:
        return None
    return event


def analyse_symbol(symbol, daily, hourly, thirty_minute=None):
    if daily is None or hourly is None or len(daily) < 150 or len(hourly) < 80:
        return [], "insufficient_data"

    daily = daily.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    hourly = clean_closed_intraday(hourly).dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if len(hourly) < 80:
        return [], "insufficient_intraday"
    if thirty_minute is not None:
        thirty_minute = clean_closed_intraday(thirty_minute).dropna(subset=["Open", "High", "Low", "Close"]).copy()

    weekly = resample_ohlc(daily, "W-FRI")
    monthly = resample_ohlc(daily, "ME")
    four_hour = resample_4h_nse(hourly)

    # Exclude incomplete weekly/monthly candles.
    now = datetime.now(IST)
    daily_dates = [ts.date() for ts in daily.index.tz_convert("Asia/Kolkata")]
    latest_daily_date = max(daily_dates) if daily_dates else None
    if now.weekday() == 4 and now.time() >= datetime.strptime("15:35", "%H:%M").time() and latest_daily_date == now.date():
        week_cutoff = now.date()
    else:
        this_friday = now.date() + timedelta(days=(4 - now.weekday()) if now.weekday() <= 4 else -(now.weekday() - 4))
        week_cutoff = this_friday - timedelta(days=7)
    weekly = weekly[[ts.date() <= week_cutoff for ts in weekly.index]]

    first_next_month = (now.date().replace(day=28) + timedelta(days=4)).replace(day=1)
    calendar_month_end = first_next_month - timedelta(days=1)
    last_weekday = calendar_month_end
    while last_weekday.weekday() >= 5:
        last_weekday -= timedelta(days=1)
    if (now.date() == last_weekday and now.time() >= datetime.strptime("15:35", "%H:%M").time()
            and latest_daily_date == now.date()):
        month_cutoff = calendar_month_end
    else:
        month_cutoff = now.date().replace(day=1) - timedelta(days=1)
    monthly = monthly[[ts.date() <= month_cutoff for ts in monthly.index]]

    candidates = []

    # SETUP A: A valid Weekly external break with its own OB + aggressive FVG,
    # followed by an LTF retracement into the Weekly FVG/its candle POI/Weekly OB,
    # followed by a fresh 1H close-only structure break in the Weekly direction.
    w_events, _ = structure_events(weekly, PIVOT_HTF, PIVOT_HTF)
    h_events, _ = structure_events(hourly, PIVOT_LTF, PIVOT_LTF)
    w_event = latest_event(w_events)
    if w_event:
        weekly_zones = detect_ob_fvg(weekly, w_event, recent_bars=12)
        if weekly_zones:
            retest = evaluate_htf_retest(hourly, w_event, weekly_zones, w_event["direction"])
            if retest:
                valid_ltf = [
                    e for e in h_events
                    if e["direction"] == w_event["direction"]
                    and e["kind"] in ("CHoCH", "BOS")
                    and e["index"] > retest["retest_index"]
                    and e["index"] >= len(hourly) - LTF_RECENT_BARS
                ]
                h_event = valid_ltf[-1] if valid_ltf else None
                if h_event:
                    candidates.append({
                        "symbol": symbol,
                        "setup": "Weekly + 1H",
                        "bias": w_event["direction"],
                        "htf_event": w_event["kind"],
                        "ltf_event": h_event["kind"],
                        "htf_break_level": round(float(w_event["level"]), 4),
                        "ltf_break_level": round(float(h_event["level"]), 4),
                        "price": float(hourly["Close"].iloc[-1]),
                        "htf_event_time": w_event["time"],
                        "ltf_event_time": h_event["time"],
                        **weekly_zones,
                        **retest,
                    })

    # SETUP B: A valid Monthly external break with its own OB + aggressive FVG,
    # followed by an LTF retracement into the Monthly FVG/its candle POI/Monthly OB,
    # followed by a fresh 4H close-only structure break in the Monthly direction.
    m_events, _ = structure_events(monthly, PIVOT_HTF, PIVOT_HTF)
    f_events, _ = structure_events(four_hour, PIVOT_LTF, PIVOT_LTF)
    m_event = latest_event(m_events)
    if m_event:
        monthly_zones = detect_ob_fvg(monthly, m_event, recent_bars=12)
        if monthly_zones:
            retest = evaluate_htf_retest(four_hour, m_event, monthly_zones, m_event["direction"])
            if retest:
                valid_ltf = [
                    e for e in f_events
                    if e["direction"] == m_event["direction"]
                    and e["kind"] in ("CHoCH", "BOS")
                    and e["index"] > retest["retest_index"]
                    and e["index"] >= len(four_hour) - 6
                ]
                f_event = valid_ltf[-1] if valid_ltf else None
                if f_event:
                    candidates.append({
                        "symbol": symbol,
                        "setup": "Monthly + 4H",
                        "bias": m_event["direction"],
                        "htf_event": m_event["kind"],
                        "ltf_event": f_event["kind"],
                        "htf_break_level": round(float(m_event["level"]), 4),
                        "ltf_break_level": round(float(f_event["level"]), 4),
                        "price": float(hourly["Close"].iloc[-1]),
                        "htf_event_time": m_event["time"],
                        "ltf_event_time": f_event["time"],
                        **monthly_zones,
                        **retest,
                    })

    # SETUP C: Daily external break with its own OB + aggressive FVG,
    # followed by a 30-minute retest into the Daily FVG / FVG-candle POI / Daily OB,
    # followed by a fresh 30m close-only structure break in the Daily direction.
    if thirty_minute is not None and len(thirty_minute) >= 80:
        d_events, _ = structure_events(daily, PIVOT_HTF, PIVOT_HTF)
        d_event = latest_event(d_events)
        thirty_events, _ = structure_events(thirty_minute, PIVOT_LTF, PIVOT_LTF)
        if d_event:
            daily_zones = detect_ob_fvg(daily, d_event, recent_bars=12)
            if daily_zones:
                retest = evaluate_htf_retest(thirty_minute, d_event, daily_zones, d_event["direction"])
                if retest:
                    valid_ltf = [
                        e for e in thirty_events
                        if e["direction"] == d_event["direction"]
                        and e["kind"] in ("CHoCH", "BOS")
                        and e["index"] > retest["retest_index"]
                        and e["index"] >= len(thirty_minute) - LTF_RECENT_BARS
                    ]
                    thirty_event = valid_ltf[-1] if valid_ltf else None
                    if thirty_event:
                        candidates.append({
                            "symbol": symbol,
                            "setup": "Daily + 30M",
                            "bias": d_event["direction"],
                            "htf_event": d_event["kind"],
                            "ltf_event": thirty_event["kind"],
                            "htf_break_level": round(float(d_event["level"]), 4),
                            "ltf_break_level": round(float(thirty_event["level"]), 4),
                            "price": float(thirty_minute["Close"].iloc[-1]),
                            "htf_event_time": d_event["time"],
                            "ltf_event_time": thirty_event["time"],
                            **daily_zones,
                            **retest,
                        })

    return candidates, "ok"


REPORT_SETUPS = ("Daily + 30M", "Weekly + 1H", "Monthly + 4H")
REPORT_BIASES = ("Bullish", "Bearish")


def _fmt_price(value):
    """Format a numeric value for human-readable report output."""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_time(value):
    """Shorten ISO timestamps for readable reports without changing source values."""
    if not value:
        return "N/A"
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.strftime("%d %b %Y, %H:%M IST")
    except (TypeError, ValueError):
        return str(value)


def _status(value):
    return "Yes" if bool(value) else "No"


def _report_row(r):
    """Flatten an existing signal record for CSV/XLSX/DOCX exports.
    This function only formats already-computed results; it does not alter signal logic.
    """
    return {
        "Stock": r.get("symbol", ""),
        "Setup": r.get("setup", ""),
        "Direction": r.get("bias", ""),
        "HTF Structure": r.get("htf_event", ""),
        "HTF Break Level": r.get("htf_break_level", ""),
        "HTF Break Time": r.get("htf_event_time", ""),
        "LTF Structure": r.get("ltf_event", ""),
        "LTF Break Level": r.get("ltf_break_level", ""),
        "LTF Break Time": r.get("ltf_event_time", ""),
        "Current Price": r.get("price", ""),
        "HTF OB Low": r.get("ob_low", ""),
        "HTF OB High": r.get("ob_high", ""),
        "OB Time": r.get("ob_time", ""),
        "OB Candle Count": r.get("ob_candle_count", ""),
        "HTF FVG Low": r.get("fvg_low", ""),
        "HTF FVG High": r.get("fvg_high", ""),
        "FVG Time": r.get("fvg_time", ""),
        "Displacement (ATR)": r.get("displacement_atr", ""),
        "Retest Type": r.get("retest_type", ""),
        "Retest Time": r.get("retest_time", ""),
        "FVG Mitigated": r.get("fvg_mitigated", ""),
        "OB Mitigated": r.get("ob_mitigated", ""),
        "OB Fully Covered": r.get("ob_fully_covered", ""),
        "FVG POI Low": r.get("fvg_poi_low", ""),
        "FVG POI High": r.get("fvg_poi_high", ""),
        "FVG POI Active": r.get("fvg_poi_active", ""),
        "OB POI Low": r.get("ob_poi_low", ""),
        "OB POI High": r.get("ob_poi_high", ""),
        "OB POI Active": r.get("ob_poi_active", ""),
    }


def format_message(results, scanned, failed):
    """Create a structured plain-text email body from existing signal records only."""
    now = datetime.now(IST).strftime("%d %b %Y · %H:%M IST")
    successful = max(0, scanned - failed)
    lines = [
        "NIFTY 200 | DAILY MULTI-TIMEFRAME SMC SCAN",
        f"Scan time: {now}",
        f"Data coverage: {successful}/{scanned} stocks analysed",
    ]
    if failed:
        lines.append(f"DATA WARNING: {failed} stock(s) could not be analysed; results may be incomplete.")
    lines.extend(["", "=" * 64])

    for setup in REPORT_SETUPS:
        lines.extend(["", setup.upper(), "-" * 64])
        for bias in REPORT_BIASES:
            matches = sorted(
                (r for r in results if r.get("setup") == setup and r.get("bias") == bias),
                key=lambda r: r.get("symbol", ""),
            )
            lines.extend(["", f"{bias.upper()} SETUPS ({len(matches)})", "-" * 32])
            if not matches:
                lines.append("No qualifying stocks.")
                continue

            for number, r in enumerate(matches, 1):
                lines.extend([
                    "",
                    f"{number}. {r.get('symbol', 'UNKNOWN')}  |  Current price: ₹{_fmt_price(r.get('price'))}",
                    f"   HIGHER TIMEFRAME ({'Daily' if setup == 'Daily + 30M' else 'Weekly' if setup == 'Weekly + 1H' else 'Monthly'})",
                    f"   Structure: {r.get('htf_event', 'N/A')} | Break level: ₹{_fmt_price(r.get('htf_break_level'))}",
                    f"   Break time: {_fmt_time(r.get('htf_event_time'))}",
                    f"   Order Block: ₹{_fmt_price(r.get('ob_low'))} – ₹{_fmt_price(r.get('ob_high'))}",
                    f"   OB candle(s): {r.get('ob_candle_count', 'N/A')} | OB time: {_fmt_time(r.get('ob_time'))}",
                    f"   Fair Value Gap: ₹{_fmt_price(r.get('fvg_low'))} – ₹{_fmt_price(r.get('fvg_high'))}",
                    f"   FVG time: {_fmt_time(r.get('fvg_time'))} | Displacement: {r.get('displacement_atr', 'N/A')} ATR",
                    f"   LOWER TIMEFRAME ({'30M' if setup == 'Daily + 30M' else '1H' if setup == 'Weekly + 1H' else '4H'})",
                    f"   Structure: {r.get('ltf_event', 'N/A')} | Break level: ₹{_fmt_price(r.get('ltf_break_level'))}",
                    f"   Break time: {_fmt_time(r.get('ltf_event_time'))}",
                    f"   RETRACEMENT / ZONE STATUS",
                    f"   Retest: {r.get('retest_type', 'N/A')} | Time: {_fmt_time(r.get('retest_time'))}",
                    f"   FVG mitigated: {_status(r.get('fvg_mitigated'))} | OB mitigated: {_status(r.get('ob_mitigated'))} | OB fully covered: {_status(r.get('ob_fully_covered'))}",
                ])
                if r.get("fvg_poi_low") is not None and r.get("fvg_poi_high") is not None:
                    lines.append(
                        f"   FVG mitigation candle POI: ₹{_fmt_price(r.get('fvg_poi_low'))} – ₹{_fmt_price(r.get('fvg_poi_high'))} "
                        f"| Active until OB reached: {_status(r.get('fvg_poi_active'))}"
                    )
                if r.get("ob_poi_low") is not None and r.get("ob_poi_high") is not None:
                    lines.append(
                        f"   OB mitigation candle POI: ₹{_fmt_price(r.get('ob_poi_low'))} – ₹{_fmt_price(r.get('ob_poi_high'))} "
                        f"| Active until OB fully covered: {_status(r.get('ob_poi_active'))}"
                    )

    lines.extend([
        "",
        "=" * 64,
        "STRUCTURE RULE: BOS/CHoCH requires a completed candle close beyond the swing level; wick-only breaks are ignored.",
        "IMPORTANT: SMC zones are algorithmic approximations. Verify against charts before trading.",
    ])
    return "\n".join(lines)


def _format_html(results, scanned, failed):
    """Build a readable HTML version of the same report; no signal calculations occur here."""
    now = datetime.now(IST).strftime("%d %b %Y · %H:%M IST")
    successful = max(0, scanned - failed)
    parts = [
        "<html><body style='font-family:Arial,Helvetica,sans-serif;color:#172033;line-height:1.45'>",
        "<h1 style='margin-bottom:4px'>NIFTY 200 — Daily SMC Scan</h1>",
        f"<p style='color:#5b6577;margin-top:0'>{html.escape(now)} &nbsp;|&nbsp; Data coverage: <b>{successful}/{scanned}</b> stocks analysed</p>",
    ]
    if failed:
        parts.append(
            f"<p style='padding:10px;background:#fff3cd;border:1px solid #ffe08a;border-radius:6px'><b>Data warning:</b> {failed} stock(s) could not be analysed. Results may be incomplete.</p>"
        )

    for setup in REPORT_SETUPS:
        ltf = "30M" if setup == "Daily + 30M" else "1H" if setup == "Weekly + 1H" else "4H"
        htf = "Daily" if setup == "Daily + 30M" else "Weekly" if setup == "Weekly + 1H" else "Monthly"
        parts.append(f"<h2 style='border-bottom:2px solid #d8dee9;padding-bottom:6px'>{html.escape(setup)}</h2>")
        for bias in REPORT_BIASES:
            matches = sorted(
                (r for r in results if r.get("setup") == setup and r.get("bias") == bias),
                key=lambda r: r.get("symbol", ""),
            )
            color = "#137333" if bias == "Bullish" else "#b3261e"
            parts.append(f"<h3 style='color:{color};margin-bottom:8px'>{bias} ({len(matches)})</h3>")
            if not matches:
                parts.append("<p><i>No qualifying stocks.</i></p>")
                continue
            for r in matches:
                symbol = html.escape(str(r.get("symbol", "UNKNOWN")))
                parts.append(
                    "<div style='border:1px solid #d8dee9;border-radius:8px;padding:14px;margin:10px 0 16px'>"
                    f"<h3 style='margin:0 0 8px'>{symbol} <span style='font-size:14px;color:#5b6577;font-weight:normal'>Current price: ₹{_fmt_price(r.get('price'))}</span></h3>"
                    "<table style='border-collapse:collapse;width:100%;font-size:13px'>"
                )
                rows = [
                    ("Higher timeframe", f"{htf} · {r.get('htf_event', 'N/A')} · Break ₹{_fmt_price(r.get('htf_break_level'))} · {_fmt_time(r.get('htf_event_time'))}"),
                    ("Order Block", f"₹{_fmt_price(r.get('ob_low'))} – ₹{_fmt_price(r.get('ob_high'))} · {r.get('ob_candle_count', 'N/A')} candle(s) · {_fmt_time(r.get('ob_time'))}"),
                    ("Fair Value Gap", f"₹{_fmt_price(r.get('fvg_low'))} – ₹{_fmt_price(r.get('fvg_high'))} · {_fmt_time(r.get('fvg_time'))} · Displacement {r.get('displacement_atr', 'N/A')} ATR"),
                    ("Lower timeframe", f"{ltf} · {r.get('ltf_event', 'N/A')} · Break ₹{_fmt_price(r.get('ltf_break_level'))} · {_fmt_time(r.get('ltf_event_time'))}"),
                    ("Retest", f"{r.get('retest_type', 'N/A')} · {_fmt_time(r.get('retest_time'))}"),
                    ("Mitigation status", f"FVG: {_status(r.get('fvg_mitigated'))} · OB: {_status(r.get('ob_mitigated'))} · OB fully covered: {_status(r.get('ob_fully_covered'))}"),
                ]
                if r.get("fvg_poi_low") is not None and r.get("fvg_poi_high") is not None:
                    rows.append(("FVG mitigation POI", f"₹{_fmt_price(r.get('fvg_poi_low'))} – ₹{_fmt_price(r.get('fvg_poi_high'))} · Active until OB reached: {_status(r.get('fvg_poi_active'))}"))
                if r.get("ob_poi_low") is not None and r.get("ob_poi_high") is not None:
                    rows.append(("OB mitigation POI", f"₹{_fmt_price(r.get('ob_poi_low'))} – ₹{_fmt_price(r.get('ob_poi_high'))} · Active until OB fully covered: {_status(r.get('ob_poi_active'))}"))
                for label, value in rows:
                    parts.append(
                        "<tr>"
                        f"<td style='padding:7px;border-top:1px solid #e5e9f0;width:160px;color:#5b6577'><b>{html.escape(label)}</b></td>"
                        f"<td style='padding:7px;border-top:1px solid #e5e9f0'>{html.escape(str(value))}</td>"
                        "</tr>"
                    )
                parts.append("</table></div>")

    parts.extend([
        "<hr><p style='font-size:12px;color:#5b6577'>BOS/CHoCH requires a completed candle close beyond the swing level; wick-only breaks are ignored. SMC zones are algorithmic approximations; verify charts before trading.</p>",
        "</body></html>",
    ])
    return "".join(parts)


def _make_csv_bytes(results):
    output = io.StringIO(newline="")
    rows = [_report_row(r) for r in sorted(results, key=lambda x: (x.get("setup", ""), x.get("bias", ""), x.get("symbol", "")))]
    headers = list(_report_row({}).keys())
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _make_xlsx_bytes(results, scanned, failed):
    output = io.BytesIO()
    rows = [_report_row(r) for r in sorted(results, key=lambda x: (x.get("setup", ""), x.get("bias", ""), x.get("symbol", "")))]
    columns = list(_report_row({}).keys())
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary = pd.DataFrame([
            {"Metric": "Report", "Value": "NIFTY 200 Daily Multi-Timeframe SMC Scan"},
            {"Metric": "Generated (IST)", "Value": datetime.now(IST).strftime("%d %b %Y %H:%M IST")},
            {"Metric": "Data coverage", "Value": f"{max(0, scanned-failed)}/{scanned}"},
            {"Metric": "Failed/incomplete stocks", "Value": failed},
            {"Metric": "Total qualifying signals", "Value": len(results)},
            {"Metric": "Note", "Value": "Formatting/export only; verify SMC approximations against charts."},
        ])
        summary.to_excel(writer, sheet_name="Summary", index=False)
        all_df = pd.DataFrame(rows, columns=columns)
        all_df.to_excel(writer, sheet_name="All Signals", index=False)
        for setup, sheet in (("Daily + 30M", "Daily + 30M"), ("Weekly + 1H", "Weekly + 1H"), ("Monthly + 4H", "Monthly + 4H")):
            setup_df = all_df[all_df["Setup"] == setup] if not all_df.empty else pd.DataFrame(columns=columns)
            setup_df.to_excel(writer, sheet_name=sheet, index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                max_len = min(max((len(str(cell.value or "")) for cell in col[:100]), default=10) + 2, 42)
                ws.column_dimensions[col[0].column_letter].width = max(12, max_len)
    return output.getvalue()


def _make_docx_bytes(text, results, scanned, failed):
    output = io.BytesIO()
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.55)
    section.bottom_margin = Inches(0.55)
    section.left_margin = Inches(0.65)
    section.right_margin = Inches(0.65)
    doc.styles["Normal"].font.name = "Aptos"
    doc.styles["Normal"].font.size = Pt(9)
    doc.add_heading("NIFTY 200 — Daily Multi-Timeframe SMC Scan", 0)
    doc.add_paragraph(f"Generated: {datetime.now(IST).strftime('%d %b %Y · %H:%M IST')}")
    doc.add_paragraph(f"Data coverage: {max(0, scanned-failed)}/{scanned} stocks analysed · Qualifying signals: {len(results)}")
    if failed:
        p = doc.add_paragraph()
        p.add_run(f"WARNING: {failed} stock(s) could not be analysed; results may be incomplete.").bold = True

    for setup in REPORT_SETUPS:
        doc.add_heading(setup, level=1)
        for bias in REPORT_BIASES:
            matches = sorted(
                (r for r in results if r.get("setup") == setup and r.get("bias") == bias),
                key=lambda r: r.get("symbol", ""),
            )
            doc.add_heading(f"{bias} ({len(matches)})", level=2)
            if not matches:
                doc.add_paragraph("No qualifying stocks.")
                continue
            table = doc.add_table(rows=1, cols=5)
            table.style = "Light Shading Accent 1"
            hdr = table.rows[0].cells
            for cell, title in zip(hdr, ("Stock / Price", "HTF structure & zones", "LTF confirmation", "Retest / mitigation", "POI")):
                cell.text = title
            for r in matches:
                cells = table.add_row().cells
                cells[0].text = f"{r.get('symbol','')}\nPrice: ₹{_fmt_price(r.get('price'))}"
                cells[1].text = (
                    f"{r.get('htf_event','N/A')} @ ₹{_fmt_price(r.get('htf_break_level'))}\n"
                    f"OB: ₹{_fmt_price(r.get('ob_low'))}–₹{_fmt_price(r.get('ob_high'))}\n"
                    f"FVG: ₹{_fmt_price(r.get('fvg_low'))}–₹{_fmt_price(r.get('fvg_high'))}\n"
                    f"Displacement: {r.get('displacement_atr','N/A')} ATR"
                )
                cells[2].text = f"{r.get('ltf_event','N/A')} @ ₹{_fmt_price(r.get('ltf_break_level'))}\n{_fmt_time(r.get('ltf_event_time'))}"
                cells[3].text = (
                    f"{r.get('retest_type','N/A')}\nFVG mitigated: {_status(r.get('fvg_mitigated'))}\n"
                    f"OB mitigated: {_status(r.get('ob_mitigated'))}\nOB fully covered: {_status(r.get('ob_fully_covered'))}"
                )
                poi_lines = []
                if r.get("fvg_poi_low") is not None and r.get("fvg_poi_high") is not None:
                    poi_lines.append(f"FVG POI: ₹{_fmt_price(r.get('fvg_poi_low'))}–₹{_fmt_price(r.get('fvg_poi_high'))}")
                if r.get("ob_poi_low") is not None and r.get("ob_poi_high") is not None:
                    poi_lines.append(f"OB POI: ₹{_fmt_price(r.get('ob_poi_low'))}–₹{_fmt_price(r.get('ob_poi_high'))}")
                cells[4].text = "\n".join(poi_lines) if poi_lines else "—"
    doc.add_paragraph("BOS/CHoCH requires a completed candle close beyond the swing level; wick-only breaks are ignored.")
    doc.add_paragraph("SMC zones are algorithmic approximations. Verify charts before trading.")
    doc.save(output)
    return output.getvalue()


def send_email(text, results=None, scanned=0, failed=0):
    """Send structured HTML/plain-text report plus CSV, XLSX, TXT, and DOCX attachments."""
    username = os.environ.get("EMAIL_USER", "").strip()
    app_password = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    recipient = os.environ.get("EMAIL_TO", "").strip()
    if not all([username, app_password, recipient]):
        raise RuntimeError("Email secrets are missing: EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_TO.")

    results = results or []
    now = datetime.now(IST)
    date_tag = now.strftime("%Y-%m-%d")
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = recipient
    msg["Subject"] = f"Nifty 200 Daily SMC Scan — {now.strftime('%d %b %Y')}"
    msg.set_content(text)
    msg.add_alternative(_format_html(results, scanned, failed), subtype="html")

    attachments = [
        (f"nifty200_smc_scan_{date_tag}.txt", text.encode("utf-8"), "text", "plain"),
        (f"nifty200_smc_scan_{date_tag}.csv", _make_csv_bytes(results), "text", "csv"),
        (f"nifty200_smc_scan_{date_tag}.xlsx", _make_xlsx_bytes(results, scanned, failed),
         "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (f"nifty200_smc_scan_{date_tag}.docx", _make_docx_bytes(text, results, scanned, failed),
         "application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ]
    for filename, payload, maintype, subtype in attachments:
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(username, app_password)
        server.send_message(msg)
    LOG.info("Email report with CSV/XLSX/TXT/DOCX attachments sent to %s", recipient)


def main():
    symbols = fetch_symbols()
    LOG.info("Loaded %d Nifty 200 constituents", len(symbols))
    # Daily bars provide enough history to construct weekly/monthly structure.
    daily_data = download_chunks(symbols, period="5y", interval="1d", chunk_size=10)
    # Yahoo hourly history is limited and unofficial; availability varies by symbol.
    hourly_data = download_chunks(symbols, period="730d", interval="1h", chunk_size=10)
    # Yahoo Finance generally limits 30-minute intraday history to about 60 days.
    thirty_minute_data = download_chunks(symbols, period="60d", interval="30m", chunk_size=10)

    results, failed = [], 0
    for symbol in symbols:
        try:
            matches, status = analyse_symbol(
                symbol, daily_data.get(symbol), hourly_data.get(symbol), thirty_minute_data.get(symbol)
            )
            results.extend(matches)
            if status != "ok":
                failed += 1
                LOG.warning("%s skipped: %s", symbol, status)
        except Exception:
            failed += 1
            LOG.exception("Analysis failed for %s", symbol)

    message = format_message(results, len(symbols), failed)
    print(message)
    # Do not send a misleading normal report or signal attachments when too much data is missing.
    if failed > max(5, int(len(symbols) * 0.10)):
        message = ("NIFTY 200 SCAN FAILED: data unavailable for too many constituents/timeframes. "
                   f"Coverage {len(symbols)-failed}/{len(symbols)}. Check GitHub Actions logs.")
        send_email(message, results=[], scanned=len(symbols), failed=failed)
    else:
        send_email(message, results=results, scanned=len(symbols), failed=failed)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.exception("Scanner failed")
        # Best effort failure notification, if messaging credentials are configured.
        try:
            send_email("NIFTY 200 SCAN FAILED. Please check the GitHub Actions run logs.")
        except Exception:
            LOG.exception("Could not send failure alert")
        sys.exit(1)
