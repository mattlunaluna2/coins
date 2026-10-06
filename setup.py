#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SETUP — Alerta cuando una moneda ENTRA en tendencia (15m)

- Lista fija de monedas premium
- Sin filtro de BTC
- Filtros por moneda:
    * CON setup CoinBeacon: bias + conf + ADX + DI + SQZ + ATR%
    * SIN setup CoinBeacon: solo técnicos (ADX + DI + SQZ + ATR%)
- Solo envía Telegram en transición NO-cumple → cumple
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ═══════════════════════════════════════════════
# LISTA DE MONEDAS PREMIUM
# ═══════════════════════════════════════════════
SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "LINK", "LTC",
    "SUI", "RAY", "INJ", "STX", "HYPE",
    "FLUID", "ORCA", "GEOD", "SYRUP", "PEAQ", "SKY", "AKT", "AXS",
    "ATH", "STRK", "AAVE", "MORPHO", "LDO", "COMP", "FET",
    "BEAM", "IOTA", "HNT", "CHIP", "BAT", "RUNE", "XTZ",
    "APT", "AVAX", "ARB", "UNI", "SUSHI", "DASH", "ENA", "NEAR",
    "GALA", "ALGO", "DOT", "ICP", "FIL", "IMX", "ZEC", "WLD",
    "PYTH", "ZEN", "KAVA", "TWT",
    "SAND", "MANA", "APE", "SEI", "ZRO", "PUMP",
    "SENT", "API3", "CETUS", "AERO",
]

# ═══════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════
TIMEFRAME = "15m"
SLEEP_ENTRE_LLAMADAS = 1.0

# Filtros
CONFIANZA_MIN = 6.0
ADX_LENGTH = 14
ADX_UMBRAL = 23.0
ATR_PERIOD = 14
ATR_VENTANA = 100
ATR_UMBRAL_MIN = 20.0

SQZ_BB_LENGTH = 20
SQZ_BB_MULT = 2.0
SQZ_KC_LENGTH = 20
SQZ_KC_MULT = 1.5

STATE_FILE = Path("data/setup_state.json")

LIMA_OFFSET = timedelta(hours=-5)

COINBEACON_SETUP_URL = "https://api.coinbeacon.io/setups/binance"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"


# ============================================================
# INDICADORES
# ============================================================

def _sma(serie, length):
    if len(serie) < length:
        return None
    return sum(serie[-length:]) / length


def _stdev(serie, length):
    if len(serie) < length:
        return None
    v = serie[-length:]
    m = sum(v) / length
    return (sum((x - m) ** 2 for x in v) / length) ** 0.5


def _linreg_value(y):
    n = len(y)
    if n < 2:
        return y[-1] if y else 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(y) / n
    num = sum((i - x_mean) * (y[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return y[-1]
    slope = num / den
    return y_mean + slope * ((n - 1) - x_mean)


def calcular_adx(velas, length=14):
    n = len(velas)
    if n < length * 2:
        return None
    highs = [v["high"] for v in velas]
    lows = [v["low"] for v in velas]
    closes = [v["close"] for v in velas]
    tr_list, plus_dm_list, minus_dm_list = [], [], []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm_list.append(up if (up > dn and up > 0) else 0.0)
        minus_dm_list.append(dn if (dn > up and dn > 0) else 0.0)

    def smooth(data, period):
        sm = [sum(data[:period])]
        for i in range(period, len(data)):
            sm.append(sm[-1] - (sm[-1] / period) + data[i])
        return sm

    atr_s = smooth(tr_list, length)
    plus_s = smooth(plus_dm_list, length)
    minus_s = smooth(minus_dm_list, length)
    di_plus_l, di_minus_l, dx_l = [], [], []
    for i in range(len(atr_s)):
        if atr_s[i] == 0:
            continue
        dip = (plus_s[i] / atr_s[i]) * 100
        dim = (minus_s[i] / atr_s[i]) * 100
        di_plus_l.append(dip)
        di_minus_l.append(dim)
        s = dip + dim
        if s != 0:
            dx_l.append(abs(dip - dim) / s * 100)
    if len(dx_l) < length:
        return None
    adx = sum(dx_l[-length:]) / length
    return {"adx": adx,
            "di_plus": di_plus_l[-1] if di_plus_l else None,
            "di_minus": di_minus_l[-1] if di_minus_l else None}


def calcular_squeeze_momentum(velas, length=20, mult=2.0, lengthKC=20, multKC=1.5):
    if len(velas) < 2 * lengthKC:
        return None
    highs = [v["high"] for v in velas]
    lows = [v["low"] for v in velas]
    closes = [v["close"] for v in velas]
    n = lengthKC
    basis = _sma(closes, length)
    dev = _stdev(closes, length)
    if basis is None or dev is None:
        return None
    dev *= mult
    upperBB, lowerBB = basis + dev, basis - dev
    ma = _sma(closes, n)
    if ma is None:
        return None
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            pc = closes[i-1]
            trs.append(max(highs[i]-lows[i], abs(highs[i]-pc), abs(lows[i]-pc)))
    rangema = _sma(trs, n)
    if rangema is None:
        return None
    serie = []
    for i in range(n - 1, len(closes)):
        hh = max(highs[i-n+1:i+1])
        ll = min(lows[i-n+1:i+1])
        sc = sum(closes[i-n+1:i+1]) / n
        ref = 0.25 * (hh + ll) + 0.5 * sc
        serie.append(closes[i] - ref)
    if len(serie) < n + 1:
        return None
    m_act = _linreg_value(serie[-n:])
    m_prev = _linreg_value(serie[-n-1:-1])
    if m_act > 0:
        color = "lime" if m_act > m_prev else "green"
    else:
        color = "red" if m_act < m_prev else "maroon"
    return {"color": color, "momentum": m_act}


def calcular_atr_percentile(velas, period=14, ventana=100):
    if len(velas) < period + ventana + 1:
        return None
    trs = []
    for i in range(1, len(velas)):
        high = velas[i]["high"]
        low = velas[i]["low"]
        pc = velas[i-1]["close"]
        tr = max(high - low, abs(high - pc), abs(low - pc))
        trs.append(tr)
    if len(trs) < period + ventana:
        return None
    atrs = []
    suma = sum(trs[:period])
    atrs.append(suma / period)
    for i in range(period, len(trs)):
        suma = suma - trs[i - period] + trs[i]
        atrs.append(suma / period)
    if len(atrs) < ventana:
        return None
    actual = atrs[-1]
    hist = atrs[-ventana:]
    menores = sum(1 for x in hist if x <= actual)
    return round((menores / len(hist)) * 100, 2)


def obtener_velas_okx(symbol, timeframe="15m", limit=200):
    base = symbol.replace("USDT", "")
    inst_id = f"{base}-USDT"
    url = f"{OKX_CANDLES_URL}?instId={inst_id}&bar={timeframe}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    if data.get("code") != "0":
        return []
    velas = data.get("data", [])
    if not velas:
        return []
    velas.reverse()
    out = []
    for v in velas:
        try:
            out.append({
                "ts": int(v[0]),
                "open": float(v[1]),
                "high": float(v[2]),
                "low": float(v[3]),
                "close": float(v[4]),
                "volume": float(v[5]),
            })
        except (ValueError, IndexError):
            continue
    return out


# ============================================================
# HELPERS
# ============================================================

def hora_lima():
    return datetime.now(timezone.utc) + LIMA_OFFSET


def cargar_estado():
    if not STATE_FILE.exists():
        return {"monedas": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "monedas" in data:
            return data
    except Exception:
        pass
    return {"monedas": {}}


def guardar_estado(estado):
    STATE_FILE.parent.mkdir(exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2)


def enviar_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = f"chat_id={urllib.parse.quote(str(chat_id))}&text={urllib.parse.quote(msg)}".encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception:
        return False


def consultar_setup(symbol_binance):
    token = os.environ.get("COINBEACON_TOKEN")
    if not token:
        return None
    url = f"{COINBEACON_SETUP_URL}/{symbol_binance}/{TIMEFRAME}/full"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Cookie": f"access_token={token}",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def extraer_datos_clave(data):
    if not data:
        return None
    setup = data.get("setup", {})
    if not setup:
        return None
    archetype = setup.get("archetype", {}) or {}
    return {
        "price": data.get("price") or setup.get("price") or data.get("currentPrice"),
        "bias": setup.get("bias", ""),
        "regime": (setup.get("regime", {}) or {}).get("label", ""),
        "confidence": setup.get("confidence", 0),
        "direction_score": setup.get("directionScore", 0),
        "percentile": setup.get("scorePercentile", 0),
        "archetype_key": archetype.get("key", ""),
        "archetype_label": archetype.get("label", ""),
    }


def esta_en_tendencia_con_setup(actual, filtros):
    """
    Evalúa con CoinBeacon: bias + conf + ADX + DI + SQZ + ATR%
    """
    if not filtros:
        return False, "sin datos técnicos"

    bias = actual.get("bias", "")
    conf = actual.get("confidence", 0)

    if bias != "bullish":
        return False, f"bias={bias}"
    if conf < CONFIANZA_MIN:
        return False, f"conf {conf:.1f} < {CONFIANZA_MIN}"

    adx_data = filtros.get("adx")
    if not adx_data:
        return False, "sin ADX"
    adx_val = adx_data.get("adx", 0)
    di_plus = adx_data.get("di_plus") or 0
    di_minus = adx_data.get("di_minus") or 0
    if adx_val < ADX_UMBRAL:
        return False, f"ADX {adx_val:.1f} < {ADX_UMBRAL}"
    if di_plus <= di_minus:
        return False, f"DI+ {di_plus:.1f} <= DI- {di_minus:.1f}"

    sqz = filtros.get("sqz")
    if not sqz:
        return False, "sin SQZ"
    if sqz.get("color") not in ("lime", "maroon"):
        return False, f"SQZ {sqz.get('color')}"

    atr_pct = filtros.get("atr_pct")
    if atr_pct is None:
        return False, "sin ATR"
    if atr_pct < ATR_UMBRAL_MIN:
        return False, f"ATR% {atr_pct:.1f} < {ATR_UMBRAL_MIN}"

    return True, {
        "adx": adx_val,
        "di_plus": di_plus,
        "di_minus": di_minus,
        "sqz_color": sqz.get("color"),
        "sqz_val": sqz.get("momentum", 0),
        "atr_pct": atr_pct,
    }


def esta_en_tendencia_sin_setup(filtros):
    """
    Evalúa SIN CoinBeacon: solo técnicos.
    Como no hay bias, se infiere bullish por:
      - DI+ > DI-
      - Squeeze lime o maroon
    """
    if not filtros:
        return False, "sin datos técnicos"

    adx_data = filtros.get("adx")
    if not adx_data:
        return False, "sin ADX"
    adx_val = adx_data.get("adx", 0)
    di_plus = adx_data.get("di_plus") or 0
    di_minus = adx_data.get("di_minus") or 0

    if adx_val < ADX_UMBRAL:
        return False, f"ADX {adx_val:.1f} < {ADX_UMBRAL}"
    if di_plus <= di_minus:
        return False, f"DI+ {di_plus:.1f} <= DI- {di_minus:.1f}"

    sqz = filtros.get("sqz")
    if not sqz:
        return False, "sin SQZ"
    if sqz.get("color") not in ("lime", "maroon"):
        return False, f"SQZ {sqz.get('color')}"

    atr_pct = filtros.get("atr_pct")
    if atr_pct is None:
        return False, "sin ATR"
    if atr_pct < ATR_UMBRAL_MIN:
        return False, f"ATR% {atr_pct:.1f} < {ATR_UMBRAL_MIN}"

    return True, {
        "adx": adx_val,
        "di_plus": di_plus,
        "di_minus": di_minus,
        "sqz_color": sqz.get("color"),
        "sqz_val": sqz.get("momentum", 0),
        "atr_pct": atr_pct,
    }


def construir_mensaje_alerta(symbol, actual, datos_filtros, con_setup):
    precio = actual.get("price") or 0
    adx = datos_filtros["adx"]
    di_plus = datos_filtros["di_plus"]
    di_minus = datos_filtros["di_minus"]
    sqz = datos_filtros["sqz_color"]
    sqz_val = datos_filtros["sqz_val"]
    atr = datos_filtros["atr_pct"]

    if con_setup:
        bias = actual.get("bias", "?").upper()
        regime = actual.get("regime", "")
        conf = actual.get("confidence", 0)
        score = actual.get("direction_score", 0)
        arch_label = actual.get("archetype_label", "")
        return (
            f"🎯 {symbol} ENTRA EN TENDENCIA\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Precio: ${precio:.6f}\n"
            f"🎯 Bias: {bias}\n"
            f"📊 Régimen: {regime}\n"
            f"💪 Confianza: {conf:.1f}/10\n"
            f"📈 Score: {score}\n"
            f"🔧 Setup: {arch_label}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📊 ADX: {adx:.1f} | DI+ {di_plus:.1f} > DI- {di_minus:.1f}\n"
            f"📈 Squeeze: {sqz.upper()} ({sqz_val:+.4f})\n"
            f"📉 ATR%: {atr:.1f}\n"
            f"🕐 {hora_lima().strftime('%H:%M')} Lima\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
    else:
        return (
            f"🎯 {symbol} ENTRA EN TENDENCIA\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Sin setup CoinBeacon (solo técnico)\n"
            f"💰 Precio: ${precio:.6f}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📊 ADX: {adx:.1f} | DI+ {di_plus:.1f} > DI- {di_minus:.1f}\n"
            f"📈 Squeeze: {sqz.upper()} ({sqz_val:+.4f})\n"
            f"📉 ATR%: {atr:.1f}\n"
            f"🕐 {hora_lima().strftime('%H:%M')} Lima\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 70, flush=True)
    print("📐 SETUP 15M — Detección de entradas en tendencia", flush=True)
    print(f"   {len(SYMBOLS)} monedas | Filtros: bias + conf + ADX + DI + SQZ + ATR%", flush=True)
    print("=" * 70, flush=True)
    print(f"Hora UTC: {datetime.now(timezone.utc).isoformat()}\n", flush=True)

    estado = cargar_estado()
    monedas_estado = estado.get("monedas", {})

    enviadas_con_setup = 0
    enviadas_sin_setup = 0
    errores = 0
    en_tendencia_total = 0
    nuevas = 0

    for i, base in enumerate(SYMBOLS, 1):
        symbol_binance = f"{base}USDT"

        # 1. Setup CoinBeacon (opcional)
        data = consultar_setup(symbol_binance)
        if data:
            actual = extraer_datos_clave(data)
            con_setup = actual is not None
        else:
            actual = None
            con_setup = False

        # 2. Velas OKX (SIEMPRE, para calcular técnicos)
        velas = obtener_velas_okx(symbol_binance, TIMEFRAME, 200)
        filtros = {}
        if velas and len(velas) >= 50:
            try:
                filtros = {
                    "adx": calcular_adx(velas, ADX_LENGTH),
                    "sqz": calcular_squeeze_momentum(velas, SQZ_BB_LENGTH, SQZ_BB_MULT, SQZ_KC_LENGTH, SQZ_KC_MULT),
                    "atr_pct": calcular_atr_percentile(velas, ATR_PERIOD, ATR_VENTANA),
                }
            except Exception:
                filtros = {}

        # Si no hay setup NI velas → error
        if not actual and not filtros:
            errores += 1
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue

        # Si no hay setup CoinBeacon, construir "actual" mínimo
        if not actual:
            precio_actual = velas[-1]["close"] if velas else 0
            actual = {
                "price": precio_actual,
                "bias": "bullish (técnico)",
                "regime": "N/A (sin CoinBeacon)",
                "confidence": 0,
                "direction_score": 0,
                "archetype_label": "Sin setup CoinBeacon",
            }

        # 3. Evaluar tendencia
        if con_setup:
            cumple, detalle = esta_en_tendencia_con_setup(actual, filtros)
        else:
            cumple, detalle = esta_en_tendencia_sin_setup(filtros)

        anterior = monedas_estado.get(symbol_binance)
        estaba_en_tendencia = bool(anterior.get("en_tendencia")) if anterior else False

        if cumple:
            en_tendencia_total += 1

        # 4. Alerta SOLO en transición NO → SÍ
        if cumple and not estaba_en_tendencia and anterior is not None:
            msg = construir_mensaje_alerta(base, actual, detalle, con_setup)
            if enviar_telegram(msg):
                if con_setup:
                    enviadas_con_setup += 1
                    print(f"✅ {base} ENTRA en tendencia (CoinBeacon) → alerta enviada", flush=True)
                else:
                    enviadas_sin_setup += 1
                    print(f"✅ {base} ENTRA en tendencia (solo técnico) → alerta enviada", flush=True)
        elif cumple and anterior is None:
            nuevas += 1

        # 5. Log de salida de tendencia
        if not cumple and estaba_en_tendencia:
            print(f"⚠️ {base} SALE de tendencia ({detalle})", flush=True)

        # Guardar estado
        monedas_estado[symbol_binance] = {
            "bias": actual.get("bias"),
            "confidence": actual.get("confidence"),
            "price": actual.get("price"),
            "en_tendencia": cumple,
            "motivo": detalle if not cumple else "OK",
            "con_setup": con_setup,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        time.sleep(SLEEP_ENTRE_LLAMADAS)

    estado["monedas"] = monedas_estado
    estado["updated_at"] = datetime.now(timezone.utc).isoformat()
    guardar_estado(estado)

    print("\n" + "=" * 70, flush=True)
    print(f"🎯 Alertas enviadas (con CoinBeacon): {enviadas_con_setup}", flush=True)
    print(f"🎯 Alertas enviadas (solo técnico): {enviadas_sin_setup}", flush=True)
    print(f"📊 Monedas en tendencia ahora: {en_tendencia_total}/{len(SYMBOLS)}", flush=True)
    print(f"🆕 Primeras (solo guardadas): {nuevas}", flush=True)
    print(f"⚠️ Errores (sin setup ni velas): {errores}", flush=True)
    print("=" * 70, flush=True)
    print("🏁 TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
