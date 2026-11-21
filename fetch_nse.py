import csv
import io
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

PAGE_URL = "https://www.nseindia.in/option-chain"


def to_float_or_none(val):
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "NA", "N/A", "null", "None"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_nse_expiry(exp_str):
    if not exp_str:
        return None
    s = str(exp_str).strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def resolve_expiries(expiry_strings, today_ist):
    parsed = []
    for s in expiry_strings:
        dt = parse_nse_expiry(s)
        if dt and dt >= today_ist:
            parsed.append((dt, s))
    if not parsed:
        for s in expiry_strings:
            dt = parse_nse_expiry(s)
            if dt:
                parsed.append((dt, s))

    parsed.sort(key=lambda x: x[0])
    if not parsed:
        return None, None

    wk_dt, wk_str = parsed[0]
    same_month = [item for item in parsed if item[0].year == wk_dt.year and item[0].month == wk_dt.month]
    if same_month:
        _, mo_str = same_month[-1]
    else:
        last_dt = parsed[-1][0]
        last_month_items = [item for item in parsed if item[0].year == last_dt.year and item[0].month == last_dt.month]
        _, mo_str = last_month_items[-1]

    return wk_str, mo_str


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

    strikes = sorted({r["strike"] for r in rows_for_expiry if r.get("strike") is not None})
    if not strikes:
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

    atm_strike = min(strikes, key=lambda s: abs(s - underlying))
    near_rows = [
        r for r in rows_for_expiry
        if r.get("strike") is not None and abs(r["strike"] - atm_strike) <= 500
    ] or rows_for_expiry

    call_pcts = []
    put_pcts = []
    call_oi_chg_sum = 0.0
    put_oi_chg_sum = 0.0
    call_oi_tot_sum = 0.0
    put_oi_tot_sum = 0.0
    oi_contracts = []

    for r in near_rows:
        strike = r["strike"]
        ce = r.get("CE") or {}
        pe = r.get("PE") or {}

        ce_pct = ce.get("pct_chg")
        if ce_pct is None and ce.get("ltp") is not None and ce.get("prev_close") not in (None, 0):
            ce_pct = ((ce["ltp"] - ce["prev_close"]) / ce["prev_close"]) * 100.0
        if ce_pct is not None:
            call_pcts.append(ce_pct)
        ce_oi_chg = ce.get("oi_chg") or 0.0
        ce_oi = ce.get("oi") or 0.0
        call_oi_chg_sum += ce_oi_chg
        call_oi_tot_sum += ce_oi
        oi_contracts.append(("CE", strike, ce_oi))

        pe_pct = pe.get("pct_chg")
        if pe_pct is None and pe.get("ltp") is not None and pe.get("prev_close") not in (None, 0):
            pe_pct = ((pe["ltp"] - pe["prev_close"]) / pe["prev_close"]) * 100.0
        if pe_pct is not None:
            put_pcts.append(pe_pct)
        pe_oi_chg = pe.get("oi_chg") or 0.0
        pe_oi = pe.get("oi") or 0.0
        put_oi_chg_sum += pe_oi_chg
        put_oi_tot_sum += pe_oi
        oi_contracts.append(("PE", strike, pe_oi))

    avg_call_pct = sum(call_pcts) / len(call_pcts) if call_pcts else 0.0
    avg_put_pct = sum(put_pcts) / len(put_pcts) if put_pcts else 0.0

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
    top_set = {(opt_t, st) for opt_t, st, _ in top_oi}
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


def normalize_from_oc_json(oc_json):
    records = oc_json.get("records") or {}
    underlying = to_float_or_none(records.get("underlyingValue"))
    expiries = records.get("expiryDates") or []
    rows = []

    for item in records.get("data") or []:
        strike = to_float_or_none(item.get("strikePrice"))
        exp = item.get("expiryDate")
        if strike is None:
            continue
        ce_raw = item.get("CE") if isinstance(item.get("CE"), dict) else {}
        pe_raw = item.get("PE") if isinstance(item.get("PE"), dict) else {}
        rows.append({
            "expiry": exp,
            "strike": strike,
            "CE": {
                "ltp": to_float_or_none(ce_raw.get("lastPrice")),
                "prev_close": to_float_or_none(ce_raw.get("previousClose")),
                "pct_chg": to_float_or_none(ce_raw.get("pchangeinOpenInterest")),
                "oi_chg": to_float_or_none(ce_raw.get("changeinOpenInterest")),
                "oi": to_float_or_none(ce_raw.get("openInterest")),
            },
            "PE": {
                "ltp": to_float_or_none(pe_raw.get("lastPrice")),
                "prev_close": to_float_or_none(pe_raw.get("previousClose")),
                "pct_chg": to_float_or_none(pe_raw.get("pchangeinOpenInterest")),
                "oi_chg": to_float_or_none(pe_raw.get("changeinOpenInterest")),
                "oi": to_float_or_none(pe_raw.get("openInterest")),
            },
        })

    return underlying, expiries, rows


def normalize_from_csv_text(csv_text, selected_expiry=None):
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    expiries = []
    seen_exp = set()

    for raw in reader:
        if not raw:
            continue
        row = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        exp = row.get("expiry date") or row.get("expiry") or selected_expiry
        if exp and exp not in seen_exp:
            seen_exp.add(exp)
            expiries.append(exp)

        strike = to_float_or_none(row.get("strike price") or row.get("strike"))
        if strike is None:
            continue

        rows.append({
            "expiry": exp,
            "strike": strike,
            "CE": {
                "ltp": to_float_or_none(row.get("ce ltp") or row.get("call ltp") or row.get("ltp")),
                "prev_close": to_float_or_none(row.get("ce prev close") or row.get("prev close")),
                "pct_chg": to_float_or_none(row.get("ce chng") or row.get("chng")),
                "oi_chg": to_float_or_none(row.get("ce chng in oi") or row.get("chng in oi")),
                "oi": to_float_or_none(row.get("ce oi") or row.get("oi")),
            },
            "PE": {
                "ltp": to_float_or_none(row.get("pe ltp") or row.get("put ltp")),
                "prev_close": to_float_or_none(row.get("pe prev close")),
                "pct_chg": to_float_or_none(row.get("pe chng")),
                "oi_chg": to_float_or_none(row.get("pe chng in oi")),
                "oi": to_float_or_none(row.get("pe oi")),
            },
        })

    return None, expiries, rows


def scrape_option_chain_page():
    captured_json = []
    captured_csv = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()

        def handle_response(resp):
            ctype = (resp.headers.get("content-type") or "").lower()
            url = resp.url.lower()
            try:
                if "option-chain" in url and "json" in ctype:
                    captured_json.append(resp.json())
                elif "csv" in ctype or url.endswith(".csv"):
                    captured_csv.append(resp.text())
            except Exception:
                pass

        page.on("response", handle_response)

        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)

        # Extract visible Underlying Index and active expiry from page text/selects
        body_text = page.inner_text("body")
        underlying_dom = None
        for line in body_text.splitlines():
            if "Underlying Index" in line and "NIFTY" in line:
                parts = line.split("NIFTY", 1)[-1].split()
                for tok in parts:
                    val = to_float_or_none(tok)
                    if val is not None and val > 1000:
                        underlying_dom = val
                        break

        selected_expiry = None
        try:
            sel = page.locator("select").nth(2)
            if sel.count() > 0:
                selected_expiry = sel.input_value().strip()
        except Exception:
            pass

        # If no JSON/CSV response was intercepted yet, try clicking "Download (.csv)"
        if not captured_json and not captured_csv:
            try:
                dl_btn = page.get_by_text("Download (.csv)", exact=False).first
                if dl_btn.count() > 0:
                    with page.expect_download(timeout=10000) as dl_info:
                        dl_btn.click()
                    dl_path = dl_info.value.path()
                    with open(dl_path, "r", encoding="utf-8-sig", errors="replace") as f:
                        captured_csv.append(f.read())
            except Exception:
                pass

        # Fallback: parse visible HTML table rows (CALLS | STRIKE | PUTS)
        dom_rows = []
        if not captured_json and not captured_csv:
            raw_table_rows = page.evaluate(
                """() => Array.from(document.querySelectorAll('table tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th, td')).map(c => c.innerText.trim())
                )"""
            )
            for cells in raw_table_rows:
                # Standard NSE OC table has 21 columns: 10 CALLS + 1 STRIKE + 10 PUTS
                if len(cells) >= 21:
                    strike = to_float_or_none(cells[10])
                    if strike is not None:
                        dom_rows.append({
                            "expiry": selected_expiry,
                            "strike": strike,
                            "CE": {
                                "oi": to_float_or_none(cells[0]),
                                "oi_chg": to_float_or_none(cells[1]),
                                "pct_chg": to_float_or_none(cells[5]),
                                "ltp": to_float_or_none(cells[4]),
                                "prev_close": None,
                            },
                            "PE": {
                                "ltp": to_float_or_none(cells[15]),
                                "pct_chg": to_float_or_none(cells[14]),
                                "oi_chg": to_float_or_none(cells[18]),
                                "oi": to_float_or_none(cells[19]),
                                "prev_close": None,
                            },
                        })

        browser.close()

    if captured_json:
        underlying, expiries, rows = normalize_from_oc_json(captured_json[-1])
        source_mode = "page_json"
    elif captured_csv:
        underlying, expiries, rows = normalize_from_csv_text(captured_csv[-1], selected_expiry)
        source_mode = "page_csv"
    else:
        underlying, expiries, rows = None, [selected_expiry] if selected_expiry else [], dom_rows
        source_mode = "page_dom"

    final_underlying = underlying or underlying_dom
    return source_mode, final_underlying, expiries, rows


def load_existing_json(path="nse_data.json"):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def fetch_nse_snapshot(primary_expiry="weekly"):
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    existing = load_existing_json("nse_data.json")

    source_mode, underlying, expiries, rows = scrape_option_chain_page()
    wk_exp, mo_exp = resolve_expiries(expiries, now_ist.date())

    wk_rows = [r for r in rows if r.get("expiry") == wk_exp] if wk_exp else rows
    mo_rows = [r for r in rows if r.get("expiry") == mo_exp] if mo_exp else rows

    wk_metrics = compute_expiry_metrics(wk_rows, underlying)
    mo_metrics = compute_expiry_metrics(mo_rows, underlying)

    wk_diag = {"expiry": wk_exp, **wk_metrics}
    mo_diag = {"expiry": mo_exp, **mo_metrics}
    primary = mo_diag if primary_expiry == "monthly" else wk_diag

    payload = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "fetch_status": f"ok ({source_mode})",
        "source_url": PAGE_URL,
        "expiry_scope": primary_expiry,
        "weekly_expiry": wk_exp,
        "monthly_expiry": mo_exp,
        "nse_adv": existing.get("nse_adv"),
        "nse_dec": existing.get("nse_dec"),
        "nifty_adv": existing.get("nifty_adv"),
        "nifty_dec": existing.get("nifty_dec"),
        "sec_bank": existing.get("sec_bank", ""),
        "sec_oil": existing.get("sec_oil", ""),
        "sec_it": existing.get("sec_it", ""),
        "opt_vol": primary.get("opt_vol", ""),
        "opt_oi": primary.get("opt_oi", ""),
        "atm_both_flag": primary.get("atm_both_flag", "no"),
        "atm_chart": existing.get("atm_chart", ""),
        "high": existing.get("high"),
        "low": existing.get("low"),
        "ltp": underlying if underlying is not None else existing.get("ltp"),
        "oc_diagnostics": {
            "weekly": wk_diag,
            "monthly": mo_diag,
        },
    }

    with open("nse_data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(
        f"Updated nse_data.json ({source_mode}) | Underlying: {payload['ltp']} | "
        f"Weekly: {wk_exp} | Monthly: {mo_exp} at {payload['updated_at']}"
    )


if __name__ == "__main__":
    fetch_nse_snapshot(primary_expiry="weekly")
