"""
CRYPTO BOT PRO v12
- Todo async: ninguna llamada bloquea el bot
- Chat IA con Gemini (gratuito)
- /mercado carga TOP_IDS al arrancar de forma async
- /analizar un solo mensaje por moneda
- /cancelar funcional
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
GEMINI_KEY     = os.environ.get("GEMINI_KEY", "")
PORTFOLIO_FILE = "portfolio.json"
GECKO          = "https://api.coingecko.com/api/v3"
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Catálogo ──────────────────────────────────────────────────────────────────
CATALOGUE = {}
TOP_IDS   = []

# ── Estado ────────────────────────────────────────────────────────────────────
state = {
    "portfolio":    {},
    "chat_history": [],   # formato Gemini: [{role, parts:[{text}]}]
    "cancel":       False,
}

def load_state():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                saved = json.load(f)
                state["portfolio"] = saved.get("portfolio", {})
            log.info("Cartera: %d activos", len(state["portfolio"]))
        except Exception as e:
            log.warning("No se pudo cargar cartera: %s", e)

def save_state():
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump({"portfolio": state["portfolio"]}, f, indent=2)
    except Exception as e:
        log.error("Error guardando: %s", e)

# ── Helpers HTTP síncronos (se ejecutan en executor para no bloquear) ─────────
def _get(url, params=None, retries=3, base_wait=8):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 429:
                import time; time.sleep(base_wait * (i + 1))
                continue
            if r.status_code in (502, 503):
                import time; time.sleep(6)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            import time; time.sleep(4)
        except Exception as e:
            log.warning("_get intento %d: %s", i+1, e)
            import time; time.sleep(3)
    return None

def _post(url, payload, headers=None, retries=2):
    for i in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            return r
        except requests.exceptions.Timeout:
            import time; time.sleep(4)
        except Exception as e:
            log.warning("_post intento %d: %s", i+1, e)
            import time; time.sleep(3)
    return None

# ── Ejecutar bloqueante en thread pool ────────────────────────────────────────
async def run(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

# ── CoinGecko ─────────────────────────────────────────────────────────────────
def _fetch_prices(coin_ids):
    if not coin_ids:
        return {}
    result = {}
    ids = list(dict.fromkeys(coin_ids))
    for i in range(0, len(ids), 50):
        batch = ids[i:i+50]
        data = _get(f"{GECKO}/simple/price", {
            "ids": ",".join(batch), "vs_currencies": "usd",
            "include_24hr_change": "true", "include_7d_change": "true",
            "include_24hr_vol": "true", "include_market_cap": "true",
        })
        if data:
            result.update(data)
        if i + 50 < len(ids):
            import time; time.sleep(1.5)
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
    ids = []
    for page in [1, 2, 3]:
        data = _get(f"{GECKO}/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 100, "page": page, "sparkline": "false",
        })
        if data:
            ids.extend(c["id"] for c in data)
        import time; time.sleep(1.5)
    TOP_IDS.extend(ids)
    log.info("TOP_IDS: %d", len(TOP_IDS))

def _fetch_top_markets():
    """Obtiene top 100 con precios incluidos en una sola llamada (para /mercado)."""
    result = []
    for page in [1, 2]:
        data = _get(f"{GECKO}/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 50, "page": page, "sparkline": "false",
            "price_change_percentage": "24h,7d",
        })
        if data:
            result.extend(data)
        import time; time.sleep(1.5)
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
    # Fallback: CoinGecko search
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
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    g = [max(d, 0) for d in deltas[-period:]]
    l = [max(-d, 0) for d in deltas[-period:]]
    ag, al = sum(g)/period, sum(l)/period
    return round(100 - 100/(1 + ag/al), 2) if al else 100.0

def calc_ema(prices, period):
    if not prices: return 0.0
    k, ema = 2/(period+1), prices[0]
    for p in prices[1:]: ema = p*k + ema*(1-k)
    return ema

def calc_macd(prices):
    if len(prices) < 26: return 0.0, 0.0
    m = calc_ema(prices, 12) - calc_ema(prices, 26)
    return round(m, 8), round(m * 0.85, 8)

def calc_bb(prices, period=20):
    if len(prices) < period:
        p = prices[-1] if prices else 0
        return p, p, p
    w = prices[-period:]
    avg = sum(w)/period
    std = (sum((x-avg)**2 for x in w)/period)**0.5
    return round(avg-2*std, 8), round(avg, 8), round(avg+2*std, 8)

def score_prices(prices, price, chg24, chg7):
    all_p = prices + [price]
    rsi = calc_rsi(all_p)
    ema7 = calc_ema(all_p, 7)
    ema14 = calc_ema(all_p, 14)
    macd_v, macd_s = calc_macd(all_p)
    bb_lo, _, bb_hi = calc_bb(all_p)
    vs7  = (price - ema7)  / ema7  * 100 if ema7  else 0
    vs14 = (price - ema14) / ema14 * 100 if ema14 else 0
    bb_pos = (price - bb_lo) / (bb_hi - bb_lo) * 100 if (bb_hi - bb_lo) else 50
    score, reasons = 0, []
    if rsi <= 25:    score += 3; reasons.append(f"RSI sobreventa extrema ({rsi})")
    elif rsi <= 35:  score += 2; reasons.append(f"RSI sobreventa ({rsi}) — zona compra")
    elif rsi <= 45:  score += 1; reasons.append(f"RSI bajo ({rsi})")
    elif rsi >= 75:  score -= 3; reasons.append(f"RSI sobrecompra extrema ({rsi})")
    elif rsi >= 65:  score -= 2; reasons.append(f"RSI elevado ({rsi})")
    elif rsi >= 55:  score -= 1; reasons.append(f"RSI moderado-alto ({rsi})")
    if macd_v > 0 and macd_v > macd_s:   score += 2; reasons.append("MACD alcista")
    elif macd_v > 0:                       score += 1; reasons.append("MACD positivo")
    elif macd_v < 0 and macd_v < macd_s:  score -= 2; reasons.append("MACD bajista")
    else:                                  score -= 1; reasons.append("MACD negativo")
    if vs7 < -5:    score += 2; reasons.append(f"Precio {abs(vs7):.1f}% bajo EMA7")
    elif vs7 < -2:  score += 1; reasons.append(f"Bajo EMA7 ({vs7:.1f}%)")
    elif vs7 > 8:   score -= 2; reasons.append(f"Extendido sobre EMA7 ({vs7:.1f}%)")
    elif vs7 > 4:   score -= 1; reasons.append(f"Algo alto sobre EMA7 ({vs7:.1f}%)")
    if ema7 > ema14: score += 1; reasons.append("EMA7 > EMA14 — tendencia alcista")
    else:            score -= 1; reasons.append("EMA7 < EMA14 — tendencia bajista")
    if bb_pos < 15:  score += 2; reasons.append("Cerca banda inferior Bollinger")
    elif bb_pos > 85: score -= 2; reasons.append("Cerca banda superior Bollinger")
    if chg24 < -10:  score += 1; reasons.append(f"Caída 24h ({chg24:.1f}%) — posible rebote")
    elif chg24 > 15: score -= 1; reasons.append(f"Subida fuerte 24h ({chg24:.1f}%)")
    return score, reasons, rsi, ema7, ema14, macd_v, bb_pos, vs7, vs14

def signal_label(score):
    if score >= 5:  return "🟢 COMPRAR FUERTE"
    if score >= 3:  return "🟢 COMPRAR"
    if score >= 1:  return "🟡 POSIBLE COMPRA"
    if score <= -5: return "🔴 VENDER FUERTE"
    if score <= -3: return "🔴 VENDER"
    if score <= -1: return "🟠 POSIBLE VENTA"
    return "⚪ ESPERAR / MANTENER"

def calc_conf(score):
    return min(93, 60 + abs(score) * 5)

def _do_full_analysis(coin_id, price_info):
    price = price_info.get("usd", 0)
    chg24 = price_info.get("usd_24h_change", 0) or 0
    chg7  = price_info.get("usd_7d_change",  0) or 0
    hist  = _fetch_history(coin_id, 30)
    score_d, reasons_d, rsi_d, ema7_d, ema14_d, macd_d, bb_pos_d, vs7_d, vs14_d = \
        score_prices(hist, price, chg24, chg7)
    conf_d = calc_conf(score_d)
    target = round(price * (1 + max(0.05, abs(vs7_d)/100 + 0.03)), 8)
    sl     = round(price * (1 - max(0.04, abs(vs14_d)/100 + 0.02)), 8)
    # 4h
    ohlc = _fetch_ohlc(coin_id, 2)
    sig_4h, reasons_4h, conf_4h = "⚪ ESPERAR", [], 50
    if ohlc and len(ohlc) >= 8:
        closes = [c[4] for c in ohlc]
        s4, r4, *_ = score_prices(closes[:-1], closes[-1], chg24, 0)
        conf_4h = calc_conf(s4)
        sig_4h  = signal_label(s4)
        reasons_4h = r4[:2]
    return {
        "price": price, "chg24": chg24, "chg7": chg7,
        "signal": signal_label(score_d), "confidence": conf_d, "score": score_d,
        "rsi": rsi_d, "ema7": ema7_d, "ema14": ema14_d, "macd": macd_d,
        "bb_pos": bb_pos_d, "target": target, "stop_loss": sl,
        "reasons": reasons_d,
        "signal_4h": sig_4h, "confidence_4h": conf_4h, "reasons_4h": reasons_4h,
    }

# ── Formateo ──────────────────────────────────────────────────────────────────
def fp(p):
    if not p:     return "$0"
    if p >= 1000: return f"${p:,.2f}"
    if p >= 1:    return f"${p:.4f}"
    if p >= 0.01: return f"${p:.6f}"
    return f"${p:.8f}"

def pc(v):
    return f"{'🟢 +' if v >= 0 else '🔴 '}{v:.2f}%"

def build_msg(coin_id, a, holding=None):
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
        f"  {a['signal_4h']}   Confianza: {a['confidence_4h']}%",
    ]
    for r in a["reasons_4h"]:
        lines.append(f"  · {r}")
    if holding:
        units, avg = holding["units"], holding["avg_buy"]
        cur  = price * units
        inv  = avg * units
        prf  = cur - inv
        pnl  = (prf / inv * 100) if inv else 0
        lines += [
            "",
            "┌─ TU POSICIÓN ────────────────────",
            f"│ {units} uds · Compra media: {fp(avg)}",
            f"│ Valor: {fp(cur)}",
            f"└ {'💰' if prf >= 0 else '📉'} P&L: {fp(prf)} ({pnl:+.2f}%)",
        ]
    lines.append(f"\n_🕐 {datetime.now().strftime('%d/%m %H:%M')}_")
    return "\n".join(lines)

# ── Chat IA con Gemini (async) ────────────────────────────────────────────────
def _portfolio_ctx():
    p = state["portfolio"]
    if not p: return "Cartera vacía."
    return "\n".join(
        f"{sym(c)}: {d['units']} uds @ precio medio {fp(d['avg_buy'])}"
        for c, d in p.items()
    )

def _call_gemini(user_msg):
    if not GEMINI_KEY:
        return (
            "⚠️ Chat IA no disponible\n\n"
            "Añade la variable GEMINI_KEY en Railway.\n"
            "Consíguela gratis en aistudio.google.com → Get API Key"
        )

    # Historial limpio: solo últimos 6 turnos, alternancia correcta
    clean, last_role = [], None
    for m in state["chat_history"][-12:]:
        if (isinstance(m, dict)
                and m.get("role") in ("user", "model")
                and isinstance(m.get("parts"), list)
                and m["parts"]
                and str(m["parts"][0].get("text", "")).strip()
                and m["role"] != last_role):
            clean.append(m)
            last_role = m["role"]
    if clean and clean[-1]["role"] == "model":
        clean = clean[:-1]

    payload = {
        "system_instruction": {"parts": [{"text": (
            "Eres un asesor de trading de criptomonedas. "
            "REGLAS IMPORTANTES:\n"
            "1. Responde SIEMPRE en español\n"
            "2. Respuestas CORTAS y DIRECTAS, máximo 5-6 líneas\n"
            "3. Sin saludos ni introducciones largas\n"
            "4. Sin formato markdown complejo, solo texto plano con algún emoji\n"
            "5. Ve directo al grano con la recomendación\n"
            "6. Siempre advierte brevemente el riesgo al final\n"
            f"Cartera del usuario:\n{_portfolio_ctx()}"
        )}]},
        "contents": clean + [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {
            "maxOutputTokens": 250,   # respuestas cortas para no cortar
            "temperature": 0.5,
        },
    }

    try:
        r = _post(f"{GEMINI_URL}?key={GEMINI_KEY.strip()}", payload)
        if r is None:
            return "⚠️ Sin conexión con Gemini. Inténtalo de nuevo."

        if not r.ok:
            try:
                err = r.json().get("error", {}).get("message", r.text)
            except Exception:
                err = r.text
            log.error("Gemini %d: %s", r.status_code, err)
            if r.status_code == 400 and clean:
                # Reintentar sin historial
                state["chat_history"] = []
                payload2 = {**payload, "contents": [{"role": "user", "parts": [{"text": user_msg}]}]}
                r2 = _post(f"{GEMINI_URL}?key={GEMINI_KEY.strip()}", payload2)
                if r2 and r2.ok:
                    reply = r2.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    state["chat_history"] = [
                        {"role": "user",  "parts": [{"text": user_msg}]},
                        {"role": "model", "parts": [{"text": reply}]},
                    ]
                    return reply
            if r.status_code == 403:
                return "❌ GEMINI_KEY inválida. Compruébala en Railway."
            if r.status_code == 429:
                return "⚠️ Límite de Gemini alcanzado. Espera 1 minuto."
            return f"❌ Error Gemini ({r.status_code})"

        reply = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Guardar en historial
        state["chat_history"].append({"role": "user",  "parts": [{"text": user_msg}]})
        state["chat_history"].append({"role": "model", "parts": [{"text": reply}]})
        if len(state["chat_history"]) > 20:
            state["chat_history"] = state["chat_history"][-20:]
        return reply

    except (KeyError, IndexError) as e:
        log.error("Gemini respuesta inesperada: %s", e)
        return "⚠️ Gemini devolvió una respuesta vacía. Inténtalo de nuevo."
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
    "  /cartera — Ver cartera con P\\&L\n"
    "  /precio BTC — Precio actual\n"
    "  /buscar PEPE — Buscar cualquier cripto\n\n"
    "📊 *ANÁLISIS*\n"
    "  /analizar BTC — Análisis diario \\+ 4h\n"
    "  /analizar — Analizar toda tu cartera\n\n"
    "🔍 *MERCADO*\n"
    "  /mercado — Top 100 oportunidades ahora\n\n"
    "💬 *CHAT IA*\n"
    "  /chat ¿Vendo mi SOL? — Chat con IA\n"
    "  O escribe directamente sin /\n"
    "  /resetChat — Borrar historial\n\n"
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
        prf = cur - inv
        pnl = (prf/inv*100) if inv else 0
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
        msg  = await update.message.reply_text("🔄 Obteniendo precio...")
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
    prf  = (sell - pos["avg_buy"]) * units
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
        await msg.edit_text(build_msg(coin_id, a, holding=p.get(coin_id)), parse_mode="Markdown")
        return

    if not p:
        await update.message.reply_text(
            "Tu cartera está vacía.\n\nUsa /compra BTC 0.5 para añadir\no /mercado para buscar oportunidades."); return

    n   = len(p)
    msg = await update.message.reply_text(
        f"🔍 Analizando {n} activo{'s' if n>1 else ''} de tu cartera...\n"
        f"_Escribe /cancelar para parar._", parse_mode="Markdown")
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
        await update.message.reply_text(build_msg(cid, a, holding=pos), parse_mode="Markdown")

    await msg.edit_text(
        f"✅ Análisis completado — {n} activo{'s' if n>1 else ''}.\n_🕐 {datetime.now().strftime('%H:%M')}_",
        parse_mode="Markdown")

# ── Mercado ───────────────────────────────────────────────────────────────────
async def cmd_mercado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "🔍 *Analizando el top 100 del mercado...*\n_Esto tarda ~30 segundos._",
        parse_mode="Markdown")

    # Obtener top 100 con precios incluidos directamente (sin depender de TOP_IDS)
    markets = await run(_fetch_top_markets)
    if not markets:
        await msg.edit_text(
            "⚠️ No pude obtener datos del mercado ahora mismo.\n"
            "CoinGecko puede estar limitando peticiones. Espera 30s e intenta de nuevo.")
        return

    # Puntuar por cambios de precio + volumen
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

        # Añadir al catálogo local si no estaba
        if cid and cid not in CATALOGUE:
            CATALOGUE[cid] = {"symbol": sym_c, "name": name}

        score = 0
        if chg24 < -8:    score += 2
        elif chg24 < -3:  score += 1
        elif chg24 > 10:  score -= 2
        elif chg24 > 5:   score -= 1
        if chg7 < -15:    score += 2
        elif chg7 < -5:   score += 1
        elif chg7 > 20:   score -= 2
        if mcap > 0 and vol/mcap > 0.15:
            score += 1

        scored.append({
            "id": cid, "symbol": sym_c, "name": name,
            "price": price, "chg24": chg24, "chg7": chg7,
            "vol": vol, "mcap": mcap, "score": score,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    buy_list  = [x for x in scored if x["score"] >= 1]
    avoid     = [x for x in scored if x["score"] <= -2]

    lines = [f"🔍 *Scanner de Mercado — {datetime.now().strftime('%H:%M')}*\n"]

    if buy_list:
        lines.append("🟢 *POSIBLES OPORTUNIDADES DE COMPRA*\n")
        for x in buy_list[:10]:
            tag  = " 📦" if x["id"] in state["portfolio"] else ""
            lines.append(
                f"  *{x['symbol']}{tag}* — {fp(x['price'])}\n"
                f"    24h: {pc(x['chg24'])}  7d: {pc(x['chg7'])}\n"
                f"    Vol: ${x['vol']/1e6:.1f}M  MCap: ${x['mcap']/1e9:.2f}B\n"
            )

    if avoid:
        lines.append("🔴 *PRECAUCIÓN* (subidas fuertes)\n")
        for x in avoid[:5]:
            tag = " 📦" if x["id"] in state["portfolio"] else ""
            lines.append(f"  *{x['symbol']}{tag}* — {fp(x['price'])} — 24h: {pc(x['chg24'])}")

    lines += [
        "",
        "💡 Para análisis completo: `/analizar BTC`",
        "_📦 = ya tienes en cartera_",
        f"_Escaneadas: {len(scored)} monedas_",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

# ── Cancelar ──────────────────────────────────────────────────────────────────
async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["cancel"] = True
    await update.message.reply_text("🛑 Cancelando en el siguiente paso...")

# ── Chat IA ───────────────────────────────────────────────────────────────────
async def _send_reply(update, msg, reply):
    """Envía la respuesta dividiendo si supera el límite de Telegram."""
    # Quitar markdown complejo que Telegram puede rechazar
    safe = (reply
        .replace("**", "*")
        .replace("__", "_")
        .strip())

    MAX = 3800  # margen bajo el límite de 4096
    if len(safe) <= MAX:
        try:
            await msg.edit_text(safe, parse_mode="Markdown")
        except Exception:
            await msg.edit_text(safe)
    else:
        # Dividir en trozos por párrafos
        await msg.delete()
        partes = []
        actual = ""
        for linea in safe.split("\n"):
            if len(actual) + len(linea) + 1 > MAX:
                if actual:
                    partes.append(actual.strip())
                actual = linea
            else:
                actual += "\n" + linea if actual else linea
        if actual:
            partes.append(actual.strip())
        for parte in partes:
            try:
                await update.message.reply_text(parte, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(parte)

async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Uso: /chat ¿Debo vender mi ETH?\nO escribe directamente sin /chat."
        )
        return
    user_msg = " ".join(ctx.args)
    msg      = await update.message.reply_text("🧠 Pensando...")
    reply    = await run(_call_gemini, user_msg)
    await _send_reply(update, msg, reply)

async def cmd_reset_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["chat_history"] = []
    await update.message.reply_text("🗑️ Historial del chat borrado.")

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_msg = (update.message.text or "").strip()
    if not user_msg:
        return
    msg   = await update.message.reply_text("🧠 Pensando...")
    reply = await run(_call_gemini, user_msg)
    await _send_reply(update, msg, reply)

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
        cid = q.data.split(":")[1]
        msg = await q.message.reply_text(f"🔍 Analizando {sym(cid)}...")
        info = await run(_fetch_prices, [cid])
        if not info or cid not in info:
            await msg.edit_text("⚠️ No pude obtener datos ahora mismo."); return
        a = await run(_do_full_analysis, cid, info[cid])
        await msg.edit_text(
            build_msg(cid, a, holding=state["portfolio"].get(cid)),
            parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
async def startup(app):
    """Carga el catálogo al arrancar de forma async para no bloquear."""
    log.info("Cargando catálogo en background...")
    await run(_fetch_catalogue)
    await run(_fetch_top_ids)
    log.info("Catálogo listo. Bot operativo.")

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no configurado")

    load_state()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(startup).build()

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

    log.info("Bot v11 arrancado")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
