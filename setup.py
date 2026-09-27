#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SETUP — Smart Setup Bot (CoinBeacon /setups)
- Sigue top N monedas de OKX
- Detecta INICIO de movimiento alcista con sistema de puntuación por pesos
- Solo alerta cuando la puntuación ≥ UMBRAL_PUNTOS
- Primera vez que ve una moneda: solo guarda, no alerta
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
TIMEFRAME = "15m"
TOP_MONEDAS = 50
SLEEP_ENTRE_LLAMADAS = 1.2

# Filtros base
CONFIANZA_MIN = 7.0
DIRECTION_SCORE_MIN = 70

# Umbral de puntuación para alertar (sobre 100)
UMBRAL_PUNTOS = 40

# Alertas opcionales
ALERTA_NUEVO_PATRON = True
ALERTA_CONFIRMADO_TF = True

# Excluir stablecoins
EXCLUIR = {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "BUSD", "USDD"}

STATE_FILE = Path("data/setup_state.json")
SIGNALS_LOG = Path("data/setup_log.jsonl")

LIMA_OFFSET = timedelta(hours=-5)

COINBEACON_SETUP_URL = "https://api.coinbeacon.io/setups/binance"
OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"


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


def obtener_top_monedas(limit=50):
    try:
        req = urllib.request.Request(OKX_TICKERS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"⚠️ OKX tickers: {str(e)[:60]}", flush=True)
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

    return {
        "price": data.get("price") or setup.get("price") or data.get("currentPrice"),
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
    """
    Calcula un score 0-100 según el peso de cada señal.
    """
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

    # Bonus por confianza
    conf = actual.get("confidence", 0)
    if conf >= 8.5:
        puntos += 10
    elif conf >= 8.0:
        puntos += 5

    # Bonus por percentil
    pctl = actual.get("percentile", 0)
    if pctl >= 95:
        puntos += 10
    elif pctl >= 90:
        puntos += 5

    return puntos


def construir_mensaje(symbol, actual, anterior, motivos, puntuacion):
    precio = actual.get("price", 0)
    bias = actual.get("bias", "")
    regime = actual.get("regime", "")
    cont = actual.get("continuation", 0)
    rev = actual.get("reversal", 0)
    score = actual.get("direction_score", 0)
    confirmed = actual.get("confirmed_by_tf", "")

    # Círculo según bias
    if bias == "bullish":
        emoji = "🟢"
    elif bias == "bearish":
        emoji = "🔴"
    else:
        emoji = "⚪"

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
    lineas.append(f"📊 Score: {score}")

    if confirmed:
        lineas.append(f"✅ Confirmado por: {confirmed}")

    lineas.append(f"🔮 Continuación: {cont}/100")
    lineas.append(f"🔮 Reversión: {rev}/100")
    lineas.append(f"🎯 Puntuación: {puntuacion}/100")
    lineas.append(f"🕐 {hora_lima().strftime('%H:%M')} Lima")
    lineas.append(f"━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lineas)


def detectar_cambios(actual, anterior):
    """
    Solo alerta INICIO de movimiento alcista.
    Primera vez → solo guarda, no alerta.
    Requiere puntuación ≥ UMBRAL_PUNTOS y sin contradicciones.
    """
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

    # Filtro base: confianza mínima
    if conf < CONFIANZA_MIN:
        return []
    if score < DIRECTION_SCORE_MIN and bias_actual != "bullish":
        return []

    # Filtro contradicción: bias bullish pero régimen bajista → rebote, no tendencia
    if bias_actual == "bullish" and "TREND DOWN" in regime_actual:
        return []

    # SEÑAL 1: Cambio de bias a BULLISH
    if bias_actual == "bullish" and bias_antes != "bullish":
        motivos.append({
            "tipo": "bias_bullish",
            "titulo": "INICIO ALCISTA — Bias cambió a BULLISH",
            "texto": f"De {bias_antes or 'neutral'} → BULLISH (score {score})",
        })

    # SEÑAL 2: Nuevo archetype alcista
    if (arch_actual and arch_actual != arch_antes and arch_dir == "long"):
        motivos.append({
            "tipo": "nuevo_archetype_long",
            "titulo": "INICIO ALCISTA — Nuevo setup LONG",
            "texto": f"Setup: {actual.get('archetype_label','')}",
        })

    # SEÑAL 3: Régimen alcista
    if (regime_actual in ("TREND UP", "STRONG TREND UP")
            and regime_antes not in ("TREND UP", "STRONG TREND UP")):
        motivos.append({
            "tipo": "regimen_alcista",
            "titulo": "INICIO ALCISTA — Nuevo régimen alcista",
            "texto": f"Régimen: {regime_actual}",
        })

    # SEÑAL 4: Nuevo patrón alcista
    if ALERTA_NUEVO_PATRON:
        patrones_antes = set(anterior.get("patterns", []))
        patrones_ahora = set(actual.get("patterns", []))
        nuevos = patrones_ahora - patrones_antes
        patrones_bull = {"Double Bottom", "Hammer", "Morning Star",
                         "Bullish Engulfing", "Inverse Head and Shoulders",
                         "Three White Soldiers", "Bullish Harami",
                         "Hikkake"}
        nuevos_bull = [p for p in nuevos if p in patrones_bull]
        if nuevos_bull:
            motivos.append({
                "tipo": "nuevo_patron_bull",
                "titulo": "INICIO ALCISTA — Patrón detectado",
                "texto": f"Patrón: {', '.join(nuevos_bull)}",
            })

    # SEÑAL 5: Confirmación por TF mayor
    if (ALERTA_CONFIRMADO_TF and confirmed and not confirmed_antes
            and bias_actual == "bullish"):
        motivos.append({
            "tipo": "confirmado_tf_bull",
            "titulo": "INICIO ALCISTA — Confirmado por TF mayor",
            "texto": f"Confirmado por: {confirmed}",
        })

    # SEÑAL 6: Setup fuerte (percentil 95+)
    pctl_antes = anterior.get("percentile", 0)
    if (bias_actual == "bullish" and pctl >= 95 and pctl_antes < 95):
        motivos.append({
            "tipo": "setup_fuerte",
            "titulo": "SETUP ALCISTA FUERTE",
            "texto": f"Percentil {pctl:.1f} (antes {pctl_antes:.1f})",
        })

    # Deduplicar régimen duplicado
    tipos = [m["tipo"] for m in motivos]
    if "bias_bullish" in tipos and "regimen_alcista" in tipos:
        motivos = [m for m in motivos if m["tipo"] != "regimen_alcista"]
        for m in motivos:
            if m["tipo"] == "bias_bullish":
                m["texto"] += f" | Régimen: {regime_actual}"

    if not motivos:
        return []

    # Calcular puntuación por pesos
    puntuacion = calcular_puntuacion(motivos, actual, anterior)

    # Guardar puntuación en el primer motivo
    motivos[0]["puntuacion"] = puntuacion

    # Umbral mínimo de puntuación
    if puntuacion < UMBRAL_PUNTOS:
        return []

    return motivos


def main():
    print("=" * 70, flush=True)
    print("📐 SETUP", flush=True)
    print(f"   Timeframe: {TIMEFRAME} | Top {TOP_MONEDAS} monedas", flush=True)
    print(f"   Confianza min: {CONFIANZA_MIN} | Score min: {DIRECTION_SCORE_MIN}", flush=True)
    print(f"   Umbral puntuación: {UMBRAL_PUNTOS}/100", flush=True)
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

        if not anterior:
            nuevas_monedas += 1
            print(f"[{i}/{len(monedas)}] 🆕 {base} (primera vez, guardando)", flush=True)
        else:
            print(f"[{i}/{len(monedas)}] 🔍 {base} | bias={actual['bias']} regime={actual['regime']} conf={actual['confidence']:.1f}", flush=True)

        motivos = detectar_cambios(actual, anterior)

        if motivos:
            puntuacion = motivos[0].get("puntuacion", 0)
            print(f"      🎯 {len(motivos)} señal(es) | puntos={puntuacion}/100", flush=True)
            msg = construir_mensaje(base, actual, anterior, motivos, puntuacion)
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
                })
                print(f"      ✅ enviado", flush=True)

        monedas_estado[symbol_binance] = {
            **actual,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        time.sleep(SLEEP_ENTRE_LLAMADAS)

    estado["monedas"] = monedas_estado
    estado["updated_at"] = datetime.now(timezone.utc).isoformat()
    guardar_estado(estado)

    print("\n" + "=" * 70, flush=True)
    print(f"🎯 Alertas enviadas: {enviadas}", flush=True)
    print(f"⚠️ Errores: {errores}", flush=True)
    print(f"🆕 Monedas nuevas (primera vez): {nuevas_monedas}", flush=True)
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
