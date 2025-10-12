# monitor.py — Iberia (IB) – 4 adultos – ECONOMY (branded fare: Optimal)
# Tramos exactos por hora:
#   1) SJU → FCO  (2026-05-06 20:25)
#   2) FCO → MAD  (2026-05-17 14:45)
#   3) MAD → SJU  (2026-05-20 15:50)
#
# Notificaciones: Email (SMTP)
# Umbrales de alerta (Economy / Optimal):
#   SJU→FCO < 850 USD, FCO→MAD < 350 USD, MAD→SJU < 550 USD

import os
import smtplib
import pytz
import requests
import json
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
PR_TZ = pytz.timezone("America/Puerto_Rico")

# ===== Amadeus =====
AMADEUS_API_KEY    = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
AMADEUS_ENV        = os.getenv("AMADEUS_ENV", "test").lower()
CURRENCY           = os.getenv("CURRENCY", "USD")

# ===== Email (SMTP) =====
SMTP_HOST    = os.getenv("SMTP_HOST")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER")
SMTP_PASS    = os.getenv("SMTP_PASS")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
SMTP_FROM    = os.getenv("SMTP_FROM")
SMTP_TO      = [e.strip() for e in os.getenv("SMTP_TO", "").split(",") if e.strip()]

# ===== Parámetros del viaje =====
TRAVELERS = [{"id": str(i), "travelerType": "ADULT"} for i in range(1, 5)]

LEGS = [
    ("SJU", "FCO", "2026-05-06", "20:25", "SJU → FCO (2026-05-06 20:25)"),
    ("FCO", "MAD", "2026-05-17", "14:45", "FCO → MAD (2026-05-17 14:45)"),
    ("MAD", "SJU", "2026-05-20", "15:50", "MAD → SJU (2026-05-20 15:50)"),
]

THRESHOLDS = {
    ("SJU", "FCO", "2026-05-06", "20:25"): 850.0,
    ("FCO", "MAD", "2026-05-17", "14:45"): 350.0,
    ("MAD", "SJU", "2026-05-20", "15:50"): 550.0,
}

STATE_PATH = "leg_price_state.json"

def amadeus_host():
    return "https://api.amadeus.com" if AMADEUS_ENV == "production" else "https://test.api.amadeus.com"

def get_access_token():
    url = amadeus_host() + "/v1/security/oauth2/token"
    data = {"grant_type": "client_credentials", "client_id": AMADEUS_API_KEY, "client_secret": AMADEUS_API_SECRET}
    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

def search_leg_offers(token, origin, destination, date_iso):
    """Single-leg search, Iberia only, ECONOMY cabin, branded fares enabled."""
    url = amadeus_host() + "/v2/shopping/flight-offers"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "currencyCode": CURRENCY,
        "originDestinations": [{
            "id": "1",
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDateTimeRange": {"date": date_iso}
        }],
        "travelers": TRAVELERS,
        "sources": ["GDS"],
        "searchCriteria": {
            "additionalInformation": {"brandedFares": True},
            "flightFilters": {
                "carrierRestrictions": {"includedCarrierCodes": ["IB"]},
                "cabinRestrictions": [{
                    "cabin": "ECONOMY",             # <- ECONOMY only
                    "coverage": "MOST_SEGMENTS",
                    "originDestinationIds": ["1"]
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

def offer_is_for_exact_time(offer, origin, destination, date_iso, time_hhmm):
    """Match exact local departure time for the first segment of the first itinerary."""
    itins = offer.get("itineraries", [])
    if not itins: return False
    segs = itins[0].get("segments", [])
    if not segs: return False
    first = segs[0]
    dep = first.get("departure", {})
    arr = first.get("arrival", {})
    at = dep.get("at", "")
    if dep.get("iataCode") != origin: return False
    if arr.get("iataCode") != destination: return False
    if not at.startswith(date_iso + "T"): return False
    hhmm = at.split("T")[1][:5] if "T" in at else at[11:16]
    return hhmm == time_hhmm

def offer_is_economy_optimal(offer):
    """
    Enforces BOTH:
      - Branded fare contains 'OPTIMAL'
      - The fareDetails cabin is ECONOMY (defensive, aunque ya filtramos ECONOMY arriba)
    """
    saw_optimal = False
    econ_ok = True  # default true; si encontramos cabin y no es ECONOMY, lo marcamos false
    for t in offer.get("travelerPricings", []):
        for fd in t.get("fareDetailsBySegment", []):
            brand = (fd.get("brandedFare") or "").upper()
            cabin = (fd.get("cabin") or "").upper()
            if "OPTIMAL" in brand:
                saw_optimal = True
            if cabin and cabin != "ECONOMY":
                econ_ok = False
    return saw_optimal and econ_ok

def best_economy_optimal_price(data, origin, destination, date_iso, time_hhmm):
    """Pick the cheapest offer that matches exact time AND is Economy/Optimal."""
    offers = data.get("data", [])
    best = None
    for off in offers:
        validating = off.get("validatingAirlineCodes", [])
        if validating and "IB" not in validating:
            continue
        if not offer_is_for_exact_time(off, origin, destination, date_iso, time_hhmm):
            continue
        if not offer_is_economy_optimal(off):
            continue
        price = off.get("price", {}).get("grandTotal")
        if not price:
            continue
        try:
            val = float(price)
        except:
            continue
        if best is None or val < best[0]:
            best = (val, off)
    return best

def first_departure_local_str(offer):
    """Pretty local departure time for the chosen offer."""
    try:
        segs = offer.get("itineraries", [])[0].get("segments", [])
        dep = segs[0].get("departure", {}).get("at", "")
        dt = parse_iso(dep)
        return dt.strftime("%Y-%m-%d %H:%M") if dt else dep.replace("T", " ")[:16]
    except Exception:
        return "(hora no disponible)"

def notify_email(subject, body):
    if not (SMTP_HOST and SMTP_FROM and SMTP_TO):
        print("[Email] Missing SMTP settings; skipping"); return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_FROM, ", ".join(SMTP_TO)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            if SMTP_USE_TLS: server.starttls()
            if SMTP_USER and SMTP_PASS: server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, SMTP_TO, msg.as_string())
        print("[Email] Sent")
    except Exception as e:
        print("[Email Error]", e)

def load_state():
    if not os.path.exists(STATE_PATH): return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def save_state(obj):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f: json.dump(obj, f)
    except Exception as e: print("[State Save Error]", e)

def main():
    if not (AMADEUS_API_KEY and AMADEUS_API_SECRET):
        raise RuntimeError("Faltan credenciales de Amadeus.")
    token = get_access_token()
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"[Iberia Watch — Economy / Optimal] {now}", "Precios actuales por tramo exacto:", ""]
    state, alerts = load_state(), []

    for (o, d, date_iso, hhmm, label) in LEGS:
        key = f"{o}-{d}-{date_iso}-{hhmm}"
        threshold = THRESHOLDS.get((o, d, date_iso, hhmm))
        last = state.get(key, {}).get("last_price")
        alerted = state.get(key, {}).get("alerted_below", False)

        try:
            data = search_leg_offers(token, o, d, date_iso)
            best = best_economy_optimal_price(data, o, d, date_iso, hhmm)
            if not best:
                lines.append(f"• {label}: (Economy / Optimal no disponible)")
                # No tocamos last_price para no “perder” referencia; o setéalo a None si prefieres
                continue

            price, offer = best
            dep = first_departure_local_str(offer)
            delta = ""
            if isinstance(last, (int, float)):
                diff = price - last
                delta = f" (▼ {abs(diff):.2f})" if diff < 0 else f" (▲ {diff:.2f})" if diff > 0 else " (sin cambio)"

            lines.append(f"• {label}: {CURRENCY} {price:.2f}{delta}   Salida: {dep}   [Economy / Optimal]")

            # Alertas por cruce de umbral hacia abajo
            if threshold and price < threshold and not alerted:
                alerts.append(f"{label}: bajó de {CURRENCY} {threshold:.2f} → ahora {CURRENCY} {price:.2f}")
                state.setdefault(key, {})["alerted_below"] = True
            elif threshold and price >= threshold and alerted:
                state.setdefault(key, {})["alerted_below"] = False

            state.setdefault(key, {})["last_price"] = price

        except Exception as e:
            lines.append(f"• {label}: ERROR ({e})")

    msg = "\n".join(lines)
    print(msg)
    notify_email("Iberia – Economy/Optimal price update", msg)

    if alerts:
        alert_body = "[Iberia Watch] ALERTAS Economy / Optimal\n\n" + "\n".join(alerts)
        print(alert_body)
        notify_email("Iberia ALERTA – Economy/Optimal bajo umbral", alert_body)

    save_state(state)

if __name__ == "__main__":
    main()
