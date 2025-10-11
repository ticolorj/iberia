# monitor.py — Iberia (IB) – 4 adultos – ECONOMY
# Búsqueda por tramos separados:
#   1) SJU → FCO  (2026-05-06)
#   2) FCO → MAD  (2026-05-17)
#   3) MAD → SJU  (2026-05-20)
#
# Notificaciones: Email (SMTP)
# Requiere variables en .env:
#   AMADEUS_API_KEY, AMADEUS_API_SECRET, AMADEUS_ENV (test|production), CURRENCY (opcional, default USD)
#   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_USE_TLS=true|false, SMTP_FROM, SMTP_TO
#
# Sugerencia: prueba primero con AMADEUS_ENV=test; para precios reales usa production con claves de prod.

import os
import smtplib
import pytz
import requests
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

PR_TZ = pytz.timezone("America/Puerto_Rico")

# ===== Amadeus =====
AMADEUS_API_KEY   = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET= os.getenv("AMADEUS_API_SECRET")
AMADEUS_ENV       = os.getenv("AMADEUS_ENV", "test").lower()  # "test" o "production"
CURRENCY          = os.getenv("CURRENCY", "USD")

# ===== Email (SMTP) =====
SMTP_HOST   = os.getenv("SMTP_HOST")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER   = os.getenv("SMTP_USER")
SMTP_PASS   = os.getenv("SMTP_PASS")
SMTP_USE_TLS= os.getenv("SMTP_USE_TLS", "true").lower() == "true"
SMTP_FROM   = os.getenv("SMTP_FROM")
SMTP_TO     = [e.strip() for e in os.getenv("SMTP_TO", "").split(",") if e.strip()]

# ===== Parámetros del viaje (4 ADT, ECONOMY, Iberia) =====
TRAVELERS = [{"id": str(i), "travelerType": "ADULT"} for i in range(1, 5)]

LEGS = [
    # (origen, destino, fecha ISO, label imprimible)
    ("SJU", "FCO", "2026-05-06", "SJU → FCO (2026-05-06)"),
    ("FCO", "MAD", "2026-05-17", "FCO → MAD (2026-05-17)"),
    ("MAD", "SJU", "2026-05-20", "MAD → SJU (2026-05-20)"),
]

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
        # Muestra más detalle si hay error
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
            "flightFilters": {
                "carrierRestrictions": {"includedCarrierCodes": ["IB"]},
                "cabinRestrictions": [{
                    "cabin": "ECONOMY",
                    "coverage": "MOST_SEGMENTS",
                    "originDestinationIds": ["1"]
                }],
            },
            "maxFlightOffers": 100
        }
    }

    r = requests.post(url, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    return r.json()

def best_price_from_response(data):
    """
    Devuelve (precio_float, oferta_json) del mejor precio.
    Prefiere validación por Iberia si está disponible.
    """
    offers = data.get("data", [])
    best = None
    for off in offers:
        price = off.get("price", {}).get("grandTotal")
        if not price:
            continue
        try:
            price_val = float(price)
        except:
            continue
        validating = off.get("validatingAirlineCodes", [])
        # Si hay validadora y no es IB, lo saltamos (opcional; puedes comentarlo si quieres permitir combinaciones)
        if validating and "IB" not in validating:
            continue
        if best is None or price_val < best[0]:
            best = (price_val, off)
    return best  # puede ser None si no hubo coincidencias

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

def main():
    # Validaciones mínimas
    if not (AMADEUS_API_KEY and AMADEUS_API_SECRET):
        raise RuntimeError("Faltan credenciales de Amadeus (AMADEUS_API_KEY / AMADEUS_API_SECRET).")

    token = get_access_token()

    # Ejecuta tres búsquedas independientes (una por tramo)
    results = []
    for (o, d, date_iso, label) in LEGS:
        try:
            data = search_leg_offers(token, o, d, date_iso)
            best = best_price_from_response(data)
            results.append((label, best))
        except Exception as e:
            print(f"[ERROR] {label}: {e}")
            results.append((label, None))

    # Armar el mensaje
    now_pr = datetime.now(PR_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"[Iberia Watch — legs] {now_pr}",
             f"Moneda: {CURRENCY}",
             "Resultados por tramo (mejor precio para 4 ADT, ECONOMY, IB):",
             ""]

    total = 0.0
    all_have_price = True

    for label, best in results:
        if best is None:
            lines.append(f"• {label}: SIN OFERTAS COINCIDENTES")
            all_have_price = False
        else:
            price, offer = best
            val = ",".join(offer.get("validatingAirlineCodes", []))
            lines.append(f"• {label}: {CURRENCY} {price:.2f}   (validating: {val})")
            total += price

    lines.append("")
    if all_have_price:
        lines.append(f"TOTAL COMBINADO (suma tramos separados): {CURRENCY} {total:.2f}")
    else:
        lines.append("TOTAL COMBINADO: N/D (no hay precio en todos los tramos)")

    lines.append("")
    lines.append("Nota: Los precios por tramo no siempre equivalen al precio de un ticket multicity; verifícalos antes de comprar.")

    message = "\n".join(lines)
    print(message)

    # Enviar email
    notify_email("Iberia price update — separate legs", message)

if __name__ == "__main__":
    main()
