import json
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

BASE_URL = "https://www.nseindia.in"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/option-chain",
    "Origin": BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

RETRYABLE_STATUS_CODES = {401, 403, 408, 425, 429, 500, 502, 503, 504}


def pct_to_sig(pct_val):
    if pct_val is None:
        return ""
    if pct_val > 0.15:
        return "bull"
    if pct_val < -0.15:
        return "bear"
    return "flat"


def to_int_or_none(val):
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def warm_up_session(session):
    """Refresh NSE session cookies on nseindia.in."""
    resp = session.get(f"{BASE_URL}/option-chain", timeout=15, allow_redirects=True)
    resp.raise_for_status()
    time.sleep(random.uniform(0.8, 1.5))


def request_with_retry(
    session,
    url,
    max_retries=4,
    base_delay=1.5,
    max_delay=20.0,
    timeout=15,
    expect_json=True,
    allow_404=False,
):
    """
    Perform an HTTP GET with exponential backoff + jitter.
    Re-warms NSE cookies on auth/rate-limit responses.
    """
    last_err = None

    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)

            if allow_404 and resp.status_code == 404:
                print(f"[Info] {url} returned 404; using fallback data.")
                return None

            if resp.status_code in RETRYABLE_STATUS_CODES:
                raise requests.HTTPError(
                    f"Retryable HTTP {resp.status_code} for {resp.url}", response=resp
                )

            resp.raise_for_status()

            if expect_json:
                return resp.json()
            return resp.text

        except (requests.RequestException, ValueError) as err:
            last_err = err
            if attempt >= max_retries:
                break

            status = getattr(getattr(err, "response", None), "status_code", None)
            if status in (401, 403, 429):
                try:
                    warm_up_session(session)
                except requests.RequestException:
                    pass

            exp_wait = min(max_delay, base_delay * (2 ** attempt))
            jitter = random.uniform(0.25, 0.85)
            sleep_for = exp_wait + jitter
            print(
                f"[Retry {attempt + 1}/{max_retries}] {url} failed ({err}). "
                f"Backing off {sleep_for:.2f}s..."
            )
            time.sleep(sleep_for)

    raise RuntimeError(f"Failed to fetch {url} after {max_retries + 1} attempts") from last_err


def extract_overall_nse_breadth(session, all_ind_json):
    for item in all_ind_json.get("data", []):
        idx_name = (item.get("index") or "").upper()
        if idx_name in ("NIFTY TOTAL MARKET", "ALL", "NIFTY 500"):
            adv = to_int_or_none(item.get("advances"))
            dec = to_int_or_none(item.get("declines"))
            if adv is not None and dec is not None:
                return adv, dec

    for idx_query in ("NIFTY%20500", "SECURITIES%20IN%20F%26O"):
        try:
            time.sleep(random.uniform(0.6, 1.2))
            payload = request_with_retry(
                session,
                f"{BASE_URL}/api/equity-stockIndices?index={idx_query}",
                max_retries=2,
                base_delay=1.0,
                timeout=12,
                allow_404=True,
            )
            if not isinstance(payload, dict):
                continue
            adv_obj = payload.get("advance") or {}
            adv = to_int_or_none(adv_obj.get("advances"))
            dec = to_int_or_none(adv_obj.get("declines"))
            if adv is not None and dec is not None:
                return adv, dec
        except Exception:
            continue

    return None, None


def parse_nse_expiry(exp_str):
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(exp_str, fmt).date()
        except ValueError:
            continue
    return None


def resolve_expiries(records, today_ist):
    raw_expiries = records.get("expiryDates") or []
    parsed = []
    for s in raw_expiries:
        dt = parse_nse_expiry(s)
        if dt and dt >= today_ist:
            parsed.append((dt, s))

    if not parsed:
        seen = set()
        for r in records.get("data", []):
            s = r.get("expiryDate")
            if s and s not in seen:
                seen.add(s)
                dt = parse_nse_expiry(s)
                if dt and dt >= today_ist:
                    parsed.append((dt, s))

    parsed.sort(key=lambda x: x[0])
    if not parsed:
        return None, None

    nearest_weekly_dt, nearest_weekly_str = parsed[0]

    same_month = [
        item for item in parsed
        if item[0].year == nearest_weekly_dt.year and item[0].month == nearest_weekly_dt.month
    ]
    if same_month:
        _, monthly_str = same_month[-1]
    else:
        next_month_dt = parsed[-1][0]
        next_month_items = [
            item for item in parsed
            if item[0].year == next_month_dt.year and item[0].month == next_month_dt.month
        ]
        _, monthly_str = next_month_items[-1]

    return nearest_weekly_str, monthly_str


def compute_expiry_metrics(rows_for_expiry, underlying):
    if not rows_for_expiry or not underlying:
        return {
            "opt_vol": "",
            "opt_oi": "",
            "atm_both_flag": "no",
            "atm_strike": None,
            "avg_call_pct": 0.0,
            "avg_put_pct": 0.0,
            "call_oi_chg": 0.0,
            "put_oi_chg": 0.0,
        }

    strikes = sorted({r.get("strikePrice") for r in rows_for_expiry if r.get("strikePrice") is not None})
    atm_strike = min(strikes, key=lambda s: abs(s - underlying)) if strikes else None

    near_rows = [
        r for r in rows_for_expiry
        if r.get("strikePrice") is not None and abs(r.get("strikePrice") - atm_strike) <= 500
    ] or rows_for_expiry

    call_pct_changes = []
    put_pct_changes = []
    call_oi_chg_sum = 0.0
    put_oi_chg_sum = 0.0
    call_oi_tot_sum = 0.0
    put_oi_tot_sum = 0.0
    oi_contracts = []

    for r in near_rows:
        strike = r.get("strikePrice")
        ce = r.get("CE")
        pe = r.get("PE")

        if isinstance(ce, dict):
            last = ce.get("lastPrice")
            prev = ce.get("previousClose")
            if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and prev > 0:
                call_pct_changes.append(((last - prev) / prev) * 100.0)
            oi_chg = ce.get("changeinOpenInterest") or 0.0
            oi_tot = ce.get("openInterest") or 0.0
            call_oi_chg_sum += oi_chg
            call_oi_tot_sum += oi_tot
            oi_contracts.append(("CE", strike, oi_tot))

        if isinstance(pe, dict):
            last = pe.get("lastPrice")
            prev = pe.get("previousClose")
            if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and prev > 0:
                put_pct_changes.append(((last - prev) / prev) * 100.0)
            oi_chg = pe.get("changeinOpenInterest") or 0.0
            oi_tot = pe.get("openInterest") or 0.0
            put_oi_chg_sum += oi_chg
            put_oi_tot_sum += oi_tot
            oi_contracts.append(("PE", strike, oi_tot))

    avg_call_pct = sum(call_pct_changes) / len(call_pct_changes) if call_pct_changes else 0.0
    avg_put_pct = sum(put_pct_changes) / len(put_pct_changes) if put_pct_changes else 0.0

    if avg_call_pct > avg_put_pct + 2.0:
        opt_vol = "bull"
    elif avg_put_pct > avg_call_pct + 2.0:
        opt_vol = "bear"
    else:
        opt_vol = ""

    put_oi_score = put_oi_chg_sum if (put_oi_chg_sum != 0 or call_oi_chg_sum != 0) else put_oi_tot_sum
    call_oi_score = call_oi_chg_sum if (put_oi_chg_sum != 0 or call_oi_chg_sum != 0) else call_oi_tot_sum

    if put_oi_score > call_oi_score:
        opt_oi = "bull"
    elif call_oi_score > put_oi_score:
        opt_oi = "bear"
    else:
        opt_oi = ""

    top_oi = sorted(oi_contracts, key=lambda x: x[2], reverse=True)[:6]
    top_set = {(opt_type, strike) for opt_type, strike, _ in top_oi}
    atm_both_active = ("CE", atm_strike) in top_set and ("PE", atm_strike) in top_set

    return {
        "opt_vol": opt_vol,
        "opt_oi": opt_oi,
        "atm_both_flag": "yes" if atm_both_active else "no",
        "atm_strike": atm_strike,
        "avg_call_pct": round(avg_call_pct, 2),
        "avg_put_pct": round(avg_put_pct, 2),
        "call_oi_chg": call_oi_chg_sum,
        "put_oi_chg": put_oi_chg_sum,
    }


def analyze_option_chain(oc_json, spot_price, today_ist, primary_expiry="weekly"):
    records = oc_json.get("records", {})
    data_rows = records.get("data", [])
    underlying = spot_price or records.get("underlyingValue") or 0

    weekly_exp, monthly_exp = resolve_expiries(records, today_ist)

    weekly_rows = [r for r in data_rows if r.get("expiryDate") == weekly_exp] if weekly_exp else []
    monthly_rows = [r for r in data_rows if r.get("expiryDate") == monthly_exp] if monthly_exp else []

    weekly_metrics = compute_expiry_metrics(weekly_rows, underlying)
    monthly_metrics = compute_expiry_metrics(monthly_rows, underlying)

    chosen = monthly_metrics if primary_expiry == "monthly" else weekly_metrics

    return {
        "primary": chosen,
        "weekly": {"expiry": weekly_exp, **weekly_metrics},
        "monthly": {"expiry": monthly_exp, **monthly_metrics},
    }


def fetch_nse_snapshot(primary_expiry="weekly"):
    session = requests.Session()
    session.headers.update(HEADERS)

    warm_up_session(session)

    all_ind = request_with_retry(
        session, f"{BASE_URL}/api/allIndices", timeout=15
    )
    time.sleep(random.uniform(0.7, 1.3))

    nifty_eq = None
    try:
        nifty_eq = request_with_retry(
            session,
            f"{BASE_URL}/api/equity-stockIndices?index=NIFTY%2050",
            max_retries=2,
            timeout=15,
            allow_404=True,
        )
    except RuntimeError as err:
        print(f"[Warn] equity-stockIndices failed ({err}); falling back to allIndices/option-chain.")

    time.sleep(random.uniform(0.7, 1.3))

    oc_data = request_with_retry(
        session,
        f"{BASE_URL}/api/option-chain-indices?symbol=NIFTY",
        timeout=15,
    )

    nse_adv, nse_dec = extract_overall_nse_breadth(session, all_ind)

    indices_by_name = {item.get("index"): item for item in all_ind.get("data", [])}
    nifty_eq = nifty_eq if isinstance(nifty_eq, dict) else {}
    nifty_meta = nifty_eq.get("metadata") or {}
    adv_dec = nifty_eq.get("advance") or {}

    nifty_50_item = indices_by_name.get("NIFTY 50") or {}
    bank_item = indices_by_name.get("NIFTY BANK") or indices_by_name.get("NIFTY FIN SERVICE") or {}
    oil_item = indices_by_name.get("NIFTY OIL & GAS") or {}
    it_item = indices_by_name.get("NIFTY IT") or {}

    oc_underlying = (oc_data.get("records") or {}).get("underlyingValue")

    high_val = nifty_50_item.get("high") or nifty_meta.get("high")
    low_val = nifty_50_item.get("low") or nifty_meta.get("low")
    ltp_val = nifty_50_item.get("last") or nifty_meta.get("lastPrice") or oc_underlying

    nifty_adv = to_int_or_none(adv_dec.get("advances"))
    nifty_dec = to_int_or_none(adv_dec.get("declines"))
    if nifty_adv is None:
        nifty_adv = to_int_or_none(nifty_50_item.get("advances"))
    if nifty_dec is None:
        nifty_dec = to_int_or_none(nifty_50_item.get("declines"))

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    oc_analysis = analyze_option_chain(oc_data, ltp_val, now_ist.date(), primary_expiry=primary_expiry)
    primary = oc_analysis["primary"]

    payload = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "expiry_scope": primary_expiry,
        "weekly_expiry": oc_analysis["weekly"]["expiry"],
        "monthly_expiry": oc_analysis["monthly"]["expiry"],
        "nse_adv": nse_adv,
        "nse_dec": nse_dec,
        "nifty_adv": nifty_adv,
        "nifty_dec": nifty_dec,
        "sec_bank": pct_to_sig(bank_item.get("percentChange")),
        "sec_oil": pct_to_sig(oil_item.get("percentChange")),
        "sec_it": pct_to_sig(it_item.get("percentChange")),
        "opt_vol": primary["opt_vol"],
        "opt_oi": primary["opt_oi"],
        "atm_both_flag": primary["atm_both_flag"],
        "atm_chart": "",
        "high": high_val,
        "low": low_val,
        "ltp": ltp_val,
        "oc_diagnostics": {
            "weekly": oc_analysis["weekly"],
            "monthly": oc_analysis["monthly"],
        },
    }

    with open("nse_data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(
        f"Updated nse_data.json | Overall A/D: {nse_adv}/{nse_dec} | "
        f"NIFTY A/D: {nifty_adv}/{nifty_dec} at {payload['updated_at']}"
    )


if __name__ == "__main__":
    fetch_nse_snapshot(primary_expiry="weekly")
