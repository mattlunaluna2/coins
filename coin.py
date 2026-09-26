#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COIN — Pump/Dump Bot
- Sigue TODAS las monedas de CoinBeacon
- Solo avisa PUMP de cualquier moneda
- Solo avisa DUMP de monedas con PUMP previa (< 24h)
- 5m: % ≥ 3% | 10m: % ≥ 5%
- Vol confirmed obligatorio + cooldown 30 min + 1 alerta por dirección/run
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

SYMBOLS = []

PCT_MIN_5M = 3.0
PCT_MIN_10M = 5.0
REQUIERE_VOL_CONFIRMED = True
COOLDOWN_MIN = 30
PUMP_ACTIVA_HORAS = 24

STATE_FILE = Path("data/coin_state.json")
SIGNALS_LOG = Path("data/coin_log.jsonl")

LIMA_OFFSET = timedelta(hours=-5)

COINBEACON_URL = "https://api.coinbeacon.io/pumping/events"


def hora_lima():
    return datetime.now(timezone.utc) + LIMA_OFFSET


def cargar_estado():
    if not STATE_FILE.exists():
        return {"vistos": [], "cooldown": {}, "pumps_activas": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if "vistos" not in data:
                data["vistos"] = []
            if "cooldown" not in data:
                data["cooldown"] = {}
            if "pumps_activas" not in data:
                data["pumps_activas"] = {}
            return data
    except Exception:
        pass
    return {"vistos": [], "cooldown": {}, "pumps_activas": {}}


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


def consultar_pumping_events():
    token = os.environ.get("COINBEACON_TOKEN")
    if not token:
        print("⚠️ COINBEACON_TOKEN no configurado", flush=True)
        return []

    types = "pump_5m,pump_10m,dump_5m,dump_10m"
    url = f"{COINBEACON_URL}?exchange=binance&types={types}&pair=USDT&limit=500"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Cookie": f"access_token={token}",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("items", [])
    except Exception as e:
        print(f"⚠️ CoinBeacon: {str(e)[:80]}", flush=True)
        return []


def clasificar_evento(ev):
    tipo = ev.get("type", "")
    if "pump" in tipo:
        emoji = "🚀"
        tipo_str = "PUMP 5m" if "5m" in tipo else "PUMP 10m"
    elif "dump" in tipo:
        emoji = "💥"
        tipo_str = "DUMP 5m" if "5m" in tipo else "DUMP 10m"
    else:
        emoji = "⚡"
        tipo_str = tipo
    return emoji, tipo_str


def main():
    print("=" * 70, flush=True)
    print("🪙 COIN — Pump/Dump Bot", flush=True)
    print(f"   Pump: cualquier moneda", flush=True)
    print(f"   Dump: solo monedas con pump activa (< {PUMP_ACTIVA_HORAS}h)", flush=True)
    print(f"   Umbrales: 5m ≥ {PCT_MIN_5M}% | 10m ≥ {PCT_MIN_10M}%", flush=True)
    print(f"   Vol confirmed: {REQUIERE_VOL_CONFIRMED} | Cooldown: {COOLDOWN_MIN} min", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {datetime.now(timezone.utc).isoformat()}", flush=True)

    estado = cargar_estado()
    vistos = set(estado.get("vistos", []))
    cooldowns = estado.get("cooldown", {})
    pumps_activas = estado.get("pumps_activas", {})

    ahora_ts = datetime.now(timezone.utc).timestamp()

    # Limpiar pumps antiguas
    pumps_activas_limpias = {}
    for sym, ts_pump in pumps_activas.items():
        try:
            edad_h = (ahora_ts - float(ts_pump)) / 3600
            if edad_h < PUMP_ACTIVA_HORAS:
                pumps_activas_limpias[sym] = ts_pump
        except (ValueError, TypeError):
            continue
    pumps_activas = pumps_activas_limpias

    if pumps_activas:
        print(f"\n💼 Pumps activas ({len(pumps_activas)}): {', '.join(pumps_activas.keys())}", flush=True)

    print("\n📡 Consultando CoinBeacon...", flush=True)
    eventos = consultar_pumping_events()
    print(f"   Total eventos recibidos: {len(eventos)}", flush=True)

    if not eventos:
        print("   ⚠️ Sin eventos", flush=True)
        return

    eventos_por_grupo = {}
    for ev in eventos:
        symbol_base = ev.get("symbol", "").replace("USDT", "")
        tipo = ev.get("type", "")
        spotted_at = ev.get("spottedAt", 0)
        clave_grupo = f"{symbol_base}_{tipo}"
        actual = eventos_por_grupo.get(clave_grupo)
        if actual is None or spotted_at > actual.get("spottedAt", 0):
            eventos_por_grupo[clave_grupo] = ev

    eventos_filtrados = list(eventos_por_grupo.values())
    print(f"   Eventos únicos por moneda+tipo: {len(eventos_filtrados)}", flush=True)

    direcciones_por_moneda = {}
    enviadas = 0

    for ev in eventos_filtrados:
        symbol_full = ev.get("symbol", "")
        symbol_base = symbol_full.replace("USDT", "")
        tipo = ev.get("type", "")
        spotted_at = ev.get("spottedAt", 0)
        pct = ev.get("pct", 0)
        rvol = ev.get("rvol", 0)
        vol_ok = ev.get("volConfirmed", False)
        bias = ev.get("bias", "")
        clasif = ev.get("classification", "")
        setup = ev.get("setupScore", 0)

        es_pump = "pump" in tipo
        es_dump = "dump" in tipo

        clave_evento = f"{symbol_full}_{tipo}_{spotted_at}"
        if clave_evento in vistos:
            continue

        if REQUIERE_VOL_CONFIRMED and not vol_ok:
            vistos.add(clave_evento)
            continue

        if "5m" in tipo:
            if abs(pct) < PCT_MIN_5M:
                vistos.add(clave_evento)
                continue
        else:
            if abs(pct) < PCT_MIN_10M:
                vistos.add(clave_evento)
                continue

        clave_cooldown = f"{symbol_base}_{tipo}_{bias}_{clasif}"
        ultimo = cooldowns.get(clave_cooldown, 0)
        if ahora_ts - ultimo < COOLDOWN_MIN * 60:
            vistos.add(clave_evento)
            continue

        if es_dump and symbol_base not in pumps_activas:
            print(f"      ⏭️ {symbol_base} {tipo} omitido (sin pump previa)", flush=True)
            vistos.add(clave_evento)
            continue

        direccion = "up" if es_pump else "down"
        direcciones_usadas = direcciones_por_moneda.get(symbol_base, set())
        if direccion in direcciones_usadas:
            print(f"      ⏭️ {symbol_base} {tipo} omitido (ya alertado {direccion})", flush=True)
            vistos.add(clave_evento)
            continue

        price = ev.get("price", 0)
        prev_price = ev.get("prevPrice", 0)
        quote_vol = ev.get("quoteVolume24h", 0)

        emoji, tipo_str = clasificar_evento(ev)

        ts_ev = datetime.fromtimestamp(spotted_at / 1000, tz=timezone.utc) + LIMA_OFFSET
        ts_str = ts_ev.strftime("%H:%M")

        if prev_price and prev_price > 0:
            precio_txt = f"${prev_price:.6f} → ${price:.6f}"
        else:
            precio_txt = f"${price:.6f}"

        vol_txt = f"⚡ RVOL: {rvol:.2f}×"
        if vol_ok:
            vol_txt += "  ✅ Vol confirmed"

        tag_extra = ""
        if es_dump and symbol_base in pumps_activas:
            tag_extra = "🔄 Cierre de pump previa\n"

        msg = (
            f"{emoji} {symbol_base} — {tipo_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💵 {precio_txt}\n"
            f"📊 Movimiento: {pct:+.2f}%\n"
            f"{vol_txt}\n"
            f"🎯 Smart Setup: {setup:.1f}/10\n"
            f"💰 Vol 24h: ${quote_vol:,.0f}\n"
            f"🧭 {bias.upper()} | {clasif}\n"
            f"{tag_extra}"
            f"🕐 {ts_str} Lima\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )

        if enviar_telegram(msg):
            enviadas += 1
            vistos.add(clave_evento)
            cooldowns[clave_cooldown] = ahora_ts
            direcciones_por_moneda.setdefault(symbol_base, set()).add(direccion)

            if es_pump:
                pumps_activas[symbol_base] = ahora_ts
            elif es_dump and symbol_base in pumps_activas:
                pumps_activas.pop(symbol_base, None)
                print(f"      ✅ {symbol_base} cerrada (dump tras pump)", flush=True)

            log_senal({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol_base,
                "tipo": tipo,
                "pct": pct,
                "price": price,
                "prev_price": prev_price,
                "rvol": rvol,
                "vol_conf": vol_ok,
                "setup": setup,
                "bias": bias,
                "clasif": clasif,
            })
            print(f"   {emoji} {symbol_base} {tipo} {pct:+.2f}% (setup {setup:.1f}) → enviado", flush=True)

    todos_vistos = list(vistos)
    if len(todos_vistos) > 3000:
        todos_vistos = todos_vistos[-3000:]

    if len(cooldowns) > 300:
        cooldowns_ordenados = sorted(cooldowns.items(), key=lambda x: x[1], reverse=True)[:300]
        cooldowns = dict(cooldowns_ordenados)

    estado["vistos"] = todos_vistos
    estado["cooldown"] = cooldowns
    estado["pumps_activas"] = pumps_activas
    estado["updated_at"] = datetime.now(timezone.utc).isoformat()
    guardar_estado(estado)

    print("\n" + "=" * 70, flush=True)
    print(f"🎯 Alertas enviadas: {enviadas}", flush=True)
    print(f"💼 Pumps activas al cierre: {len(pumps_activas)}", flush=True)
    print("=" * 70, flush=True)
    print("🏁 TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
