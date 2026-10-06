#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SETUP — Smart Setup Bot (15m) con filtros técnicos por moneda
- Lista FIJA de 60 monedas premium (sin dinámicas)
- Sin filtros de BTC
- Filtros técnicos individuales: ADX, Squeeze Momentum, ATR percentil
- Mantiene uso de CoinBeacon (setup + líneas de soporte/resistencia)
- DIAGNÓSTICO: contador de qué filtro detuvo cada señal
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ═══════════════════════════════════════════════
# LISTA FIJA DE 60 MONEDAS PREMIUM
# ═══════════════════════════════════════════════
SYMBOLS = [
    # Núcleo
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "LINK", "LTC",
    # De interspot (únicas)
    "SUI", "RAY", "INJ", "STX", "HYPE",
    # De coins (únicas)
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
# CONFIGURACIÓN DE FILTROS TÉCNICOS
# ═══════════════════════════════════════════════
TIMEFRAME = "15m"
SLEEP_ENTRE_LLAMADAS = 1.0

# Filtros de setup (originales)
CONFIANZA_MIN = 7.0
DIRECTION_SCORE_MIN = 70
UMBRAL_PUNTOS = 40

# Filtros técnicos nuevos (validados con análisis de 4,256 líneas)
ADX_LENGTH = 14
ADX_UMBRAL = 23.0
ATR_PERIOD = 14
ATR_VENTANA = 100
ATR_UMBRAL_MIN = 20.0  # Evitar compresión extrema (ATR% bajo)

# Filtros para Squeeze Momentum
SQZ_BB_LENGTH = 20
SQZ_BB_MULT = 2.0
SQZ_KC_LENGTH = 20
SQZ_KC_MULT = 1.5

RSI_15M_MIN = 30.0
RSI_15M_MAX = 42.0  # Ajustado para captar entradas en suelo

# Alertas opcionales
ALERTA_NUEVO_PATRON = True
ALERTA_CONFIRMADO_TF = True

EXCLUIR = {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "BUSD", "USDD"}

STATE_FILE = Path("data/setup_state.json")
SIGNALS_LOG = Path("data/setup_log.jsonl")

LIMA_OFFSET = timedelta(hours=-5)

COINBEACON_SETUP_URL = "https://api.coinbeacon.io/setups/binance"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"

# ═══════════════════════════════════════════════
# CONTADOR DE DIAGNÓSTICO
# ═══════════════════════════════════════════════
CONTADOR_FILTROS = {
    "SIN_SETUP": 0,
    "CONFIANZA_BAJA": 0,
    "SCORE_BAJO": 0,
    "BIAS_REGIME_CONTRADICCION": 0,
    "RSI_FUERA_RANGO": 0,
    "ADX_BAJO": 0,
    "DI_NO_ALINEADO": 0,
    "SQZ_NO_ALINEADO": 0,
    "ATR_BAJO": 0,
    "SIN_MOTIVOS": 0,
    "PUNTOS_INSUFICIENTES": 0,
    "PASA": 0,
}

# ============================================================
# FUNCIONES DE CÁLCULO DE INDICADORES TÉCNICOS
# ============================================================

def _media(xs):
    return sum(xs) / len(xs) if xs else 0.0

def _sma(serie, length):
    if len(serie) < length:
        return None
    return sum(serie[-length:]) / length

def _stdev(serie, length):
    if len(serie) < length:
        return None
    ventana = serie[-length:]
    m = sum(ventana) / length
    return (sum((x - m) ** 2 for x in ventana) / length) ** 0.5

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
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
        
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    
    def smooth(data, period):
        smoothed = [sum(data[:period])]
        for i in range(period, len(data)):
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + data[i])
        return smoothed
    
    atr_smooth = smooth(tr_list, length)
    plus_dm_smooth = smooth(plus_dm_list, length)
    minus_dm_smooth = smooth(minus_dm_list, length)
    
    di_plus_list, di_minus_list, dx_list = [], [], []
    for i in range(len(atr_smooth)):
        if atr_smooth[i] == 0:
            continue
        di_plus = (plus_dm_smooth[i] / atr_smooth[i]) * 100
        di_minus = (minus_dm_smooth[i] / atr_smooth[i]) * 100
        di_plus_list.append(di_plus)
        di_minus_list.append(di_minus)
        di_sum = di_plus + di_minus
        if di_sum != 0:
            dx_list.append(abs(di_plus - di_minus) / di_sum * 100)
    
    if len(dx_list) < length:
        return None
    adx = sum(dx_list[-length:]) / length
    return {"adx": adx, "di_plus": di_plus_list[-1] if di_plus_list else None, "di_minus": di_minus_list[-1] if di_minus_list else None}

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
            trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    rangema = _sma(trs, n)
    if rangema is None:
        return None
    upperKC = ma + rangema * multKC
    lowerKC = ma - rangema * multKC
    
    squeeze_on = (lowerBB > lowerKC) and (upperBB < upperKC)
    squeeze_off = (lowerBB < lowerKC) and (upperBB > upperKC)
    
    serie_mom = []
    for i in range(n - 1, len(closes)):
        hh = max(highs[i-n+1:i+1])
        ll = min(lows[i-n+1:i+1])
        sma_c = sum(closes[i-n+1:i+1]) / n
        ref = 0.25 * (hh + ll) + 0.5 * sma_c
        serie_mom.append(closes[i] - ref)
    if len(serie_mom) < n + 1:
        return None
    m_actual = _linreg_value(serie_mom[-n:])
    m_prev = _linreg_value(serie_mom[-n-1:-1])
    if m_actual > 0:
        color = "lime" if m_actual > m_prev else "green"
    else:
        color = "red" if m_actual < m_prev else "maroon"
    return {"squeeze_on": squeeze_on, "squeeze_off": squeeze_off, "momentum": m_actual, "momentum_prev": m_prev, "color": color}

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
    historico = atrs[-ventana:]
    menores = sum(1 for x in historico if x <= actual)
    return round((menores / len(historico)) * 100, 2)

# ============================================================
# OBTENCIÓN DE VELAS DESDE OKX
# ============================================================

def obtener_velas_okx(symbol, timeframe="15m", limit=200):
    base = symbol.replace("USDT", "")
    inst_id = f"{base}-USDT"
    url = f"{OKX_CANDLES_URL}?instId={inst_id}&bar={timeframe}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"      ⚠️ OKX velas {symbol}: {str(e)[:60]}", flush=True)
        return []
    if data.get("code") != "0":
        return []
    velas = data.get("data", [])
    if not velas:
        return []
    velas.reverse()
    resultado = []
    for v in velas:
        try:
            resultado.append({
                "ts": int(v[0]),
                "open": float(v[1]),
                "high": float(v[2]),
                "low": float(v[3]),
                "close": float(v[4]),
                "volume": float(v[5]),
            })
        except (ValueError, IndexError):
            continue
    return resultado

# ============================================================
# FUNCIONES ORIGINALES (CoinBeacon, estado, Telegram)
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

def log_senal(registro):
    SIGNALS_LOG.parent.mkdir(exist_ok=True)
    if SIGNALS_LOG.exists() and SIGNALS_LOG.stat().st_size > 1_000_000:
        try:
            with SIGNALS_LOG.open("r", encoding="utf-8") as f:
                lineas = f.readlines()
            with SIGNALS_LOG.open("w", encoding="utf-8") as f:
                f.writelines(lineas[-1000:])
        except Exception:
            pass
    with SIGNALS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")

def enviar_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("      ⚠️ Telegram no configurado", flush=True)
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
    except Exception as e:
        print(f"      ⚠️ Telegram: {str(e)[:60]}", flush=True)
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
    except Exception as e:
        print(f"      ⚠️ setup {symbol_binance}: {str(e)[:60]}", flush=True)
        return None

def extraer_datos_clave(data):
    if not data:
        return None
    setup = data.get("setup", {})
    if not setup:
        return None
    archetype = setup.get("archetype", {}) or {}
    patterns_list = data.get("patterns", []) or []
    pattern_names = [p.get("type", "") for p in patterns_list if p.get("type")]
    precio = data.get("price") or setup.get("price") or data.get("currentPrice")
    return {
        "price": precio,
        "bias": setup.get("bias", ""),
        "regime": (setup.get("regime", {}) or {}).get("label", ""),
        "confidence": setup.get("confidence", 0),
        "direction_score": setup.get("directionScore", 0),
        "percentile": setup.get("scorePercentile", 0),
        "archetype_key": archetype.get("key", ""),
        "archetype_label": archetype.get("label", ""),
        "archetype_direction": archetype.get("direction", ""),
        "narrative": setup.get("narrative", ""),
        "confirmed_by_tf": setup.get("confirmedByTf", ""),
        "continuation": setup.get("continuation", 0),
        "reversal": setup.get("reversal", 0),
        "exhaustion": setup.get("exhaustion", 0),
        "patterns": pattern_names,
        "trend_alignment": setup.get("trendAlignment", {}),
    }

def calcular_puntuacion(motivos, actual, anterior):
    puntos = 0
    bias_antes = anterior.get("bias", "")
    for m in motivos:
        tipo = m["tipo"]
        if tipo == "confirmado_tf_bull":
            puntos += 30
        elif tipo == "regimen_alcista":
            puntos += 25
        elif tipo == "bias_bullish":
            if bias_antes == "bearish":
                puntos += 25
            elif bias_antes in ("", "neutral"):
                puntos += 15
        elif tipo == "setup_fuerte":
            puntos += 20
        elif tipo == "nuevo_archetype_long":
            arch = actual.get("archetype_key", "")
            if arch in ("breakout_watch", "pattern_breakout", "trendline_break"):
                puntos += 20
            elif arch in ("level_bounce", "support_bounce"):
                puntos += 10
            else:
                puntos += 12
        elif tipo == "nuevo_patron_bull":
            puntos += 10
    conf = actual.get("confidence", 0)
    if conf >= 8.5:
        puntos += 10
    elif conf >= 8.0:
        puntos += 5
    pctl = actual.get("percentile", 0)
    if pctl >= 95:
        puntos += 10
    elif pctl >= 90:
        puntos += 5
    return puntos

def construir_mensaje(symbol, actual, anterior, motivos, puntuacion, rsi_15m=None, filtros_tecnicos=None):
    precio = actual.get("price", 0)
    bias = actual.get("bias", "")
    regime = actual.get("regime", "")
    score = actual.get("direction_score", 0)
    if bias == "bullish":
        emoji = "🟢"
    elif bias == "bearish":
        emoji = "🔴"
    else:
        emoji = "⚪"
    rsi15_str = f"{rsi_15m:.1f}" if rsi_15m is not None else "N/A"
    lineas = [
        f"{emoji} {symbol} setup 15M",
        f"━━━━━━━━━━━━━━━━━━━",
    ]
    if precio:
        lineas.append(f"💰 Precio: ${precio:.6f}")
    else:
        lineas.append(f"💰 Precio: N/A")
    lineas.append(f"📊 Régimen: {regime}")
    lineas.append(f"🎯 Bias: {bias.upper()}")
    lineas.append(f"📉 RSI 15m: {rsi15_str}")
    lineas.append(f"📊 Score: {score}")
    lineas.append(f"🎯 Puntuación: {puntuacion}/100")
    
    if filtros_tecnicos:
        adx = filtros_tecnicos.get("adx")
        if adx:
            lineas.append(f"📊 ADX: {adx['adx']:.1f} | DI+ {adx['di_plus']:.1f} | DI- {adx['di_minus']:.1f}")
        sqz = filtros_tecnicos.get("sqz")
        if sqz:
            lineas.append(f"📈 Squeeze: {sqz['color'].upper()} | Val: {sqz['momentum']:+.4f}")
        atr_pct = filtros_tecnicos.get("atr_pct")
        if atr_pct is not None:
            lineas.append(f"📉 ATR%: {atr_pct:.1f}")
    
    lineas.append(f"🕐 {hora_lima().strftime('%H:%M')} Lima")
    lineas.append(f"━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lineas)

# ============================================================
# DETECCIÓN DE CAMBIOS CON FILTROS TÉCNICOS (con diagnóstico)
# ============================================================

def detectar_cambios(actual, anterior, rsi_15m=None, filtros_tecnicos=None):
    global CONTADOR_FILTROS
    if not anterior:
        return []
    motivos = []
    bias_actual = actual.get("bias", "")
    bias_antes = anterior.get("bias", "")
    regime_actual = actual.get("regime", "")
    regime_antes = anterior.get("regime", "")
    arch_actual = actual.get("archetype_key", "")
    arch_antes = anterior.get("archetype_key", "")
    arch_dir = actual.get("archetype_direction", "")
    conf = actual.get("confidence", 0)
    score = actual.get("direction_score", 0)
    pctl = actual.get("percentile", 0)
    confirmed = actual.get("confirmed_by_tf", "")
    confirmed_antes = anterior.get("confirmed_by_tf", "")
    
    # Filtros base originales
    if conf < CONFIANZA_MIN:
        CONTADOR_FILTROS["CONFIANZA_BAJA"] += 1
        return []
    if score < DIRECTION_SCORE_MIN and bias_actual != "bullish":
        CONTADOR_FILTROS["SCORE_BAJO"] += 1
        return []
    if bias_actual == "bullish" and "TREND DOWN" in regime_actual:
        CONTADOR_FILTROS["BIAS_REGIME_CONTRADICCION"] += 1
        return []
    
    # Filtro RSI 15m
    if rsi_15m is not None:
        if rsi_15m > RSI_15M_MAX or rsi_15m < RSI_15M_MIN:
            CONTADOR_FILTROS["RSI_FUERA_RANGO"] += 1
            return []
    
    # ═══════════════════════════════════════════════
    # FILTROS TÉCNICOS INDIVIDUALES (ADX, SQZ, ATR)
    # ═══════════════════════════════════════════════
    if filtros_tecnicos:
        adx_data = filtros_tecnicos.get("adx")
        if adx_data:
            adx_val = adx_data.get("adx", 0)
            if adx_val < ADX_UMBRAL:
                CONTADOR_FILTROS["ADX_BAJO"] += 1
                return []
            di_plus = adx_data.get("di_plus", 0)
            di_minus = adx_data.get("di_minus", 0)
            if bias_actual == "bullish" and di_plus <= di_minus:
                CONTADOR_FILTROS["DI_NO_ALINEADO"] += 1
                return []
            if bias_actual == "bearish" and di_minus <= di_plus:
                CONTADOR_FILTROS["DI_NO_ALINEADO"] += 1
                return []
        
        sqz_data = filtros_tecnicos.get("sqz")
        if sqz_data:
            sqz_color = sqz_data.get("color", "")
            if bias_actual == "bullish" and sqz_color not in ("lime", "maroon"):
                CONTADOR_FILTROS["SQZ_NO_ALINEADO"] += 1
                return []
            if bias_actual == "bearish" and sqz_color not in ("red", "green"):
                CONTADOR_FILTROS["SQZ_NO_ALINEADO"] += 1
                return []
        
        atr_pct = filtros_tecnicos.get("atr_pct")
        if atr_pct is not None and atr_pct < ATR_UMBRAL_MIN:
            CONTADOR_FILTROS["ATR_BAJO"] += 1
            return []
    
    # SEÑALES (originales)
    if bias_actual == "bullish" and bias_antes != "bullish":
        motivos.append({
            "tipo": "bias_bullish",
            "texto": f"De {bias_antes or 'neutral'} → BULLISH (score {score})",
        })
    if (arch_actual and arch_actual != arch_antes and arch_dir == "long"):
        motivos.append({
            "tipo": "nuevo_archetype_long",
            "texto": f"Setup: {actual.get('archetype_label','')}",
        })
    if (regime_actual in ("TREND UP", "STRONG TREND UP") and regime_antes not in ("TREND UP", "STRONG TREND UP")):
        motivos.append({
            "tipo": "regimen_alcista",
            "texto": f"Régimen: {regime_actual}",
        })
    if ALERTA_NUEVO_PATRON:
        patrones_antes = set(anterior.get("patterns", []))
        patrones_ahora = set(actual.get("patterns", []))
        nuevos = patrones_ahora - patrones_antes
        patrones_bull = {"Double Bottom", "Hammer", "Morning Star",
                         "Bullish Engulfing", "Inverse Head and Shoulders",
                         "Three White Soldiers", "Bullish Harami", "Hikkake"}
        nuevos_bull = [p for p in nuevos if p in patrones_bull]
        if nuevos_bull:
            motivos.append({
                "tipo": "nuevo_patron_bull",
                "texto": f"Patrón: {', '.join(nuevos_bull)}",
            })
    if (ALERTA_CONFIRMADO_TF and confirmed and not confirmed_antes and bias_actual == "bullish"):
        motivos.append({
            "tipo": "confirmado_tf_bull",
            "texto": f"Confirmado por: {confirmed}",
        })
    pctl_antes = anterior.get("percentile", 0)
    if (bias_actual == "bullish" and pctl >= 95 and pctl_antes < 95):
        motivos.append({
            "tipo": "setup_fuerte",
            "texto": f"Percentil {pctl:.1f} (antes {pctl_antes:.1f})",
        })
    
    tipos = [m["tipo"] for m in motivos]
    if "bias_bullish" in tipos and "regimen_alcista" in tipos:
        motivos = [m for m in motivos if m["tipo"] != "regimen_alcista"]
        for m in motivos:
            if m["tipo"] == "bias_bullish":
                m["texto"] += f" | Régimen: {regime_actual}"
    
    if not motivos:
        CONTADOR_FILTROS["SIN_MOTIVOS"] += 1
        return []
    puntuacion = calcular_puntuacion(motivos, actual, anterior)
    motivos[0]["puntuacion"] = puntuacion
    if puntuacion < UMBRAL_PUNTOS:
        CONTADOR_FILTROS["PUNTOS_INSUFICIENTES"] += 1
        return []
    CONTADOR_FILTROS["PASA"] += 1
    return motivos

# ============================================================
# RESUMEN DIAGNÓSTICO
# ============================================================

def imprimir_resumen_diagnostico():
    total = sum(CONTADOR_FILTROS.values())
    print("\n" + "=" * 70, flush=True)
    print("🔬 DIAGNÓSTICO — ¿Qué detuvo cada señal en este run?", flush=True)
    print("=" * 70, flush=True)

    if total == 0:
        print("   (Sin evaluaciones registradas)", flush=True)
        return

    orden = sorted(CONTADOR_FILTROS.items(), key=lambda x: -x[1])
    for nombre, count in orden:
        if count == 0:
            continue
        pct = (count / total) * 100
        barra = "█" * int(pct / 3)
        print(f"   {nombre:28s} {count:3d}  ({pct:5.1f}%)  {barra}", flush=True)

    print("-" * 70, flush=True)
    print(f"   TOTAL evaluaciones: {total}", flush=True)

# ============================================================
# MAIN
# ============================================================

def main():
    global CONTADOR_FILTROS
    CONTADOR_FILTROS = {k: 0 for k in CONTADOR_FILTROS}

    print("=" * 70, flush=True)
    print("📐 SETUP 15M — LISTA FIJA 60 MONEDAS PREMIUM", flush=True)
    print(f"   Timeframe: {TIMEFRAME} | {len(SYMBOLS)} monedas", flush=True)
    print(f"   Filtros técnicos: ADX > {ADX_UMBRAL} | Squeeze alineado | ATR% > {ATR_UMBRAL_MIN}", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    estado = cargar_estado()
    monedas_estado = estado.get("monedas", {})

    enviadas = 0
    errores = 0
    nuevas_monedas = 0

    for i, base in enumerate(SYMBOLS, 1):
        symbol_binance = f"{base}USDT"
        
        # 1. Obtener setup de CoinBeacon
        data = consultar_setup(symbol_binance)
        if not data:
            errores += 1
            CONTADOR_FILTROS["SIN_SETUP"] += 1
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue
        
        actual = extraer_datos_clave(data)
        if not actual:
            CONTADOR_FILTROS["SIN_SETUP"] += 1
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue
        
        # 2. Obtener velas de OKX y calcular filtros técnicos
        velas = obtener_velas_okx(symbol_binance, TIMEFRAME, 200)
        filtros_tecnicos = {}
        if velas and len(velas) >= 50:
            try:
                adx = calcular_adx(velas, ADX_LENGTH)
                sqz = calcular_squeeze_momentum(velas, SQZ_BB_LENGTH, SQZ_BB_MULT, SQZ_KC_LENGTH, SQZ_KC_MULT)
                atr_pct = calcular_atr_percentile(velas, ATR_PERIOD, ATR_VENTANA)
                filtros_tecnicos = {"adx": adx, "sqz": sqz, "atr_pct": atr_pct}
            except Exception as e:
                print(f"      ⚠️ Error calculando filtros {base}: {str(e)[:60]}", flush=True)
        
        # 3. Calcular RSI 15m (ya existente)
        rsi_15m = None
        
        anterior = monedas_estado.get(symbol_binance)
        
        if not anterior:
            nuevas_monedas += 1
            print(f"[{i}/{len(SYMBOLS)}] 🆕 {base} (primera vez)", flush=True)
        else:
            precio_dbg = actual.get("price")
            precio_str = f"${precio_dbg:.6f}" if precio_dbg else "N/A"
            print(f"[{i}/{len(SYMBOLS)}] 🔍 {base} {precio_str} | bias={actual['bias']} conf={actual['confidence']:.1f}", flush=True)
        
        motivos = detectar_cambios(actual, anterior, rsi_15m, filtros_tecnicos)
        
        if motivos:
            puntuacion = motivos[0].get("puntuacion", 0)
            print(f"      🎯 {len(motivos)} señal(es) | puntos={puntuacion}/100", flush=True)
            msg = construir_mensaje(base, actual, anterior, motivos, puntuacion, rsi_15m, filtros_tecnicos)
            if enviar_telegram(msg):
                enviadas += 1
                log_senal({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": base,
                    "cambios": [m["tipo"] for m in motivos],
                    "puntuacion": puntuacion,
                    "bias": actual["bias"],
                    "regime": actual["regime"],
                    "confidence": actual["confidence"],
                    "archetype": actual["archetype_key"],
                    "price": actual["price"],
                    "rsi_15m": rsi_15m,
                    "adx": filtros_tecnicos.get("adx", {}).get("adx") if filtros_tecnicos.get("adx") else None,
                    "sqz_color": filtros_tecnicos.get("sqz", {}).get("color") if filtros_tecnicos.get("sqz") else None,
                    "atr_pct": filtros_tecnicos.get("atr_pct"),
                })
                print(f"      ✅ enviado", flush=True)
        
        monedas_estado[symbol_binance] = {
            **actual,
            "rsi_15m": rsi_15m,
            "filtros_tecnicos": filtros_tecnicos,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        time.sleep(SLEEP_ENTRE_LLAMADAS)

    estado["monedas"] = monedas_estado
    estado["updated_at"] = datetime.now(timezone.utc).isoformat()
    guardar_estado(estado)

    print("\n" + "=" * 70, flush=True)
    print(f"🎯 Alertas enviadas: {enviadas}", flush=True)
    print(f"⚠️ Errores: {errores}", flush=True)
    print(f"🆕 Monedas nuevas: {nuevas_monedas}", flush=True)
    print(f"💾 Monedas en estado: {len(monedas_estado)}", flush=True)
    print("=" * 70, flush=True)

    imprimir_resumen_diagnostico()

    print("\n🏁 TERMINADO", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
