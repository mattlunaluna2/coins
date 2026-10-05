#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
RECOLECTOR ALTS — repo: mattlunaluna2/coins
OKX primario + MEXC fallback. Paralelo con retry.
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "LINK", "LTC",
    "FLUID", "ORCA", "GEOD", "SYRUP", "PEAQ", "SKY", "TIA", "AXS",
    "ATH", "STRK", "AAVE", "MORPHO", "LDO", "COMP", "FET",
    "BEAM", "IOTA", "HNT", "CHIP", "BAT", "RUNE", "XTZ",
    "APT", "AVAX", "ARB", "UNI", "SUSHI", "DASH", "ENA", "NEAR",
    "GALA", "ALGO", "DOT", "ICP", "FIL", "IMX", "ZEC", "WLD",
    "PYTH", "ZEN", "KAVA", "TWT",
    "SAND", "MANA", "APE", "SUPER", "ZRO", "PUMP",
    "SENT", "API3", "CETUS", "AERO",
]

OKX_LIMIT_VELAS = 200
RETENCION_PULSO_H = 168
MAX_WORKERS = 3
PAUSA_ENTRE_REQ = 0.2
MAX_REINTENTOS = 3

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

OKX_INTERVALOS = {"5m": "5m", "15m": "15m", "1h": "1H"}
MEXC_INTERVALOS = {"5m": "5m", "15m": "15m", "1h": "60m"}


def fetch_con_retry(url, max_intentos=MAX_REINTENTOS, headers=None):
    if headers is None:
        headers = {"User-Agent": "Mozilla/5.0"}
    for intento in range(max_intentos):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** intento)
                continue
            return None
        except Exception:
            return None
    return None


def fetch_okx(symbol, intervalo, limite=OKX_LIMIT_VELAS):
    bar = OKX_INTERVALOS.get(intervalo)
    if not bar:
        return []
    inst_id = f"{symbol}-USDT"
    url = (
        f"https://www.okx.com/api/v5/market/candles"
        f"?instId={inst_id}&bar={bar}&limit={limite}"
    )
    raw = fetch_con_retry(url)
    if not raw or raw.get("code") != "0":
        return []

    data = raw.get("data", [])
    data.reverse()

    velas = []
    for k in data:
        try:
            velas.append({
                "ts": int(k[0]),
                "o":  float(k[1]),
                "h":  float(k[2]),
                "l":  float(k[3]),
                "c":  float(k[4]),
                "v":  float(k[5]),
            })
        except (ValueError, IndexError):
            continue
    time.sleep(PAUSA_ENTRE_REQ)
    return velas


def fetch_mexc(symbol, intervalo, limite=OKX_LIMIT_VELAS):
    """MEXC API — formato compatible con Binance. No bloquea GitHub Actions."""
    bar = MEXC_INTERVALOS.get(intervalo)
    if not bar:
        return []
    pair = f"{symbol}USDT"
    url = (
        f"https://api.mexc.com/api/v3/klines"
        f"?symbol={pair}&interval={bar}&limit={limite}"
    )
    raw = fetch_con_retry(url)
    if not raw or not isinstance(raw, list):
        return []

    velas = []
    for k in raw:
        try:
            velas.append({
                "ts": int(k[0]),
                "o":  float(k[1]),
                "h":  float(k[2]),
                "l":  float(k[3]),
                "c":  float(k[4]),
                "v":  float(k[5]),
            })
        except (ValueError, IndexError):
            continue
    time.sleep(PAUSA_ENTRE_REQ)
    return velas


def fetch_klines(symbol, intervalo, limite=OKX_LIMIT_VELAS):
    velas = fetch_okx(symbol, intervalo, limite)
    if velas:
        return velas, "okx"
    velas = fetch_mexc(symbol, intervalo, limite)
    if velas:
        return velas, "mexc"
    return [], "none"


def calcular_rsi(prices, period=14):
    if not prices or len(prices) < period + 1:
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


def calcular_atr_percentile(velas, period=14, ventana=100):
    if len(velas) < period + ventana + 1:
        return None

    trs = []
    for i in range(1, len(velas)):
        high = velas[i]["h"]
        low  = velas[i]["l"]
        pc   = velas[i-1]["c"]
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


def direccion(velas):
    if len(velas) < 2:
        return "?"
    return "up" if velas[-1]["c"] > velas[-2]["c"] else "down"


def cache_path(symbol):
    return CACHE_DIR / f"{symbol}.json"


def cargar_cache(symbol):
    p = cache_path(symbol)
    if not p.exists():
        return {"symbol": symbol, "updated_at": None, "pulso": []}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"symbol": symbol, "updated_at": None, "pulso": []}
        return data
    except Exception:
        return {"symbol": symbol, "updated_at": None, "pulso": []}


def guardar_cache(symbol, data):
    with cache_path(symbol).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def procesar_moneda(symbol, ahora):
    velas_5m,  src_5m  = fetch_klines(symbol, "5m")
    velas_15m, src_15m = fetch_klines(symbol, "15m")
    velas_1h,  src_1h  = fetch_klines(symbol, "1h")

    if not velas_15m:
        return symbol, False, "sin velas 15m", None

    rsi15 = calcular_rsi([v["c"] for v in velas_15m])
    rsi1h = calcular_rsi([v["c"] for v in velas_1h]) if velas_1h else None
    atr_pct15 = calcular_atr_percentile(velas_15m, period=14, ventana=100)

    price = velas_15m[-1]["c"]
    vol_contratos = velas_15m[-1].get("v")

    rvol = 1.0
    if len(velas_15m) >= 21 and vol_contratos:
        vols_previos = [v["v"] for v in velas_15m[-21:-1] if v.get("v")]
        if vols_previos:
            vol_promedio = sum(vols_previos) / len(vols_previos)
            if vol_promedio > 0:
                rvol = vol_contratos / vol_promedio

    sample = {
        "ts":         int(ahora.timestamp()),
        "price":      round(price, 8),
        "rvol15":     round(rvol, 2),
        "atr_pct15":  atr_pct15,
        "rsi15":      round(rsi15, 2) if rsi15 is not None else None,
        "rsi1h":      round(rsi1h, 2) if rsi1h is not None else None,
        "dir15":      direccion(velas_15m),
        "dir1h":      direccion(velas_1h) if velas_1h else "?",
    }

    cache = cargar_cache(symbol)
    pulso = cache.get("pulso", [])

    if pulso and pulso[-1].get("ts") == sample["ts"]:
        return symbol, False, "sin cambios", src_15m

    pulso.append(sample)
    limite_ts = ahora.timestamp() - RETENCION_PULSO_H * 3600
    pulso = [p for p in pulso if p.get("ts", 0) >= limite_ts]

    cache["symbol"] = symbol
    cache["updated_at"] = ahora.isoformat()
    cache["pulso"] = pulso
    cache["source"] = src_15m

    cache["velas_5m"]  = velas_5m
    cache["velas_15m"] = velas_15m
    cache["velas_1h"]  = velas_1h

    guardar_cache(symbol, cache)

    return symbol, True, "ok", src_15m


def main():
    ahora = datetime.now(timezone.utc)
    inicio = time.time()

    print("\n" + "=" * 70, flush=True)
    print("📦 RECOLECTOR ALTS — repo mattlunaluna2/coins", flush=True)
    print(f"   {len(SYMBOLS)} monedas | 5m, 15m, 1h", flush=True)
    print(f"   Fuente: OKX (primario) + MEXC (fallback)", flush=True)
    print(f"   Workers: {MAX_WORKERS} | Pausa: {PAUSA_ENTRE_REQ}s", flush=True)
    print("=" * 70, flush=True)
    print(f"\nHora UTC: {ahora.isoformat()}", flush=True)

    guardados = 0
    fallidos = 0
    sin_cambios = 0
    de_mexc = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futuros = {
            executor.submit(procesar_moneda, sym, ahora): sym
            for sym in SYMBOLS
        }

        for futuro in as_completed(futuros):
            sym = futuros[futuro]
            try:
                symbol, ok, msg, src = futuro.result()
                if ok:
                    guardados += 1
                    marca = " [MEXC]" if src == "mexc" else ""
                    print(f"   ✅ {symbol}: {msg}{marca}", flush=True)
                    if src == "mexc":
                        de_mexc.append(symbol)
                elif msg == "sin cambios":
                    sin_cambios += 1
                else:
                    fallidos += 1
                    print(f"   ❌ {symbol}: {msg}", flush=True)
            except Exception as e:
                fallidos += 1
                print(f"   ❌ {sym}: excepción {str(e)[:60]}", flush=True)

    duracion = time.time() - inicio

    print("\n" + "=" * 70, flush=True)
    print(f"Guardados:     {guardados}", flush=True)
    print(f"Sin cambios:   {sin_cambios}", flush=True)
    print(f"Fallidos:      {fallidos}", flush=True)
    if de_mexc:
        print(f"Desde MEXC:    {len(de_mexc)} → {', '.join(de_mexc)}", flush=True)
    print(f"⏱️  Duración:   {duracion:.1f}s", flush=True)
    print(f"💾 Cache dir:  {CACHE_DIR}", flush=True)
    print("🏁 PROGRAMA TERMINADO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR GENERAL: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
