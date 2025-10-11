
import os, json, csv, pytz, smtplib, requests
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()
PR_TZ = pytz.timezone("America/Puerto_Rico")

AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
AMADEUS_ENV = os.getenv("AMADEUS_ENV", "test").lower()
CURRENCY = os.getenv("CURRENCY", "USD")

NOTIFY_CHANNELS = [c.strip().lower() for c in os.getenv("NOTIFY_CHANNELS", "").split(",") if c.strip()]

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_USE_TLS = str(os.getenv("SMTP_USE_TLS", "true")).lower() == "true"
SMTP_FROM = os.getenv("SMTP_FROM")
SMTP_TO = [e.strip() for e in os.getenv("SMTP_TO", "").split(",") if e.strip()]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

STATE_PATH = "price_state.json"
HISTORY_CSV = "price_history.csv"

TRAVELERS = [{"id": str(i), "travelerType": "ADULT"} for i in range(1,5)]
ORIGIN_DESTINATIONS = [
    {"id": "1", "originLocationCode": "SJU", "destinationLocationCode": "FCO", "departureDateTimeRange": {"date": "2026-05-06"}},
    {"id": "2", "originLocationCode": "FCO", "destinationLocationCode": "MAD", "departureDateTimeRange": {"date": "2026-05-17"}},
    {"id": "3", "originLocationCode": "MAD", "destinationLocationCode": "SJU", "departureDateTimeRange": {"date": "2026-05-20"}},
]
REQUIRED_DEPARTURES = {
    "1": {"origin": "SJU", "date": "2026-05-06", "time": "20:25"},
    "2": {"origin": "FCO", "date": "2026-05-17", "time": "14:45"},
    "3": {"origin": "MAD", "date": "2026-05-20", "time": "15:50"},
}

def amadeus_host():
    return "https://api.amadeus.com" if AMADEUS_ENV == "production" else "https://test.api.amadeus.com"

def get_access_token():
    url = amadeus_host() + "/v1/security/oauth2/token"
    data = {"grant_type": "client_credentials", "client_id": AMADEUS_API_KEY, "client_secret": AMADEUS_API_SECRET}
    r = requests.post(url, data=data, timeout=30); r.raise_for_status()
    return r.json()["access_token"]

def search_flight_offers(token):
    url = amadeus_host() + "/v2/shopping/flight-offers"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "currencyCode": CURRENCY,
        "originDestinations": ORIGIN_DESTINATIONS,
        "travelers": TRAVELERS,
        "sources": ["GDS"],
        "searchCriteria": {
            "additionalInformation": {"brandedFares": True},
            "flightFilters": {
                "carrierRestrictions": {"includedCarrierCodes": ["IB"]},
                "cabinRestrictions": [{
                    "cabin": "ECONOMY",
                    "coverage": "MOST_SEGMENTS",
                    "originDestinationIds": ["1","2","3"]
                }],
            },
            "maxFlightOffers": 200
        }
    }
    r = requests.post(url, headers=headers, json=body, timeout=90); r.raise_for_status()
    return r.json()

def parse_iso(dt_str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

def format_duration(dur):
    if not dur or not dur.startswith("PT"): return dur or ""
    h,m=0,0; tmp=dur[2:]
    if "H" in tmp:
        p=tmp.split("H"); h=int(p[0]) if p[0] else 0; tmp=p[1] if len(p)>1 else ""
    if "M" in tmp:
        p=tmp.split("M"); m=int(p[0]) if p[0] else 0
    return f"{h}h {m}m".strip()

def offer_matches_required_times(off):
    itins = off.get("itineraries", [])
    if len(itins) < 3: return False
    for od_idx, itin in enumerate(itins, start=1):
        req = REQUIRED_DEPARTURES[str(od_idx)]
        segs = itin.get("segments", [])
        if not segs: return False
        first = segs[0]; dep=first.get("departure", {}); arr=first.get("arrival", {})
        origin_iata = dep.get("iataCode"); dest_expected = ORIGIN_DESTINATIONS[od_idx-1]["destinationLocationCode"]
        at = dep.get("at","")
        if origin_iata != req["origin"] or arr.get("iataCode") != dest_expected: return False
        if not at.startswith(req["date"]+"T"): return False
        hhmm = at.split("T")[1][:5] if "T" in at else at[11:16]
        if hhmm != req["time"]: return False
    return True

def branded_is_optima(off):
    for t in off.get("travelerPricings", []):
        for fd in t.get("fareDetailsBySegment", []):
            brand=(fd.get("brandedFare") or "").upper()
            if "OPTIMA" in brand or "OPTIMAL" in brand: return True
    return False

def price_float(off):
    try: return float(off.get("price", {}).get("grandTotal", "9999999"))
    except: return 9999999.0

def summarize_offer(off):
    total = off.get("price", {}).get("grandTotal", "0")
    try: total_f=float(total)
    except: total_f=0.0
    validating=",".join(off.get("validatingAirlineCodes", []))
    itins = off.get("itineraries", [])

    mins=[]; 
    for itin in itins:
        dur=itin.get("duration",""); h,m=0,0; tmp=dur[2:] if dur.startswith("PT") else ""
        if "H" in tmp:
            p=tmp.split("H"); h=int(p[0]) if p[0] else 0; tmp=p[1] if len(p)>1 else ""
        if "M" in tmp:
            p=tmp.split("M"); m=int(p[0]) if p[0] else 0
        mins.append(h*60+m)
    total_mins=sum(mins) if mins else 0.0
    per_leg_est = [(total_f*(mm/total_mins)) if total_mins>0 else (total_f/max(1,len(itins))) for mm in mins]

    lines=[f"Total: {CURRENCY} {total} | Validating: {validating}"]
    if itins:
        lines.append("Estimated price per leg (proportional to duration):")
        for idx,(itin,est) in enumerate(zip(itins, per_leg_est), start=1):
            lines.append(f"  Leg {idx}: ~{CURRENCY} {est:.2f} (dur: {format_duration(itin.get('duration'))})")
    for idx,itin in enumerate(itins, start=1):
        lines.append(f"  Itinerary {idx} (dur: {format_duration(itin.get('duration'))}):")
        for seg in itin.get("segments", []):
            carrier=seg.get("carrierCode",""); num=seg.get("number","")
            dep=seg.get("departure",{}); arr=seg.get("arrival",{})
            d_air=dep.get("iataCode",""); a_air=arr.get("iataCode","")
            d_time=dep.get("at",""); a_time=arr.get("at","")
            d_dt=parse_iso(d_time); a_dt=parse_iso(a_time)
            d_fmt=d_dt.strftime("%Y-%m-%d %H:%M") if d_dt else d_time
            a_fmt=a_dt.strftime("%Y-%m-%d %H:%M") if a_dt else a_time
            seg_dur=format_duration(seg.get("duration"))
            op=seg.get("operating",{}); op_car=op.get("carrierCode")
            op_note=f" (operated by {op_car})" if op_car and op_car!=carrier else ""
            lines.append(f"    {carrier}{num}{op_note}: {d_air} {d_fmt} → {a_air} {a_fmt} ({seg_dur})")
    tp=off.get("travelerPricings",[])
    if tp:
        try:
            per=[]; 
            for t in tp:
                per.append(f"{t.get('travelerType','')}: {t.get('price',{}).get('total','?')} {CURRENCY}")
            lines.append("  Traveler pricing: " + " | ".join(per))
        except: pass
    mark=" (OPTIMA detected)" if branded_is_optima(off) else ""
    return total_f, "\n".join(lines)+mark

def notify_email(subject, body):
    if not (SMTP_HOST and SMTP_FROM and SMTP_TO):
        print("[Email] Missing SMTP settings; skipping"); return
    msg=MIMEText(body, "plain", "utf-8"); msg["Subject"]=subject; msg["From"]=SMTP_FROM; msg["To"]=", ".join(SMTP_TO)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USE_TLS: server.starttls()
            if SMTP_USER and SMTP_PASS: server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, SMTP_TO, msg.as_string())
        print("[Email] Sent")
    except Exception as e:
        print("[Email Error]", e)

def notify_telegram(body):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[Telegram] Missing token/chat_id; skipping"); return
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r=requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": body}, timeout=30)
        if r.status_code!=200: print("[Telegram Error]", r.status_code, r.text)
        else: print("[Telegram] Sent")
    except Exception as e:
        print("[Telegram Error]", e)

def notify_discord(body):
    if not DISCORD_WEBHOOK_URL:
        print("[Discord] Missing webhook url; skipping"); return
    try:
        r=requests.post(DISCORD_WEBHOOK_URL, json={"content": body}, timeout=30)
        if r.status_code>=300: print("[Discord Error]", r.status_code, r.text)
        else: print("[Discord] Sent")
    except Exception as e:
        print("[Discord Error]", e)

def broadcast(subject, body):
    ch=set(NOTIFY_CHANNELS)
    if not ch:
        print("[Notify] No channels configured; printing only:\\n", body); return
    if "email" in ch: notify_email(subject, body)
    if "telegram" in ch: notify_telegram(body)
    if "discord" in ch: notify_discord(body)

def load_last_price():
    if not os.path.exists(STATE_PATH): return None
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f: obj=json.load(f); return float(obj.get("last_price"))
    except Exception: return None

def save_last_price(price):
    try:
        with open(STATE_PATH,"w",encoding="utf-8") as f: json.dump({"last_price": price}, f)
    except Exception as e: print("[State Save Error]", e)

def append_history(price, note=""):
    exists=os.path.exists(HISTORY_CSV)
    try:
        with open(HISTORY_CSV,"a",newline="",encoding="utf-8") as f:
            w=csv.writer(f)
            if not exists: w.writerow(["timestamp_pr","price","note"])
            w.writerow([datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"), f"{price:.2f}", note])
    except Exception as e: print("[History CSV Error]", e)

def main():
    if not (AMADEUS_API_KEY and AMADEUS_API_SECRET):
        raise RuntimeError("Missing Amadeus credentials")
    token=get_access_token()
    data=search_flight_offers(token)
    offers=data.get("data", [])
    exact=[]; optima=[]
    for off in offers:
        if offer_matches_required_times(off):
            exact.append(off)
            if branded_is_optima(off): optima.append(off)
    now_pr=datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    subject="Iberia multi-city — exact times (every 4h)"
    if not exact:
        msg=(f"[Iberia Watch] {now_pr}\\n"
             f"No exact-match offer for requested departures now.\\n"
             f"Required (local): SJU 20:25 (May 06), FCO 14:45 (May 17), MAD 15:50 (May 20).")
        print(msg); broadcast(subject, msg); return
    pool=optima if optima else exact
    pool.sort(key=price_float); chosen=pool[0]
    total_price, breakdown = summarize_offer(chosen)
    last=load_last_price()
    if last is None: note="(first run)"
    elif total_price>last: note=f"▲ Price up by {CURRENCY} {total_price-last:.2f}"
    elif total_price<last: note=f"▼ Price down by {CURRENCY} {last-total_price:.2f}"
    else: note="↔ No change"
    save_last_price(total_price); append_history(total_price, note)
    body=(f"[Iberia Watch] {now_pr}\\n"
          f"Exact multi-city (4 ADT) — Economy{' (OPTIMA)' if 'OPTIMA' in breakdown or 'OPTIMAL' in breakdown else ''}\\n"
          f"Total price: {CURRENCY} {total_price:.2f}  {note}\\n"
          f"Fixed local departures: SJU 20:25 (May 06), FCO 14:45 (May 17), MAD 15:50 (May 20)\\n\\n"
          f"{breakdown}\\n\\n"
          f"Note: per-leg amounts are ESTIMATES. Availability and prices change quickly.")
    print(body); broadcast(subject, body)

if __name__=="__main__":
    main()
