"""
CRYPTO BOT PRO v14 — Gestor de Cartera Proactivo
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
· Todo en EUROS (€)
· Monitor 4h: alertas de venta, DCA, trailing stop, volatilidad
· Reporte detallado: top ganador, top perdedor, dominancia BTC
· Trailing stop dinámico si beneficio > 5%
· Detector de oportunidad DCA (-10% + RSI<35)
· Detector de volatilidad (movimiento >5% en 4h)
· Scanner de mercado /mercado
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
PORTFOLIO_FILE = "portfolio.json"
GECKO          = "https://api.coingecko.com/api/v3"
CURRENCY       = "eur"          # moneda base
CURRENCY_SYM   = "€"
MONITOR_HOURS  = 4              # análisis automático cada N horas
PROFIT_ALERT   = 10.0           # % beneficio para sugerir toma de ganancias
SELL_CONF_MIN  = 60             # confianza mínima para alertas de venta
DCA_DROP_PCT   = 10.0           # caída % sobre avg_buy para sugerir DCA
TRAILING_MIN_PROFIT = 5.0       # % beneficio mínimo para activar trailing stop
VOLATILITY_PCT = 5.0            # % de movimiento en 4h para alerta de volatilidad

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Catálogo ──────────────────────────────────────────────────────────────────
CATALOGUE = {}   # coin_id → {symbol, name}
TOP_IDS   = []   # top 300 por market cap

# ── Estado ────────────────────────────────────────────────────────────────────
state = {
    "portfolio":      {},    # {coin_id: {units, avg_buy, last_price}}
    "cancel":         False,
    "last_monitor":   None,
    "prev_prices":    {},    # {coin_id: price} del ciclo anterior para detectar volatilidad
}

def load_state():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                saved = json.load(f)
                state["portfolio"]   = saved.get("portfolio", {})
                state["prev_prices"] = saved.get("prev_prices", {})
            log.info("Cartera cargada: %d activos", len(state["portfolio"]))
        except Exception as e:
            log.warning("Error cargando cartera: %s", e)

def save_state():
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump({
                "portfolio":   state["portfolio"],
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

# ── CoinGecko API (todo en EUR) ───────────────────────────────────────────────
def _fetch_prices(coin_ids):
    import time
    if not coin_ids:
        return {}
    result = {}
    ids = list(dict.fromkeys(coin_ids))
    for i in range(0, len(ids), 50):
        data = _get(f"{GECKO}/simple/price", {
            "ids":                  ",".join(ids[i:i+50]),
            "vs_currencies":        CURRENCY,
            "include_24hr_change":  "true",
            "include_7d_change":    "true",
            "include_24hr_vol":     "true",
            "include_market_cap":   "true",
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
    # OHLC de CoinGecko devuelve siempre en USD internamente pero los valores
    # relativos sirven igual para los indicadores técnicos
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
    import time
    result = []
    for page in [1, 2]:
        data = _get(f"{GECKO}/coins/markets", {
            "vs_currency":              CURRENCY,
            "order":                    "market_cap_desc",
            "per_page":                 50,
            "page":                     page,
            "sparkline":                "false",
            "price_change_percentage":  "24h,7d",
        })
        if data:
            result.extend(data)
        time.sleep(1.5)
    return result

def _fetch_global():
    """Dominancia de BTC y capitalización global."""
    data = _get(f"{GECKO}/global")
    if data:
        gd = data.get("data", {})
        btc_dom   = gd.get("market_cap_percentage", {}).get("btc", 0)
        total_cap = gd.get("total_market_cap", {}).get(CURRENCY, 0)
        chg_24h   = gd.get("market_cap_change_percentage_24h_usd", 0)  # % no tiene moneda
        return {"btc_dominance": round(btc_dom, 1),
                "total_cap_eur": total_cap,
                "cap_change_24h": round(chg_24h, 2)}
    return {"btc_dominance": 0, "total_cap_eur": 0, "cap_change_24h": 0}

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
        "price":  price,  "chg24": chg24, "chg7": chg7,
        "signal": signal_label(sc_d), "confidence": conf_d, "score": sc_d,
        "rsi":    rsi_d,  "ema7":  ema7_d, "ema14": ema14_d,
        "bb_pos": bb_d,   "target": target, "stop_loss": sl,
        "reasons": reas_d,
        "signal_4h": sig_4h, "confidence_4h": conf_4h, "score_4h": sc_4h,
        "reasons_4h": reas_4h,
    }

# ── Trailing Stop dinámico ────────────────────────────────────────────────────
def calc_trailing_stop(avg_buy, current_price):
    """
    Si la posición tiene beneficio > TRAILING_MIN_PROFIT%,
    devuelve un stop-loss dinámico que protege el capital invertido (break-even + margen).
    """
    pnl_pct = ((current_price / avg_buy) - 1) * 100 if avg_buy else 0
    if pnl_pct < TRAILING_MIN_PROFIT:
        return None, pnl_pct
    # Stop en break-even + 1% de margen
    trailing_sl = round(avg_buy * 1.01, 8)
    return trailing_sl, pnl_pct

# ── Formateo ──────────────────────────────────────────────────────────────────
def fp(p):
    """Formatea precio en euros."""
    if not p:     return f"0{CURRENCY_SYM}"
    if p >= 1000: return f"{p:,.2f}{CURRENCY_SYM}"
    if p >= 1:    return f"{p:.4f}{CURRENCY_SYM}"
    if p >= 0.01: return f"{p:.6f}{CURRENCY_SYM}"
    return f"{p:.8f}{CURRENCY_SYM}"

def fv(v):
    """Formatea valor en euros (millones/miles)."""
    if v >= 1e9:  return f"{v/1e9:.2f}B{CURRENCY_SYM}"
    if v >= 1e6:  return f"{v/1e6:.1f}M{CURRENCY_SYM}"
    if v >= 1e3:  return f"{v/1e3:.1f}K{CURRENCY_SYM}"
    return f"{v:.2f}{CURRENCY_SYM}"

def pc(v):
    return f"{'🟢 +' if v >= 0 else '🔴 '}{v:.2f}%"

# ── Mensajes de análisis ──────────────────────────────────────────────────────
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
        units, avg = holding["units"], holding["avg_buy"]
        cur = price * units
        inv = avg * units
        prf = cur - inv
        pnl = (prf / inv * 100) if inv else 0
        trailing_sl, _ = calc_trailing_stop(avg, price)
        lines += [
            "",
            "┌─ TU POSICIÓN ────────────────────",
            f"│ {units} uds · Compra media: {fp(avg)}",
            f"│ Valor actual: {fp(cur)}",
            f"└ {'💰' if prf>=0 else '📉'} P&L: {fp(prf)} ({pnl:+.2f}%)",
        ]
        if trailing_sl:
            lines.append(f"  🔒 Trailing stop activo: {fp(trailing_sl)} (break-even +1%)")

    lines.append(f"\n_🕐 {datetime.now().strftime('%d/%m %H:%M')}_")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# MONITOR AUTOMÁTICO CADA 4H
# ══════════════════════════════════════════════════════════════════════════════
async def monitor_loop(app):
    await asyncio.sleep(30)
    log.info("Monitor %dh iniciado", MONITOR_HOURS)
    while True:
        try:
            await do_monitor(app.bot)
        except Exception as e:
            log.error("Error en monitor: %s", e)
        log.info("Próximo monitor en %dh", MONITOR_HOURS)
        await asyncio.sleep(MONITOR_HOURS * 3600)

async def do_monitor(bot):
    portfolio = state["portfolio"]
    if not portfolio:
        log.info("Monitor: cartera vacía")
        return
    if not CHAT_ID:
        log.warning("Monitor: CHAT_ID no configurado")
        return

    now_str = datetime.now().strftime("%d/%m %H:%M")
    log.info("Monitor iniciando — %d activos", len(portfolio))
    state["last_monitor"] = now_str

    # Obtener precios y datos globales en paralelo
    prices_data = await run(_fetch_prices, list(portfolio.keys()))
    global_data = await run(_fetch_global)

    alerts    = []   # lista de mensajes de alerta a enviar
    pnl_list  = []   # para reporte: [(coin_id, pnl_pct, price, cur_value)]

    for coin_id, pos in portfolio.items():
        info = prices_data.get(coin_id)
        if not info:
            log.warning("Monitor: sin precio para %s", coin_id)
            continue

        price = info.get(CURRENCY, 0)
        chg24 = info.get(f"{CURRENCY}_24h_change", 0) or 0
        if not price:
            continue

        units, avg = pos["units"], pos["avg_buy"]
        pnl_pct   = ((price / avg) - 1) * 100 if avg else 0
        cur_value  = price * units
        inv_value  = avg * units
        profit_eur = cur_value - inv_value

        pnl_list.append((coin_id, pnl_pct, price, cur_value))

        # ── Detector de volatilidad ───────────────────────────────────────────
        prev_price = state["prev_prices"].get(coin_id)
        vol_pct = 0.0
        if prev_price and prev_price > 0:
            vol_pct = abs((price - prev_price) / prev_price * 100)
            if vol_pct >= VOLATILITY_PCT:
                direction = "subida" if price > prev_price else "caída"
                alerts.append(
                    f"⚡ *ALTA VOLATILIDAD — {sym(coin_id)}*\n"
                    f"_{direction.capitalize()} del {vol_pct:.1f}% en las últimas {MONITOR_HOURS}h_\n\n"
                    f"  Precio: *{fp(price)}*   24h: {pc(chg24)}\n"
                    f"  Tu P&L: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n"
                    f"_Monitor {now_str}_"
                )

        # Guardar precio actual para el próximo ciclo
        state["prev_prices"][coin_id] = price

        # ── Análisis técnico completo ─────────────────────────────────────────
        a = await run(_do_full_analysis, coin_id, info)

        trailing_sl, _ = calc_trailing_stop(avg, price)

        # ── Lógica de alertas proactivas ──────────────────────────────────────

        # 1. Oportunidad DCA: caída >= 10% sobre avg_buy y RSI < 35
        drop_from_avg = ((avg - price) / avg * 100) if avg else 0
        if drop_from_avg >= DCA_DROP_PCT and a["rsi"] < 35:
            alerts.append(
                f"💡 *OPORTUNIDAD DCA — {sym(coin_id)}*\n"
                f"_Caída del {drop_from_avg:.1f}% sobre tu precio de compra con RSI en sobreventa_\n\n"
                f"  Precio actual: *{fp(price)}*\n"
                f"  Tu compra media: {fp(avg)}\n"
                f"  RSI: `{a['rsi']}` — Zona de acumulación\n"
                f"  P&L actual: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n\n"
                f"  Promediar te bajaría el coste medio y mejoraría el punto de equilibrio.\n"
                f"  ⚠️ Solo si tienes convicción en el activo.\n"
                f"_Monitor {now_str}_"
            )
            continue   # no generar alerta de venta si hay señal DCA

        # 2. Trailing stop activado y precio cayendo por debajo
        if trailing_sl and price <= trailing_sl * 1.01:
            alerts.append(
                f"🔒 *TRAILING STOP — {sym(coin_id)}*\n"
                f"_El precio se acerca al stop dinámico de break-even_\n\n"
                f"  Precio actual: *{fp(price)}*\n"
                f"  Trailing stop: {fp(trailing_sl)}\n"
                f"  P&L actual: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n"
                f"  Considera vender para proteger tus ganancias.\n"
                f"_Monitor {now_str}_"
            )
            continue

        # 3. Señal técnica fuerte de venta
        if a["score"] <= -3 and a["confidence"] >= SELL_CONF_MIN:
            alerts.append(
                f"🚨 *ALERTA VENTA — {sym(coin_id)}*\n"
                f"_Señales técnicas apuntan a presión bajista_\n\n"
                f"  Precio: *{fp(price)}*   24h: {pc(chg24)}\n"
                f"  Señal diaria: {a['signal']} ({a['confidence']}%)\n"
                f"  Señal 4h: {a['signal_4h']} ({a['confidence_4h']}%)\n"
                f"  RSI: `{a['rsi']}`\n\n"
                f"  Razones:\n"
                + "\n".join(f"  · {r}" for r in a["reasons"][:3]) +
                f"\n\n  Tu posición: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n"
                f"_Monitor {now_str}_"
            )

        # 4. Señal bajista diaria y 4h confirmada (aunque no llegue a -3)
        elif a["score"] <= -2 and a["score_4h"] <= -2 and a["confidence"] >= SELL_CONF_MIN:
            alerts.append(
                f"🚨 *SEÑAL BAJISTA CONFIRMADA — {sym(coin_id)}*\n"
                f"_Tendencia bajista en timeframe diario y 4h simultáneamente_\n\n"
                f"  Precio: *{fp(price)}*   24h: {pc(chg24)}\n"
                f"  Diario: {a['signal']} | 4H: {a['signal_4h']}\n"
                f"  P&L actual: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n"
                f"_Monitor {now_str}_"
            )

        # 5. RSI sobrecompra extrema + beneficio significativo
        elif a["rsi"] >= 75 and pnl_pct >= 10:
            alerts.append(
                f"⚠️ *TOMA DE GANANCIAS — {sym(coin_id)}*\n"
                f"_RSI en sobrecompra extrema con beneficio acumulado importante_\n\n"
                f"  Precio: *{fp(price)}*\n"
                f"  RSI: `{a['rsi']}` — Sobrecompra\n"
                f"  P&L: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n"
                f"  Considera tomar parcialmente los beneficios.\n"
                f"_Monitor {now_str}_"
            )

        # 6. Objetivo de beneficio alcanzado + señal no alcista
        elif pnl_pct >= PROFIT_ALERT and a["score"] <= 0:
            alerts.append(
                f"💰 *OBJETIVO DE BENEFICIO — {sym(coin_id)}*\n"
                f"_Has alcanzado +{PROFIT_ALERT:.0f}% con señal neutral o bajista_\n\n"
                f"  Precio: *{fp(price)}*\n"
                f"  P&L: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n"
                f"  Señal: {a['signal']}\n"
                f"  Considera asegurar parte de las ganancias.\n"
                f"_Monitor {now_str}_"
            )

        # 7. Stop-loss clásico: precio cerca del stop sugerido por el análisis
        elif price <= a["stop_loss"] * 1.02:
            alerts.append(
                f"🛑 *STOP-LOSS CERCANO — {sym(coin_id)}*\n"
                f"_El precio se acerca al nivel de stop-loss sugerido_\n\n"
                f"  Precio actual: *{fp(price)}*\n"
                f"  Stop-loss sugerido: {fp(a['stop_loss'])}\n"
                f"  P&L: {fp(profit_eur)} ({pnl_pct:+.2f}%)\n"
                f"_Monitor {now_str}_"
            )

        await asyncio.sleep(0.8)

    # Guardar precios del ciclo actual para el siguiente
    save_state()

    # ── Enviar alertas ────────────────────────────────────────────────────────
    for alert_text in alerts:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=alert_text, parse_mode="Markdown")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error("Error enviando alerta: %s", e)

    # ── Reporte detallado (siempre, haya o no alertas) ────────────────────────
    await _send_monitor_report(bot, pnl_list, global_data, len(alerts), now_str)

async def _send_monitor_report(bot, pnl_list, global_data, n_alerts, now_str):
    """Reporte de estado de cartera con top ganador, top perdedor y mercado global."""
    if not pnl_list:
        return

    portfolio = state["portfolio"]

    # Calcular totales
    total_inv = total_cur = 0
    for cid, pnl_pct, price, cur_val in pnl_list:
        pos = portfolio.get(cid, {})
        inv = pos.get("avg_buy", 0) * pos.get("units", 0)
        total_inv += inv
        total_cur += cur_val

    total_pnl     = total_cur - total_inv
    total_pnl_pct = (total_pnl / total_inv * 100) if total_inv else 0

    # Top ganador y top perdedor
    pnl_sorted   = sorted(pnl_list, key=lambda x: x[1], reverse=True)
    top_winner   = pnl_sorted[0]   if pnl_sorted else None
    top_loser    = pnl_sorted[-1]  if len(pnl_sorted) > 1 else None

    # Dominancia BTC
    btc_dom   = global_data.get("btc_dominance", 0)
    total_cap = global_data.get("total_cap_eur", 0)
    cap_chg   = global_data.get("cap_change_24h", 0)

    # Sentimiento de mercado basado en dominancia
    if btc_dom > 60:
        market_mood = "🔵 Dominancia BTC alta — mercado en modo defensivo/Bitcoin"
    elif btc_dom > 50:
        market_mood = "🟡 Dominancia BTC moderada — equilibrio BTC/altcoins"
    else:
        market_mood = "🟢 Dominancia BTC baja — temporada de altcoins posible"

    lines = [
        f"📋 *INFORME DE CARTERA — {now_str}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{'🟢' if total_pnl >= 0 else '🔴'} *Balance Total*",
        f"  Invertido: {fp(total_inv)}",
        f"  Valor actual: {fp(total_cur)}",
        f"  P&L: {fp(total_pnl)} ({total_pnl_pct:+.2f}%)",
        "",
    ]

    if top_winner:
        pos_w = portfolio.get(top_winner[0], {})
        inv_w = pos_w.get("avg_buy", 0) * pos_w.get("units", 0)
        pnl_w = top_winner[3] - inv_w
        lines += [
            f"🏆 *Top Ganador:* {sym(top_winner[0])} ({coin_name(top_winner[0])})",
            f"  {fp(top_winner[2])} · P&L: {fp(pnl_w)} ({top_winner[1]:+.2f}%)",
        ]

    if top_loser and top_loser[0] != top_winner[0]:
        pos_l = portfolio.get(top_loser[0], {})
        inv_l = pos_l.get("avg_buy", 0) * pos_l.get("units", 0)
        pnl_l = top_loser[3] - inv_l
        lines += [
            f"📉 *Top Perdedor:* {sym(top_loser[0])} ({coin_name(top_loser[0])})",
            f"  {fp(top_loser[2])} · P&L: {fp(pnl_l)} ({top_loser[1]:+.2f}%)",
        ]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🌍 *Estado Global del Mercado*",
        f"  Dominancia BTC: *{btc_dom}%*",
        f"  Cap. total: {fv(total_cap)}",
        f"  Variación 24h: {pc(cap_chg)}",
        f"  {market_mood}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if n_alerts == 0:
        lines += [
            "✅ *Sin señales de acción detectadas*",
            "Tu cartera está estable. No hay movimientos urgentes recomendados.",
        ]
    else:
        lines.append(f"⚠️ Se han enviado *{n_alerts} alerta{'s' if n_alerts>1 else ''}* en este ciclo.")

    lines.append(f"\n_Próximo análisis en {MONITOR_HOURS}h_")

    try:
        await bot.send_message(
            chat_id    = CHAT_ID,
            text       = "\n".join(lines),
            parse_mode = "Markdown",
        )
    except Exception as e:
        log.error("Error enviando reporte: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════════════════════════════════
HELP = (
    "👋 *Crypto Bot Pro v14 — Comandos*\n\n"
    "📦 *CARTERA*\n"
    "  /compra BTC 0\\.5 — Registrar compra\n"
    "  /compra BTC 0\\.5 52000 — Con precio manual en €\n"
    "  /venta ETH 1\\.2 — Registrar venta\n"
    "  /cartera — Ver cartera con P\\&L en €\n"
    "  /precio BTC — Precio actual en €\n"
    "  /buscar PEPE — Buscar cualquier cripto\n\n"
    "📊 *ANÁLISIS*\n"
    "  /analizar BTC — Análisis diario \\+ 4h\n"
    "  /analizar — Analizar toda tu cartera\n\n"
    "🔍 *MERCADO*\n"
    "  /mercado — Top 100 oportunidades en €\n\n"
    "🔔 *MONITOR 4H*\n"
    "  /monitor — Estado del monitor automático\n"
    "  /forzarmonitor — Ejecutar análisis ahora\n\n"
    "⚙️ /cancelar — Parar comando en curso\n\n"
    "_Precios en euros · Monitor cada 4h_"
)

async def cmd_start(u, c): await u.message.reply_text(HELP, parse_mode="MarkdownV2")
async def cmd_ayuda(u, c): await u.message.reply_text(HELP, parse_mode="MarkdownV2")

# ── Cartera ───────────────────────────────────────────────────────────────────
async def cmd_cartera(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["portfolio"]
    if not p:
        await update.message.reply_text("Tu cartera está vacía.\n\nUsa /compra BTC 0.5 para añadir.")
        return
    msg  = await update.message.reply_text("🔄 Obteniendo precios en €...")
    data = await run(_fetch_prices, list(p.keys()))
    ti = tc = 0
    lines = [f"💼 *Tu Cartera* (en {CURRENCY_SYM})\n"]
    for cid, pos in p.items():
        units, avg = pos["units"], pos["avg_buy"]
        info  = data.get(cid, {})
        price = info.get(CURRENCY, 0)
        chg24 = info.get(f"{CURRENCY}_24h_change", 0) or 0
        inv, cur = avg*units, price*units
        prf = cur - inv
        pnl = (prf / inv * 100) if inv else 0
        ti += inv; tc += cur
        trailing_sl, _ = calc_trailing_stop(avg, price)
        trailing_line   = f"  🔒 Trailing stop: {fp(trailing_sl)}\n" if trailing_sl else ""
        lines.append(
            f"{'🟢' if prf>=0 else '🔴'} *{sym(cid)}*\n"
            f"  {units} uds · Compra: {fp(avg)} · Ahora: {fp(price)}\n"
            f"  24h: {pc(chg24)}\n"
            f"  Valor: {fp(cur)} · P&L: {fp(prf)} ({pnl:+.2f}%)\n"
            + trailing_line
        )
    tp   = tc - ti
    tpct = (tp / ti * 100) if ti else 0
    lines += [
        "━━━━━━━━━━━━━━━━",
        f"{'🟢' if tp>=0 else '🔴'} *TOTAL*",
        f"  Invertido: `{fp(ti)}`",
        f"  Valor actual: `{fp(tc)}`",
        f"  P&L: `{fp(tp)}` ({tpct:+.2f}%)",
        f"\n_🕐 {datetime.now().strftime('%H:%M:%S')}_",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

async def cmd_compra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            f"Uso: `/compra BTC 0.5` o `/compra BTC 0.5 52000`\n_Precios en {CURRENCY_SYM}_",
            parse_mode="Markdown")
        return
    coin_id = resolve_coin(args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{args[0]}'. Prueba /buscar {args[0]}")
        return
    try:
        units = float(args[1].replace(",", "."))
        if units <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ La cantidad debe ser un número positivo.")
        return
    if len(args) >= 3:
        try:
            buy_price = float(args[2].replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ El precio debe ser un número.")
            return
    else:
        msg  = await update.message.reply_text("🔄 Obteniendo precio de mercado en €...")
        info = await run(_fetch_prices, [coin_id])
        if not info or coin_id not in info:
            await msg.edit_text(
                f"⚠️ No pude obtener el precio de *{sym(coin_id)}*.\n"
                f"Indícalo manualmente: `/compra {sym(coin_id)} {units} <precio_en_eur>`",
                parse_mode="Markdown")
            return
        buy_price = info[coin_id].get(CURRENCY, 0)
        await msg.delete()

    p = state["portfolio"]
    if coin_id in p:
        ou, oa = p[coin_id]["units"], p[coin_id]["avg_buy"]
        nu = ou + units
        na = (ou*oa + units*buy_price) / nu
        p[coin_id] = {"units": round(nu, 8), "avg_buy": round(na, 8)}
        extra = "Posición ampliada."
    else:
        p[coin_id] = {"units": round(units, 8), "avg_buy": round(buy_price, 8)}
        extra = "Nueva posición creada."
    save_state()
    await update.message.reply_text(
        f"✅ *Compra registrada*\n\n"
        f"  *{sym(coin_id)}* — {units} uds @ {fp(buy_price)}\n"
        f"  Invertido: {fp(units*buy_price)}\n"
        f"  Precio medio ahora: {fp(p[coin_id]['avg_buy'])}\n\n_{extra}_",
        parse_mode="Markdown")

async def cmd_venta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Uso: `/venta BTC 0.5`", parse_mode="Markdown")
        return
    coin_id = resolve_coin(args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{args[0]}'.")
        return
    try:
        units = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ La cantidad debe ser un número.")
        return
    p = state["portfolio"]
    if coin_id not in p:
        await update.message.reply_text(
            f"⚠️ No tienes *{sym(coin_id)}* en cartera.", parse_mode="Markdown")
        return
    pos  = p[coin_id]
    info = await run(_fetch_prices, [coin_id])
    sell = info.get(coin_id, {}).get(CURRENCY, pos["avg_buy"])
    prf  = (sell - pos["avg_buy"]) * units
    pnl  = ((sell / pos["avg_buy"]) - 1) * 100 if pos["avg_buy"] else 0
    if units >= pos["units"]:
        del p[coin_id]
        remaining = 0
    else:
        p[coin_id]["units"] = round(pos["units"] - units, 8)
        remaining = p[coin_id]["units"]
    save_state()
    await update.message.reply_text(
        f"{'💰' if prf>=0 else '📉'} *Venta registrada*\n\n"
        f"  *{sym(coin_id)}* — {units} uds @ {fp(sell)}\n"
        f"  Compra media: {fp(pos['avg_buy'])}\n"
        f"  P&L: {fp(prf)} ({pnl:+.2f}%)\n"
        f"  Restantes: `{remaining}`",
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

# ── Analizar ──────────────────────────────────────────────────────────────────
async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["portfolio"]

    # /analizar BTC
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

    # /analizar — toda la cartera
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

# ── Mercado ───────────────────────────────────────────────────────────────────
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

# ── Monitor ───────────────────────────────────────────────────────────────────
async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    last = state.get("last_monitor") or "Todavía no ejecutado"
    await update.message.reply_text(
        f"🔔 *Monitor automático*\n\n"
        f"  Frecuencia: cada *{MONITOR_HOURS} horas*\n"
        f"  Último análisis: `{last}`\n"
        f"  Activos vigilados: `{len(state['portfolio'])}`\n"
        f"  Moneda: *{CURRENCY_SYM} (euros)*\n\n"
        f"*Alertas activas:*\n"
        f"  · 🚨 Señal técnica de venta (conf. ≥{SELL_CONF_MIN}%)\n"
        f"  · 💡 Oportunidad DCA (caída ≥{DCA_DROP_PCT:.0f}% + RSI<35)\n"
        f"  · 🔒 Trailing stop break-even (beneficio ≥{TRAILING_MIN_PROFIT:.0f}%)\n"
        f"  · ⚡ Alta volatilidad (movimiento ≥{VOLATILITY_PCT:.0f}% en 4h)\n"
        f"  · 💰 Objetivo beneficio ≥{PROFIT_ALERT:.0f}%\n"
        f"  · 🛑 Precio cerca del stop-loss\n\n"
        f"Usa /forzarmonitor para ejecutar ahora.",
        parse_mode="Markdown")

async def cmd_forzar_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not state["portfolio"]:
        await update.message.reply_text(
            "Tu cartera está vacía. Añade criptos con /compra primero.")
        return
    await update.message.reply_text(
        f"🔄 Ejecutando análisis de {len(state['portfolio'])} activos...\n"
        f"_Recibirás el informe en unos segundos._",
        parse_mode="Markdown")
    await do_monitor(ctx.bot)

async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["cancel"] = True
    await update.message.reply_text("🛑 Cancelando en el siguiente paso...")

# ── Callbacks inline ──────────────────────────────────────────────────────────
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

    log.info("Bot v14 arrancado — monitor cada %dh — moneda: %s", MONITOR_HOURS, CURRENCY_SYM)
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
