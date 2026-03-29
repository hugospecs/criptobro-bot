"""
CRYPTO BOT PRO v5
- Cartera personal
- Análisis bajo demanda (sin monitor en background)
- Análisis de mercado completo
- Chat IA
"""

import os, json, time, logging, requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY", "")
PORTFOLIO_FILE = "portfolio.json"
GECKO          = "https://api.coingecko.com/api/v3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Catálogo dinámico ─────────────────────────────────────────────────────────
CATALOGUE   = {}   # coin_id -> {symbol, name}
TOP_IDS     = []   # top 250 por market cap
_cat_loaded = False

def load_catalogue():
    global _cat_loaded
    if _cat_loaded:
        return
    log.info("Cargando catálogo CoinGecko...")
    try:
        r = requests.get(f"{GECKO}/coins/list", params={"include_platform": "false"}, timeout=30)
        r.raise_for_status()
        for c in r.json():
            CATALOGUE[c["id"]] = {"symbol": c["symbol"].upper(), "name": c["name"]}
        log.info("Catálogo: %d monedas", len(CATALOGUE))
    except Exception as e:
        log.error("Error catálogo: %s", e)

    try:
        ids = []
        for page in [1, 2, 3]:
            r = requests.get(
                f"{GECKO}/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 100, "page": page, "sparkline": "false"},
                timeout=20,
            )
            r.raise_for_status()
            ids.extend(c["id"] for c in r.json())
            time.sleep(1.5)
        TOP_IDS.extend(ids)
        log.info("Top IDs: %d", len(TOP_IDS))
    except Exception as e:
        log.error("Error top IDs: %s", e)

    _cat_loaded = True

def sym(coin_id):
    return CATALOGUE.get(coin_id, {}).get("symbol", coin_id.upper())

def coin_name(coin_id):
    return CATALOGUE.get(coin_id, {}).get("name", coin_id)

def resolve_coin(text):
    """Símbolo, nombre o ID → coin_id. Prioriza mayor market cap si hay ambigüedad."""
    u = text.strip().lower()
    if u in CATALOGUE:
        return u
    # exact symbol
    by_sym = [cid for cid, d in CATALOGUE.items() if d["symbol"].lower() == u]
    if by_sym:
        for tid in TOP_IDS:
            if tid in by_sym:
                return tid
        return by_sym[0]
    # exact name
    by_name = [cid for cid, d in CATALOGUE.items() if d["name"].lower() == u]
    if by_name:
        return by_name[0]
    # partial
    partial = [cid for cid, d in CATALOGUE.items()
               if u in d["symbol"].lower() or u in d["name"].lower()]
    if partial:
        for tid in TOP_IDS:
            if tid in partial:
                return tid
        return partial[0]
    # CoinGecko search API
    try:
        r = requests.get(f"{GECKO}/search", params={"query": text}, timeout=10)
        r.raise_for_status()
        hits = r.json().get("coins", [])
        if hits:
            cid = hits[0]["id"]
            if cid not in CATALOGUE:
                CATALOGUE[cid] = {"symbol": hits[0]["symbol"].upper(), "name": hits[0]["name"]}
            return cid
    except Exception:
        pass
    return None

# ── Estado ────────────────────────────────────────────────────────────────────
state = {
    "portfolio":    {},   # {coin_id: {units, avg_buy}}
    "chat_history": [],
}

def load_state():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                saved = json.load(f)
                if "portfolio" in saved:
                    state["portfolio"] = saved["portfolio"]
            log.info("Cartera cargada: %d activos", len(state["portfolio"]))
        except Exception as e:
            log.warning("No se pudo cargar cartera: %s", e)

def save_state():
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump({"portfolio": state["portfolio"]}, f, indent=2)
    except Exception as e:
        log.error("Error guardando cartera: %s", e)

# ── API CoinGecko con reintentos ──────────────────────────────────────────────
def _get(url, params, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 429:
                wait = 12 * (i + 1)
                log.warning("Rate limit — esperando %ds", wait)
                time.sleep(wait)
                continue
            if r.status_code in (502, 503):
                time.sleep(8)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            log.warning("Timeout intento %d", i+1)
            time.sleep(5)
        except Exception as e:
            log.error("_get error: %s", e)
            time.sleep(3)
    return None

def get_prices(coin_ids):
    if not coin_ids:
        return {}
    result = {}
    ids = list(dict.fromkeys(coin_ids))
    for i in range(0, len(ids), 50):
        batch = ids[i:i+50]
        data  = _get(f"{GECKO}/simple/price", {
            "ids": ",".join(batch), "vs_currencies": "usd",
            "include_24hr_change": "true", "include_7d_change": "true",
            "include_24hr_vol": "true", "include_market_cap": "true",
        })
        if data:
            result.update(data)
        if i + 50 < len(ids):
            time.sleep(2)
    return result

def get_ohlc(coin_id, days=2):
    """OHLC de las últimas horas — para análisis de 4h."""
    data = _get(f"{GECKO}/coins/{coin_id}/ohlc",
                {"vs_currency": "usd", "days": days})
    return data or []

def get_history(coin_id, days=30):
    data = _get(f"{GECKO}/coins/{coin_id}/market_chart",
                {"vs_currency": "usd", "days": days, "interval": "daily"})
    if data:
        return [p[1] for p in data.get("prices", [])]
    return []

# ── Indicadores técnicos ──────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    g = [max(d, 0) for d in deltas[-period:]]
    l = [max(-d, 0) for d in deltas[-period:]]
    ag, al = sum(g)/period, sum(l)/period
    return round(100 - 100/(1 + ag/al), 2) if al else 100.0

def calc_ema(prices, period):
    if not prices:
        return 0.0
    k, ema = 2/(period+1), prices[0]
    for p in prices[1:]:
        ema = p*k + ema*(1-k)
    return ema

def calc_macd(prices):
    if len(prices) < 26:
        return 0.0, 0.0
    m = calc_ema(prices, 12) - calc_ema(prices, 26)
    return round(m, 8), round(m * 0.85, 8)

def calc_bb(prices, period=20):
    if len(prices) < period:
        p = prices[-1] if prices else 0
        return p, p, p
    w   = prices[-period:]
    avg = sum(w) / period
    std = (sum((x-avg)**2 for x in w) / period) ** 0.5
    return round(avg-2*std, 8), round(avg, 8), round(avg+2*std, 8)

def score_prices(prices, price, chg24, chg7):
    """Calcula score técnico y razones a partir de una lista de precios."""
    all_p  = prices + [price]
    rsi    = calc_rsi(all_p)
    ema7   = calc_ema(all_p, 7)
    ema14  = calc_ema(all_p, 14)
    macd_v, macd_s = calc_macd(all_p)
    bb_lo, bb_mid, bb_hi = calc_bb(all_p)
    vs7    = (price - ema7)  / ema7  * 100 if ema7  else 0
    vs14   = (price - ema14) / ema14 * 100 if ema14 else 0
    bb_pos = (price - bb_lo) / (bb_hi - bb_lo) * 100 if (bb_hi - bb_lo) else 50

    score, reasons = 0, []

    if rsi <= 25:   score += 3; reasons.append(f"RSI sobreventa extrema ({rsi})")
    elif rsi <= 35: score += 2; reasons.append(f"RSI sobreventa ({rsi}) — zona de compra")
    elif rsi <= 45: score += 1; reasons.append(f"RSI bajo ({rsi})")
    elif rsi >= 75: score -= 3; reasons.append(f"RSI sobrecompra extrema ({rsi})")
    elif rsi >= 65: score -= 2; reasons.append(f"RSI elevado ({rsi})")
    elif rsi >= 55: score -= 1; reasons.append(f"RSI moderado-alto ({rsi})")

    if macd_v > 0 and macd_v > macd_s: score += 2; reasons.append("MACD alcista (cruce positivo)")
    elif macd_v > 0:                    score += 1; reasons.append("MACD positivo")
    elif macd_v < 0 and macd_v < macd_s: score -= 2; reasons.append("MACD bajista (cruce negativo)")
    else:                               score -= 1; reasons.append("MACD negativo")

    if vs7 < -5:   score += 2; reasons.append(f"Precio {abs(vs7):.1f}% bajo EMA7 — posible suelo")
    elif vs7 < -2: score += 1; reasons.append(f"Precio bajo EMA7 ({vs7:.1f}%)")
    elif vs7 > 8:  score -= 2; reasons.append(f"Precio extendido sobre EMA7 ({vs7:.1f}%)")
    elif vs7 > 4:  score -= 1; reasons.append(f"Precio algo alto sobre EMA7 ({vs7:.1f}%)")

    if ema7 > ema14: score += 1; reasons.append("EMA7 > EMA14 — tendencia alcista")
    else:            score -= 1; reasons.append("EMA7 < EMA14 — tendencia bajista")

    if bb_pos < 15:  score += 2; reasons.append("Cerca de banda inferior Bollinger — rebote probable")
    elif bb_pos > 85: score -= 2; reasons.append("Cerca de banda superior Bollinger — posible techo")

    if chg24 < -10:  score += 1; reasons.append(f"Caída 24h ({chg24:.1f}%) — posible sobrerreacción")
    elif chg24 > 15: score -= 1; reasons.append(f"Subida fuerte 24h ({chg24:.1f}%) — considerar toma de beneficios")

    return score, reasons, rsi, ema7, ema14, macd_v, bb_pos, vs7, vs14

def signal_label(score, confidence):
    if score >= 5:   return "🟢 COMPRAR FUERTE"
    if score >= 3:   return "🟢 COMPRAR"
    if score >= 1:   return "🟡 POSIBLE COMPRA"
    if score <= -5:  return "🔴 VENDER FUERTE"
    if score <= -3:  return "🔴 VENDER"
    if score <= -1:  return "🟠 POSIBLE VENTA"
    return "⚪ ESPERAR / MANTENER"

def calc_confidence(score):
    return min(93, 60 + abs(score) * 5)

# ── Formateo ──────────────────────────────────────────────────────────────────
def fp(p):
    if not p:     return "$0"
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    if p >= 0.01: return f"${p:.6f}"
    return f"${p:.8f}"

def pc(v):
    return f"{'🟢 +' if v >= 0 else '🔴 '}{v:.2f}%"

# ── Análisis completo de una cripto (diario + 4h) ─────────────────────────────
def full_analysis(coin_id, price_info):
    """
    Devuelve dict con:
    - Análisis diario (30 días)
    - Análisis 4h (OHLC últimas 48h)
    """
    price = price_info.get("usd", 0)
    chg24 = price_info.get("usd_24h_change", 0) or 0
    chg7  = price_info.get("usd_7d_change",  0) or 0
    vol   = price_info.get("usd_24h_vol",    0) or 0
    mcap  = price_info.get("usd_market_cap", 0) or 0

    # ── Análisis diario ───────────────────────────────────────────────────────
    hist_daily = get_history(coin_id, 30)
    score_d, reasons_d, rsi_d, ema7_d, ema14_d, macd_d, bb_pos_d, vs7_d, vs14_d = \
        score_prices(hist_daily, price, chg24, chg7)
    conf_d  = calc_confidence(score_d)
    sig_d   = signal_label(score_d, conf_d)
    target  = round(price * (1 + max(0.05, abs(vs7_d)/100 + 0.03)), 8)
    sl      = round(price * (1 - max(0.04, abs(vs14_d)/100 + 0.02)), 8)

    # ── Análisis 4h (OHLC) ────────────────────────────────────────────────────
    ohlc = get_ohlc(coin_id, days=2)
    sig_4h, reasons_4h, conf_4h = "⚪ ESPERAR", [], 50
    if ohlc and len(ohlc) >= 8:
        closes_4h = [c[4] for c in ohlc]   # índice 4 = close en OHLC de CoinGecko
        score_4h, reasons_4h, *_ = score_prices(closes_4h[:-1], closes_4h[-1], chg24, 0)
        conf_4h = calc_confidence(score_4h)
        sig_4h  = signal_label(score_4h, conf_4h)
        reasons_4h = reasons_4h[:3]   # solo top 3 razones para no saturar

    return {
        "price": price, "chg24": chg24, "chg7": chg7, "vol": vol, "mcap": mcap,
        # diario
        "signal":     sig_d,   "confidence":  conf_d,  "score": score_d,
        "rsi":        rsi_d,   "ema7":        ema7_d,  "ema14": ema14_d,
        "macd":       macd_d,  "bb_pos":      bb_pos_d,
        "target":     target,  "stop_loss":   sl,
        "reasons":    reasons_d,
        # 4h
        "signal_4h":   sig_4h,  "confidence_4h": conf_4h,
        "reasons_4h":  reasons_4h,
    }

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
        "│ Razones principales:",
    ]
    for r in a["reasons"][:4]:
        lines.append(f"│  · {r}")

    lines += [
        "│",
        "└─ PREDICCIÓN 4H ──────────────────",
        f"  {a['signal_4h']}",
        f"  Confianza: {a['confidence_4h']}%",
    ]
    for r in a["reasons_4h"][:2]:
        lines.append(f"  · {r}")

    if holding:
        units   = holding["units"]
        avg_buy = holding["avg_buy"]
        current = price * units
        inv     = avg_buy * units
        profit  = current - inv
        pnl_pct = (profit / inv * 100) if inv else 0
        lines += [
            "",
            "┌─ TU POSICIÓN ────────────────────",
            f"│ {units} uds · Compra media: {fp(avg_buy)}",
            f"│ Valor: {fp(current)}",
            f"└ {'💰' if profit >= 0 else '📉'} P&L: {fp(profit)} ({pnl_pct:+.2f}%)",
        ]

    lines.append(f"\n_🕐 {datetime.now().strftime('%d/%m %H:%M')}_")
    return "\n".join(l for l in lines if l is not None)

async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["cancel"] = True
    await update.message.reply_text("🛑 Cancelando... el comando actual se detendrá en el siguiente paso.")

# ── Chat IA ───────────────────────────────────────────────────────────────────
def portfolio_ctx():
    p = state["portfolio"]
    if not p:
        return "Cartera vacía."
    return "\n".join(
        f"{sym(cid)}: {d['units']} uds @ precio medio {fp(d['avg_buy'])}"
        for cid, d in p.items()
    )

def ask_claude(user_msg):
    """Chat IA usando Google Gemini (tier gratuito, sin tarjeta)."""
    gemini_key = os.environ.get("GEMINI_KEY", "")
    if not gemini_key:
        return (
            "⚠️ *Chat IA no disponible*\n\n"
            "Añade la variable `GEMINI_KEY` en Railway con tu API key de Google AI Studio.\n"
            "Consíguela gratis en: *aistudio.google.com* → Get API Key\n"
            "_(No requiere tarjeta de crédito)_"
        )

    # Limpiar y validar historial (Gemini requiere alternancia user/model)
    clean = [
        m for m in state["chat_history"]
        if isinstance(m, dict)
        and m.get("role") in ("user", "model")
        and isinstance(m.get("parts"), list)
        and m["parts"]
        and isinstance(m["parts"][0].get("text"), str)
        and m["parts"][0]["text"].strip()
    ]
    # Garantizar alternancia correcta
    valid = []
    last_role = None
    for m in clean[-12:]:
        if m["role"] != last_role:
            valid.append(m)
            last_role = m["role"]
    if valid and valid[-1]["role"] == "model":
        valid = valid[:-1]

    system_text = (
        "Eres un experto en trading de criptomonedas. Respondes en español, "
        "usando formato Telegram (*negrita*, _cursiva_). Eres claro, honesto y siempre "
        "adviertes que el trading conlleva riesgo.\n"
        f"Cartera actual del usuario:\n{portfolio_ctx()}"
    )

    # Gemini REST API
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key.strip()}"
    payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": valid + [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.7},
    }

    try:
        r = requests.post(url, json=payload, timeout=30)

        if not r.ok:
            body = r.json() if "application/json" in r.headers.get("content-type", "") else {}
            err  = body.get("error", {}).get("message", r.text)
            log.error("Gemini API %d: %s", r.status_code, err)
            if r.status_code == 400:
                return f"❌ Error Gemini 400: `{err}`"
            if r.status_code == 403:
                return "❌ *API Key inválida* — Comprueba que la GEMINI\\_KEY en Railway es correcta."
            if r.status_code == 429:
                return "⚠️ Límite de peticiones alcanzado. Espera 1 minuto e inténtalo de nuevo."
            return f"❌ Error Gemini ({r.status_code}): `{err}`"

        data    = r.json()
        reply   = data["candidates"][0]["content"]["parts"][0]["text"]

        # Guardar en historial con formato Gemini (role: user/model)
        state["chat_history"].append({"role": "user",  "parts": [{"text": user_msg}]})
        state["chat_history"].append({"role": "model", "parts": [{"text": reply}]})
        if len(state["chat_history"]) > 40:
            state["chat_history"] = state["chat_history"][-40:]
        return reply

    except requests.exceptions.Timeout:
        return "⚠️ Gemini tardó demasiado. Inténtalo de nuevo."
    except Exception as e:
        log.error("Gemini excepción: %s", e)
        return f"❌ Error inesperado: {e}"

# ══════════════════════════════════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════════════════════════════════

HELP = (
    "👋 *Crypto Bot Pro — Comandos*\n\n"
    "📦 *CARTERA*\n"
    "  /compra BTC 0\\.5 — Registrar compra\n"
    "  /compra BTC 0\\.5 65000 — Con precio manual\n"
    "  /venta ETH 1\\.2 — Registrar venta\n"
    "  /cartera — Ver cartera con P\\&L actual\n"
    "  /precio BTC — Precio en tiempo real\n"
    "  /buscar PEPE — Buscar cualquier cripto\n\n"
    "📊 *ANÁLISIS*\n"
    "  /analizar BTC — Análisis diario \\+ 4h de una cripto\n"
    "  /analizar — Análisis de toda tu cartera\n\n"
    "🔍 *MERCADO*\n"
    "  /mercado — Escanea el top 100 y ve qué comprar\n\n"
    "💬 *CHAT IA*\n"
    "  /chat ¿Vendo mi SOL? — Pregunta lo que quieras\n"
    "  Escribe sin / para chatear directamente\n"
    "  /resetChat — Borrar historial\n"
    "  _\\(Requiere GEMINI\\_KEY en Railway\\)_\n\n"
    "⚙️ *OTROS*\n"
    "  /cancelar — Para cualquier comando en curso\n"
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
    data = get_prices(list(p.keys()))

    ti = tc = 0
    lines = ["💼 *Tu Cartera*\n"]
    for cid, pos in p.items():
        units, avg = pos["units"], pos["avg_buy"]
        info  = data.get(cid, {})
        price = info.get("usd", 0)
        chg24 = info.get("usd_24h_change", 0) or 0
        inv   = avg * units
        cur   = price * units
        prf   = cur - inv
        pnl   = (prf / inv * 100) if inv else 0
        ti   += inv; tc += cur
        lines.append(
            f"{'🟢' if prf >= 0 else '🔴'} *{sym(cid)}*\n"
            f"  {units} uds · Compra: {fp(avg)} · Ahora: {fp(price)}\n"
            f"  24h: {pc(chg24)}\n"
            f"  Valor: {fp(cur)} · P&L: {fp(prf)} ({pnl:+.2f}%)\n"
        )
    tp   = tc - ti
    tpct = (tp / ti * 100) if ti else 0
    lines += [
        "━━━━━━━━━━━━━━━━",
        f"{'🟢' if tp >= 0 else '🔴'} *TOTAL*",
        f"  Invertido: `{fp(ti)}`",
        f"  Valor actual: `{fp(tc)}`",
        f"  P&L: `{fp(tp)}` ({tpct:+.2f}%)",
        f"\n_Actualizado: {datetime.now().strftime('%H:%M:%S')}_",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def cmd_compra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: `/compra BTC 0.5` o `/compra BTC 0.5 65000`",
            parse_mode="Markdown",
        )
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
        msg  = await update.message.reply_text("🔄 Obteniendo precio de mercado...")
        info = get_prices([coin_id])
        if not info or coin_id not in info:
            await msg.edit_text(
                f"⚠️ No pude obtener el precio de *{sym(coin_id)}*.\n\n"
                f"Indícalo manualmente: `/compra {sym(coin_id)} {units} <precio>`",
                parse_mode="Markdown",
            )
            return
        buy_price = info[coin_id]["usd"]
        await msg.delete()

    p = state["portfolio"]
    if coin_id in p:
        ou, oa  = p[coin_id]["units"], p[coin_id]["avg_buy"]
        nu       = ou + units
        na       = (ou*oa + units*buy_price) / nu
        p[coin_id] = {"units": round(nu, 8), "avg_buy": round(na, 8)}
        extra = "Posición ampliada."
    else:
        p[coin_id] = {"units": round(units, 8), "avg_buy": round(buy_price, 8)}
        extra = "Nueva posición creada."
    save_state()

    await update.message.reply_text(
        f"✅ *Compra registrada*\n\n"
        f"  *{sym(coin_id)}* — {units} uds @ {fp(buy_price)}\n"
        f"  Invertido: {fp(units * buy_price)}\n"
        f"  Precio medio ahora: {fp(p[coin_id]['avg_buy'])}\n\n"
        f"_{extra}_",
        parse_mode="Markdown",
    )


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
        await update.message.reply_text(f"⚠️ No tienes *{sym(coin_id)}* en cartera.", parse_mode="Markdown")
        return

    pos   = p[coin_id]
    info  = get_prices([coin_id])
    sell  = info.get(coin_id, {}).get("usd", pos["avg_buy"])
    prf   = (sell - pos["avg_buy"]) * units
    pnl   = ((sell / pos["avg_buy"]) - 1) * 100 if pos["avg_buy"] else 0

    if units >= pos["units"]:
        del p[coin_id]; remaining = 0
    else:
        p[coin_id]["units"] = round(pos["units"] - units, 8)
        remaining = p[coin_id]["units"]
    save_state()

    await update.message.reply_text(
        f"{'💰' if prf >= 0 else '📉'} *Venta registrada*\n\n"
        f"  *{sym(coin_id)}* — {units} uds @ {fp(sell)}\n"
        f"  Compra media: {fp(pos['avg_buy'])}\n"
        f"  P&L: {fp(prf)} ({pnl:+.2f}%)\n"
        f"  Unidades restantes: `{remaining}`",
        parse_mode="Markdown",
    )


async def cmd_precio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: `/precio BTC`", parse_mode="Markdown")
        return
    coin_id = resolve_coin(ctx.args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{ctx.args[0]}'. Prueba /buscar {ctx.args[0]}")
        return
    msg  = await update.message.reply_text("🔄 Obteniendo precio...")
    info = get_prices([coin_id])
    if not info or coin_id not in info:
        await msg.edit_text(
            f"⚠️ No pude obtener el precio de *{sym(coin_id)}* ahora mismo.\n"
            f"CoinGecko puede estar limitando peticiones. Espera 30s e inténtalo de nuevo.",
            parse_mode="Markdown",
        )
        return
    d = info[coin_id]
    await msg.edit_text(
        f"💰 *{sym(coin_id)} — {coin_name(coin_id)}*\n\n"
        f"  Precio: *{fp(d['usd'])}*\n"
        f"  24h: {pc(d.get('usd_24h_change', 0) or 0)}\n"
        f"  7d:  {pc(d.get('usd_7d_change',  0) or 0)}\n"
        f"  Vol 24h: ${(d.get('usd_24h_vol',    0) or 0)/1e9:.3f}B\n"
        f"  Mkt cap: ${(d.get('usd_market_cap', 0) or 0)/1e9:.2f}B",
        parse_mode="Markdown",
    )


async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: `/buscar PEPE` o `/buscar hedera`", parse_mode="Markdown")
        return
    query   = " ".join(ctx.args)
    msg     = await update.message.reply_text(f"🔍 Buscando '{query}'...")
    coin_id = resolve_coin(query)
    if not coin_id:
        await msg.edit_text(f"❌ No encontré ninguna cripto con '{query}'.")
        return
    info = get_prices([coin_id])
    if not info or coin_id not in info:
        await msg.edit_text(
            f"✅ Encontrada: *{coin_name(coin_id)}* ({sym(coin_id)})\n"
            f"ID: `{coin_id}`\n\n"
            f"⚠️ Sin datos de precio disponibles (posible moneda sin liquidez).",
            parse_mode="Markdown",
        )
        return
    d = info[coin_id]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📊 Analizar {sym(coin_id)}", callback_data=f"analyse:{coin_id}"),
        InlineKeyboardButton("📥 Registrar compra",         callback_data=f"buy_prompt:{coin_id}"),
    ]])
    await msg.edit_text(
        f"✅ *{coin_name(coin_id)}* ({sym(coin_id)})\n\n"
        f"  Precio: *{fp(d['usd'])}*\n"
        f"  24h: {pc(d.get('usd_24h_change', 0) or 0)}\n"
        f"  7d:  {pc(d.get('usd_7d_change',  0) or 0)}\n"
        f"  Vol 24h: ${(d.get('usd_24h_vol',    0) or 0)/1e9:.3f}B\n"
        f"  Mkt cap: ${(d.get('usd_market_cap', 0) or 0)/1e9:.2f}B",
        parse_mode="Markdown",
        reply_markup=kb,
    )

# ── Analizar ──────────────────────────────────────────────────────────────────
async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = state["portfolio"]

    # /analizar BTC → análisis individual
    if ctx.args:
        coin_id = resolve_coin(ctx.args[0])
        if not coin_id:
            await update.message.reply_text(
                f"❌ No reconozco '{ctx.args[0]}'. Prueba /buscar {ctx.args[0]}"
            )
            return
        msg = await update.message.reply_text(
            f"🔍 Analizando *{sym(coin_id)}*...",
            parse_mode="Markdown",
        )
        info = get_prices([coin_id])
        if not info or coin_id not in info:
            await msg.edit_text(
                "⚠️ No pude obtener datos ahora mismo.\n"
                "CoinGecko puede estar limitando peticiones. Espera 30s e inténtalo."
            )
            return
        a = full_analysis(coin_id, info[coin_id])
        await msg.edit_text(
            build_analysis_msg(coin_id, a, holding=p.get(coin_id)),
            parse_mode="Markdown",
        )
        return

    # /analizar sin parámetro → toda la cartera, un mensaje por moneda
    if not p:
        await update.message.reply_text(
            "Tu cartera está vacía.\n\nUsa /compra BTC 0.5 para añadir criptos\n"
            "o /mercado para buscar oportunidades."
        )
        return

    n   = len(p)
    msg = await update.message.reply_text(
        f"🔍 Analizando {n} activo{'s' if n > 1 else ''} de tu cartera...\n"
        f"_~{n * 6} segundos. Escribe /cancelar para parar._",
        parse_mode="Markdown",
    )
    data = get_prices(list(p.keys()))

    for cid, pos in p.items():
        # Comprobar si el usuario canceló
        if state.get("cancel"):
            state["cancel"] = False
            await msg.edit_text("🛑 Análisis cancelado.")
            return

        info = data.get(cid)
        if not info:
            await update.message.reply_text(f"⚠️ Sin datos para {sym(cid)}, saltando.")
            continue

        await msg.edit_text(f"🔍 Analizando *{sym(cid)}*...", parse_mode="Markdown")
        a = full_analysis(cid, info)

        # Un solo mensaje por moneda con todo incluido
        await update.message.reply_text(
            build_analysis_msg(cid, a, holding=pos),
            parse_mode="Markdown",
        )
        time.sleep(1.5)

    await msg.edit_text(
        f"✅ Análisis completado — {n} activo{'s' if n > 1 else ''}.\n"
        f"_🕐 {datetime.now().strftime('%H:%M')}_",
        parse_mode="Markdown",
    )

# ── Mercado ───────────────────────────────────────────────────────────────────
async def cmd_mercado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not TOP_IDS:
        await update.message.reply_text("⚠️ El catálogo todavía se está cargando. Espera 30 segundos e inténtalo de nuevo.")
        return

    scan = TOP_IDS[:100]
    msg  = await update.message.reply_text(
        "🔍 *Analizando el top 100 del mercado...*\n\n"
        "_Obteniendo precios en tiempo real y calculando indicadores.\n"
        "Esto tardará aproximadamente 1 minuto._",
        parse_mode="Markdown",
    )

    # Precios en lotes
    data = {}
    for i in range(0, len(scan), 50):
        batch = scan[i:i+50]
        chunk = get_prices(batch)
        data.update(chunk)
        if i + 50 < len(scan):
            time.sleep(2)

    # Puntuar sin pedir historial (para no saturar) — usamos solo datos de precio
    scored = []
    for cid in scan:
        info = data.get(cid)
        if not info:
            continue
        price = info.get("usd", 0)
        chg24 = info.get("usd_24h_change", 0) or 0
        chg7  = info.get("usd_7d_change",  0) or 0
        if not price:
            continue
        # Score rápido solo con cambios de precio (sin historial)
        score = 0
        if chg24 < -8:   score += 2
        elif chg24 < -3: score += 1
        elif chg24 > 10: score -= 2
        elif chg24 > 5:  score -= 1
        if chg7 < -15:   score += 2
        elif chg7 < -5:  score += 1
        elif chg7 > 20:  score -= 2
        vol  = info.get("usd_24h_vol", 0) or 0
        mcap = info.get("usd_market_cap", 0) or 0
        if mcap > 0 and vol / mcap > 0.15:
            score += 1   # volumen inusualmente alto — señal de interés
        scored.append((cid, score, price, chg24, chg7, vol, mcap))

    # Ordenar: mejores oportunidades de compra primero (caídas con volumen)
    scored.sort(key=lambda x: x[1], reverse=True)

    buy_list  = [(c, s, p, c24, c7, v, m) for c, s, p, c24, c7, v, m in scored if s >= 1]
    wait_list = [(c, s, p, c24, c7, v, m) for c, s, p, c24, c7, v, m in scored if s == 0]
    avoid     = [(c, s, p, c24, c7, v, m) for c, s, p, c24, c7, v, m in scored if s <= -2]

    lines = [f"🔍 *Scanner de Mercado — {datetime.now().strftime('%H:%M')}*\n"]

    if buy_list:
        lines.append("🟢 *POSIBLES COMPRAS* (caídas con volumen activo)\n")
        for cid, sc, pr, c24, c7, vol, mc in buy_list[:10]:
            tag  = " 📦" if cid in state["portfolio"] else ""
            lines.append(
                f"  *{sym(cid)}{tag}* — {fp(pr)}\n"
                f"    24h: {pc(c24)}  7d: {pc(c7)}\n"
                f"    Vol: ${vol/1e6:.1f}M  MCap: ${mc/1e9:.2f}B\n"
            )

    if avoid:
        lines.append("🔴 *PRECAUCIÓN* (subidas fuertes — posible toma de beneficios)\n")
        for cid, sc, pr, c24, c7, vol, mc in avoid[:5]:
            tag = " 📦" if cid in state["portfolio"] else ""
            lines.append(f"  *{sym(cid)}{tag}* — {fp(pr)} — 24h: {pc(c24)}")

    lines += [
        "",
        "💡 *Para análisis completo de cualquiera:*",
        "`/analizar BTC` o `/analizar PEPE`",
        "",
        "_📦 = ya tienes en cartera_",
        f"_Escaneadas: {len(scored)} monedas · Datos: CoinGecko_",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

# ── Chat IA ───────────────────────────────────────────────────────────────────
async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Uso: `/chat ¿Debo vender mi ETH?`\n\nO escribe directamente sin /chat.",
            parse_mode="Markdown",
        )
        return
    msg   = await update.message.reply_text("🧠 Pensando...")
    reply = ask_claude(" ".join(ctx.args))
    await msg.edit_text(reply, parse_mode="Markdown")

async def cmd_reset_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["chat_history"] = []
    await update.message.reply_text("🗑️ Historial del chat borrado.")

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_msg = (update.message.text or "").strip()
    if not user_msg:
        return
    msg   = await update.message.reply_text("🧠 Pensando...")
    reply = ask_claude(user_msg)
    await msg.edit_text(reply, parse_mode="Markdown")

# ── Callbacks inline ──────────────────────────────────────────────────────────
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data.startswith("buy_prompt:"):
        cid = q.data.split(":")[1]
        await q.message.reply_text(
            f"Para registrar la compra de *{sym(cid)}*:\n\n"
            f"`/compra {sym(cid)} <cantidad>`\n"
            f"Ejemplo: `/compra {sym(cid)} 100`",
            parse_mode="Markdown",
        )

    elif q.data.startswith("analyse:"):
        cid  = q.data.split(":")[1]
        msg  = await q.message.reply_text(f"🔍 Analizando {sym(cid)}...")
        info = get_prices([cid])
        if not info or cid not in info:
            await msg.edit_text("⚠️ No pude obtener datos ahora mismo. Intenta en 30 segundos.")
            return
        a = full_analysis(cid, info[cid])
        await msg.edit_text(
            build_analysis_msg(cid, a, holding=state["portfolio"].get(cid)),
            parse_mode="Markdown",
        )

# ══════════════════════════════════════════════════════════════════════════════
# MAIN — sin monitor en background
# ══════════════════════════════════════════════════════════════════════════════
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no configurado")
    if not CHAT_ID:
        raise RuntimeError("CHAT_ID no configurado")

    load_state()
    load_catalogue()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("ayuda",     cmd_ayuda))
    app.add_handler(CommandHandler("cartera",   cmd_cartera))
    app.add_handler(CommandHandler("compra",    cmd_compra))
    app.add_handler(CommandHandler("venta",     cmd_venta))
    app.add_handler(CommandHandler("precio",    cmd_precio))
    app.add_handler(CommandHandler("buscar",    cmd_buscar))
    app.add_handler(CommandHandler("analizar",  cmd_analizar))
    app.add_handler(CommandHandler("mercado",   cmd_mercado))
    app.add_handler(CommandHandler("cancelar",  cmd_cancelar))
    app.add_handler(CommandHandler("chat",      cmd_chat))
    app.add_handler(CommandHandler("resetChat", cmd_reset_chat))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("✅ Bot v5 arrancado — sin monitor en background")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
