import json
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/",
}

def pct_to_sig(pct_val):
    if pct_val is None:
        return ""
    if pct_val > 0.15:
        return "bull"
    if pct_val < -0.15:
        return "bear"
    return "flat"

def fetch_nse_snapshot():
    session = requests.Session()
    session.headers.update(HEADERS)
    # Initialize NSE cookies
    session.get("https://www.nseindia.com", timeout=10)

    all_ind = session.get("https://www.nseindia.com/api/allIndices", timeout=10).json()
    nifty_eq = session.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050", timeout=10).json()

    indices_by_name = {item.get("index"): item for item in all_ind.get("data", [])}
    nifty_meta = nifty_eq.get("metadata", {})
    adv_dec = nifty_eq.get("advance", {})

    nifty_50_item = indices_by_name.get("NIFTY 50", {})
    bank_item = indices_by_name.get("NIFTY BANK") or indices_by_name.get("NIFTY FIN SERVICE", {})
    oil_item = indices_by_name.get("NIFTY OIL & GAS", {})
    it_item = indices_by_name.get("NIFTY IT", {})

    ist_now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")

    payload = {
        "updated_at": ist_now,
        "nse_adv": None,  # Optional: fill from market turnover/advance endpoint if needed
        "nse_dec": None,
        "nifty_adv": int(adv_dec.get("advances", 0)),
        "nifty_dec": int(adv_dec.get("declines", 0)),
        "sec_bank": pct_to_sig(bank_item.get("percentChange")),
        "sec_oil": pct_to_sig(oil_item.get("percentChange")),
        "sec_it": pct_to_sig(it_item.get("percentChange")),
        "opt_vol": "",
        "opt_oi": "",
        "atm_both_flag": "no",
        "atm_chart": "",
        "high": nifty_50_item.get("high") or nifty_meta.get("high"),
        "low": nifty_50_item.get("low") or nifty_meta.get("low"),
        "ltp": nifty_50_item.get("last") or nifty_meta.get("lastPrice"),
    }

    with open("nse_data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print("Updated nse_data.json at", ist_now)

if __name__ == "__main__":
    fetch_nse_snapshot()
