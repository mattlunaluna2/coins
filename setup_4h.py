#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SETUP 4H — Smart Setup Bot (4h)
- Detecta INICIO de tendencia alcista en 4h
- Filtro RSI 4h entre 34 y 50 (solo suelos, no techos)
- Confirmado por 1d
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ═══════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════
TIMEFRAME = "4h"
TOP_MONEDAS = 40
SLEEP_ENTRE_LLAMADAS = 1.2

CONFIANZA_MIN = 6.0
DIRECTION_SCORE_MIN = 55

UMBRAL_PUNTOS = 35

# Filtro RSI 4h: solo alertar si está entre 34 y 50
RSI_4H_MIN = 30.0
RSI_4H_MAX = 34.0

EXCLUIR = {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "BUSD", "USDD"}

STATE_FILE = Path("data/setup_4h_state.json")
SIGNALS_LOG = Path("data/setup_4h_log.jsonl")

LIMA_OFFSET = timedelta(hours=-5)

COINBEACON_SETUP_URL = "https://api.coinbeacon.io/setups/binance"
OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"


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


def obtener_top_monedas(limit=40):
    try:
        req = urllib.request.Request(OKX_TICKERS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    if data.get("code") != "0":
        return []
    items = data.get("data", [])
    pares = []
    for it in items:
        inst_id = it.get("instId", "")
        if not inst_id.endswith("-USDT"):
            continue
        base = inst_id.replace("-USDT", "")
        if base in EXCLUIR:
            continue
        try:
            vol = float(it.get("volCcy24h", 0))
        except (ValueError, TypeError):
            vol = 0
        pares.append({"symbol": f"{base}USDT", "base": base, "vol": vol})
    pares.sort(key=lambda x: x["vol"], reverse=True)
    return pares[:limit]


def calcular_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def obtener_rsi_4h(symbol_binance):
    if symbol_binance.endswith("USDT"):
        base = symbol_binance[:-4]
        inst_id = f"{base}-USDT"
    else:
        return None

    url = f"{OKX_CANDLES_URL}?instId={inst_id}&bar=4H&limit=30"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

    if data.get("code") != "0":
        return None

    velas = data.get("data", [])
    if len(velas) < 15:
        return None

    velas.reverse()
    cierres = [float(v[4]) for v in velas]
    return calcular_rsi(cierres)


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
    detectors = data.get("detectors", {}) or {}
    patterns_list = detectors.get("patterns", []) or []
    pattern_names = [p.get("type", "") for p in patterns_list if p.get("type")]

    precio = data.get("price") or setup.get("price")

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
            elif arch in ("level_bounce", "support_bounce", "breakout_retest"):
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


def construir_mensaje(symbol, actual, anterior, motivos, puntuacion, rsi_4h=None):
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

    rsi_str = f"{rsi_4h:.1f}" if rsi_4h is not None else "N/A"

    lineas = [
        f"{emoji} {symbol} setup 4H",
        f"━━━━━━━━━━━━━━━━━━━",
    ]

    if precio is not None and precio > 0:
        lineas.append(f"💰 ${precio:,.6f}")
    else:
        lineas.append(f"💰 N/A")

    lineas.append(f"📊 {regime}")
    lineas.append(f"🎯 {bias.upper()} | RSI 4h: {rsi_str}")
    lineas.append(f"🔥 Score: {score} | Puntuación: {puntuacion}")
    lineas.append(f"🕐 {hora_lima().strftime('%H:%M')} Lima")
    lineas.append(f"━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lineas)


def detectar_cambios(actual, anterior, rsi_4h=None):
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

    if conf < CONFIANZA_MIN:
        return []
    if score < DIRECTION_SCORE_MIN and bias_actual != "bullish":
        return []

    if bias_actual == "bullish" and "TREND DOWN" in regime_actual:
        return []

    # Filtro RSI 4h entre 34 y 50
    if rsi_4h is not None:
        if rsi_4h > RSI_4H_MAX:
            return []
        if rsi_4h < RSI_4H_MIN:
            return []

    if bias_actual == "bullish" and bias_antes != "bullish":
        motivos.append({
            "tipo": "bias_bullish",
            "texto": f"De {bias_antes or 'neutral'} → BULLISH",
        })

    if (arch_actual and arch_actual != arch_antes and arch_dir == "long"):
        motivos.append({
            "tipo": "nuevo_archetype_long",
            "texto": f"Setup: {actual.get('archetype_label','')}",
        })

    if (regime_actual in ("TREND UP", "STRONG TREND UP")
            and regime_antes not in ("TREND UP", "STRONG TREND UP")):
        motivos.append({
            "tipo": "regimen_alcista",
            "texto": f"Régimen: {regime_actual}",
        })

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

    if (confirmed and not confirmed_antes and bias_actual == "bullish"):
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
        return []

    puntuacion = calcular_puntuacion(motivos, actual, anterior)
    motivos[0]["puntuacion"] = puntuacion

    if puntuacion < UMBRAL_PUNTOS:
        return []

    return motivos


def main():
    print("=" * 70, flush=True)
    print("📐 SETUP 4H", flush=True)
    print(f"   Timeframe: {TIMEFRAME} | Top {TOP_MONEDAS} monedas", flush=True)
    print(f"   Confianza min: {CONFIANZA_MIN} | Score min: {DIRECTION_SCORE_MIN}", flush=True)
    print(f"   Umbral puntuación: {UMBRAL_PUNTOS}/100", flush=True)
    print(f"   RSI 4h entre {RSI_4H_MIN} y {RSI_4H_MAX}", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    estado = cargar_estado()
    monedas_estado = estado.get("monedas", {})

    print("\n📡 Obteniendo top monedas de OKX...", flush=True)
    monedas = obtener_top_monedas(TOP_MONEDAS)
    print(f"   Monedas a analizar: {len(monedas)}", flush=True)

    if not monedas:
        print("⚠️ Sin monedas", flush=True)
        return

    enviadas = 0
    errores = 0
    nuevas_monedas = 0

    for i, m in enumerate(monedas, 1):
        symbol_binance = m["symbol"]
        base = m["base"]

        data = consultar_setup(symbol_binance)
        if not data:
            errores += 1
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue

        actual = extraer_datos_clave(data)
        if not actual:
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue

        anterior = monedas_estado.get(symbol_binance)

        rsi_4h = obtener_rsi_4h(symbol_binance)
        rsi_str = f"{rsi_4h:.1f}" if rsi_4h is not None else "N/A"

        if not anterior:
            nuevas_monedas += 1
            print(f"[{i}/{len(monedas)}] 🆕 {base} RSI4h={rsi_str}", flush=True)
        else:
            precio_dbg = actual.get("price")
            precio_str = f"${precio_dbg:.6f}" if precio_dbg else "N/A"
            print(f"[{i}/{len(monedas)}] 🔍 {base} {precio_str} | bias={actual['bias']} regime={actual['regime']} conf={actual['confidence']:.1f} | RSI4h={rsi_str}", flush=True)

        motivos = detectar_cambios(actual, anterior, rsi_4h)

        if motivos:
            puntuacion = motivos[0].get("puntuacion", 0)
            print(f"      🎯 {len(motivos)} señal(es) | puntos={puntuacion}/100", flush=True)
            msg = construir_mensaje(base, actual, anterior, motivos, puntuacion, rsi_4h)
            if enviar_telegram(msg):
                enviadas += 1
                log_senal({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": base,
                    "tf": "4h",
                    "cambios": [m["tipo"] for m in motivos],
                    "puntuacion": puntuacion,
                    "bias": actual["bias"],
                    "regime": actual["regime"],
                    "confidence": actual["confidence"],
                    "archetype": actual["archetype_key"],
                    "price": actual["price"],
                    "rsi_4h": rsi_4h,
                })
                print(f"      ✅ enviado", flush=True)

        monedas_estado[symbol_binance] = {
            **actual,
            "rsi_4h": rsi_4h,
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
    print("🏁 TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
