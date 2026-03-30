"""
CRYPTO BOT PRO v13
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
· Cartera personal (compra / venta / ver)
· Análisis bajo demanda (/analizar)
· Monitor automático cada 4h — avisa cuando vender
· Scanner de mercado (/mercado)
· Sin chat, sin bloqueos, sin background threads
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID         = os.environ.get("CHAT_ID", "")
PORTFOLIO_FILE  = "portfolio.json"
GECKO           = "https://api.coingecko.com/api/v3"
MONITOR_HOURS   = 4          # análisis automático cada 4 horas
SELL_THRESHOLD  = 2          # score mínimo (negativo) para alertar venta
PROFIT_ALERT    = 15.0       # % de ganancia mínima para sugerir toma de beneficios

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Catálogo ──────────────────────────────────────────────────────────────────
CATALOGUE = {}
TOP_IDS   = []

# ── Estado ────────────────────────────────────────────────────────────────────
state = {
    "portfolio": {},     # {coin_id: {units, avg_buy}}
    "cancel":    False,
    "last_monitor": None,
}

def load_state():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                saved = json.load(f)
                state["portfolio"] = saved.get("portfolio", {})
            log.info("Cartera cargada: %d activos", len(state["portfolio"]))
        except Exception as e:
            log.warning("Error cargando cartera: %s", e)

def save_state():
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump({"portfolio": state["portfolio"]}, f, indent=2)
    except Exception as e:
        log.error("Error guardando: %s", e)

# ── HTTP síncrono (siempre en executor) ───────────────────────────────────────
def _get(url, params=None, retries=3):
    import time
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 429:
                wait = 10 * (i + 1)
                log.warning("Rate limit CoinGecko — esperando %ds", wait)
                time.sleep(wait)
                continue
            if r.status_code in (502, 503):
                time.sleep(8); continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            time.sleep(5)
        except Exception as e:
            log.warning("_get intento %d: %s", i+1, e)
            time.sleep(3)
    return None

async def run(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

# ── CoinGecko ─────────────────────────────────────────────────────────────────
def _fetch_prices(coin_ids):
    import time
    if not coin_ids: return {}
    result = {}
    ids = list(dict.fromkeys(coin_ids))
    for i in range(0, len(ids), 50):
        data = _get(f"{GECKO}/simple/price", {
            "ids": ",".join(ids[i:i+50]),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_7d_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
        })
        if data: result.update(data)
        if i + 50 < len(ids): time.sleep(1.5)
    return result

def _fetch_history(coin_id, days=30):
    data = _get(f"{GECKO}/coins/{coin_id}/market_chart",
                {"vs_currency": "usd", "days": days, "interval": "daily"})
    return [p[1] for p in data.get("prices", [])] if data else []

def _fetch_ohlc(coin_id, days=2):
    return _get(f"{GECKO}/coins/{coin_id}/ohlc",
                {"vs_currency": "usd", "days": days}) or []

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
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 100, "page": page, "sparkline": "false",
        })
        if data: ids.extend(c["id"] for c in data)
        time.sleep(1.5)
    TOP_IDS.extend(ids)
    log.info("TOP_IDS: %d", len(TOP_IDS))

def _fetch_top_markets():
    import time
    result = []
    for page in [1, 2]:
        data = _get(f"{GECKO}/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 50, "page": page, "sparkline": "false",
            "price_change_percentage": "24h,7d",
        })
        if data: result.extend(data)
        time.sleep(1.5)
    return result

# ── Catálogo helpers ──────────────────────────────────────────────────────────
def sym(coin_id):
    return CATALOGUE.get(coin_id, {}).get("symbol", coin_id.upper())

def coin_name(coin_id):
    return CATALOGUE.get(coin_id, {}).get("name", coin_id)

def resolve_coin(text):
    u = text.strip().lower()
    if u in CATALOGUE: return u
    by_sym = [c for c, d in CATALOGUE.items() if d["symbol"].lower() == u]
    if by_sym:
        for t in TOP_IDS:
            if t in by_sym: return t
        return by_sym[0]
    by_name = [c for c, d in CATALOGUE.items() if d["name"].lower() == u]
    if by_name: return by_name[0]
    partial = [c for c, d in CATALOGUE.items()
               if u in d["symbol"].lower() or u in d["name"].lower()]
    if partial:
        for t in TOP_IDS:
            if t in partial: return t
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
    if len(prices) < period + 1: return 50.0
    d = [prices[i]-prices[i-1] for i in range(1, len(prices))]
    g = [max(x,0) for x in d[-period:]]
    l = [max(-x,0) for x in d[-period:]]
    ag, al = sum(g)/period, sum(l)/period
    return round(100 - 100/(1 + ag/al), 2) if al else 100.0

def calc_ema(prices, period):
    if not prices: return 0.0
    k, e = 2/(period+1), prices[0]
    for p in prices[1:]: e = p*k + e*(1-k)
    return e

def calc_macd(prices):
    if len(prices) < 26: return 0.0, 0.0
    m = calc_ema(prices, 12) - calc_ema(prices, 26)
    return round(m, 8), round(m*0.85, 8)

def calc_bb(prices, period=20):
    if len(prices) < period:
        p = prices[-1] if prices else 0
        return p, p, p
    w = prices[-period:]
    avg = sum(w)/period
    std = (sum((x-avg)**2 for x in w)/period)**0.5
    return round(avg-2*std,8), round(avg,8), round(avg+2*std,8)

def score_prices(prices, price, chg24, chg7):
    all_p = prices + [price]
    rsi = calc_rsi(all_p)
    ema7  = calc_ema(all_p, 7)
    ema14 = calc_ema(all_p, 14)
    macd_v, macd_s = calc_macd(all_p)
    bb_lo, _, bb_hi = calc_bb(all_p)
    vs7   = (price-ema7) /ema7 *100 if ema7  else 0
    vs14  = (price-ema14)/ema14*100 if ema14 else 0
    bb_pos = (price-bb_lo)/(bb_hi-bb_lo)*100 if (bb_hi-bb_lo) else 50
    score, reasons = 0, []

    if rsi <= 25:   score+=3; reasons.append(f"RSI sobreventa extrema ({rsi})")
    elif rsi <= 35: score+=2; reasons.append(f"RSI sobreventa ({rsi})")
    elif rsi <= 45: score+=1; reasons.append(f"RSI bajo ({rsi})")
    elif rsi >= 75: score-=3; reasons.append(f"RSI sobrecompra extrema ({rsi})")
    elif rsi >= 65: score-=2; reasons.append(f"RSI elevado ({rsi})")
    elif rsi >= 55: score-=1; reasons.append(f"RSI alto ({rsi})")

    if macd_v>0 and macd_v>macd_s:   score+=2; reasons.append("MACD alcista (cruce positivo)")
    elif macd_v>0:                     score+=1; reasons.append("MACD positivo")
    elif macd_v<0 and macd_v<macd_s:  score-=2; reasons.append("MACD bajista (cruce negativo)")
    else:                              score-=1; reasons.append("MACD negativo")

    if vs7 < -5:   score+=2; reasons.append(f"Precio {abs(vs7):.1f}% bajo EMA7")
    elif vs7 < -2: score+=1; reasons.append(f"Bajo EMA7 ({vs7:.1f}%)")
    elif vs7 > 8:  score-=2; reasons.append(f"Extendido sobre EMA7 ({vs7:.1f}%)")
    elif vs7 > 4:  score-=1; reasons.append(f"Alto sobre EMA7 ({vs7:.1f}%)")

    if ema7>ema14: score+=1; reasons.append("EMA7>EMA14 — tendencia alcista")
    else:          score-=1; reasons.append("EMA7<EMA14 — tendencia bajista")

    if bb_pos < 15:  score+=2; reasons.append("Cerca banda inferior Bollinger")
    elif bb_pos > 85: score-=2; reasons.append("Cerca banda superior Bollinger")

    if chg24 < -10:  score+=1; reasons.append(f"Caída 24h ({chg24:.1f}%)")
    elif chg24 > 15: score-=1; reasons.append(f"Subida fuerte 24h ({chg24:.1f}%)")

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
    return min(93, 60 + abs(score)*5)

def _do_full_analysis(coin_id, price_info):
    import time
    price = price_info.get("usd", 0)
    chg24 = price_info.get("usd_24h_change", 0) or 0
    chg7  = price_info.get("usd_7d_change",  0) or 0

    hist  = _fetch_history(coin_id, 30)
    sc_d, reas_d, rsi_d, ema7_d, ema14_d, macd_d, bb_d, vs7_d, vs14_d = \
        score_prices(hist, price, chg24, chg7)
    conf_d = calc_conf(sc_d)
    target = round(price*(1+max(0.05, abs(vs7_d)/100+0.03)), 8)
    sl     = round(price*(1-max(0.04, abs(vs14_d)/100+0.02)), 8)

    time.sleep(0.5)   # pausa entre peticiones

    ohlc = _fetch_ohlc(coin_id, 2)
    sc_4h, reas_4h, conf_4h, sig_4h = 0, [], 50, "⚪ MANTENER"
    if ohlc and len(ohlc) >= 8:
        closes = [c[4] for c in ohlc]
        sc_4h, reas_4h, *_ = score_prices(closes[:-1], closes[-1], chg24, 0)
        conf_4h = calc_conf(sc_4h)
        sig_4h  = signal_label(sc_4h)
        reas_4h = reas_4h[:2]

    return {
        "price": price, "chg24": chg24, "chg7": chg7,
        "signal": signal_label(sc_d), "confidence": conf_d, "score": sc_d,
        "rsi": rsi_d, "ema7": ema7_d, "ema14": ema14_d,
        "bb_pos": bb_d, "target": target, "stop_loss": sl,
        "reasons": reas_d,
        "signal_4h": sig_4h, "confidence_4h": conf_4h, "score_4h": sc_4h,
        "reasons_4h": reas_4h,
    }

# ── Formato ───────────────────────────────────────────────────────────────────
def fp(p):
    if not p: return "$0"
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    if p >= 0.01: return f"${p:.6f}"
    return f"${p:.8f}"

def pc(v):
    return f"{'🟢 +' if v>=0 else '🔴 '}{v:.2f}%"

def build_analysis_msg(coin_id, a, holding=None):
    price = a["price"]
    lines = [
        f"📊 *{sym(coin_id)} — {coin_name(coin_id)}*",
        f"💰 *{fp(price)}*   24h: {pc(a['chg24'])}   7d: {pc(a['chg7'])}",
        "",
        "┌─ AHORA ──────────────────────────",
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
        cur = price*units; inv = avg*units; prf = cur-inv
        pnl = (prf/inv*100) if inv else 0
        lines += [
            "",
            "┌─ TU POSICIÓN ────────────────────",
            f"│ {units} uds · Compra media: {fp(avg)}",
            f"│ Valor actual: {fp(cur)}",
            f"└ {'💰' if prf>=0 else '📉'} P&L: {fp(prf)} ({pnl:+.2f}%)",
        ]
    lines.append(f"\n_🕐 {datetime.now().strftime('%d/%m %H:%M')}_")
    return "\n".join(lines)

def build_sell_alert(coin_id, a, holding, reason):
    """Mensaje de alerta de venta para el monitor automático."""
    price = a["price"]
    units, avg = holding["units"], holding["avg_buy"]
    cur = price*units; inv = avg*units; prf = cur-inv
    pnl = (prf/inv*100) if inv else 0

    lines = [
        f"🚨 *ALERTA — {sym(coin_id)}*",
        f"_{reason}_",
        "",
        f"💰 Precio actual: *{fp(price)}*",
        f"📉 24h: {pc(a['chg24'])}",
        "",
        f"Señal diaria: {a['signal']} ({a['confidence']}%)",
        f"Señal 4h: {a['signal_4h']} ({a['confidence_4h']}%)",
        f"RSI: `{a['rsi']}`",
        "",
        "Razones principales:",
    ]
    for r in a["reasons"][:3]:
        lines.append(f"  · {r}")
    lines += [
        "",
        "Tu posición:",
        f"  {units} uds · Compra: {fp(avg)}",
        f"  {'💰' if prf>=0 else '📉'} P&L: {fp(prf)} ({pnl:+.2f}%)",
        "",
        f"_Monitor automático — {datetime.now().strftime('%d/%m %H:%M')}_",
    ]
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# MONITOR AUTOMÁTICO CADA 4H
# ══════════════════════════════════════════════════════════════════════════════
async def monitor_loop(app):
    """Corre en background. Analiza la cartera cada 4h y manda alertas de venta."""
    await asyncio.sleep(30)   # esperar a que el bot arranque del todo
    log.info("Monitor 4h iniciado")

    while True:
        try:
            await do_monitor(app.bot)
        except Exception as e:
            log.error("Error en monitor: %s", e)

        # Esperar exactamente MONITOR_HOURS horas
        log.info("Próximo monitor en %dh", MONITOR_HOURS)
        await asyncio.sleep(MONITOR_HOURS * 3600)

async def do_monitor(bot):
    portfolio = state["portfolio"]
    if not portfolio:
        log.info("Monitor: cartera vacía, nada que analizar")
        return
    if not CHAT_ID:
        log.warning("Monitor: CHAT_ID no configurado")
        return

    log.info("Monitor iniciando análisis de %d activos...", len(portfolio))
    state["last_monitor"] = datetime.now().strftime("%d/%m %H:%M")

    prices_data = await run(_fetch_prices, list(portfolio.keys()))
    alerts_sent = 0

    for coin_id, pos in portfolio.items():
        info = prices_data.get(coin_id)
        if not info:
            log.warning("Monitor: sin precio para %s", coin_id)
            continue

        price = info.get("usd", 0)
        if not price:
            continue

        # Análisis completo
        a = await run(_do_full_analysis, coin_id, info)

        units, avg = pos["units"], pos["avg_buy"]
        pnl_pct = ((price/avg)-1)*100 if avg else 0

        alert_reason = None

        # ── Criterios de alerta de venta ─────────────────────────────────────

        # 1. Señal técnica fuerte de venta
        if a["score"] <= -3 and a["confidence"] >= 68:
            alert_reason = "Señales técnicas indican momento de venta"

        # 2. Señal de venta diaria + 4h coinciden
        elif a["score"] <= -2 and a["score_4h"] <= -2:
            alert_reason = "Señal bajista confirmada en timeframe diario y 4h"

        # 3. RSI sobrecompra extrema con beneficio
        elif a["rsi"] >= 75 and pnl_pct >= 10:
            alert_reason = f"RSI en sobrecompra ({a['rsi']}) con +{pnl_pct:.1f}% de beneficio — considera tomar ganancias"

        # 4. Toma de beneficios: ganancia alta + señal neutral o bajista
        elif pnl_pct >= PROFIT_ALERT and a["score"] <= 0:
            alert_reason = f"Beneficio de +{pnl_pct:.1f}% alcanzado — buen momento para tomar ganancias"

        # 5. Stop-loss: precio cerca del stop sugerido
        elif price <= a["stop_loss"] * 1.02:
            alert_reason = f"Precio cerca del stop-loss sugerido ({fp(a['stop_loss'])})"

        if alert_reason:
            msg_text = build_sell_alert(coin_id, a, pos, alert_reason)
            try:
                await bot.send_message(
                    chat_id    = CHAT_ID,
                    text       = msg_text,
                    parse_mode = "Markdown",
                )
                alerts_sent += 1
                log.info("Alerta enviada: %s — %s", sym(coin_id), alert_reason)
            except Exception as e:
                log.error("Error enviando alerta %s: %s", coin_id, e)

        await asyncio.sleep(1)   # pausa entre monedas

    # Resumen del monitor (aunque no haya alertas)
    now = datetime.now().strftime("%d/%m %H:%M")
    if alerts_sent == 0:
        summary = (
            f"🔄 *Monitor 4h — {now}*\n\n"
            f"Revisados {len(portfolio)} activos.\n"
            f"✅ Sin señales de venta en este momento. Tu cartera está estable."
        )
        try:
            await bot.send_message(chat_id=CHAT_ID, text=summary, parse_mode="Markdown")
        except Exception as e:
            log.error("Error enviando resumen: %s", e)
    else:
        log.info("Monitor completado: %d alertas enviadas", alerts_sent)

# ══════════════════════════════════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════════════════════════════════
HELP = (
    "👋 *Crypto Bot Pro — Comandos*\n\n"
    "📦 *CARTERA*\n"
    "  /compra BTC 0\\.5 — Registrar compra\n"
    "  /compra BTC 0\\.5 65000 — Con precio manual\n"
    "  /venta ETH 1\\.2 — Registrar venta\n"
    "  /cartera — Ver cartera con P\\&L\n"
    "  /precio BTC — Precio actual\n"
    "  /buscar PEPE — Buscar cualquier cripto\n\n"
    "📊 *ANÁLISIS*\n"
    "  /analizar BTC — Análisis diario \\+ 4h\n"
    "  /analizar — Analizar toda tu cartera\n\n"
    "🔍 *MERCADO*\n"
    "  /mercado — Top 100 oportunidades ahora\n\n"
    "🔔 *MONITOR AUTOMÁTICO*\n"
    "  /monitor — Ver estado del monitor \\(cada 4h\\)\n"
    "  /forzarmonitor — Ejecutar análisis ahora\n\n"
    "⚙️ /cancelar — Parar comando en curso\n"
)

async def cmd_start(u, c): await u.message.reply_text(HELP, parse_mode="MarkdownV2")
async def cmd_ayuda(u, c): await u.message.reply_text(HELP, parse_mode="MarkdownV2")

# ── Cartera ───────────────────────────────────────────────────────────────────
async def cmd_cartera(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["portfolio"]
    if not p:
        await update.message.reply_text("Tu cartera está vacía.\n\nUsa /compra BTC 0.5 para añadir.")
        return
    msg  = await update.message.reply_text("🔄 Obteniendo precios...")
    data = await run(_fetch_prices, list(p.keys()))
    ti = tc = 0
    lines = ["💼 *Tu Cartera*\n"]
    for cid, pos in p.items():
        units, avg = pos["units"], pos["avg_buy"]
        info  = data.get(cid, {})
        price = info.get("usd", 0)
        chg24 = info.get("usd_24h_change", 0) or 0
        inv, cur = avg*units, price*units
        prf = cur-inv; pnl = (prf/inv*100) if inv else 0
        ti += inv; tc += cur
        lines.append(
            f"{'🟢' if prf>=0 else '🔴'} *{sym(cid)}*\n"
            f"  {units} uds · Compra: {fp(avg)} · Ahora: {fp(price)}\n"
            f"  24h: {pc(chg24)}\n"
            f"  Valor: {fp(cur)} · P&L: {fp(prf)} ({pnl:+.2f}%)\n"
        )
    tp = tc-ti; tpct = (tp/ti*100) if ti else 0
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
            "Uso: `/compra BTC 0.5` o `/compra BTC 0.5 65000`", parse_mode="Markdown")
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
            await update.message.reply_text("❌ El precio debe ser un número."); return
    else:
        msg  = await update.message.reply_text("🔄 Obteniendo precio de mercado...")
        info = await run(_fetch_prices, [coin_id])
        if not info or coin_id not in info:
            await msg.edit_text(
                f"⚠️ No pude obtener el precio de *{sym(coin_id)}*.\n"
                f"Indícalo manualmente: `/compra {sym(coin_id)} {units} <precio>`",
                parse_mode="Markdown")
            return
        buy_price = info[coin_id]["usd"]
        await msg.delete()
    p = state["portfolio"]
    if coin_id in p:
        ou, oa = p[coin_id]["units"], p[coin_id]["avg_buy"]
        nu = ou+units; na = (ou*oa+units*buy_price)/nu
        p[coin_id] = {"units": round(nu,8), "avg_buy": round(na,8)}
        extra = "Posición ampliada."
    else:
        p[coin_id] = {"units": round(units,8), "avg_buy": round(buy_price,8)}
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
        await update.message.reply_text("Uso: `/venta BTC 0.5`", parse_mode="Markdown"); return
    coin_id = resolve_coin(args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{args[0]}'."); return
    try:
        units = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ La cantidad debe ser un número."); return
    p = state["portfolio"]
    if coin_id not in p:
        await update.message.reply_text(
            f"⚠️ No tienes *{sym(coin_id)}* en cartera.", parse_mode="Markdown"); return
    pos  = p[coin_id]
    info = await run(_fetch_prices, [coin_id])
    sell = info.get(coin_id, {}).get("usd", pos["avg_buy"])
    prf  = (sell-pos["avg_buy"])*units
    pnl  = ((sell/pos["avg_buy"])-1)*100 if pos["avg_buy"] else 0
    if units >= pos["units"]:
        del p[coin_id]; remaining = 0
    else:
        p[coin_id]["units"] = round(pos["units"]-units, 8)
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
        await update.message.reply_text("Uso: `/precio BTC`", parse_mode="Markdown"); return
    coin_id = resolve_coin(ctx.args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{ctx.args[0]}'. Prueba /buscar {ctx.args[0]}"); return
    msg  = await update.message.reply_text("🔄 Obteniendo precio...")
    info = await run(_fetch_prices, [coin_id])
    if not info or coin_id not in info:
        await msg.edit_text("⚠️ No pude obtener el precio ahora mismo. Espera 30s e intenta de nuevo."); return
    d = info[coin_id]
    await msg.edit_text(
        f"💰 *{sym(coin_id)} — {coin_name(coin_id)}*\n\n"
        f"  Precio: *{fp(d['usd'])}*\n"
        f"  24h: {pc(d.get('usd_24h_change',0) or 0)}\n"
        f"  7d:  {pc(d.get('usd_7d_change',0) or 0)}\n"
        f"  Vol 24h: ${(d.get('usd_24h_vol',0) or 0)/1e9:.3f}B\n"
        f"  Mkt cap: ${(d.get('usd_market_cap',0) or 0)/1e9:.2f}B",
        parse_mode="Markdown")

async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: `/buscar PEPE`", parse_mode="Markdown"); return
    query   = " ".join(ctx.args)
    msg     = await update.message.reply_text(f"🔍 Buscando '{query}'...")
    coin_id = await run(resolve_coin, query)
    if not coin_id:
        await msg.edit_text(f"❌ No encontré ninguna cripto con '{query}'."); return
    info = await run(_fetch_prices, [coin_id])
    if not info or coin_id not in info:
        await msg.edit_text(
            f"✅ Encontrada: *{coin_name(coin_id)}* ({sym(coin_id)})\n"
            f"⚠️ Sin datos de precio disponibles.", parse_mode="Markdown"); return
    d = info[coin_id]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📊 Analizar {sym(coin_id)}", callback_data=f"analyse:{coin_id}"),
        InlineKeyboardButton("📥 Registrar compra",         callback_data=f"buy_prompt:{coin_id}"),
    ]])
    await msg.edit_text(
        f"✅ *{coin_name(coin_id)}* ({sym(coin_id)})\n\n"
        f"  Precio: *{fp(d['usd'])}*\n"
        f"  24h: {pc(d.get('usd_24h_change',0) or 0)}\n"
        f"  7d:  {pc(d.get('usd_7d_change',0) or 0)}\n"
        f"  Vol 24h: ${(d.get('usd_24h_vol',0) or 0)/1e9:.3f}B\n"
        f"  Mkt cap: ${(d.get('usd_market_cap',0) or 0)/1e9:.2f}B",
        parse_mode="Markdown", reply_markup=kb)

# ── Analizar ──────────────────────────────────────────────────────────────────
async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["portfolio"]
    if ctx.args:
        coin_id = resolve_coin(ctx.args[0])
        if not coin_id:
            await update.message.reply_text(f"❌ No reconozco '{ctx.args[0]}'. Prueba /buscar {ctx.args[0]}"); return
        msg  = await update.message.reply_text(f"🔍 Analizando *{sym(coin_id)}*...", parse_mode="Markdown")
        info = await run(_fetch_prices, [coin_id])
        if not info or coin_id not in info:
            await msg.edit_text("⚠️ No pude obtener datos ahora. Espera 30s e intenta de nuevo."); return
        a = await run(_do_full_analysis, coin_id, info[coin_id])
        await msg.edit_text(build_analysis_msg(coin_id, a, holding=p.get(coin_id)), parse_mode="Markdown")
        return
    if not p:
        await update.message.reply_text(
            "Tu cartera está vacía.\n\nUsa /compra BTC 0.5 para añadir\no /mercado para buscar oportunidades."); return
    n   = len(p)
    msg = await update.message.reply_text(
        f"🔍 Analizando {n} activo{'s' if n>1 else ''} de tu cartera...\n_Escribe /cancelar para parar._",
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
        await update.message.reply_text(build_analysis_msg(cid, a, holding=pos), parse_mode="Markdown")
    await msg.edit_text(
        f"✅ Análisis completado — {n} activo{'s' if n>1 else ''}.\n_🕐 {datetime.now().strftime('%H:%M')}_",
        parse_mode="Markdown")

# ── Mercado ───────────────────────────────────────────────────────────────────
async def cmd_mercado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "🔍 *Analizando el top 100 del mercado...*\n_~30 segundos._",
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
    lines = [f"🔍 *Scanner de Mercado — {datetime.now().strftime('%H:%M')}*\n"]
    if buy_list:
        lines.append("🟢 *POSIBLES OPORTUNIDADES DE COMPRA*\n")
        for x in buy_list[:10]:
            tag = " 📦" if x["id"] in state["portfolio"] else ""
            lines.append(
                f"  *{x['symbol']}{tag}* — {fp(x['price'])}\n"
                f"    24h: {pc(x['chg24'])}  7d: {pc(x['chg7'])}\n"
                f"    Vol: ${x['vol']/1e6:.1f}M  MCap: ${x['mcap']/1e9:.2f}B\n")
    if avoid:
        lines.append("🔴 *PRECAUCIÓN* (subidas fuertes)\n")
        for x in avoid[:5]:
            tag = " 📦" if x["id"] in state["portfolio"] else ""
            lines.append(f"  *{x['symbol']}{tag}* — {fp(x['price'])} — 24h: {pc(x['chg24'])}")
    lines += ["", "💡 Análisis completo: `/analizar BTC`",
              "_📦 = ya tienes en cartera_",
              f"_Escaneadas: {len(scored)} monedas_"]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

# ── Monitor ───────────────────────────────────────────────────────────────────
async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    last = state.get("last_monitor") or "Todavía no se ha ejecutado"
    await update.message.reply_text(
        f"🔔 *Monitor automático*\n\n"
        f"  Frecuencia: cada *{MONITOR_HOURS} horas*\n"
        f"  Último análisis: `{last}`\n"
        f"  Activos vigilados: `{len(state['portfolio'])}`\n\n"
        f"El bot analiza tu cartera automáticamente y te avisa si detecta:\n"
        f"  · Señales técnicas de venta\n"
        f"  · Beneficios elevados + señal bajista\n"
        f"  · RSI en sobrecompra con ganancia\n"
        f"  · Precio cerca del stop-loss\n\n"
        f"Usa /forzarmonitor para ejecutarlo ahora.",
        parse_mode="Markdown")

async def cmd_forzar_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not state["portfolio"]:
        await update.message.reply_text("Tu cartera está vacía. Añade criptos con /compra primero.")
        return
    await update.message.reply_text(
        f"🔄 Ejecutando análisis de {len(state['portfolio'])} activos...\n"
        f"_Recibirás las alertas en unos segundos._",
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
            await msg.edit_text("⚠️ No pude obtener datos ahora mismo."); return
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
    log.info("Catálogo listo.")
    asyncio.create_task(monitor_loop(app))

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no configurado")

    load_state()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(startup).build()

    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("ayuda",          cmd_ayuda))
    app.add_handler(CommandHandler("cartera",        cmd_cartera))
    app.add_handler(CommandHandler("compra",         cmd_compra))
    app.add_handler(CommandHandler("venta",          cmd_venta))
    app.add_handler(CommandHandler("precio",         cmd_precio))
    app.add_handler(CommandHandler("buscar",         cmd_buscar))
    app.add_handler(CommandHandler("analizar",       cmd_analizar))
    app.add_handler(CommandHandler("mercado",        cmd_mercado))
    app.add_handler(CommandHandler("monitor",        cmd_monitor))
    app.add_handler(CommandHandler("forzarmonitor",  cmd_forzar_monitor))
    app.add_handler(CommandHandler("cancelar",       cmd_cancelar))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_handler))

    log.info("Bot v13 arrancado — monitor cada %dh", MONITOR_HOURS)
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
