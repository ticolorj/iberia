import os
import json
import csv
import pytz
import smtplib
import requests
from datetime import datetime
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

PR_TZ = pytz.timezone("America/Puerto_Rico")

# ===== Config =====
TOP_N = int(os.getenv("TOP_N", "10"))   # cuántas ofertas listar (para no enviar mails gigantes)

# ===== Amadeus =====
AMADEUS_API_KEY = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
AMADEUS_ENV = os.getenv("AMADEUS_ENV", "test").lower()
CURRENCY = os.getenv("CURRENCY", "USD")

# ===== Notificaciones =====
NOTIFY_CHANNELS = [c.strip().lower() for c in os.getenv("NOTIFY_CHANNELS", "").split(",") if c.strip()]

# Email (SMTP)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_USE_TLS = str(os.getenv("SMTP_USE_TLS", "true")).lower() == "true"
SMTP_FROM = os.getenv("SMTP_FROM")
SMTP_TO = [e.strip() for e in os.getenv("SMTP_TO", "").split(",") if e.strip()]

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Discord
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

import urllib.parse

def link_google_flights_multicity():
    # Uses Google Flights natural-language query via the `q` parameter.
    # Reference: https://www.google.com/travel/flights?q=...
    # Source: StackOverflow shows that the 'q' parameter works for building queries.
    q = (
        "Flights from SJU to FCO on 2026-05-06, "
        "from FCO to MAD on 2026-05-17, "
        "from MAD to SJU on 2026-05-20"
    )
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)

def link_iberia_multicity():
    # Official Iberia multi-legs page (can fill the form there)
    return "https://www.iberia.com/us/flight-search-engine/multiple-legs/"

def link_expedia_note():
    # Expedia doesn’t offer a public deeplink spec for multi-city you can count on.
    # If you obtain Expedia Group XAP API access, we can power a legal integration
    # and include an Expedia deeplink from the API response.
    return (
        "Expedia note: official Expedia Group XAP Flight Listings API exists; "
        "request access at developers.expediagroup.com and I’ll integrate it."
    )

# Estado / histórico
STATE_PATH = "price_state.json"
HISTORY_CSV = "price_history.csv"

# ===== Viajeros y rutas (sin restricción de hora; solo fecha y origen/destino) =====
TRAVELERS = [{"id": str(i), "travelerType": "ADULT"} for i in range(1, 5)]
ORIGIN_DESTINATIONS = [
    {"id": "1", "originLocationCode": "SJU", "destinationLocationCode": "FCO", "departureDateTimeRange": {"date": "2026-05-06"}},
    {"id": "2", "originLocationCode": "FCO", "destinationLocationCode": "MAD", "departureDateTimeRange": {"date": "2026-05-17"}},
    {"id": "3", "originLocationCode": "MAD", "destinationLocationCode": "SJU", "departureDateTimeRange": {"date": "2026-05-20"}},
]

def amadeus_host():
    return "https://api.amadeus.com" if AMADEUS_ENV == "production" else "https://test.api.amadeus.com"

def get_access_token():
    url = amadeus_host() + "/v1/security/oauth2/token"
    data = {"grant_type": "client_credentials", "client_id": AMADEUS_API_KEY, "client_secret": AMADEUS_API_SECRET}
    r = requests.post(url, data=data, timeout=30)
    if r.status_code != 200:
        try:
            print("[Amadeus Token Error]", r.status_code, r.json())
        except Exception:
            print("[Amadeus Token Error Raw]", r.status_code, r.text)
        r.raise_for_status()
    return r.json()["access_token"]

def search_flight_offers(token):
    """Pide branded fares para detectar OPTIMA/OPTIMAL; Iberia en Economy; multi-city; 4 ADT."""
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
                "carrierRestrictions": {"includedCarrierCodes": ["IB"]},  # Iberia
                "cabinRestrictions": [{
                    "cabin": "ECONOMY",
                    "coverage": "MOST_SEGMENTS",
                    "originDestinationIds": ["1", "2", "3"]
                }],
            },
            "maxFlightOffers": 200
        }
    }
    r = requests.post(url, headers=headers, json=body, timeout=90)
    r.raise_for_status()
    return r.json()

def parse_iso(dt_str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

def format_duration(dur):
    # "PT10H25M" -> "10h 25m"
    if not dur or not dur.startswith("PT"):
        return dur or ""
    h, m = 0, 0
    tmp = dur[2:]
    if "H" in tmp:
        p = tmp.split("H")
        h = int(p[0]) if p[0] else 0
        tmp = p[1] if len(p) > 1 else ""
    if "M" in tmp:
        p = tmp.split("M")
        m = int(p[0]) if p[0] else 0
    return f"{h}h {m}m".strip()

def matches_routes_no_time(offer):
    """
    Acepta cualquier HORA. Solo valida que:
      - haya 3 itinerarios,
      - el primer segmento de cada itinerario salga del origen esperado
        y llegue al destino esperado (de acuerdo a ORIGIN_DESTINATIONS),
      - y que las fechas de salida correspondan a 2026-05-06 / 17 / 20.
    """
    itins = offer.get("itineraries", [])
    if len(itins) < 3:
        return False
    for od_idx, itin in enumerate(itins, start=1):
        segs = itin.get("segments", [])
        if not segs:
            return False
        first = segs[0]
        dep = first.get("departure", {})
        arr = first.get("arrival", {})
        origin_expected = ORIGIN_DESTINATIONS[od_idx-1]["originLocationCode"]
        dest_expected = ORIGIN_DESTINATIONS[od_idx-1]["destinationLocationCode"]
        date_expected = ORIGIN_DESTINATIONS[od_idx-1]["departureDateTimeRange"]["date"]
        if dep.get("iataCode") != origin_expected:
            return False
        if arr.get("iataCode") != dest_expected:
            return False
        at = dep.get("at", "")
        if not at.startswith(date_expected + "T"):
            return False
    return True

def branded_is_optima(offer):
    """Detecta marca 'OPTIMA'/'OPTIMAL' en branded fares (si el API la expone)."""
    for t in offer.get("travelerPricings", []):
        for fd in t.get("fareDetailsBySegment", []):
            brand = (fd.get("brandedFare") or "").upper()
            if "OPTIMA" in brand or "OPTIMAL" in brand:
                return True
    return False

def price_float(offer):
    try:
        return float(offer.get("price", {}).get("grandTotal", "9999999"))
    except:
        return 9999999.0

def summarize_offer(offer):
    """
    Devuelve (precio_total_float, texto_resumen_detallado, es_optima_bool).
    Incluye estimación por leg proporcional a la duración de cada itinerario.
    """
    total = offer.get("price", {}).get("grandTotal", "0")
    try:
        total_f = float(total)
    except:
        total_f = 0.0

    validating = ",".join(offer.get("validatingAirlineCodes", []))
    itins = offer.get("itineraries", [])

    # Estimación por leg (proporcional a duración)
    mins = []
    for itin in itins:
        dur = itin.get("duration", "")
        h, m = 0, 0
        tmp = dur[2:] if dur.startswith("PT") else ""
        if "H" in tmp:
            p = tmp.split("H")
            h = int(p[0]) if p[0] else 0
            tmp = p[1] if len(p) > 1 else ""
        if "M" in tmp:
            p = tmp.split("M")
            m = int(p[0]) if p[0] else 0
        mins.append(h*60 + m)
    total_mins = sum(mins) if mins else 0.0
    per_leg_est = []
    if total_mins > 0:
        for mm in mins:
            per_leg_est.append(total_f * (mm / total_mins))
    else:
        n = max(1, len(itins))
        per_leg_est = [total_f / n] * n

    lines = [f"Total: {CURRENCY} {total} | Validating: {validating}"]
    if itins:
        lines.append("Estimated price per leg (proportional to duration):")
        for idx, (itin, est) in enumerate(zip(itins, per_leg_est), start=1):
            dur_txt = format_duration(itin.get("duration"))
            lines.append(f"  Leg {idx}: ~{CURRENCY} {est:.2f} (dur: {dur_txt})")

    # Detalle de segmentos
    for idx, itin in enumerate(itins, start=1):
        dur_txt = format_duration(itin.get("duration"))
        lines.append(f"  Itinerary {idx} (dur: {dur_txt}):")
        for seg in itin.get("segments", []):
            carrier = seg.get("carrierCode", "")
            num = seg.get("number", "")
            dep = seg.get("departure", {})
            arr = seg.get("arrival", {})
            d_air = dep.get("iataCode", "")
            a_air = arr.get("iataCode", "")
            d_time = dep.get("at", "")
            a_time = arr.get("at", "")
            d_dt = parse_iso(d_time)
            a_dt = parse_iso(a_time)
            d_fmt = d_dt.strftime("%Y-%m-%d %H:%M") if d_dt else d_time
            a_fmt = a_dt.strftime("%Y-%m-%d %H:%M") if a_dt else a_time
            seg_dur = format_duration(seg.get("duration"))
            op = seg.get("operating", {})
            op_car = op.get("carrierCode")
            op_note = f" (operated by {op_car})" if op_car and op_car != carrier else ""
            lines.append(f"    {carrier}{num}{op_note}: {d_air} {d_fmt} → {a_air} {a_fmt} ({seg_dur})")

    # Precio por viajero (si viene)
    tp = offer.get("travelerPricings", [])
    if tp:
        try:
            per = []
            for t in tp:
                pax_type = t.get("travelerType", "")
                price = t.get("price", {})
                per.append(f"{pax_type}: {price.get('total', '?')} {CURRENCY}")
            lines.append("  Traveler pricing: " + " | ".join(per))
        except Exception:
            pass

    is_optima = branded_is_optima(offer)
    mark = " (OPTIMA detected)" if is_optima else ""
    return total_f, "\n".join(lines) + mark, is_optima

# ===== Notifiers =====
def notify_email(subject, body):
    if not (SMTP_HOST and SMTP_FROM and SMTP_TO):
        print("[Email] Missing SMTP settings; skipping")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(SMTP_TO)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, SMTP_TO, msg.as_string())
        print("[Email] Sent")
    except Exception as e:
        print("[Email Error]", e)

def notify_telegram(body):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[Telegram] Missing token/chat_id; skipping")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": body}, timeout=30)
        if r.status_code != 200:
            print("[Telegram Error]", r.status_code, r.text)
        else:
            print("[Telegram] Sent")
    except Exception as e:
        print("[Telegram Error]", e)

def notify_discord(body):
    if not DISCORD_WEBHOOK_URL:
        print("[Discord] Missing webhook url; skipping")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": body}, timeout=30)
        if r.status_code >= 300:
            print("[Discord Error]", r.status_code, r.text)
        else:
            print("[Discord] Sent")
    except Exception as e:
        print("[Discord Error]", e)

def broadcast(subject, body):
    channels = set(NOTIFY_CHANNELS)
    if not channels:
        print("[Notify] No channels configured; printing only:\n", body)
        return
    if "email" in channels:
        notify_email(subject, body)
    if "telegram" in channels:
        notify_telegram(body)
    if "discord" in channels:
        notify_discord(body)

# ===== Estado / Histórico =====
def load_last_price():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
            return float(obj.get("last_price"))
    except Exception:
        return None

def save_last_price(price):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_price": price}, f)
    except Exception as e:
        print("[State Save Error]", e)

def append_history(price, note=""):
    exists = os.path.exists(HISTORY_CSV)
    try:
        with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["timestamp_pr", "best_price", "note"])
            w.writerow([datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"), f"{price:.2f}", note])
    except Exception as e:
        print("[History CSV Error]", e)

# ===== Main =====
def main():
    # Validación básica de credenciales
    if not (AMADEUS_API_KEY and AMADEUS_API_SECRET):
        raise RuntimeError("Missing Amadeus credentials; set AMADEUS_API_KEY and AMADEUS_API_SECRET")
    token = get_access_token()
    data = search_flight_offers(token)

    offers = data.get("data", [])
    # Filtrar por rutas/fechas (sin hora exacta)
    candidates = [off for off in offers if matches_routes_no_time(off)]

    now_pr = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    subject = "Iberia multi-city — all matches (every 4h)"

    if not candidates:
        msg = (
            f"[Iberia Watch] {now_pr}\n"
            f"No offers found for the requested multi-city dates/routes (any time)."
        )
        print(msg)
        broadcast(subject, msg)
        return

    # Orden: primero OPTIMA/OPTIMAL, luego precio ascendente
    candidates.sort(key=lambda o: (not branded_is_optima(o), price_float(o)))

    blocks = []
    best_price = None
    for i, off in enumerate(candidates, start=1):
        if i > TOP_N:
            break
        total_f, block_text, is_optima = summarize_offer(off)
        tag = "[OPTIMA] " if is_optima else ""
        blocks.append(f"Offer #{i} {tag}\n{block_text}")
        if best_price is None or total_f < best_price:
            best_price = total_f

    # Cambio vs última corrida usando el mejor precio de esta corrida
    last_price = load_last_price()
    if last_price is None:
        change_note = "(first run)"
    elif best_price > last_price:
        change_note = f"▲ Best price up by {CURRENCY} {best_price - last_price:.2f}"
    elif best_price < last_price:
        change_note = f"▼ Best price down by {CURRENCY} {last_price - best_price:.2f}"
    else:
        change_note = "↔ Best price unchanged"

    save_last_price(best_price)
    append_history(best_price, change_note)

    header = (
        f"[Iberia Watch] {now_pr}\n"
        f"Multi-city for 4 ADT — Economy (pref. OPTIMA when available)\n"
        f"Routes/dates (any time): SJU→FCO 2026-05-06 | FCO→MAD 2026-05-17 | MAD→SJU 2026-05-20\n"
        f"Best price this run: {CURRENCY} {best_price:.2f}  {change_note}\n"
        f"Showing up to {TOP_N} matching offers (ordered by OPTIMA first, then price):\n\n"
    )
    body = header + ("\n\n".join(blocks))
    print(body)
    broadcast(subject, body)

if __name__ == "__main__":
    main()
