# monitor.py — Iberia (IB) – 4 adultos – ECONOMY
# Tramos exactos por hora:
#   1) SJU → FCO  (2026-05-06 20:25)
#   2) FCO → MAD  (2026-05-17 14:45)
#   3) MAD → SJU  (2026-05-20 15:50)
#
# Notificaciones: Email (SMTP)
# Umbrales de alerta (OPTIMA/OPTIMAL):
#   SJU→FCO < 850 USD, FCO→MAD < 350 USD, MAD→SJU < 550 USD
#
# .env requerido:
#   AMADEUS_API_KEY, AMADEUS_API_SECRET, AMADEUS_ENV (test|production), CURRENCY
#   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_USE_TLS=true|false, SMTP_FROM, SMTP_TO

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
AMADEUS_ENV        = os.getenv("AMADEUS_ENV", "test").lower()  # "test" o "production"
CURRENCY           = os.getenv("CURRENCY", "USD")

# ===== Email (SMTP) =====
SMTP_HOST    = os.getenv("SMTP_HOST")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER")
SMTP_PASS    = os.getenv("SMTP_PASS")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
SMTP_FROM    = os.getenv("SMTP_FROM")
SMTP_TO      = [e.strip() for e in os.getenv("SMTP_TO", "").split(",") if e.strip()]

# ===== Parámetros del viaje (4 ADT, ECONOMY, Iberia) =====
TRAVELERS = [{"id": str(i), "travelerType": "ADULT"} for i in range(1, 5)]

# Cada tramo con hora exacta requerida (local del aeropuerto) HH:MM
LEGS = [
    # (origin, dest, date, time_HHMM, label)
    ("SJU", "FCO", "2026-05-06", "20:25", "SJU → FCO (2026-05-06 20:25)"),
    ("FCO", "MAD", "2026-05-17", "14:45", "FCO → MAD (2026-05-17 14:45)"),
    ("MAD", "SJU", "2026-05-20", "15:50", "MAD → SJU (2026-05-20 15:50)"),
]

# Umbrales de alerta (OPTIMA/OPTIMAL) por tramo
THRESHOLDS = {
    ("SJU", "FCO", "2026-05-06", "20:25"): 850.0,
    ("FCO", "MAD", "2026-05-17", "14:45"): 350.0,
    ("MAD", "SJU", "2026-05-20", "15:50"): 550.0,
}

STATE_PATH = "leg_price_state.json"  # para recordar si ya alertó por estar debajo

def amadeus_host():
    return "https://api.amadeus.com" if AMADEUS_ENV == "production" else "https://test.api.amadeus.com"

def get_access_token():
    url = amadeus_host() + "/v1/security/oauth2/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": AMADEUS_API_KEY,
        "client_secret": AMADEUS_API_SECRET
    }
    r = requests.post(url, data=data, timeout=30)
    if r.status_code != 200:
        try:
            print("[Amadeus Token Error]", r.status_code, r.json())
        except Exception:
            print("[Amadeus Token Error Raw]", r.status_code, r.text)
        r.raise_for_status()
    return r.json()["access_token"]

def search_leg_offers(token, origin, destination, date_iso):
    """
    Busca ofertas SOLO para un tramo: origin -> destination en date_iso.
    Filtros: Iberia (IB), ECONOMY, 4 ADT.
    """
    url = amadeus_host() + "/v2/shopping/flight-offers"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    body = {
        "currencyCode": CURRENCY,
        "originDestinations": [
            {
                "id": "1",
                "originLocationCode": origin,
                "destinationLocationCode": destination,
                "departureDateTimeRange": {"date": date_iso}
            }
        ],
        "travelers": TRAVELERS,
        "sources": ["GDS"],
        "searchCriteria": {
            "additionalInformation": {"brandedFares": True},
            "flightFilters": {
                "carrierRestrictions": {"includedCarrierCodes": ["IB"]},
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
    r.raise_for_status()
    return r.json()

def parse_iso(dt_str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

def offer_is_for_exact_time(offer, origin, destination, date_iso, time_hhmm):
    """
    Verifica que el primer segmento del primer itinerario:
      - salga de 'origin' en fecha 'date_iso' y hora 'time_hhmm' (local),
      - llegue a 'destination' (primer segmento).
    """
    itins = offer.get("itineraries", [])
    if not itins:
        return False
    segs = itins[0].get("segments", [])
    if not segs:
        return False
    first = segs[0]
    dep = first.get("departure", {})
    arr = first.get("arrival", {})
    at = dep.get("at", "")  # e.g. "2026-05-06T20:25:00"
    if dep.get("iataCode") != origin:
        return False
    if arr.get("iataCode") != destination:
        return False
    if not at.startswith(date_iso + "T"):
        return False
    hhmm = at.split("T")[1][:5] if "T" in at else at[11:16]
    return hhmm == time_hhmm

def offer_is_optima(offer):
    """
    Detecta si en fareDetailsBySegment aparece marca OPTIMA/OPTIMAL (branded fares).
    """
    for t in offer.get("travelerPricings", []):
        for fd in t.get("fareDetailsBySegment", []):
            brand = (fd.get("brandedFare") or "").upper()
            if "OPTIMA" in brand or "OPTIMAL" in brand:
                return True
    return False

def best_optima_price_for_exact_time(data, origin, destination, date_iso, time_hhmm):
    """
    Filtra ofertas que coincidan EXACTAMENTE con la hora de salida y devuelvan OPTIMA/OPTIMAL,
    y elige la de menor precio.
    Retorna (price_float, offer) o None.
    """
    offers = data.get("data", [])
    best = None
    for off in offers:
        # Mantener validadora IB si aparece
        validating = off.get("validatingAirlineCodes", [])
        if validating and "IB" not in validating:
            continue
        if not offer_is_for_exact_time(off, origin, destination, date_iso, time_hhmm):
            continue
        if not offer_is_optima(off):
            continue
        price = off.get("price", {}).get("grandTotal")
        if not price:
            continue
        try:
            price_val = float(price)
        except:
            continue
        if best is None or price_val < best[0]:
            best = (price_val, off)
    return best

# ===== Email notifier =====
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

# ===== Estado para anti-spam de alertas =====
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
            json.dump(obj, f)
    except Exception as e:
        print("[State Save Error]", e)

def main():
    # Validaciones mínimas
    if not (AMADEUS_API_KEY and AMADEUS_API_SECRET):
        raise RuntimeError("Faltan credenciales de Amadeus (AMADEUS_API_KEY / AMADEUS_API_SECRET).")

    token = get_access_token()
    now_pr = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"[Iberia Watch — exact legs (OPTIMA)] {now_pr}",
             f"Moneda: {CURRENCY}",
             "Resultados (precio OPTIMA si existe) para tramos exactos por hora:",
             ""]
    state = load_state()
    alerts = []

    for (o, d, date_iso, hhmm, label) in LEGS:
        try:
            data = search_leg_offers(token, o, d, date_iso)
            best = best_optima_price_for_exact_time(data, o, d, date_iso, hhmm)  # (price, offer) o None

            key = f"{o}-{d}-{date_iso}-{hhmm}"
            threshold = THRESHOLDS.get((o, d, date_iso, hhmm), None)

            if best is None:
                lines.append(f"• {label}: SIN OFERTAS OPTIMA A ESA HORA")
                # Mantén el último precio como None
                state.setdefault(key, {})
                state[key]["last_price"] = None
                state[key].setdefault("alerted_below", False)
            else:
                price, offer = best
                dep_at = "(hora no disponible)"
                try:
                    first_seg = offer.get("itineraries", [])[0].get("segments", [])[0]
                    dep_at_raw = first_seg.get("departure", {}).get("at", "")
                    dep_at = (datetime.fromisoformat(dep_at_raw).strftime("%Y-%m-%d %H:%M")
                              if dep_at_raw else dep_at)
                except Exception:
                    pass

                lines.append(f"• {label}: {CURRENCY} {price:.2f}   Salida (local): {dep_at}   [OPTIMA]")

                # Lógica de alerta por cruce de umbral (solo cuando baja de umbral)
                state.setdefault(key, {})
                last_price = state[key].get("last_price")
                alerted_below = state[key].get("alerted_below", False)

                crossed_down = False
                if threshold is not None:
                    if price < threshold and (not alerted_below):
                        crossed_down = True
                        alerts.append(
                            f"ALERTA: {label} bajó de {CURRENCY} {threshold:.2f} → ahora {CURRENCY} {price:.2f} (OPTIMA)"
                        )
                        state[key]["alerted_below"] = True
                    elif price >= threshold and alerted_below:
                        # se “resetea” si volvió a subir por encima
                        state[key]["alerted_below"] = False

                state[key]["last_price"] = price

        except Exception as e:
            print(f"[ERROR] {label}: {e}")
            lines.append(f"• {label}: ERROR consultando el tramo")

    # Resumen y notas
    lines.append("")
    lines.append("Nota: cada tramo se consulta y evalúa de forma independiente; los importes son para 4 ADT en ECONOMY (OPTIMA si está disponible).")
    message = "\n".join(lines)

    # Enviar correo con el reporte general
    notify_email("Iberia exact flights — OPTIMA price report", message)

    # Si hubo alertas por cruce de umbral, enviar correo adicional con solo alertas
    if alerts:
        alert_body = "[Iberia Watch] Price drop alerts (OPTIMA)\n\n" + "\n".join(alerts)
        notify_email("Iberia ALERTA — Precio por debajo del umbral (OPTIMA)", alert_body)

    save_state(state)

if __name__ == "__main__":
    main()
