import json
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


def pct_to_sig(pct_val):
    if pct_val is None:
        return ""
    if pct_val > 0.15:
        return "bull"
    if pct_val < -0.15:
        return "bear"
    return "flat"


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

    # Fallback: collect unique expiry strings from records.data if expiryDates is empty
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

    # Monthly expiry is typically the final expiry of the month
    same_month = [item for item in parsed if item[0].year == nearest_weekly_dt.year and item[0].month == nearest_weekly_dt.month]
    if same_month:
        monthly_dt, monthly_str = same_month[-1]
    else:
        next_month_dt = parsed[-1][0]
        next_month_items = [item for item in parsed if item[0].year == next_month_dt.year and item[0].month == next_month_dt.month]
        monthly_dt, monthly_str = next_month_items[-1]

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

    session.get("https://www.nseindia.com/option-chain", timeout=12)

    all_ind = session.get("https://www.nseindia.com/api/allIndices", timeout=12).json()
    nifty_eq = session.get(
        "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050", timeout=12
    ).json()
    oc_data = session.get(
        "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY", timeout=12
    ).json()

    indices_by_name = {item.get("index"): item for item in all_ind.get("data", [])}
    nifty_meta = nifty_eq.get("metadata", {})
    adv_dec = nifty_eq.get("advance", {})

    nifty_50_item = indices_by_name.get("NIFTY 50", {})
    bank_item = indices_by_name.get("NIFTY BANK") or indices_by_name.get("NIFTY FIN SERVICE", {})
    oil_item = indices_by_name.get("NIFTY OIL & GAS", {})
    it_item = indices_by_name.get("NIFTY IT", {})

    high_val = nifty_50_item.get("high") or nifty_meta.get("high")
    low_val = nifty_50_item.get("low") or nifty_meta.get("low")
    ltp_val = nifty_50_item.get("last") or nifty_meta.get("lastPrice")

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    oc_analysis = analyze_option_chain(oc_data, ltp_val, now_ist.date(), primary_expiry=primary_expiry)
    primary = oc_analysis["primary"]

    payload = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "expiry_scope": primary_expiry,
        "weekly_expiry": oc_analysis["weekly"]["expiry"],
        "monthly_expiry": oc_analysis["monthly"]["expiry"],
        "nse_adv": None,
        "nse_dec": None,
        "nifty_adv": int(adv_dec.get("advances", 0)),
        "nifty_dec": int(adv_dec.get("declines", 0)),
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
        f"Updated nse_data.json ({primary_expiry} -> W:{payload['weekly_expiry']} | M:{payload['monthly_expiry']}) at {payload['updated_at']}"
    )


if __name__ == "__main__":
    fetch_nse_snapshot(primary_expiry="weekly")
