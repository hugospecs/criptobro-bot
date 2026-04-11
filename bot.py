"""
CRYPTO BOT PRO v17 — Escáner Dual IA/DePIN + Top 50
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAMBIOS v17 vs v16:
  Sistema de Escaneo Dual en Fase B:

  CAPA 1 — Radar Fijo IA/DePIN (WATCHLIST):
    · Siempre vigiladas: TAO · RENDER · FET · ONDO · AKT
    · Umbral normal: caída>8% en 24h  O  RSI<30
    · Etiqueta: 📡 RADAR FIJO

  CAPA 2 — Escáner Top 50 por Volumen:
    · Escanea las 50 monedas con más volumen del mercado
    · Umbral estricto: caída>15% EN 24h  Y  RSI<20
    · Solo alerta pánico real + sobreventa extrema
    · Etiqueta: 🔍 ESCÁNER TOP 50

  Optimizaciones API:
    · Precios Capa 1 (5 coins) en una sola llamada batch
    · Precios Capa 2 vienen del endpoint /coins/markets
      (precio incluido, sin llamada extra)
    · Pre-filtro por caída antes de hacer _fetch_history
    · Pausa adaptativa entre llamadas para evitar 429

HEREDADO DE v16:
  · Cartera por Coste Real (total_invertido_eur + cantidad_tokens)
  · Beneficio Neto = (tokens × precio) − invertido_eur
  · Break-even awareness en alertas de venta
  · Migración automática v15 → v16 → v17

MONITOR 3 FASES cada 4h:
  Fase A — Cartera (break-even real)
  Fase B — Radar Fijo WATCHLIST (parámetros normales)
  Fase C — Escáner Top 50 por volumen (pánico real)

Todo en EUROS (€)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, json, asyncio, logging, requests
from datetime import datetime
from functools import partial
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID             = os.environ.get("CHAT_ID", "")
PORTFOLIO_FILE      = "portfolio.json"
GECKO               = "https://api.coingecko.com/api/v3"
CURRENCY            = "eur"
CURRENCY_SYM        = "€"
MONITOR_HOURS       = 4
# Filtros Fase A — Cartera
SELL_CONF_MIN       = 60
DCA_DROP_PCT        = 10.0
TRAILING_MIN_PROFIT = 5.0
VOLATILITY_PCT      = 5.0
PROFIT_ALERT        = 10.0

# ── Capa 1: Radar Fijo IA/DePIN (umbrales normales) ──────────────────────────
RADAR_DROP_24H      = 8.0    # caída mínima en 24h para saltar alerta
RADAR_RSI_MAX       = 30     # RSI máximo (sobreventa confirmada)
RADAR_SCORE_MIN     = 3      # score técnico mínimo (señal moderada es suficiente)

# ── Capa 2: Escáner Top 50 por volumen (umbrales estrictos) ──────────────────
TOP50_DROP_24H      = 15.0   # caída mínima en 24h (pánico real)
TOP50_RSI_MAX       = 20     # RSI máximo (sobreventa extrema)
TOP50_SCORE_MIN     = 5      # score técnico mínimo (señal fuerte exigida)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Radar Fijo: WATCHLIST IA & DePIN 2026 (Capa 1) ───────────────────────────
WATCHLIST = {
    "bittensor":    "TAO",    # Bittensor
    "render-token": "RENDER", # Render Network
    "fetch-ai":     "FET",    # Fetch.ai / ASI Alliance
    "ondo-finance": "ONDO",   # Ondo Finance
    "akash-network":"AKT",    # Akash Network
}

# ── Catálogo ──────────────────────────────────────────────────────────────────
CATALOGUE = {}
TOP_IDS   = []

# ── Estado ────────────────────────────────────────────────────────────────────
state = {
    "portfolio":    {},
    "cancel":       False,
    "last_monitor": None,
    "prev_prices":  {},
}

def load_state():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                saved = json.load(f)
                raw_portfolio = saved.get("portfolio", {})
                # ── Migración automática v15 → v16 ──────────────────────────
                migrated = {}
                for cid, pos in raw_portfolio.items():
                    if "cantidad_tokens" in pos and "total_invertido_eur" in pos:
                        migrated[cid] = pos   # ya es formato v16
                    elif "units" in pos and "avg_buy" in pos:
                        # Convertir v15 → v16: invertido = avg_buy × units
                        migrated[cid] = {
                            "total_invertido_eur": round(pos["avg_buy"] * pos["units"], 4),
                            "cantidad_tokens":      pos["units"],
                        }
                        log.info("Migrado v15→v16: %s", cid)
                    else:
                        migrated[cid] = pos
                state["portfolio"]   = migrated
                state["prev_prices"] = saved.get("prev_prices", {})
            log.info("Cartera cargada: %d activos", len(state["portfolio"]))
        except Exception as e:
            log.warning("Error cargando cartera: %s", e)

def save_state():
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump({
                "portfolio":  state["portfolio"],
                "prev_prices": state["prev_prices"],
            }, f, indent=2)
    except Exception as e:
        log.error("Error guardando: %s", e)

# ── HTTP robusto (siempre en executor) ────────────────────────────────────────
def _get(url, params=None, retries=3):
    import time
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 429:
                wait = 12 * (i + 1)
                log.warning("Rate limit CoinGecko — esperando %ds", wait)
                time.sleep(wait)
                continue
            if r.status_code in (502, 503):
                time.sleep(8)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            time.sleep(5)
        except Exception as e:
            log.warning("_get intento %d: %s", i + 1, e)
            time.sleep(3)
    return None

async def run(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

# ── CoinGecko API ─────────────────────────────────────────────────────────────
def _fetch_prices(coin_ids):
    import time
    if not coin_ids:
        return {}
    result = {}
    ids = list(dict.fromkeys(coin_ids))
    for i in range(0, len(ids), 50):
        data = _get(f"{GECKO}/simple/price", {
            "ids":                 ",".join(ids[i:i+50]),
            "vs_currencies":       CURRENCY,
            "include_24hr_change": "true",
            "include_7d_change":   "true",
            "include_24hr_vol":    "true",
            "include_market_cap":  "true",
        })
        if data:
            result.update(data)
        if i + 50 < len(ids):
            time.sleep(1.5)
    return result

def _fetch_history(coin_id, days=30):
    data = _get(f"{GECKO}/coins/{coin_id}/market_chart", {
        "vs_currency": CURRENCY,
        "days":        days,
        "interval":    "daily",
    })
    return [p[1] for p in data.get("prices", [])] if data else []

def _fetch_ohlc(coin_id, days=2):
    return _get(f"{GECKO}/coins/{coin_id}/ohlc", {
        "vs_currency": CURRENCY,
        "days":        days,
    }) or []

def _fetch_catalogue():
    data = _get(f"{GECKO}/coins/list", {"include_platform": "false"})
    if data:
        for c in data:
            CATALOGUE[c["id"]] = {"symbol": c["symbol"].upper(), "name": c["name"]}
        log.info("Catálogo: %d monedas", len(CATALOGUE))

def _fetch_top_ids():
    import time
    ids = []
    for page in [1, 2, 3]:
        data = _get(f"{GECKO}/coins/markets", {
            "vs_currency": CURRENCY,
            "order":       "market_cap_desc",
            "per_page":    100,
            "page":        page,
            "sparkline":   "false",
        })
        if data:
            ids.extend(c["id"] for c in data)
        time.sleep(1.5)
    TOP_IDS.extend(ids)
    log.info("TOP_IDS: %d", len(TOP_IDS))

def _fetch_top_markets():
    """Top 100 monedas con precios incluidos (para /mercado y Market Hunter)."""
    import time
    result = []
    for page in [1, 2]:
        data = _get(f"{GECKO}/coins/markets", {
            "vs_currency":             CURRENCY,
            "order":                   "market_cap_desc",
            "per_page":                50,
            "page":                    page,
            "sparkline":               "false",
            "price_change_percentage": "24h,7d",
        })
        if data:
            result.extend(data)
        time.sleep(1.5)
    return result

def _fetch_top50_by_volume():
    """
    Top 50 monedas ordenadas por VOLUMEN de 24h (Capa 2 del escáner dual).
    Una sola llamada a la API — los precios vienen incluidos en la respuesta,
    por lo que NO necesitamos _fetch_prices para estas monedas.
    Pausa mínima integrada para respetar el rate-limit de CoinGecko.
    """
    import time
    data = _get(f"{GECKO}/coins/markets", {
        "vs_currency":             CURRENCY,
        "order":                   "volume_desc",       # ← ordenado por volumen
        "per_page":                50,
        "page":                    1,
        "sparkline":               "false",
        "price_change_percentage": "24h,7d",
    })
    time.sleep(1.2)   # pausa cortesía antes de la siguiente petición
    return data or []

def _fetch_global():
    data = _get(f"{GECKO}/global")
    if data:
        gd = data.get("data", {})
        return {
            "btc_dominance":  round(gd.get("market_cap_percentage", {}).get("btc", 0), 1),
            "total_cap_eur":  gd.get("total_market_cap", {}).get(CURRENCY, 0),
            "cap_change_24h": round(gd.get("market_cap_change_percentage_24h_usd", 0), 2),
        }
    return {"btc_dominance": 0, "total_cap_eur": 0, "cap_change_24h": 0}

# ── Análisis técnico de una moneda (síncrono, para executor) ──────────────────
def _do_full_analysis(coin_id, price_info):
    import time
    price = price_info.get(CURRENCY, 0)
    chg24 = price_info.get(f"{CURRENCY}_24h_change", 0) or 0
    chg7  = price_info.get(f"{CURRENCY}_7d_change",  0) or 0

    hist = _fetch_history(coin_id, 30)
    sc_d, reas_d, rsi_d, ema7_d, ema14_d, macd_d, bb_d, vs7_d, vs14_d = \
        score_prices(hist, price, chg24, chg7)
    conf_d = calc_conf(sc_d)
    target = round(price * (1 + max(0.05, abs(vs7_d)/100 + 0.03)), 8)
    sl     = round(price * (1 - max(0.04, abs(vs14_d)/100 + 0.02)), 8)

    time.sleep(0.5)

    ohlc = _fetch_ohlc(coin_id, 2)
    sc_4h, reas_4h, conf_4h, sig_4h = 0, [], 50, "⚪ MANTENER"
    if ohlc and len(ohlc) >= 8:
        closes = [c[4] for c in ohlc]
        sc_4h, reas_4h, *_ = score_prices(closes[:-1], closes[-1], chg24, 0)
        conf_4h = calc_conf(sc_4h)
        sig_4h  = signal_label(sc_4h)
        reas_4h = reas_4h[:2]

    return {
        "price":    price,  "chg24": chg24, "chg7": chg7,
        "signal":   signal_label(sc_d), "confidence": conf_d, "score": sc_d,
        "rsi":      rsi_d,  "ema7": ema7_d, "ema14": ema14_d,
        "bb_pos":   bb_d,   "target": target, "stop_loss": sl,
        "reasons":  reas_d,
        "signal_4h": sig_4h, "confidence_4h": conf_4h, "score_4h": sc_4h,
        "reasons_4h": reas_4h,
    }

def _do_hunter_analysis(coin_id, price_info):
    """
    Análisis simplificado para el Market Hunter.
    Solo necesitamos RSI y score — sin OHLC para no saturar la API.
    """
    import time
    price = price_info.get(CURRENCY, 0)
    chg24 = price_info.get(f"{CURRENCY}_24h_change", 0) or 0
    chg7  = price_info.get(f"{CURRENCY}_7d_change",  0) or 0

    hist = _fetch_history(coin_id, 30)
    if not hist:
        return None

    sc, reas, rsi, ema7, ema14, macd_v, bb_pos, vs7, vs14 = \
        score_prices(hist, price, chg24, chg7)
    conf = calc_conf(sc)
    target = round(price * (1 + max(0.05, abs(vs7)/100 + 0.03)), 8)
    sl     = round(price * (1 - max(0.04, abs(vs14)/100 + 0.02)), 8)

    time.sleep(0.8)   # pausa más larga para no saturar en el scan masivo

    return {
        "price": price, "chg24": chg24, "chg7": chg7,
        "score": sc, "rsi": rsi, "confidence": conf,
        "signal": signal_label(sc),
        "target": target, "stop_loss": sl,
        "reasons": reas,
    }

# ── Catálogo helpers ──────────────────────────────────────────────────────────
def sym(coin_id):
    return CATALOGUE.get(coin_id, {}).get("symbol", coin_id.upper())

def coin_name(coin_id):
    return CATALOGUE.get(coin_id, {}).get("name", coin_id)

def resolve_coin(text):
    u = text.strip().lower()
    if u in CATALOGUE:
        return u
    by_sym = [c for c, d in CATALOGUE.items() if d["symbol"].lower() == u]
    if by_sym:
        for t in TOP_IDS:
            if t in by_sym:
                return t
        return by_sym[0]
    by_name = [c for c, d in CATALOGUE.items() if d["name"].lower() == u]
    if by_name:
        return by_name[0]
    partial = [c for c, d in CATALOGUE.items()
               if u in d["symbol"].lower() or u in d["name"].lower()]
    if partial:
        for t in TOP_IDS:
            if t in partial:
                return t
        return partial[0]
    data = _get(f"{GECKO}/search", {"query": text})
    if data:
        hits = data.get("coins", [])
        if hits:
            cid = hits[0]["id"]
            if cid not in CATALOGUE:
                CATALOGUE[cid] = {"symbol": hits[0]["symbol"].upper(), "name": hits[0]["name"]}
            return cid
    return None

# ── Indicadores técnicos ──────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    d  = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    g  = [max(x, 0) for x in d[-period:]]
    l  = [max(-x, 0) for x in d[-period:]]
    ag, al = sum(g)/period, sum(l)/period
    return round(100 - 100/(1 + ag/al), 2) if al else 100.0

def calc_ema(prices, period):
    if not prices:
        return 0.0
    k, e = 2/(period+1), prices[0]
    for p in prices[1:]:
        e = p*k + e*(1-k)
    return e

def calc_macd(prices):
    if len(prices) < 26:
        return 0.0, 0.0
    m = calc_ema(prices, 12) - calc_ema(prices, 26)
    return round(m, 8), round(m*0.85, 8)

def calc_bb(prices, period=20):
    if len(prices) < period:
        p = prices[-1] if prices else 0
        return p, p, p
    w   = prices[-period:]
    avg = sum(w)/period
    std = (sum((x-avg)**2 for x in w)/period)**0.5
    return round(avg-2*std, 8), round(avg, 8), round(avg+2*std, 8)

def score_prices(prices, price, chg24, chg7):
    all_p  = prices + [price]
    rsi    = calc_rsi(all_p)
    ema7   = calc_ema(all_p, 7)
    ema14  = calc_ema(all_p, 14)
    macd_v, macd_s = calc_macd(all_p)
    bb_lo, _, bb_hi = calc_bb(all_p)
    vs7    = (price - ema7)  / ema7  * 100 if ema7  else 0
    vs14   = (price - ema14) / ema14 * 100 if ema14 else 0
    bb_pos = (price - bb_lo) / (bb_hi - bb_lo) * 100 if (bb_hi - bb_lo) else 50
    score, reasons = 0, []

    if rsi <= 25:    score += 3; reasons.append(f"RSI sobreventa extrema ({rsi})")
    elif rsi <= 35:  score += 2; reasons.append(f"RSI sobreventa ({rsi})")
    elif rsi <= 45:  score += 1; reasons.append(f"RSI bajo ({rsi})")
    elif rsi >= 75:  score -= 3; reasons.append(f"RSI sobrecompra extrema ({rsi})")
    elif rsi >= 65:  score -= 2; reasons.append(f"RSI elevado ({rsi})")
    elif rsi >= 55:  score -= 1; reasons.append(f"RSI alto ({rsi})")

    if macd_v > 0 and macd_v > macd_s:   score += 2; reasons.append("MACD alcista (cruce positivo)")
    elif macd_v > 0:                       score += 1; reasons.append("MACD positivo")
    elif macd_v < 0 and macd_v < macd_s:  score -= 2; reasons.append("MACD bajista (cruce negativo)")
    else:                                  score -= 1; reasons.append("MACD negativo")

    if vs7 < -5:    score += 2; reasons.append(f"Precio {abs(vs7):.1f}% bajo EMA7")
    elif vs7 < -2:  score += 1; reasons.append(f"Bajo EMA7 ({vs7:.1f}%)")
    elif vs7 > 8:   score -= 2; reasons.append(f"Extendido sobre EMA7 ({vs7:.1f}%)")
    elif vs7 > 4:   score -= 1; reasons.append(f"Alto sobre EMA7 ({vs7:.1f}%)")

    if ema7 > ema14: score += 1; reasons.append("EMA7 > EMA14 — tendencia alcista")
    else:            score -= 1; reasons.append("EMA7 < EMA14 — tendencia bajista")

    if bb_pos < 15:   score += 2; reasons.append("Cerca banda inferior Bollinger — rebote posible")
    elif bb_pos > 85: score -= 2; reasons.append("Cerca banda superior Bollinger — posible techo")

    if chg24 < -10:  score += 1; reasons.append(f"Caída 24h ({chg24:.1f}%)")
    elif chg24 > 15: score -= 1; reasons.append(f"Subida fuerte 24h ({chg24:.1f}%)")

    return score, reasons, rsi, ema7, ema14, macd_v, bb_pos, vs7, vs14

def signal_label(score):
    if score >= 5:  return "🟢 COMPRAR FUERTE"
    if score >= 3:  return "🟢 COMPRAR"
    if score >= 1:  return "🟡 POSIBLE COMPRA"
    if score <= -5: return "🔴 VENDER FUERTE"
    if score <= -3: return "🔴 VENDER"
    if score <= -1: return "🟠 POSIBLE VENTA"
    return "⚪ MANTENER"

def calc_conf(score):
    return min(93, 60 + abs(score) * 5)

def calc_trailing_stop(total_invertido_eur, cantidad_tokens, current_price):
    """
    Trailing stop basado en coste real.
    Activa si el beneficio neto supera TRAILING_MIN_PROFIT%.
    Stop = precio al que recuperas invertido + 1%.
    """
    if not cantidad_tokens or not total_invertido_eur:
        return None, 0.0
    valor_actual  = cantidad_tokens * current_price
    beneficio_pct = ((valor_actual - total_invertido_eur) / total_invertido_eur * 100)
    if beneficio_pct < TRAILING_MIN_PROFIT:
        return None, beneficio_pct
    # Precio mínimo para conservar break-even + 1%
    trailing_sl = round(total_invertido_eur * 1.01 / cantidad_tokens, 8)
    return trailing_sl, beneficio_pct

# ── Formateo ──────────────────────────────────────────────────────────────────
def fp(p):
    if not p:     return f"0{CURRENCY_SYM}"
    if p >= 1000: return f"{p:,.2f}{CURRENCY_SYM}"
    if p >= 1:    return f"{p:.4f}{CURRENCY_SYM}"
    if p >= 0.01: return f"{p:.6f}{CURRENCY_SYM}"
    return f"{p:.8f}{CURRENCY_SYM}"

def fv(v):
    if v >= 1e9:  return f"{v/1e9:.2f}B{CURRENCY_SYM}"
    if v >= 1e6:  return f"{v/1e6:.1f}M{CURRENCY_SYM}"
    if v >= 1e3:  return f"{v/1e3:.1f}K{CURRENCY_SYM}"
    return f"{v:.2f}{CURRENCY_SYM}"

def pc(v):
    return f"{'🟢 +' if v >= 0 else '🔴 '}{v:.2f}%"

# ── Mensaje de análisis completo ──────────────────────────────────────────────
def build_analysis_msg(coin_id, a, holding=None):
    price = a["price"]
    lines = [
        f"📊 *{sym(coin_id)} — {coin_name(coin_id)}*",
        f"💰 *{fp(price)}*   24h: {pc(a['chg24'])}   7d: {pc(a['chg7'])}",
        "",
        "┌─ AHORA (diario) ─────────────────",
        f"│ {a['signal']}",
        f"│ Confianza: {a['confidence']}%",
        f"│ RSI: `{a['rsi']}`   Bollinger: `{a['bb_pos']:.0f}%`",
        f"│ 🎯 Objetivo: `{fp(a['target'])}` (+{((a['target']/price-1)*100):.1f}%)" if price else "│",
        f"│ 🛑 Stop-loss: `{fp(a['stop_loss'])}` (-{((1-a['stop_loss']/price)*100):.1f}%)" if price else "│",
        "│",
        "│ Razones:",
    ]
    for r in a["reasons"][:4]:
        lines.append(f"│  · {r}")
    lines += [
        "│",
        "└─ PREDICCIÓN 4H ──────────────────",
        f"  {a['signal_4h']}   Conf: {a['confidence_4h']}%",
    ]
    for r in a["reasons_4h"]:
        lines.append(f"  · {r}")
    if holding:
        tokens  = holding["cantidad_tokens"]
        inv_eur = holding["total_invertido_eur"]
        cur_val = price * tokens
        ben_net = cur_val - inv_eur
        ben_pct = (ben_net / inv_eur * 100) if inv_eur else 0
        trailing_sl, _ = calc_trailing_stop(inv_eur, tokens, price)
        # Precio de break-even exacto
        breakeven_price = (inv_eur / tokens) if tokens else 0
        lines += [
            "",
            "┌─ TU POSICIÓN (Coste Real) ────────",
            f"│ {tokens} tokens · Invertido real: {fp(inv_eur)}",
            f"│ Break-even: {fp(breakeven_price)} por token",
            f"│ Valor actual: {fp(cur_val)}",
            f"└ {'💰' if ben_net>=0 else '📉'} Beneficio Neto: {fp(ben_net)} ({ben_pct:+.2f}%)",
        ]
        if trailing_sl:
            lines.append(f"  🔒 Trailing stop: {fp(trailing_sl)} (break-even +1%)")
    lines.append(f"\n_🕐 {datetime.now().strftime('%d/%m %H:%M')}_")
    return "\n".join(lines)

def build_hunter_alert(coin_id, a, now_str, layer="📡 RADAR FIJO"):
    """
    Mensaje de alerta para oportunidades de entrada.
    layer: etiqueta de la capa que generó la señal
      - "📡 RADAR FIJO IA/DePIN"
      - "🔍 ESCÁNER TOP 50"
    """
    price = a["price"]
    lines = [
        f"🎯 *OPORTUNIDAD DE ENTRADA DETECTADA*",
        f"*Fuente:* `{layer}`",
        f"",
        f"*{sym(coin_id)} — {coin_name(coin_id)}*",
        f"💰 Precio: *{fp(price)}*",
        f"📉 Caída 24h: {pc(a['chg24'])}",
        f"📅 Cambio 7d: {pc(a['chg7'])}",
        f"",
        f"*Indicadores técnicos:*",
        f"  RSI: `{a['rsi']}` — {'Sobreventa extrema ⚠️' if a['rsi'] < 20 else 'Sobreventa confirmada'}",
        f"  Score: `{a['score']}` — Señal {'fuerte' if a['score'] >= 5 else 'moderada'}",
        f"  Confianza: `{a['confidence']}%`",
        f"  Señal: {a['signal']}",
        f"",
        f"*Razones:*",
    ]
    for r in a["reasons"][:5]:
        lines.append(f"  · {r}")
    lines += [
        f"",
        f"*Niveles sugeridos:*",
        f"  🎯 Objetivo: `{fp(a['target'])}` (+{((a['target']/price-1)*100):.1f}%)" if price else "",
        f"  🛑 Stop-loss: `{fp(a['stop_loss'])}` (-{((1-a['stop_loss']/price)*100):.1f}%)" if price else "",
        f"",
        f"_⚠️ Solo informativo. Haz tu propio análisis antes de invertir._",
        f"_{layer} — {now_str}_",
    ]
    return "\n".join(l for l in lines if l is not None)

# ══════════════════════════════════════════════════════════════════════════════
# MONITOR DUAL CADA 4H
# ══════════════════════════════════════════════════════════════════════════════
async def monitor_loop(app):
    await asyncio.sleep(30)
    log.info("Monitor dual %dh iniciado", MONITOR_HOURS)
    while True:
        try:
            await do_monitor(app.bot)
        except Exception as e:
            log.error("Error crítico en monitor: %s", e)
        log.info("Próximo monitor en %dh", MONITOR_HOURS)
        await asyncio.sleep(MONITOR_HOURS * 3600)

async def do_monitor(bot):
    now_str = datetime.now().strftime("%d/%m %H:%M")
    state["last_monitor"] = now_str
    log.info("Monitor dual iniciando — %s", now_str)

    portfolio   = state["portfolio"]
    alerts_a    = []   # alertas de Fase A (cartera)
    pnl_list    = []   # para el reporte final
    gangas      = []   # oportunidades encontradas en Fase B

    # ══════════════════════════════════════════════════════════════════════════
    # FASE A — Gestión de Cartera
    # ══════════════════════════════════════════════════════════════════════════
    if portfolio:
        log.info("Fase A: analizando %d activos de cartera", len(portfolio))

        prices_data = await run(_fetch_prices, list(portfolio.keys()))
        global_data = await run(_fetch_global)

        for coin_id, pos in portfolio.items():
            try:
                info = prices_data.get(coin_id)
                if not info:
                    log.warning("Fase A: sin precio para %s", coin_id)
                    continue

                price = info.get(CURRENCY, 0)
                chg24 = info.get(f"{CURRENCY}_24h_change", 0) or 0
                if not price:
                    continue

                # ── Coste Real v17 ────────────────────────────────────────────
                tokens   = pos["cantidad_tokens"]
                inv_eur  = pos["total_invertido_eur"]
                cur_val  = price * tokens
                ben_net  = cur_val - inv_eur                    # beneficio neto real
                ben_pct  = (ben_net / inv_eur * 100) if inv_eur else 0
                # Precio de break-even exacto
                be_price = (inv_eur / tokens) if tokens else 0

                pnl_list.append((coin_id, ben_pct, price, cur_val))

                # ── Detector de volatilidad ───────────────────────────────────
                prev_price = state["prev_prices"].get(coin_id)
                if prev_price and prev_price > 0:
                    vol_pct = abs((price - prev_price) / prev_price * 100)
                    if vol_pct >= VOLATILITY_PCT:
                        direction = "subida" if price > prev_price else "caída"
                        alerts_a.append(
                            f"⚡ *ALTA VOLATILIDAD — {sym(coin_id)}*\n"
                            f"_{direction.capitalize()} del {vol_pct:.1f}% en las últimas {MONITOR_HOURS}h_\n\n"
                            f"  Precio: *{fp(price)}*   24h: {pc(chg24)}\n"
                            f"  Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)\n"
                            f"_Monitor {now_str}_"
                        )

                state["prev_prices"][coin_id] = price

                # ── Análisis técnico completo ─────────────────────────────────
                a = await run(_do_full_analysis, coin_id, info)
                trailing_sl, _ = calc_trailing_stop(inv_eur, tokens, price)
                drop_from_be   = ((be_price - price) / be_price * 100) if be_price else 0

                # 1. Oportunidad DCA
                if drop_from_be >= DCA_DROP_PCT and a["rsi"] < 35:
                    alerts_a.append(
                        f"💡 *OPORTUNIDAD DCA — {sym(coin_id)}*\n"
                        f"_Caída del {drop_from_be:.1f}% sobre tu break-even + RSI en sobreventa_\n\n"
                        f"  Precio actual: *{fp(price)}*\n"
                        f"  Tu break-even: {fp(be_price)}\n"
                        f"  RSI: `{a['rsi']}` — Zona de acumulación\n"
                        f"  Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)\n\n"
                        f"  Promediar bajaría tu coste medio y mejoraría el break-even.\n"
                        f"  ⚠️ Solo si tienes convicción en el activo.\n"
                        f"_Monitor {now_str}_"
                    )
                    continue

                # 2. Trailing stop amenazado
                if trailing_sl and price <= trailing_sl * 1.01:
                    alerts_a.append(
                        f"🔒 *TRAILING STOP — {sym(coin_id)}*\n"
                        f"_El precio se acerca al stop dinámico de break-even_\n\n"
                        f"  Precio actual: *{fp(price)}*\n"
                        f"  Trailing stop: {fp(trailing_sl)}\n"
                        f"  Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)\n"
                        f"  Considera vender para proteger tus ganancias.\n"
                        f"_Monitor {now_str}_"
                    )
                    continue

                # ── Nota break-even para alertas de venta ────────────────────
                def _breakeven_note():
                    """Aviso si los indicadores piden vender pero aún no cubres costes."""
                    if ben_net < 0:
                        return (
                            f"\n⚠️ *Nota:* Los indicadores sugieren salida, pero tu posición "
                            f"actual es de *{fp(ben_net)}* "
                            f"(aún no has alcanzado el punto de equilibrio)"
                        )
                    return ""

                # 3. Señal técnica fuerte de venta
                if a["score"] <= -3 and a["confidence"] >= SELL_CONF_MIN:
                    alerts_a.append(
                        f"🚨 *ALERTA VENTA — {sym(coin_id)}*\n"
                        f"_Señales técnicas apuntan a presión bajista_\n\n"
                        f"  Precio: *{fp(price)}*   24h: {pc(chg24)}\n"
                        f"  Señal diaria: {a['signal']} ({a['confidence']}%)\n"
                        f"  Señal 4h: {a['signal_4h']} ({a['confidence_4h']}%)\n"
                        f"  RSI: `{a['rsi']}`\n\n"
                        f"  Razones:\n"
                        + "\n".join(f"  · {r}" for r in a["reasons"][:3])
                        + f"\n\n  💶 Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)"
                        + _breakeven_note()
                        + f"\n_Monitor {now_str}_"
                    )

                # 4. Bajista en diario y 4h simultáneo
                elif a["score"] <= -2 and a["score_4h"] <= -2 and a["confidence"] >= SELL_CONF_MIN:
                    alerts_a.append(
                        f"🚨 *SEÑAL BAJISTA CONFIRMADA — {sym(coin_id)}*\n"
                        f"_Tendencia bajista en timeframe diario y 4h_\n\n"
                        f"  Precio: *{fp(price)}*   24h: {pc(chg24)}\n"
                        f"  Diario: {a['signal']} | 4H: {a['signal_4h']}\n"
                        f"  💶 Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)"
                        + _breakeven_note()
                        + f"\n_Monitor {now_str}_"
                    )

                # 5. RSI sobrecompra + beneficio significativo
                elif a["rsi"] >= 75 and ben_pct >= 10:
                    alerts_a.append(
                        f"⚠️ *TOMA DE GANANCIAS — {sym(coin_id)}*\n"
                        f"_RSI en sobrecompra extrema con beneficio acumulado_\n\n"
                        f"  Precio: *{fp(price)}*\n"
                        f"  RSI: `{a['rsi']}` — Sobrecompra\n"
                        f"  💶 Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)\n"
                        f"  Considera tomar parcialmente los beneficios.\n"
                        f"_Monitor {now_str}_"
                    )

                # 6. Objetivo de beneficio alcanzado
                elif ben_pct >= PROFIT_ALERT and a["score"] <= 0:
                    alerts_a.append(
                        f"💰 *OBJETIVO BENEFICIO — {sym(coin_id)}*\n"
                        f"_Has superado +{PROFIT_ALERT:.0f}% con señal neutral o bajista_\n\n"
                        f"  Precio: *{fp(price)}*\n"
                        f"  💶 Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)\n"
                        f"  Señal: {a['signal']}\n"
                        f"  Considera asegurar parte de las ganancias.\n"
                        f"_Monitor {now_str}_"
                    )

                # 7. Stop-loss clásico cercano
                elif price <= a["stop_loss"] * 1.02:
                    alerts_a.append(
                        f"🛑 *STOP-LOSS CERCANO — {sym(coin_id)}*\n"
                        f"_El precio se acerca al nivel de stop-loss sugerido_\n\n"
                        f"  Precio actual: *{fp(price)}*\n"
                        f"  Stop-loss sugerido: {fp(a['stop_loss'])}\n"
                        f"  💶 Beneficio Neto Real: {fp(ben_net)} ({ben_pct:+.2f}%)"
                        + _breakeven_note()
                        + f"\n_Monitor {now_str}_"
                    )

                await asyncio.sleep(0.8)

            except Exception as e:
                log.error("Fase A error en %s: %s", coin_id, e)
                continue

        # Guardar precios del ciclo
        save_state()

    else:
        log.info("Fase A: cartera vacía, saltando")
        global_data = await run(_fetch_global)

    # ══════════════════════════════════════════════════════════════════════════
    # FASE B — CAPA 1: Radar Fijo IA/DePIN (WATCHLIST, umbrales normales)
    # Umbral: caída>8% en 24h  OR  RSI<30
    # ══════════════════════════════════════════════════════════════════════════
    log.info("Fase B — Radar Fijo: escaneando %d monedas WATCHLIST", len(WATCHLIST))

    # Registrar símbolos en catálogo si aún no están
    for wid, wsym_str in WATCHLIST.items():
        if wid not in CATALOGUE:
            CATALOGUE[wid] = {"symbol": wsym_str, "name": wsym_str}

    try:
        wl_prices = await run(_fetch_prices, list(WATCHLIST.keys()))
    except Exception as e:
        log.error("Fase B: error obteniendo precios watchlist: %s", e)
        wl_prices = {}

    for cid in WATCHLIST:
        if cid not in wl_prices or cid in portfolio:
            continue   # sin precio o ya está en cartera (cubierta por Fase A)

        price_info = wl_prices[cid]
        chg24 = price_info.get(f"{CURRENCY}_24h_change", 0) or 0
        price = price_info.get(CURRENCY, 0)
        if not price:
            continue

        # Pre-filtro barato antes de hacer fetch_history (costoso)
        pre_ok = (chg24 <= -RADAR_DROP_24H)   # si no cae lo suficiente, aún puede pasar por RSI
        try:
            a = await run(_do_hunter_analysis, cid, price_info)
            if a is None:
                continue

            log.info("Fase B Radar: %s — 24h: %.1f%%, RSI: %s, score: %s",
                     sym(cid), chg24, a["rsi"], a["score"])

            # Umbral Capa 1: caída>8%  OR  RSI<30  (cualquiera basta)
            if ((chg24 <= -RADAR_DROP_24H or a["rsi"] < RADAR_RSI_MAX)
                    and a["score"] >= RADAR_SCORE_MIN):
                log.info("Fase B Radar: SEÑAL — %s", sym(cid))
                gangas.append((cid, a, "📡 RADAR FIJO IA/DePIN"))

            await asyncio.sleep(1.2)

        except Exception as e:
            log.warning("Fase B Radar: error en %s: %s", cid, e)

    # ══════════════════════════════════════════════════════════════════════════
    # FASE C — CAPA 2: Escáner Top 50 por Volumen (umbrales estrictos)
    # Umbral: caída>15% EN 24h  AND  RSI<20  (pánico real + sobreventa extrema)
    # Los precios vienen del endpoint /coins/markets — sin llamada extra.
    # ══════════════════════════════════════════════════════════════════════════
    log.info("Fase C — Escáner Top 50 por volumen iniciando")

    try:
        top50_markets = await run(_fetch_top50_by_volume)
    except Exception as e:
        log.error("Fase C: error obteniendo Top 50: %s", e)
        top50_markets = []

    if top50_markets:
        # Pre-filtro: solo monedas con caída ≥ umbral estricto y fuera de cartera/watchlist
        c2_candidates = []
        watchlist_ids_set = set(WATCHLIST.keys())
        for c in top50_markets:
            cid   = c.get("id", "")
            chg24 = c.get("price_change_percentage_24h", 0) or 0
            price = c.get("current_price", 0) or 0
            if not cid or not price:
                continue
            # Actualizar catálogo
            if cid not in CATALOGUE:
                CATALOGUE[cid] = {
                    "symbol": c.get("symbol", "").upper(),
                    "name":   c.get("name", ""),
                }
            # Excluir: en cartera, en watchlist (ya cubierta por Capa 1), caída insuficiente
            if cid in portfolio or cid in watchlist_ids_set:
                continue
            if chg24 <= -TOP50_DROP_24H:   # pre-filtro duro — solo entran los que caen >15%
                c2_candidates.append(c)

        log.info("Fase C: %d candidatos pre-filtrados del Top 50", len(c2_candidates))

        for c in c2_candidates:
            cid   = c.get("id", "")
            chg24 = c.get("price_change_percentage_24h", 0) or 0
            chg7  = c.get("price_change_percentage_7d_in_currency", 0) or 0
            price = c.get("current_price", 0) or 0

            # Construir price_info compatible con _do_hunter_analysis
            # (los datos de precio ya vienen del endpoint /markets, sin llamada extra)
            price_info_c2 = {
                CURRENCY:                 price,
                f"{CURRENCY}_24h_change": chg24,
                f"{CURRENCY}_7d_change":  chg7,
                f"{CURRENCY}_24h_vol":    c.get("total_volume", 0) or 0,
                f"{CURRENCY}_market_cap": c.get("market_cap", 0) or 0,
            }
            try:
                a = await run(_do_hunter_analysis, cid, price_info_c2)
                if a is None:
                    continue

                log.info("Fase C Top50: %s — 24h: %.1f%%, RSI: %s, score: %s",
                         sym(cid), chg24, a["rsi"], a["score"])

                # Umbral Capa 2: caída>15% AND RSI<20 AND score>=5 (todos deben cumplirse)
                if (a["rsi"] < TOP50_RSI_MAX and a["score"] >= TOP50_SCORE_MIN):
                    log.info("Fase C Top50: PÁNICO REAL — %s (RSI=%s, score=%s)",
                             sym(cid), a["rsi"], a["score"])
                    gangas.append((cid, a, "🔍 ESCÁNER TOP 50"))

                await asyncio.sleep(1.5)   # pausa más larga para el scan masivo

            except Exception as e:
                log.warning("Fase C Top50: error en %s: %s", cid, e)
    else:
        log.warning("Fase C: no se pudieron obtener datos del Top 50")

    # ── Enviar alertas Fase A ─────────────────────────────────────────────────
    if not CHAT_ID:
        log.warning("CHAT_ID no configurado — no se pueden enviar mensajes")
        return

    for alert_text in alerts_a:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=alert_text, parse_mode="Markdown")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error("Error enviando alerta Fase A: %s", e)

    # ── Enviar oportunidades Fases B y C (gangas) ─────────────────────────────
    for cid, a, layer in gangas:
        try:
            await bot.send_message(
                chat_id    = CHAT_ID,
                text       = build_hunter_alert(cid, a, now_str, layer=layer),
                parse_mode = "Markdown",
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error("Error enviando alerta %s %s: %s", layer, cid, e)

    # ── Reporte final ─────────────────────────────────────────────────────────
    n_radar  = sum(1 for _, _, layer in gangas if "RADAR" in layer)
    n_top50  = sum(1 for _, _, layer in gangas if "TOP 50" in layer)
    await _send_monitor_report(
        bot, pnl_list, global_data,
        n_alerts_a = len(alerts_a),
        n_radar    = n_radar,
        n_top50    = n_top50,
        now_str    = now_str,
    )

async def _send_monitor_report(bot, pnl_list, global_data,
                                n_alerts_a, n_radar, n_top50, now_str):
    """Reporte consolidado al final del ciclo: cartera + mercado global + gangas."""
    portfolio = state["portfolio"]

    # Totales de cartera (coste real v17)
    total_inv = total_cur = 0
    for cid, ben_pct, price, cur_val in pnl_list:
        pos       = portfolio.get(cid, {})
        total_inv += pos.get("total_invertido_eur", 0)
        total_cur += cur_val

    total_pnl     = total_cur - total_inv
    total_pnl_pct = (total_pnl / total_inv * 100) if total_inv else 0

    # Top ganador y perdedor
    pnl_sorted = sorted(pnl_list, key=lambda x: x[1], reverse=True)
    top_winner = pnl_sorted[0]  if pnl_sorted           else None
    top_loser  = pnl_sorted[-1] if len(pnl_sorted) > 1  else None

    # Datos globales
    btc_dom   = global_data.get("btc_dominance", 0)
    total_cap = global_data.get("total_cap_eur", 0)
    cap_chg   = global_data.get("cap_change_24h", 0)

    if btc_dom > 60:
        market_mood = "🔵 Dominancia BTC alta — mercado defensivo/Bitcoin"
    elif btc_dom > 50:
        market_mood = "🟡 Dominancia BTC moderada — equilibrio BTC/altcoins"
    else:
        market_mood = "🟢 Dominancia BTC baja — posible temporada de altcoins"

    lines = [f"📋 *INFORME MONITOR — {now_str}*", ""]

    # ── Sección cartera ───────────────────────────────────────────────────────
    if pnl_list:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"{'🟢' if total_pnl >= 0 else '🔴'} *Balance de Cartera*",
            f"  Invertido: {fp(total_inv)}",
            f"  Valor actual: {fp(total_cur)}",
            f"  P&L: {fp(total_pnl)} ({total_pnl_pct:+.2f}%)",
            "",
        ]
        if top_winner:
            pos_w   = portfolio.get(top_winner[0], {})
            inv_w   = pos_w.get("total_invertido_eur", 0)
            cur_w   = top_winner[3]
            pnl_w   = cur_w - inv_w
            lines += [
                f"🏆 *Top Ganador:* {sym(top_winner[0])} ({coin_name(top_winner[0])})",
                f"  {fp(top_winner[2])} · Benef. Neto: {fp(pnl_w)} ({top_winner[1]:+.2f}%)",
            ]
        if top_loser and top_loser[0] != (top_winner[0] if top_winner else ""):
            pos_l   = portfolio.get(top_loser[0], {})
            inv_l   = pos_l.get("total_invertido_eur", 0)
            cur_l   = top_loser[3]
            pnl_l   = cur_l - inv_l
            lines += [
                f"📉 *Top Perdedor:* {sym(top_loser[0])} ({coin_name(top_loser[0])})",
                f"  {fp(top_loser[2])} · Benef. Neto: {fp(pnl_l)} ({top_loser[1]:+.2f}%)",
            ]
        lines.append("")

    # ── Sección mercado global ────────────────────────────────────────────────
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🌍 *Estado Global del Mercado*",
        f"  Dominancia BTC: *{btc_dom}%*",
        f"  Cap. total: {fv(total_cap)}",
        f"  Variación 24h: {pc(cap_chg)}",
        f"  {market_mood}",
        "",
    ]

    # ── Sección resumen de alertas ────────────────────────────────────────────
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

    n_gangas = n_radar + n_top50
    if n_alerts_a == 0 and n_gangas == 0:
        lines += [
            "✅ *Sin señales de acción detectadas*",
            "Cartera estable. No hay movimientos urgentes recomendados.",
        ]
    else:
        if n_alerts_a > 0:
            lines.append(
                f"⚠️ *{n_alerts_a} alerta{'s' if n_alerts_a>1 else ''} de cartera* enviada{'s' if n_alerts_a>1 else ''}."
            )
        if n_radar > 0:
            lines.append(
                f"📡 *{n_radar} señal{'es' if n_radar>1 else ''} del Radar Fijo IA/DePIN.* "
                f"Revisa las alertas de OPORTUNIDAD — TAO/RENDER/FET/ONDO/AKT."
            )
        if n_top50 > 0:
            lines.append(
                f"🔍 *{n_top50} señal{'es' if n_top50>1 else ''} del Escáner Top 50.* "
                f"⚠️ Pánico real detectado — sobreventa extrema (RSI<20)."
            )

    lines.append(f"\n_Próximo análisis en {MONITOR_HOURS}h_")

    try:
        await bot.send_message(
            chat_id    = CHAT_ID,
            text       = "\n".join(lines),
            parse_mode = "Markdown",
        )
    except Exception as e:
        log.error("Error enviando reporte final: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════════════════════════════════
HELP = (
    "👋 *Crypto Bot Pro v17 — Escáner Dual*\n\n"
    "📦 *CARTERA \\(Coste Real\\)*\n"
    "  /compra TAO 10 0\\.045 — Euros invertidos \\+ tokens recibidos\n"
    "  /compra TAO 10 — Bot calcula tokens al precio actual\n"
    "  /venta TAO 0\\.02 — Registrar venta \\(P\\&L real\\)\n"
    "  /cartera — Cartera con break\\-even y coste real\n"
    "  /precio BTC — Precio actual en €\n"
    "  /buscar PEPE — Buscar cualquier cripto\n\n"
    "📊 *ANÁLISIS*\n"
    "  /analizar TAO — Análisis diario \\+ 4h\n"
    "  /analizar — Analizar toda tu cartera\n\n"
    "🔍 *MERCADO*\n"
    "  /mercado — Top 100 oportunidades en €\n\n"
    "🔔 *MONITOR 3 FASES cada 4h*\n"
    "  /monitor — Estado del sistema\n"
    "  /forzarmonitor — Ejecutar ahora\n\n"
    "📡 *Radar Fijo:* TAO · RENDER · FET · ONDO · AKT\n"
    "  ↳ Alerta si caída >8% o RSI<30\n"
    "🔍 *Escáner Top 50 por volumen:*\n"
    "  ↳ Alerta solo si caída >15% Y RSI<20 \\(pánico real\\)\n\n"
    "⚙️ /cancelar — Parar comando en curso\n\n"
    "_Precios en euros · Fase A: Cartera_\n"
    "_Fase B: Radar IA/DePIN · Fase C: Top 50_"
)

async def cmd_start(u, c): await u.message.reply_text(HELP, parse_mode="MarkdownV2")
async def cmd_ayuda(u, c): await u.message.reply_text(HELP, parse_mode="MarkdownV2")

async def cmd_cartera(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["portfolio"]
    if not p:
        await update.message.reply_text(
            "Tu cartera está vacía.\n\n"
            "Usa `/compra TAO 10 0.045` para añadir\n"
            "  (euros invertidos · tokens recibidos)\n"
            "o /mercado para buscar oportunidades.",
            parse_mode="Markdown")
        return
    msg  = await update.message.reply_text("🔄 Obteniendo precios en €...")
    data = await run(_fetch_prices, list(p.keys()))
    ti = tc = 0   # total invertido, total en cartera
    lines = [f"💼 *Tu Cartera v17 — Coste Real* ({CURRENCY_SYM})\n"]
    for cid, pos in p.items():
        tokens   = pos["cantidad_tokens"]
        inv_eur  = pos["total_invertido_eur"]
        info     = data.get(cid, {})
        price    = info.get(CURRENCY, 0)
        chg24    = info.get(f"{CURRENCY}_24h_change", 0) or 0
        cur_val  = price * tokens
        ben_net  = cur_val - inv_eur
        ben_pct  = (ben_net / inv_eur * 100) if inv_eur else 0
        be_price = (inv_eur / tokens) if tokens else 0
        ti += inv_eur
        tc += cur_val
        trailing_sl, _ = calc_trailing_stop(inv_eur, tokens, price)
        trailing_line  = f"  🔒 Trailing stop: {fp(trailing_sl)}\n" if trailing_sl else ""
        lines.append(
            f"{'🟢' if ben_net>=0 else '🔴'} *{sym(cid)}*\n"
            f"  Tokens: `{tokens}` · Invertido real: `{fp(inv_eur)}`\n"
            f"  Break-even: `{fp(be_price)}` · Ahora: `{fp(price)}`\n"
            f"  24h: {pc(chg24)}\n"
            f"  Valor actual: {fp(cur_val)} · Benef. Neto: {fp(ben_net)} ({ben_pct:+.2f}%)\n"
            + trailing_line
        )
    tp   = tc - ti
    tpct = (tp / ti * 100) if ti else 0
    lines += [
        "━━━━━━━━━━━━━━━━",
        f"{'🟢' if tp>=0 else '🔴'} *RESUMEN TOTAL*",
        f"  💶 Dinero real invertido: `{fp(ti)}`",
        f"  📊 Valor actual en mercado: `{fp(tc)}`",
        f"  {'💰' if tp>=0 else '📉'} Beneficio Neto Real: `{fp(tp)}` ({tpct:+.2f}%)",
        f"\n_🕐 {datetime.now().strftime('%H:%M:%S')}_",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

async def cmd_compra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Uso: /compra TAO 10 0.045
         /compra TAO 10       ← el bot calcula los tokens al precio actual

    Parámetros:
      arg1 = símbolo / nombre de la moneda
      arg2 = euros_invertidos  (dinero real que salió de tu cuenta)
      arg3 = cantidad_tokens   (tokens recibidos tras comisiones, opcional)
    """
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            f"*Uso:*\n"
            f"`/compra TAO 10 0.045`\n"
            f"  ↳ 10€ invertidos · 0.045 TAO recibidos\n\n"
            f"`/compra TAO 10`\n"
            f"  ↳ 10€ invertidos · tokens calculados al precio actual\n\n"
            f"_El segundo parámetro es el dinero REAL que salió de tu cuenta (incluye comisiones)._\n"
            f"_El tercero son los tokens que REALMENTE recibiste tras comisiones._",
            parse_mode="Markdown")
        return

    coin_id = resolve_coin(args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{args[0]}'. Prueba /buscar {args[0]}")
        return

    try:
        euros_invertidos = float(args[1].replace(",", "."))
        if euros_invertidos <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Los euros invertidos deben ser un número positivo.")
        return

    # Obtener precio actual siempre (para mostrar contexto aunque se den tokens manuales)
    msg_tmp  = await update.message.reply_text("🔄 Obteniendo precio de mercado en €...")
    info     = await run(_fetch_prices, [coin_id])
    price_now = info[coin_id].get(CURRENCY, 0) if (info and coin_id in info) else 0

    if len(args) >= 3:
        try:
            tokens_recibidos = float(args[2].replace(",", "."))
            if tokens_recibidos <= 0: raise ValueError
        except ValueError:
            await msg_tmp.edit_text("❌ La cantidad de tokens debe ser un número positivo.")
            return
        await msg_tmp.delete()
    else:
        # Calcular tokens al precio de mercado actual (sin comisiones implícitas)
        if not price_now:
            await msg_tmp.edit_text(
                f"⚠️ No pude obtener el precio de *{sym(coin_id)}*.\n"
                f"Indícalo manualmente: `/compra {sym(coin_id)} {euros_invertidos} <tokens_recibidos>`",
                parse_mode="Markdown")
            return
        tokens_recibidos = round(euros_invertidos / price_now, 8)
        await msg_tmp.delete()

    p = state["portfolio"]
    if coin_id in p:
        # DCA: sumar inversión y tokens
        p[coin_id]["total_invertido_eur"] = round(p[coin_id]["total_invertido_eur"] + euros_invertidos, 4)
        p[coin_id]["cantidad_tokens"]     = round(p[coin_id]["cantidad_tokens"] + tokens_recibidos, 8)
        extra = "📈 Posición ampliada (DCA)."
    else:
        p[coin_id] = {
            "total_invertido_eur": round(euros_invertidos, 4),
            "cantidad_tokens":     round(tokens_recibidos, 8),
        }
        extra = "🆕 Nueva posición creada."

    save_state()
    be_price = round(p[coin_id]["total_invertido_eur"] / p[coin_id]["cantidad_tokens"], 8)
    await update.message.reply_text(
        f"✅ *Compra registrada — v17 Coste Real*\n\n"
        f"  *{sym(coin_id)}* — {tokens_recibidos} tokens\n"
        f"  💶 Dinero real invertido: `{fp(euros_invertidos)}`\n"
        f"  📊 Precio de mercado ahora: `{fp(price_now)}`\n\n"
        f"  *Posición total:*\n"
        f"  Tokens acumulados: `{p[coin_id]['cantidad_tokens']}`\n"
        f"  Total invertido: `{fp(p[coin_id]['total_invertido_eur'])}`\n"
        f"  Break-even exacto: `{fp(be_price)}` por token\n\n"
        f"_{extra}_",
        parse_mode="Markdown")

async def cmd_venta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: `/venta TAO 0.02` — número de tokens a vender", parse_mode="Markdown")
        return
    coin_id = resolve_coin(args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{args[0]}'.")
        return
    try:
        tokens_venta = float(args[1].replace(",", "."))
        if tokens_venta <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ La cantidad debe ser un número positivo.")
        return
    p = state["portfolio"]
    if coin_id not in p:
        await update.message.reply_text(
            f"⚠️ No tienes *{sym(coin_id)}* en cartera.", parse_mode="Markdown")
        return

    pos          = p[coin_id]
    tokens_total = pos["cantidad_tokens"]
    inv_total    = pos["total_invertido_eur"]

    info  = await run(_fetch_prices, [coin_id])
    price = info.get(coin_id, {}).get(CURRENCY, 0) if info else 0

    # Proporción vendida para calcular el coste real vendido
    ratio          = min(tokens_venta / tokens_total, 1.0)
    inv_vendido    = inv_total * ratio          # euros reales que corresponden a esta venta
    valor_venta    = price * tokens_venta       # euros que recibes ahora
    ben_neto_venta = valor_venta - inv_vendido  # beneficio neto de esta venta
    ben_pct_venta  = (ben_neto_venta / inv_vendido * 100) if inv_vendido else 0

    if tokens_venta >= tokens_total:
        del p[coin_id]
        remaining = 0
        remaining_inv = 0.0
    else:
        p[coin_id]["cantidad_tokens"]     = round(tokens_total - tokens_venta, 8)
        p[coin_id]["total_invertido_eur"] = round(inv_total - inv_vendido, 4)
        remaining     = p[coin_id]["cantidad_tokens"]
        remaining_inv = p[coin_id]["total_invertido_eur"]

    save_state()
    await update.message.reply_text(
        f"{'💰' if ben_neto_venta>=0 else '📉'} *Venta registrada — v17 Coste Real*\n\n"
        f"  *{sym(coin_id)}* — {tokens_venta} tokens @ {fp(price)}\n"
        f"  💶 Coste real vendido: `{fp(inv_vendido)}`\n"
        f"  📊 Valor de venta: `{fp(valor_venta)}`\n"
        f"  Beneficio Neto Real: `{fp(ben_neto_venta)}` ({ben_pct_venta:+.2f}%)\n\n"
        f"  Tokens restantes: `{remaining}`\n"
        f"  Invertido restante: `{fp(remaining_inv)}`",
        parse_mode="Markdown")

async def cmd_precio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: `/precio BTC`", parse_mode="Markdown")
        return
    coin_id = resolve_coin(ctx.args[0])
    if not coin_id:
        await update.message.reply_text(
            f"❌ No reconozco '{ctx.args[0]}'. Prueba /buscar {ctx.args[0]}")
        return
    msg  = await update.message.reply_text("🔄 Obteniendo precio en €...")
    info = await run(_fetch_prices, [coin_id])
    if not info or coin_id not in info:
        await msg.edit_text(
            "⚠️ No pude obtener el precio ahora mismo. Espera 30s e inténtalo de nuevo.")
        return
    d = info[coin_id]
    await msg.edit_text(
        f"💰 *{sym(coin_id)} — {coin_name(coin_id)}*\n\n"
        f"  Precio: *{fp(d.get(CURRENCY, 0))}*\n"
        f"  24h: {pc(d.get(f'{CURRENCY}_24h_change', 0) or 0)}\n"
        f"  7d:  {pc(d.get(f'{CURRENCY}_7d_change',  0) or 0)}\n"
        f"  Vol 24h: {fv(d.get(f'{CURRENCY}_24h_vol', 0) or 0)}\n"
        f"  Mkt cap: {fv(d.get(f'{CURRENCY}_market_cap', 0) or 0)}",
        parse_mode="Markdown")

async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: `/buscar PEPE`", parse_mode="Markdown")
        return
    query   = " ".join(ctx.args)
    msg     = await update.message.reply_text(f"🔍 Buscando '{query}'...")
    coin_id = await run(resolve_coin, query)
    if not coin_id:
        await msg.edit_text(f"❌ No encontré ninguna cripto con '{query}'.")
        return
    info = await run(_fetch_prices, [coin_id])
    if not info or coin_id not in info:
        await msg.edit_text(
            f"✅ Encontrada: *{coin_name(coin_id)}* ({sym(coin_id)})\n"
            f"⚠️ Sin datos de precio disponibles.", parse_mode="Markdown")
        return
    d  = info[coin_id]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📊 Analizar {sym(coin_id)}", callback_data=f"analyse:{coin_id}"),
        InlineKeyboardButton("📥 Registrar compra",         callback_data=f"buy_prompt:{coin_id}"),
    ]])
    await msg.edit_text(
        f"✅ *{coin_name(coin_id)}* ({sym(coin_id)})\n\n"
        f"  Precio: *{fp(d.get(CURRENCY, 0))}*\n"
        f"  24h: {pc(d.get(f'{CURRENCY}_24h_change', 0) or 0)}\n"
        f"  7d:  {pc(d.get(f'{CURRENCY}_7d_change',  0) or 0)}\n"
        f"  Vol 24h: {fv(d.get(f'{CURRENCY}_24h_vol', 0) or 0)}\n"
        f"  Mkt cap: {fv(d.get(f'{CURRENCY}_market_cap', 0) or 0)}",
        parse_mode="Markdown", reply_markup=kb)

async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["portfolio"]
    if ctx.args:
        coin_id = resolve_coin(ctx.args[0])
        if not coin_id:
            await update.message.reply_text(
                f"❌ No reconozco '{ctx.args[0]}'. Prueba /buscar {ctx.args[0]}")
            return
        msg  = await update.message.reply_text(
            f"🔍 Analizando *{sym(coin_id)}*...", parse_mode="Markdown")
        info = await run(_fetch_prices, [coin_id])
        if not info or coin_id not in info:
            await msg.edit_text(
                "⚠️ No pude obtener datos ahora. Espera 30s e inténtalo de nuevo.")
            return
        a = await run(_do_full_analysis, coin_id, info[coin_id])
        await msg.edit_text(
            build_analysis_msg(coin_id, a, holding=p.get(coin_id)),
            parse_mode="Markdown")
        return
    if not p:
        await update.message.reply_text(
            "Tu cartera está vacía.\n\nUsa /compra BTC 0.5 para añadir\n"
            "o /mercado para buscar oportunidades.")
        return
    n   = len(p)
    msg = await update.message.reply_text(
        f"🔍 Analizando {n} activo{'s' if n>1 else ''} de tu cartera...\n"
        f"_Escribe /cancelar para parar._",
        parse_mode="Markdown")
    data = await run(_fetch_prices, list(p.keys()))
    for cid, pos in p.items():
        if state.get("cancel"):
            state["cancel"] = False
            await msg.edit_text("🛑 Análisis cancelado.")
            return
        info = data.get(cid)
        if not info:
            await update.message.reply_text(f"⚠️ Sin datos para {sym(cid)}, saltando.")
            continue
        await msg.edit_text(f"🔍 Analizando *{sym(cid)}*...", parse_mode="Markdown")
        a = await run(_do_full_analysis, cid, info)
        await update.message.reply_text(
            build_analysis_msg(cid, a, holding=pos), parse_mode="Markdown")
    await msg.edit_text(
        f"✅ Análisis completado — {n} activo{'s' if n>1 else ''}.\n"
        f"_🕐 {datetime.now().strftime('%H:%M')}_",
        parse_mode="Markdown")

async def cmd_mercado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        f"🔍 *Analizando el top 100 del mercado en {CURRENCY_SYM}...*\n_~30 segundos._",
        parse_mode="Markdown")
    markets = await run(_fetch_top_markets)
    if not markets:
        await msg.edit_text(
            "⚠️ No pude obtener datos ahora mismo.\n"
            "CoinGecko puede estar limitando peticiones. Espera 30s e intenta de nuevo.")
        return
    scored = []
    for c in markets:
        price = c.get("current_price", 0) or 0
        chg24 = c.get("price_change_percentage_24h", 0) or 0
        chg7  = c.get("price_change_percentage_7d_in_currency", 0) or 0
        vol   = c.get("total_volume", 0) or 0
        mcap  = c.get("market_cap", 0) or 0
        cid   = c.get("id", "")
        sym_c = c.get("symbol", "").upper()
        name  = c.get("name", "")
        if not price: continue
        if cid and cid not in CATALOGUE:
            CATALOGUE[cid] = {"symbol": sym_c, "name": name}
        score = 0
        if chg24 < -8:   score += 2
        elif chg24 < -3: score += 1
        elif chg24 > 10: score -= 2
        elif chg24 > 5:  score -= 1
        if chg7 < -15:   score += 2
        elif chg7 < -5:  score += 1
        elif chg7 > 20:  score -= 2
        if mcap > 0 and vol/mcap > 0.15: score += 1
        scored.append({"id": cid, "symbol": sym_c, "name": name,
                        "price": price, "chg24": chg24, "chg7": chg7,
                        "vol": vol, "mcap": mcap, "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    buy_list = [x for x in scored if x["score"] >= 1]
    avoid    = [x for x in scored if x["score"] <= -2]
    lines    = [f"🔍 *Scanner de Mercado — {datetime.now().strftime('%H:%M')}* ({CURRENCY_SYM})\n"]
    if buy_list:
        lines.append("🟢 *POSIBLES OPORTUNIDADES DE COMPRA*\n")
        for x in buy_list[:10]:
            tag = " 📦" if x["id"] in state["portfolio"] else ""
            lines.append(
                f"  *{x['symbol']}{tag}* — {fp(x['price'])}\n"
                f"    24h: {pc(x['chg24'])}  7d: {pc(x['chg7'])}\n"
                f"    Vol: {fv(x['vol'])}  MCap: {fv(x['mcap'])}\n")
    if avoid:
        lines.append("🔴 *PRECAUCIÓN* (subidas fuertes)\n")
        for x in avoid[:5]:
            tag = " 📦" if x["id"] in state["portfolio"] else ""
            lines.append(f"  *{x['symbol']}{tag}* — {fp(x['price'])} — 24h: {pc(x['chg24'])}")
    lines += [
        "",
        "💡 Análisis completo: `/analizar BTC`",
        "_📦 = ya tienes en cartera_",
        f"_Escaneadas: {len(scored)} monedas_",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    watchlist_names = " · ".join(WATCHLIST.values())
    last = state.get("last_monitor") or "Todavía no ejecutado"
    await update.message.reply_text(
        f"🔔 *Monitor 3 Fases v17 — Escáner Dual*\n\n"
        f"  Frecuencia: cada *{MONITOR_HOURS} horas*\n"
        f"  Último ciclo: `{last}`\n"
        f"  Activos en cartera: `{len(state['portfolio'])}`\n"
        f"  Moneda: *{CURRENCY_SYM} \\(euros\\)*\n\n"
        f"*Fase A — Cartera \\(Coste Real\\):*\n"
        f"  · 🚨 Señal de venta \\+ Beneficio Neto Real\n"
        f"  · ⚠️ Aviso break\\-even si posición en negativo\n"
        f"  · 💡 DCA \\(caída ≥{DCA_DROP_PCT:.0f}% break\\-even \\+ RSI<35\\)\n"
        f"  · 🔒 Trailing stop \\(beneficio ≥{TRAILING_MIN_PROFIT:.0f}%\\)\n"
        f"  · ⚡ Volatilidad ≥{VOLATILITY_PCT:.0f}% en 4h\n"
        f"  · 💰 Objetivo beneficio ≥{PROFIT_ALERT:.0f}%\n"
        f"  · 🛑 Stop\\-loss cercano\n\n"
        f"*Fase B — 📡 Radar Fijo IA/DePIN:*\n"
        f"  · Monedas: {watchlist_names}\n"
        f"  · Umbral: caída >*{RADAR_DROP_24H:.0f}%* o RSI<*{RADAR_RSI_MAX}* \\(cualquiera\\)\n"
        f"  · Score mínimo: {RADAR_SCORE_MIN}\n\n"
        f"*Fase C — 🔍 Escáner Top 50 por Volumen:*\n"
        f"  · Escanea las 50 monedas con más volumen\n"
        f"  · Umbral: caída >*{TOP50_DROP_24H:.0f}%* Y RSI<*{TOP50_RSI_MAX}* \\(pánico real\\)\n"
        f"  · Score mínimo: {TOP50_SCORE_MIN} — solo señales fuertes\n"
        f"  · Excluye cartera y Watchlist \\(ya cubiertas\\)\n\n"
        f"Usa /forzarmonitor para ejecutar ahora\\.",
        parse_mode="MarkdownV2")

async def cmd_forzar_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🔄 *Ejecutando monitor v17 \\(3 fases\\)\\.\\.\\.*\n\n"
        f"  _Fase A: {len(state['portfolio'])} activos en cartera_\n"
        f"  _Fase B: Radar Fijo — TAO · RENDER · FET · ONDO · AKT_\n"
        f"  _Fase C: Escáner Top 50 por volumen_\n\n"
        f"_Recibirás el informe completo en unos minutos\\._",
        parse_mode="MarkdownV2")
    await do_monitor(ctx.bot)

async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["cancel"] = True
    await update.message.reply_text("🛑 Cancelando en el siguiente paso...")

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("buy_prompt:"):
        cid = q.data.split(":")[1]
        await q.message.reply_text(
            f"Para registrar la compra de *{sym(cid)}*:\n\n"
            f"`/compra {sym(cid)} <cantidad>`",
            parse_mode="Markdown")
    elif q.data.startswith("analyse:"):
        cid  = q.data.split(":")[1]
        msg  = await q.message.reply_text(f"🔍 Analizando {sym(cid)}...")
        info = await run(_fetch_prices, [cid])
        if not info or cid not in info:
            await msg.edit_text("⚠️ No pude obtener datos ahora mismo.")
            return
        a = await run(_do_full_analysis, cid, info[cid])
        await msg.edit_text(
            build_analysis_msg(cid, a, holding=state["portfolio"].get(cid)),
            parse_mode="Markdown")

async def unknown_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "No entiendo ese mensaje. Usa /ayuda para ver los comandos disponibles.")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
async def startup(app):
    log.info("Cargando catálogo en background...")
    await run(_fetch_catalogue)
    await run(_fetch_top_ids)
    log.info("Catálogo listo — %d monedas", len(CATALOGUE))
    asyncio.create_task(monitor_loop(app))

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no configurado")

    load_state()

    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .post_init(startup)
           .build())

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("ayuda",         cmd_ayuda))
    app.add_handler(CommandHandler("cartera",       cmd_cartera))
    app.add_handler(CommandHandler("compra",        cmd_compra))
    app.add_handler(CommandHandler("venta",         cmd_venta))
    app.add_handler(CommandHandler("precio",        cmd_precio))
    app.add_handler(CommandHandler("buscar",        cmd_buscar))
    app.add_handler(CommandHandler("analizar",      cmd_analizar))
    app.add_handler(CommandHandler("mercado",       cmd_mercado))
    app.add_handler(CommandHandler("monitor",       cmd_monitor))
    app.add_handler(CommandHandler("forzarmonitor", cmd_forzar_monitor))
    app.add_handler(CommandHandler("cancelar",      cmd_cancelar))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_handler))

    log.info("Bot v17 arrancado — Escáner Dual IA/DePIN + Top50 — monitor 3 fases cada %dh — %s", MONITOR_HOURS, CURRENCY_SYM)
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
