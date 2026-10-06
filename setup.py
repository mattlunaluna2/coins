#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SETUP MULTI-TF — Detección de entradas en tendencia larga

- Visión 1h: tendencia de fondo (prioriza MAROON = gira al alza)
- Visión 15m: timing de entrada (acepta MAROON o LIME)
- MAROON = mejor entrada (reversión alcista temprana)
- LIME = entrada confirmada pero avanzada
- GREEN/RED = descartadas para LONG
- CoinBeacon aporta contexto (bias, conf, setup)
- Datos: cache del recolector coins → fallback OKX
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
TIMEFRAME_MACRO = "1h"
TIMEFRAME_ENTRY = "15m"
SLEEP_ENTRE_LLAMADAS = 1.0

# Filtros técnicos
ADX_LENGTH = 14
ADX_UMBRAL = 23.0
ATR_PERIOD = 14
ATR_VENTANA = 100
ATR_UMBRAL_MIN = 20.0

SQZ_BB_LENGTH = 20
SQZ_BB_MULT = 2.0
SQZ_KC_LENGTH = 20
SQZ_KC_MULT = 1.5

# Colores del Squeeze para LONG
# MAROON = "GIRA AL ALZA" → mejor entrada (temprano)
# LIME   = "SUBE FUERTE"  → entrada confirmada (avanzado)
SQZ_LONG_TEMPRANO  = "maroon"
SQZ_LONG_CONFIRMADO = "lime"
SQZ_LONG_ACEPTADOS = (SQZ_LONG_TEMPRANO, SQZ_LONG_CONFIRMADO)

# Datos
CACHE_REMOTE_BASE = (
    "https://raw.githubusercontent.com/mattlunaluna2/"
    "coins/main/data/cache"
)
CACHE_MAX_EDAD_MIN = 40
CACHE_VELAS_MINIMAS = 130

OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
COINBEACON_SETUP_URL = "https://api.coinbeacon.io/setups/binance"

STATE_FILE = Path("data/setup_state.json")
LIMA_OFFSET = timedelta(hours=-5)


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
    return {"color": color, "momentum": m_act, "momentum_prev": m_prev}


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


# ============================================================
# FUENTES DE DATOS
# ============================================================

def leer_cache_remoto(symbol):
    url = f"{CACHE_REMOTE_BASE}/{symbol}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    pulso = data.get("pulso", [])
    if not pulso:
        return None
    ultimo = pulso[-1]
    ts = ultimo.get("ts")
    if not ts:
        return None
    ahora = datetime.now(timezone.utc).timestamp()
    edad_min = (ahora - ts) / 60
    if edad_min > CACHE_MAX_EDAD_MIN:
        return None
    return data


def velas_desde_cache(cache, tf):
    if not cache:
        return []
    raw = cache.get(f"velas_{tf}") or []
    velas = []
    for v in raw:
        try:
            o = float(v["o"])
            c = float(v["c"])
            h = float(v.get("h", max(o, c)))
            l = float(v.get("l", min(o, c)))
        except (TypeError, ValueError, KeyError):
            continue
        velas.append({
            "ts": v["ts"],
            "open": o, "high": h, "low": l, "close": c,
            "volume": float(v.get("v", 0)),
        })
    return velas


def velas_desde_okx(symbol, timeframe, limit=200):
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
    raw = data.get("data", [])
    if not raw:
        return []
    raw.reverse()
    out = []
    for v in raw:
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


def obtener_velas(symbol_base, tf):
    cache = leer_cache_remoto(symbol_base)
    if cache:
        velas = velas_desde_cache(cache, tf)
        if len(velas) >= CACHE_VELAS_MINIMAS:
            return velas, "cache"
    time.sleep(0.3)
    velas = velas_desde_okx(f"{symbol_base}USDT", tf, 200)
    return velas, "okx"


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


def consultar_setup_coinbeacon(symbol_binance):
    token = os.environ.get("COINBEACON_TOKEN")
    if not token:
        return None
    url = f"{COINBEACON_SETUP_URL}/{symbol_binance}/{TIMEFRAME_ENTRY}/full"
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


def extraer_setup_coinbeacon(data):
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
        "archetype_label": archetype.get("label", ""),
    }


# ============================================================
# EVALUACIÓN MULTI-TIMEFRAME
# ============================================================

def calcular_filtros_tf(velas):
    if not velas or len(velas) < 50:
        return None
    try:
        adx = calcular_adx(velas, ADX_LENGTH)
        sqz = calcular_squeeze_momentum(velas, SQZ_BB_LENGTH, SQZ_BB_MULT,
                                         SQZ_KC_LENGTH, SQZ_KC_MULT)
        atr_pct = calcular_atr_percentile(velas, ATR_PERIOD, ATR_VENTANA)
        return {"adx": adx, "sqz": sqz, "atr_pct": atr_pct}
    except Exception:
        return None


def tf_en_tendencia_alcista(filtros, exigir_maroon=False):
    """
    Evalúa si un timeframe está en tendencia alcista.
    
    - exigir_maroon=True  → SOLO acepta maroon (mejor entrada)
    - exigir_maroon=False → acepta maroon o lime
    """
    if not filtros:
        return False, "sin datos"

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
    color = sqz.get("color")

    if exigir_maroon:
        if color != SQZ_LONG_TEMPRANO:
            return False, f"SQZ {color} (se exige maroon)"
        etiqueta = "TEMPRANO"
    else:
        if color not in SQZ_LONG_ACEPTADOS:
            return False, f"SQZ {color}"
        etiqueta = "TEMPRANO" if color == SQZ_LONG_TEMPRANO else "CONFIRMADO"

    atr_pct = filtros.get("atr_pct")
    if atr_pct is None:
        return False, "sin ATR"
    if atr_pct < ATR_UMBRAL_MIN:
        return False, f"ATR% {atr_pct:.1f} < {ATR_UMBRAL_MIN}"

    return True, {
        "adx": adx_val,
        "di_plus": di_plus,
        "di_minus": di_minus,
        "sqz_color": color,
        "sqz_val": sqz.get("momentum", 0),
        "atr_pct": atr_pct,
        "etiqueta": etiqueta,
    }


def evaluar_multitf(filtros_1h, filtros_15m):
    """
    Tendencia larga confirmada:
      - 1h en tendencia alcista (preferimos MAROON = gira al alza)
      - 15m en tendencia alcista (MAROON o LIME)
    
    Estrategia de puntos:
      - 1h MAROON + 15m MAROON  → 100 puntos (excelente)
      - 1h MAROON + 15m LIME    → 80 puntos (bueno)
      - 1h LIME   + 15m MAROON  → 70 puntos (bueno)
      - 1h LIME   + 15m LIME    → 50 puntos (válido)
    """
    # Primero evaluar 1h
    ok_1h, det_1h = tf_en_tendencia_alcista(filtros_1h, exigir_maroon=False)
    if not ok_1h:
        return False, f"1h: {det_1h}", 0

    # Evaluar 15m
    ok_15m, det_15m = tf_en_tendencia_alcista(filtros_15m, exigir_maroon=False)
    if not ok_15m:
        return False, f"15m: {det_15m}", 0

    # Calcular puntuación según combinación
    c1h = det_1h["sqz_color"]
    c15m = det_15m["sqz_color"]

    if c1h == "maroon" and c15m == "maroon":
        puntos = 100
        combo = "🟢🟢 EXCELENTE (maroon + maroon)"
    elif c1h == "maroon" and c15m == "lime":
        puntos = 80
        combo = "🟢🟡 BUENO (maroon + lime)"
    elif c1h == "lime" and c15m == "maroon":
        puntos = 70
        combo = "🟡🟢 BUENO (lime + maroon)"
    else:  # lime + lime
        puntos = 50
        combo = "🟡🟡 VÁLIDO (lime + lime)"

    return True, {"1h": det_1h, "15m": det_15m, "combo": combo}, puntos


def construir_mensaje(symbol, cb_setup, detalle_mtf, precio, puntos):
    d1h = detalle_mtf["1h"]
    d15m = detalle_mtf["15m"]
    combo = detalle_mtf["combo"]

    # Emoji según puntuación
    if puntos >= 100:
        emoji = "🏆"
    elif puntos >= 80:
        emoji = "🔥"
    elif puntos >= 70:
        emoji = "✅"
    else:
        emoji = "🟡"

    lineas = [
        f"{emoji} {symbol} ENTRA EN TENDENCIA LARGA",
        f"━━━━━━━━━━━━━━━━━━━",
        f"🎯 Calidad: {combo}",
        f"💰 Precio: ${precio:.6f}",
    ]

    if cb_setup:
        lineas.append(f"🎯 Bias: {cb_setup['bias'].upper()}")
        lineas.append(f"📊 Régimen: {cb_setup['regime']}")
        lineas.append(f"💪 Confianza: {cb_setup['confidence']:.1f}/10")
        if cb_setup.get("archetype_label"):
            lineas.append(f"🔧 Setup: {cb_setup['archetype_label']}")

    lineas.extend([
        f"",
        f"📊 VISIÓN 1h (tendencia de fondo):",
        f"   Squeeze: {d1h['sqz_color'].upper()} [{d1h['etiqueta']}]",
        f"   ADX {d1h['adx']:.1f} | DI+ {d1h['di_plus']:.1f} > DI- {d1h['di_minus']:.1f}",
        f"   Mom: {d1h['sqz_val']:+.4f} | ATR%: {d1h['atr_pct']:.1f}",
        f"",
        f"📊 VISIÓN 15m (timing de entrada):",
        f"   Squeeze: {d15m['sqz_color'].upper()} [{d15m['etiqueta']}]",
        f"   ADX {d15m['adx']:.1f} | DI+ {d15m['di_plus']:.1f} > DI- {d15m['di_minus']:.1f}",
        f"   Mom: {d15m['sqz_val']:+.4f} | ATR%: {d15m['atr_pct']:.1f}",
        f"",
        f"🕐 {hora_lima().strftime('%H:%M')} Lima",
        f"━━━━━━━━━━━━━━━━━━━",
    ])

    return "\n".join(lines_placeholder := lineas)  # por si acaso


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 70, flush=True)
    print("📐 SETUP MULTI-TF — Visión 1h + Entrada 15m", flush=True)
    print(f"   {len(SYMBOLS)} monedas | Filtros: ADX + DI + SQZ + ATR%", flush=True)
    print(f"   Prioriza MAROON (giro al alza) sobre LIME (ya subiendo)", flush=True)
    print(f"   Datos: cache coins → fallback OKX", flush=True)
    print("=" * 70, flush=True)
    print(f"Hora UTC: {datetime.now(timezone.utc).isoformat()}\n", flush=True)

    estado = cargar_estado()
    monedas_estado = estado.get("monedas", {})

    enviadas = 0
    errores = 0
    en_tendencia_total = 0
    nuevas = 0
    fuentes = {"cache": 0, "okx": 0}

    for i, base in enumerate(SYMBOLS, 1):
        symbol_binance = f"{base}USDT"

        velas_1h, src_1h = obtener_velas(base, TIMEFRAME_MACRO)
        fuentes[src_1h] = fuentes.get(src_1h, 0) + 1

        velas_15m, src_15m = obtener_velas(base, TIMEFRAME_ENTRY)
        fuentes[src_15m] = fuentes.get(src_15m, 0) + 1

        if not velas_1h or not velas_15m:
            errores += 1
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue

        filtros_1h = calcular_filtros_tf(velas_1h)
        filtros_15m = calcular_filtros_tf(velas_15m)

        if not filtros_1h or not filtros_15m:
            errores += 1
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue

        cumple, detalle, puntos = evaluar_multitf(filtros_1h, filtros_15m)

        cb_data = consultar_setup_coinbeacon(symbol_binance)
        cb_setup = extraer_setup_coinbeacon(cb_data)

        precio = velas_15m[-1]["close"] if velas_15m else 0

        anterior = monedas_estado.get(symbol_binance)
        estaba = bool(anterior.get("en_tendencia")) if anterior else False

        if cumple:
            en_tendencia_total += 1

        # Alerta solo en transición NO → SÍ
        if cumple and not estaba and anterior is not None:
            msg = construir_mensaje(base, cb_setup, detalle, precio, puntos)
            if enviar_telegram(msg):
                enviadas += 1
                c1h = detalle["1h"]["sqz_color"]
                c15m = detalle["15m"]["sqz_color"]
                print(f"✅ {base} ENTRA (1h={c1h}, 15m={c15m}) | {puntos}pts → Telegram", flush=True)
        elif cumple and anterior is None:
            nuevas += 1

        if not cumple and estaba:
            print(f"⚠️ {base} SALE ({detalle})", flush=True)

        monedas_estado[symbol_binance] = {
            "price": precio,
            "en_tendencia": cumple,
            "puntos": puntos if cumple else 0,
            "motivo": detalle if not cumple else "OK",
            "sqz_1h": filtros_1h.get("sqz", {}).get("color") if filtros_1h and filtros_1h.get("sqz") else "-",
            "sqz_15m": filtros_15m.get("sqz", {}).get("color") if filtros_15m and filtros_15m.get("sqz") else "-",
            "src_1h": src_1h,
            "src_15m": src_15m,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        time.sleep(SLEEP_ENTRE_LLAMADAS)

    estado["monedas"] = monedas_estado
    estado["updated_at"] = datetime.now(timezone.utc).isoformat()
    guardar_estado(estado)

    print("\n" + "=" * 70, flush=True)
    print(f"🎯 Alertas enviadas: {enviadas}", flush=True)
    print(f"📊 Monedas en tendencia larga ahora: {en_tendencia_total}/{len(SYMBOLS)}", flush=True)
    print(f"🆕 Primeras (solo guardadas): {nuevas}", flush=True)
    print(f"⚠️ Errores (sin velas): {errores}", flush=True)
    print(f"📦 Fuentes de datos: cache={fuentes.get('cache', 0)} | okx={fuentes.get('okx', 0)}", flush=True)
    print("=" * 70, flush=True)
    print("🏁 TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
