#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SETUP — Smart Setup Bot (CoinBeacon /setups)
- Sigue top N monedas de OKX
- Detecta cambios en el Smart Setup (bias, régimen, archetype, patrones)
- Alerta a Telegram cuando hay cambio relevante
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
SLEEP_ENTRE_LLAMADAS = 1.2     # segundos entre llamadas (rate limit 60/min)

# Filtros para alertar
CONFIANZA_MIN = 7.0            # 0-10
DIRECTION_SCORE_MIN = 70       # 0-100
PERCENTILE_MIN = 90            # 0-100 (top 10%)

# Qué alertar
ALERTA_NUEVO_SETUP = True       # archetype aparece
ALERTA_SETUP_ROTO = True        # archetype desaparece
ALERTA_CAMBIO_BIAS = True       # bullish ↔ bearish
ALERTA_CAMBIO_REGIMEN = True    # STRONG TREND UP → NEUTRAL, etc.
ALERTA_NUEVO_PATRON = True      # Double Bottom, Hammer, etc.
ALERTA_CONFIRMADO_TF = True     # pasa a confirmado por TF mayor

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
    """Extrae solo los campos que nos interesan del JSON."""
    if not data:
        return None
    setup = data.get("setup", {})
    if not setup:
        return None

    archetype = setup.get("archetype", {}) or {}
    patterns_list = data.get("patterns", []) or []
    pattern_names = [p.get("type", "") for p in patterns_list if p.get("type")]

    return {
        "price": data.get("price"),
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


def construir_mensaje(symbol, actual, anterior, motivos):
    """Construye el mensaje de Telegram con los cambios detectados."""
    precio = actual.get("price", 0)
    bias = actual.get("bias", "")
    regime = actual.get("regime", "")
    conf = actual.get("confidence", 0)
    pctl = actual.get("percentile", 0)
    archetype = actual.get("archetype_label", "")
    narrative = actual.get("narrative", "")
    cont = actual.get("continuation", 0)
    rev = actual.get("reversal", 0)
    tfa = actual.get("trend_alignment", {}) or {}
    confirmed = actual.get("confirmed_by_tf", "")

    emoji = "🟢" if bias == "bullish" else "🔴" if bias == "bearish" else "⚪"

    lineas = [
        f"{emoji} {symbol} — {motivos[0]['titulo']}",
        f"━━━━━━━━━━━━━━━━━━━",
        f"💰 Precio: ${precio:.6f}" if precio else "💰 Precio: N/A",
        f"📊 Régimen: {regime}",
        f"🎯 Bias: {bias.upper()} | Score: {actual.get('direction_score', 0)}",
        f"📈 Confianza: {conf:.1f}/10 (percentil {pctl:.1f})",
    ]

    if archetype:
        lineas.append(f"📐 Setup: {archetype}")
    if confirmed:
        lineas.append(f"✅ Confirmado por: {confirmed}")

    lineas.append(f"🔮 Continuación: {cont}/100 | Reversión: {rev}/100")

    if tfa:
        tfa_txt = " | ".join([f"{tf}:{v}" for tf, v in tfa.items()])
        lineas.append(f"📊 Alineación: {tfa_txt}")

    if narrative:
        lineas.append(f"💡 {narrative}")

    # Añadir motivo específico
    lineas.append(f"🔔 {' + '.join([m['texto'] for m in motivos])}")

    lineas.append(f"🕐 {hora_lima().strftime('%H:%M')} Lima")
    lineas.append(f"━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lineas)


def detectar_cambios(actual, anterior):
    """Compara estado actual vs anterior. Devuelve lista de motivos."""
    motivos = []

    if not anterior:
        # Primera vez que vemos esta moneda
        if (ALERTA_NUEVO_SETUP and actual["archetype_key"]
                and actual["confidence"] >= CONFIANZA_MIN
                and actual["percentile"] >= PERCENTILE_MIN):
            motivos.append({
                "tipo": "nuevo_setup",
                "titulo": "NUEVO SETUP DETECTADO",
                "texto": f"Nuevo: {actual['archetype_label']}",
            })
        return motivos

    # Cambio de bias
    if ALERTA_CAMBIO_BIAS and actual["bias"] != anterior.get("bias"):
        if actual["bias"] in ("bullish", "bearish"):
            motivos.append({
                "tipo": "cambio_bias",
                "titulo": "CAMBIO DE BIAS",
                "texto": f"De {anterior.get('bias','')} a {actual['bias']}",
            })

    # Cambio de régimen
    if ALERTA_CAMBIO_REGIMEN and actual["regime"] != anterior.get("regime"):
        motivos.append({
            "tipo": "cambio_regimen",
            "titulo": "CAMBIO DE RÉGIMEN",
            "texto": f"De {anterior.get('regime','')} a {actual['regime']}",
        })

    # Nuevo archetype
    if (ALERTA_NUEVO_SETUP and actual["archetype_key"]
            and actual["archetype_key"] != anterior.get("archetype_key")
            and actual["confidence"] >= CONFIANZA_MIN):
        motivos.append({
            "tipo": "nuevo_archetype",
            "titulo": "NUEVO SETUP",
            "texto": f"Nuevo archetype: {actual['archetype_label']}",
        })

    # Setup roto
    if (ALERTA_SETUP_ROTO and not actual["archetype_key"]
            and anterior.get("archetype_key")):
        motivos.append({
            "tipo": "setup_roto",
            "titulo": "SETUP ROTO",
            "texto": f"Se perdió: {anterior.get('archetype_label','')}",
        })

    # Nuevo patrón
    if ALERTA_NUEVO_PATRON:
        patrones_antes = set(anterior.get("patterns", []))
        patrones_ahora = set(actual.get("patterns", []))
        nuevos = patrones_ahora - patrones_antes
        if nuevos:
            motivos.append({
                "tipo": "nuevo_patron",
                "titulo": "NUEVO PATRÓN",
                "texto": f"Patrón: {', '.join(nuevos)}",
            })

    # Nueva confirmación por TF mayor
    if (ALERTA_CONFIRMADO_TF and actual["confirmed_by_tf"]
            and not anterior.get("confirmed_by_tf")):
        motivos.append({
            "tipo": "confirmado_tf",
            "titulo": "CONFIRMADO POR TF MAYOR",
            "texto": f"Confirmado por: {actual['confirmed_by_tf']}",
        })

    return motivos


def main():
    print("=" * 70, flush=True)
    print("📐 SETUP — Smart Setup Bot", flush=True)
    print(f"   Timeframe: {TIMEFRAME} | Top {TOP_MONEDAS} monedas", flush=True)
    print(f"   Confianza min: {CONFIANZA_MIN} | Score min: {DIRECTION_SCORE_MIN} | Percentile: {PERCENTILE_MIN}", flush=True)
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

    for i, m in enumerate(monedas, 1):
        symbol_binance = m["symbol"]
        base = m["base"]

        print(f"\n[{i}/{len(monedas)}] 🔍 {base}", flush=True)

        data = consultar_setup(symbol_binance)
        if not data:
            errores += 1
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue

        actual = extraer_datos_clave(data)
        if not actual:
            print(f"      ⚠️ sin datos clave", flush=True)
            time.sleep(SLEEP_ENTRE_LLAMADAS)
            continue

        anterior = monedas_estado.get(symbol_binance)

        print(f"      Bias: {actual['bias']} | Régimen: {actual['regime']} | Conf: {actual['confidence']:.1f} | Archetype: {actual['archetype_key']}", flush=True)

        motivos = detectar_cambios(actual, anterior)

        if motivos:
            print(f"      🎯 {len(motivos)} cambio(s) detectado(s): {[m['tipo'] for m in motivos]}", flush=True)
            msg = construir_mensaje(base, actual, anterior, motivos)
            if enviar_telegram(msg):
                enviadas += 1
                log_senal({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": base,
                    "cambios": [m["tipo"] for m in motivos],
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
