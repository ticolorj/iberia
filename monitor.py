# monitor.py — Iberia (IB) – 4 adultos – ECONOMY
# Tramos exactos por hora:
#   1) SJU → FCO  (2026-05-06 20:25)
#   2) FCO → MAD  (2026-05-17 14:45)
#   3) MAD → SJU  (2026-05-20 15:50)
#
# Notificaciones: Email (SMTP)
# Umbrales de alerta (Economy) — por ADULTO:
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
NUM_ADULTS = int(os.getenv("NUM_ADULTS", "4"))
TRAVELERS = [{"id": str(i), "travelerType": "ADULT"} for i in range(1, NUM_ADULTS + 1)]

LEGS = [
    ("SJU", "FCO", "2026-05-06", "20:25", "SJU → FCO (2026-05-06 20:25)"),
    ("FCO", "MAD", "2026-05-17", "14:45", "FCO → MAD (2026-05-17 14:45)"),
    ("MAD", "SJU", "2026-05-20", "15:50", "MAD → SJU (2026-05-20 15:50)"),
]

# UMBRALES POR ADULTO
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
    data = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_API_KEY,
        "client_secret": AMADEUS_API_SECRET,
    }
    r = requests.post(url, data=data, timeout=30)
    try:
        r.raise_for_status()
    except Exception:
        raise RuntimeError(f"Amadeus token error: HTTP {r.status_code} – {r.text}")
    return r.json().get("access_token")


def search_leg_offers(token, origin, destination, date_iso):
    """Búsqueda por tramo, Iberia (marketing), ECONOMY cabin, branded fares ON."""
    url = amadeus_host() + "/v2/shopping/flight-offers"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "currencyCode": CURRENCY,
        "originDestinations": [{
            "id": "1",
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDateTimeRange": {"date": date_iso}  # YYYY-MM-DD
        }],
        "travelers": TRAVELERS,
        "sources": ["GDS"],
        "searchCriteria": {
            "additionalInformation": {"brandedFares": True},
            "flightFilters": {
                "carrierRestrictions": {
                    # Restringe por marketing carrier
                    "includedCarrierCodes": ["IB"]
                },
                "cabinRestrictions": [{
                    "cabin": "ECONOMY",
                    "coverage": "MOST_SEGMENTS",
                    "originDestinationIds": ["1"]
                }],
            },
            "maxFlightOffers": 200
        }
    }
    r = requests.post(url, headers=headers, json=body, timeout=90)
    try:
        r.raise_for_status()
    except Exception:
        raise RuntimeError(f"Amadeus search error {origin}-{destination} {date_iso}: HTTP {r.status_code} – {r.text}")
    return r.json()


def parse_iso(dt_str):
    try:
        # Amadeus entrega "YYYY-MM-DDTHH:MM:SS" (local) o con offset; ambos válidos aquí.
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None


def itinerary_matches(offer, origin, destination):
    """Asegura que el primer segmento sale de 'origin' y el último llega a 'destination' (acepta conexiones)."""
    itins = offer.get("itineraries", [])
    if not itins:
        return False
    segs = itins[0].get("segments", [])
    if not segs:
        return False
    dep = segs[0].get("departure", {})
    arr = segs[-1].get("arrival", {})
    return dep.get("iataCode") == origin and arr.get("iataCode") == destination


def offer_is_for_exact_time(offer, date_iso, time_hhmm):
    """Compara la hora exacta local de salida del PRIMER segmento."""
    itins = offer.get("itineraries", [])
    if not itins:
        return False
    segs = itins[0].get("segments", [])
    if not segs:
        return False
    dep_at = segs[0].get("departure", {}).get("at", "")
    if not dep_at.startswith(date_iso + "T"):
        return False
    hhmm = dep_at.split("T")[1][:5] if "T" in dep_at else dep_at[11:16]
    return hhmm == time_hhmm


def any_marketing_ib(offer):
    """Casi redundante por el filtro, pero lo dejamos por seguridad."""
    try:
        for seg in offer.get("itineraries", [])[0].get("segments", []):
            carrier = seg.get("carrierCode")  # marketing carrier
            if carrier == "IB":
                return True
    except Exception:
        pass
    return False


def per_adult_price(offer):
    """
    Devuelve (per_adult, total_grand) como float.
    Amadeus puede incluir price.pricePerAdult.total o travelerPricings.
    """
    price = offer.get("price", {}) or {}
    grand_total = price.get("grandTotal")
    per_adult = None

    # 1) Campo directo por adulto
    ppa = price.get("pricePerAdult") or {}
    if "total" in ppa:
        try:
            per_adult = float(ppa["total"])
        except Exception:
            per_adult = None

    # 2) Calcular desde travelerPricings
    if per_adult is None:
        tps = offer.get("travelerPricings", []) or []
        adult_totals = []
        for tp in tps:
            if tp.get("travelerType") == "ADULT":
                try:
                    adult_totals.append(float(tp.get("price", {}).get("total")))
                except Exception:
                    pass
        if adult_totals:
            per_adult = sum(adult_totals) / len(adult_totals)

    # 3) Último recurso: dividir grandTotal entre # adultos
    if per_adult is None and grand_total is not None:
        try:
            per_adult = float(grand_total) / max(1, NUM_ADULTS)
        except Exception:
            per_adult = None

    try:
        grand_total = float(grand_total) if grand_total is not None else None
    except Exception:
        grand_total = None

    return per_adult, grand_total


def best_economy_price(data, origin, destination, date_iso, time_hhmm):
    """
    Devuelve la mejor oferta para esa hora exacta (acepta conexiones).
    Retorna (per_adult, grand_total, offer) o None.
    """
    offers = data.get("data", []) or []
    best = None
    for off in offers:
        if not itinerary_matches(off, origin, destination):
            continue
        if not offer_is_for_exact_time(off, date_iso, time_hhmm):
            continue
        if not any_marketing_ib(off):
            continue

        per_adult, grand_total = per_adult_price(off)
        if per_adult is None:
            continue

        if best is None or per_adult < best[0]:
            best = (per_adult, grand_total, off)
    return best


def first_departure_local_str(offer):
    try:
        segs = offer.get("itineraries", [])[0].get("segments", [])
        dep = segs[0].get("departure", {}).get("at", "")
        dt = parse_iso(dep)
        return dt.strftime("%Y-%m-%d %H:%M") if dt else dep.replace("T", " ")[:16]
    except Exception:
        return "(hora no disponible)"


def notify_email(subject, body):
    if not (SMTP_HOST and SMTP_FROM and SMTP_TO):
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_FROM, ", ".join(SMTP_TO)
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


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(obj):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[State Save Error]", e)


def main():
    if not (AMADEUS_API_KEY and AMADEUS_API_SECRET):
        raise RuntimeError("Faltan credenciales de Amadeus (AMADEUS_API_KEY/SECRET).")
    token = get_access_token()
    now = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"[Iberia Watch — Economy Fare] {now}", f"Precios actuales por tramo exacto (por adulto, {NUM_ADULTS} ADT):", ""]
    state, alerts = load_state(), []

    for (o, d, date_iso, hhmm, label) in LEGS:
        key = f"{o}-{d}-{date_iso}-{hhmm}"
        threshold = THRESHOLDS.get((o, d, date_iso, hhmm))
        last = state.get(key, {}).get("last_price_per_adult")
        alerted = state.get(key, {}).get("alerted_below", False)

        try:
            data = search_leg_offers(token, o, d, date_iso)
            best = best_economy_price(data, o, d, date_iso, hhmm)
            if not best:
                lines.append(f"• {label}: (Economy no disponible para esa hora exacta)")
                continue

            price_pp, price_total, offer = best
            dep = first_departure_local_str(offer)
            delta = ""
            if isinstance(last, (int, float)):
                diff = price_pp - last
                if diff < 0:
                    delta = f" (▼ {abs(diff):.2f})"
                elif diff > 0:
                    delta = f" (▲ {diff:.2f})"
                else:
                    delta = " (sin cambio)"

            total_str = f" | Total {CURRENCY} {price_total:.2f}" if price_total is not None else ""
            lines.append(f"• {label}: {CURRENCY} {price_pp:.2f}{delta}{total_str}   Salida: {dep}   [Economy]")

            # Alertas por adulto
            if threshold and price_pp < threshold and not alerted:
                alerts.append(f"{label}: bajó de {CURRENCY} {threshold:.2f}/ADT → ahora {CURRENCY} {price_pp:.2f}/ADT")
                state.setdefault(key, {})["alerted_below"] = True
            elif threshold and price_pp >= threshold and alerted:
                state.setdefault(key, {})["alerted_below"] = False

            state.setdefault(key, {})["last_price_per_adult"] = price_pp

        except Exception as e:
            lines.append(f"• {label}: ERROR ({e})")

    msg = "\n".join(lines)
    print(msg)
    notify_email("Iberia – Economy fare price update", msg)

    if alerts:
        alert_body = "[Iberia Watch] ALERTAS Economy\n\n" + "\n".join(alerts)
        print(alert_body)
        notify_email("Iberia ALERTA – Economy bajo umbral", alert_body)

    save_state(state)


if __name__ == "__main__":
    main()
