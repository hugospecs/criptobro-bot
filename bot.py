"""
╔══════════════════════════════════════════════════════════════╗
║          CRYPTO BOT PRO v2 — Telegram                        ║
║  Portfolio · Análisis · Scanner de mercado · Chat IA         ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import json
import asyncio
import logging
import requests
from datetime import datetime
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID         = os.environ.get("CHAT_ID", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY", "")
CHECK_INTERVAL  = 420      # 7 minutos — evita saturar el rate limit de CoinGecko
ALERT_COOLDOWN  = 7200     # 2 horas entre alertas iguales
PORTFOLIO_FILE  = "portfolio.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Catálogo dinámico de monedas ──────────────────────────────────────────────
# Se carga desde CoinGecko al arrancar y se refresca cada 24h.
# Cubre TODAS las criptos listadas (~13.000+).

COIN_CATALOGUE = {}   # { coin_id: {"symbol": "BTC", "name": "Bitcoin"} }
_catalogue_ts  = 0    # timestamp de la última carga

SCANNER_TOP_IDS = []  # top 250 por market cap para el scanner

def _load_catalogue():
    """Descarga la lista completa de monedas de CoinGecko."""
    global _catalogue_ts
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/list",
            params={"include_platform": "false"},
            timeout=30,
        )
        r.raise_for_status()
        coins = r.json()
        COIN_CATALOGUE.clear()
        for c in coins:
            cid = c["id"]
            COIN_CATALOGUE[cid] = {
                "symbol": c["symbol"].upper(),
                "name":   c["name"],
            }
        _catalogue_ts = datetime.now().timestamp()
        log.info("Catálogo cargado: %d monedas", len(COIN_CATALOGUE))
    except Exception as e:
        log.error("Error cargando catálogo: %s", e)

def _load_scanner_top():
    """Descarga el top 250 por market cap para el scanner."""
    global SCANNER_TOP_IDS
    try:
        ids = []
        for page in range(1, 4):   # 3 páginas × 100 = top 300
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 100,
                    "page": page,
                    "sparkline": "false",
                },
                timeout=20,
            )
            r.raise_for_status()
            ids.extend(c["id"] for c in r.json())
            import time; time.sleep(0.5)
        SCANNER_TOP_IDS = ids
        log.info("Scanner top cargado: %d monedas", len(SCANNER_TOP_IDS))
    except Exception as e:
        log.error("Error cargando scanner top: %s", e)

def ensure_catalogue():
    """Recarga el catálogo si tiene más de 24 horas."""
    if not COIN_CATALOGUE or (datetime.now().timestamp() - _catalogue_ts) > 86400:
        _load_catalogue()
    if not SCANNER_TOP_IDS:
        _load_scanner_top()

def sym(coin_id):
    """Devuelve el símbolo (BTC, ETH…) de un coin_id."""
    entry = COIN_CATALOGUE.get(coin_id)
    if entry:
        return entry["symbol"]
    return coin_id.upper()

def resolve_coin(user_input):
    """
    Convierte lo que escribe el usuario en un coin_id de CoinGecko.
    Acepta: símbolo (BTC), nombre (bitcoin), ID (bitcoin), parcial (bit).
    Si hay ambigüedad devuelve el de mayor market cap (posición en SCANNER_TOP_IDS).
    """
    ensure_catalogue()
    u = user_input.strip().lower()

    # 1. Coincidencia exacta por ID
    if u in COIN_CATALOGUE:
        return u

    # 2. Coincidencia exacta por símbolo
    exact_sym = [
        cid for cid, data in COIN_CATALOGUE.items()
        if data["symbol"].lower() == u
    ]
    if exact_sym:
        # Si hay varios con el mismo símbolo, preferir el de mayor market cap
        if SCANNER_TOP_IDS:
            for top_id in SCANNER_TOP_IDS:
                if top_id in exact_sym:
                    return top_id
        return exact_sym[0]

    # 3. Coincidencia exacta por nombre
    exact_name = [
        cid for cid, data in COIN_CATALOGUE.items()
        if data["name"].lower() == u
    ]
    if exact_name:
        return exact_name[0]

    # 4. Búsqueda parcial (símbolo o nombre contiene el texto)
    partial = [
        cid for cid, data in COIN_CATALOGUE.items()
        if u in data["symbol"].lower() or u in data["name"].lower()
    ]
    if partial:
        if SCANNER_TOP_IDS:
            for top_id in SCANNER_TOP_IDS:
                if top_id in partial:
                    return top_id
        return partial[0]

    # 5. Búsqueda en CoinGecko search API como último recurso
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": user_input},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("coins", [])
        if results:
            found_id = results[0]["id"]
            # Añadir al catálogo local si no estaba
            if found_id not in COIN_CATALOGUE:
                COIN_CATALOGUE[found_id] = {
                    "symbol": results[0]["symbol"].upper(),
                    "name":   results[0]["name"],
                }
            return found_id
    except Exception:
        pass

    return None

# ── Estado global ─────────────────────────────────────────────────────────────
state = {
    "portfolio":    {},   # { coin_id: {"units": float, "avg_buy": float} }
    "alerts_sent":  {},   # { key: timestamp }
    "chat_history": [],   # historial IA
}

def load_state():
    # Cargar catálogo completo al arrancar
    log.info("Cargando catálogo de monedas desde CoinGecko...")
    _load_catalogue()
    _load_scanner_top()

    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                saved = json.load(f)
                for k in ("portfolio", "alerts_sent"):
                    if k in saved:
                        state[k] = saved[k]
            log.info("Estado cargado: %d activos en cartera", len(state["portfolio"]))
        except Exception as e:
            log.warning("No se pudo cargar estado: %s", e)

def save_state():
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump({"portfolio": state["portfolio"], "alerts_sent": state["alerts_sent"]}, f, indent=2)
    except Exception as e:
        log.error("Error guardando estado: %s", e)

# ── CoinGecko API con reintentos, caché y control de rate limit ───────────────
GECKO        = "https://api.coingecko.com/api/v3"
_price_cache = {}   # { frozenset(ids): (timestamp, data) }
_hist_cache  = {}   # { (coin_id, days): (timestamp, data) }
CACHE_TTL    = 60   # segundos que un precio es válido antes de volver a pedir

import time as _time

def _gecko_get(url, params, retries=4, base_wait=8):
    """
    Hace una petición a CoinGecko con:
    - Reintentos automáticos (hasta 4 veces)
    - Espera exponencial si recibe 429 (Too Many Requests)
    - Timeout generoso
    """
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=25)

            if r.status_code == 429:
                # Rate limit: esperar y reintentar
                wait = base_wait * (2 ** attempt)
                log.warning("Rate limit CoinGecko — esperando %ds (intento %d/%d)", wait, attempt+1, retries)
                _time.sleep(wait)
                continue

            if r.status_code == 503 or r.status_code == 502:
                # Servidor caído temporalmente
                wait = base_wait * (attempt + 1)
                log.warning("CoinGecko %d — esperando %ds", r.status_code, wait)
                _time.sleep(wait)
                continue

            r.raise_for_status()
            return r.json()

        except requests.exceptions.ConnectionError:
            log.warning("Sin conexión — intento %d/%d", attempt+1, retries)
            _time.sleep(base_wait)
        except requests.exceptions.Timeout:
            log.warning("Timeout — intento %d/%d", attempt+1, retries)
            _time.sleep(4)
        except Exception as e:
            log.error("_gecko_get error inesperado: %s", e)
            _time.sleep(4)

    log.error("_gecko_get: agotados %d reintentos para %s", retries, url)
    return None


def get_prices(coin_ids):
    if not coin_ids:
        return {}

    coin_ids = list(dict.fromkeys(coin_ids))  # deduplicar
    key      = frozenset(coin_ids)
    now      = _time.time()

    # Devolver caché si es reciente
    if key in _price_cache:
        ts, cached = _price_cache[key]
        if now - ts < CACHE_TTL:
            return cached

    # Dividir en lotes de 50 si hay muchas monedas
    result = {}
    for i in range(0, len(coin_ids), 50):
        batch = coin_ids[i:i+50]
        data  = _gecko_get(
            f"{GECKO}/simple/price",
            params={
                "ids":                  ",".join(batch),
                "vs_currencies":        "usd",
                "include_24hr_change":  "true",
                "include_7d_change":    "true",
                "include_24hr_vol":     "true",
                "include_market_cap":   "true",
            },
        )
        if data:
            result.update(data)
        if i + 50 < len(coin_ids):
            _time.sleep(1.5)  # pausa entre lotes para no saturar

    if result:
        _price_cache[key] = (_time.time(), result)

    return result


def get_history(coin_id, days=30):
    key = (coin_id, days)
    now = _time.time()

    # Caché de historial: válido 10 minutos
    if key in _hist_cache:
        ts, cached = _hist_cache[key]
        if now - ts < 600:
            return cached

    data = _gecko_get(
        f"{GECKO}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
    )

    if data:
        prices = [p[1] for p in data.get("prices", [])]
        _hist_cache[key] = (_time.time(), prices)
        return prices

    # Si falla, devolver caché antigua si existe (mejor que nada)
    if key in _hist_cache:
        log.warning("Usando historial en caché antiguo para %s", coin_id)
        return _hist_cache[key][1]

    return []

# ── Indicadores técnicos ──────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    avg_g  = sum(gains) / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 2)

def calc_ema(prices, period):
    if not prices:
        return 0.0
    k   = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices):
    if len(prices) < 26:
        return 0.0, 0.0
    ema12  = calc_ema(prices, 12)
    ema26  = calc_ema(prices, 26)
    macd   = ema12 - ema26
    signal = macd * 0.85
    return round(macd, 8), round(signal, 8)

def calc_bollinger(prices, period=20):
    if len(prices) < period:
        p = prices[-1] if prices else 0
        return p, p, p
    w   = prices[-period:]
    avg = sum(w) / period
    std = (sum((x - avg) ** 2 for x in w) / period) ** 0.5
    return round(avg - 2*std, 8), round(avg, 8), round(avg + 2*std, 8)

def full_analysis(coin_id, price_info, days=30):
    price = price_info.get("usd", 0)
    chg24 = price_info.get("usd_24h_change", 0) or 0
    chg7  = price_info.get("usd_7d_change",  0) or 0
    vol   = price_info.get("usd_24h_vol",    0) or 0
    mcap  = price_info.get("usd_market_cap", 0) or 0

    hist   = get_history(coin_id, days)
    prices = hist + [price]

    rsi              = calc_rsi(prices)
    ema7             = calc_ema(prices, 7)
    ema14            = calc_ema(prices, 14)
    macd_v, macd_sig = calc_macd(prices)
    bb_low, bb_mid, bb_high = calc_bollinger(prices)

    vs_ema7  = (price - ema7)  / ema7  * 100 if ema7  else 0
    vs_ema14 = (price - ema14) / ema14 * 100 if ema14 else 0
    bb_range = bb_high - bb_low
    bb_pos   = (price - bb_low) / bb_range * 100 if bb_range else 50

    score   = 0
    reasons = []

    # RSI
    if rsi <= 25:
        score += 3; reasons.append(f"RSI en sobreventa extrema ({rsi}) — señal de compra fuerte")
    elif rsi <= 35:
        score += 2; reasons.append(f"RSI en sobreventa ({rsi}) — zona de compra")
    elif rsi <= 45:
        score += 1; reasons.append(f"RSI bajo ({rsi}) — posible acumulación")
    elif rsi >= 75:
        score -= 3; reasons.append(f"RSI en sobrecompra extrema ({rsi}) — riesgo de corrección")
    elif rsi >= 65:
        score -= 2; reasons.append(f"RSI elevado ({rsi}) — posible techo")
    elif rsi >= 55:
        score -= 1; reasons.append(f"RSI moderado-alto ({rsi})")

    # MACD
    if macd_v > 0 and macd_v > macd_sig:
        score += 2; reasons.append("MACD positivo sobre señal — cruce alcista")
    elif macd_v > 0:
        score += 1; reasons.append("MACD positivo — momentum alcista")
    elif macd_v < 0 and macd_v < macd_sig:
        score -= 2; reasons.append("MACD negativo bajo señal — cruce bajista")
    else:
        score -= 1; reasons.append("MACD negativo — momentum bajista")

    # Precio vs EMA
    if vs_ema7 < -5:
        score += 2; reasons.append(f"Precio {abs(vs_ema7):.1f}% bajo EMA7 — posible suelo")
    elif vs_ema7 < -2:
        score += 1; reasons.append(f"Precio ligeramente bajo EMA7 ({vs_ema7:.1f}%)")
    elif vs_ema7 > 8:
        score -= 2; reasons.append(f"Precio muy extendido sobre EMA7 ({vs_ema7:.1f}%)")
    elif vs_ema7 > 4:
        score -= 1; reasons.append(f"Precio algo extendido sobre EMA7 ({vs_ema7:.1f}%)")

    # EMA cruce
    if ema7 > ema14:
        score += 1; reasons.append("EMA7 > EMA14 — tendencia alcista corto plazo")
    else:
        score -= 1; reasons.append("EMA7 < EMA14 — tendencia bajista corto plazo")

    # Bollinger
    if bb_pos < 15:
        score += 2; reasons.append(f"Precio cerca de banda inferior Bollinger — posible rebote")
    elif bb_pos > 85:
        score -= 2; reasons.append(f"Precio cerca de banda superior Bollinger — posible resistencia")

    # Cambio 24h
    if chg24 < -10:
        score += 1; reasons.append(f"Caída intensa en 24h ({chg24:.1f}%) — posible sobrerreacción")
    elif chg24 > 15:
        score -= 1; reasons.append(f"Subida intensa en 24h ({chg24:.1f}%) — considerar toma de beneficios")

    # Señal final
    if score >= 5:
        signal, conf = "🟢 COMPRAR FUERTE", min(93, 72 + score * 3)
    elif score >= 3:
        signal, conf = "🟢 COMPRAR", min(85, 66 + score * 3)
    elif score >= 1:
        signal, conf = "🟡 POSIBLE COMPRA", min(72, 60 + score * 2)
    elif score <= -5:
        signal, conf = "🔴 VENDER FUERTE", min(93, 72 + abs(score) * 3)
    elif score <= -3:
        signal, conf = "🔴 VENDER", min(85, 66 + abs(score) * 3)
    elif score <= -1:
        signal, conf = "🟠 POSIBLE VENTA", min(72, 60 + abs(score) * 2)
    else:
        signal, conf = "⚪ ESPERAR", 50

    target    = round(price * (1 + max(0.05, abs(vs_ema7) / 100 + 0.03)), 8)
    stop_loss = round(price * (1 - max(0.04, abs(vs_ema14) / 100 + 0.02)), 8)

    return {
        "signal": signal, "confidence": round(conf), "score": score,
        "price": price, "chg24": chg24, "chg7": chg7,
        "rsi": rsi, "ema7": round(ema7, 6), "ema14": round(ema14, 6),
        "macd": macd_v, "macd_sig": macd_sig,
        "bb_low": bb_low, "bb_high": bb_high, "bb_pos": round(bb_pos, 1),
        "vs_ema7": round(vs_ema7, 2), "vol": vol, "mcap": mcap,
        "target": target, "stop_loss": stop_loss,
        "reasons": reasons,
    }

# ── Formato ───────────────────────────────────────────────────────────────────
def fp(p):
    if not p:          return "$0"
    if p >= 1000:      return f"${p:,.2f}"
    if p >= 1:         return f"${p:.4f}"
    if p >= 0.01:      return f"${p:.5f}"
    return f"${p:.8f}"

def pct(v):
    return f"{'🟢 +' if v >= 0 else '🔴 '}{v:.2f}%"

def build_analysis_msg(coin_id, a, show_portfolio=False):
    holding = state["portfolio"].get(coin_id)
    lines = [
        f"📊 *{sym(coin_id)} — Análisis técnico*",
        f"💰 Precio: *{fp(a['price'])}*",
        f"📈 24h: {pct(a['chg24'])}   7d: {pct(a['chg7'])}",
        "",
        f"*Señal:* {a['signal']}",
        f"*Confianza:* {a['confidence']}%",
        "",
        "*Indicadores:*",
        f"  • RSI(14): `{a['rsi']}`",
        f"  • MACD: `{a['macd']:+.6f}` / Señal: `{a['macd_sig']:+.6f}`",
        f"  • EMA7: `{fp(a['ema7'])}`   EMA14: `{fp(a['ema14'])}`",
        f"  • Bollinger pos: `{a['bb_pos']}%` (0=suelo, 100=techo)",
        "",
        "*Niveles sugeridos:*",
        f"  🎯 Objetivo: `{fp(a['target'])}` (+{((a['target']/a['price']-1)*100):.1f}%)" if a['price'] else "  🎯 Objetivo: —",
        f"  🛑 Stop-loss: `{fp(a['stop_loss'])}` (-{((1-a['stop_loss']/a['price'])*100):.1f}%)" if a['price'] else "  🛑 Stop-loss: —",
        "",
        "*Razones:*",
    ]
    for r in a["reasons"]:
        lines.append(f"  → {r}")

    if show_portfolio and holding:
        units   = holding["units"]
        avg_buy = holding["avg_buy"]
        current = a["price"] * units
        invested = avg_buy  * units
        profit  = current - invested
        pnl_pct = (profit / invested * 100) if invested else 0
        lines += [
            "",
            "*Tu posición:*",
            f"  📦 Unidades: `{units}`",
            f"  💵 Precio medio compra: `{fp(avg_buy)}`",
            f"  💼 Valor actual: `{fp(current)}`",
            f"  {'💰' if profit >= 0 else '📉'} P&L: `{fp(profit)}` ({pnl_pct:+.2f}%)",
        ]

    lines.append(f"\n_Análisis: {datetime.now().strftime('%d/%m %H:%M')}_")
    return "\n".join(lines)

# ── Chat IA ───────────────────────────────────────────────────────────────────
def portfolio_summary_text():
    p = state["portfolio"]
    if not p:
        return "Cartera vacía."
    return "\n".join(
        f"{sym(cid)}: {d['units']} uds @ precio medio {fp(d['avg_buy'])}"
        for cid, d in p.items()
    )

def ask_claude(user_msg):
    if not ANTHROPIC_KEY:
        return (
            "⚠️ *Chat IA no disponible*\n\n"
            "Para activarlo añade la variable `ANTHROPIC_KEY` en Railway "
            "con tu API key de Anthropic (console.anthropic.com).\n\n"
            "El resto del bot funciona perfectamente sin ella."
        )
    history = state["chat_history"][-12:]
    system  = (
        "Eres un experto en trading de criptomonedas. Ayudas al usuario a tomar "
        "decisiones de compra y venta de forma clara y honesta. Siempre adviertes "
        "que el trading conlleva riesgo y que no garantizas rentabilidad. "
        f"Cartera actual del usuario:\n{portfolio_summary_text()}\n"
        "Responde en español, formato Telegram (*negrita*, _cursiva_). Sé conciso."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "system": system,
                "messages": history + [{"role": "user", "content": user_msg}],
            },
            timeout=30,
        )
        r.raise_for_status()
        reply = r.json()["content"][0]["text"]
        state["chat_history"].append({"role": "user",      "content": user_msg})
        state["chat_history"].append({"role": "assistant", "content": reply})
        if len(state["chat_history"]) > 40:
            state["chat_history"] = state["chat_history"][-40:]
        return reply
    except Exception as e:
        log.error("Claude API: %s", e)
        return f"❌ Error al contactar con la IA: {e}"

# ══════════════════════════════════════════════════════════════════════════════
# COMANDOS
# ══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = (
    "👋 *Crypto Bot Pro — Comandos*\n\n"
    "📦 *CARTERA*\n"
    "  /cartera — Ver cartera completa con P\\&L\n"
    "  /compra BTC 0\\.5 — Registrar compra \\(precio opcional\\)\n"
    "  /venta ETH 1\\.2 — Registrar venta\n"
    "  /precio BTC — Precio y datos actuales\n\n"
    "📊 *ANÁLISIS*\n"
    "  /analizar — Analizar toda tu cartera\n"
    "  /analizar BTC — Analizar una cripto concreta\n\n"
    "🔍 *BÚSQUEDA Y SCANNER*\n"
    "  /buscar PEPE — Buscar cualquier cripto \\(13\\.000\\+\\)\n"
    "  /buscar hedera — Por nombre o símbolo\n"
    "  /scanner — Escanear top 100 por market cap\n"
    "  /top — Top 5 oportunidades rápidas\n\n"
    "💬 *CHAT IA*\n"
    "  /chat ¿Debo vender mi ETH? — Chat con IA\n"
    "  Escribe cualquier mensaje sin / para chatear\n"
    "  /resetChat — Borrar historial de chat\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "_Alertas automáticas cada 5 min\\._\n"
    "_Catálogo: 13\\.000\\+ criptomonedas\\._"
)

async def cmd_start(update, ctx):
    await update.message.reply_text(HELP_TEXT, parse_mode="MarkdownV2")

async def cmd_ayuda(update, ctx):
    await update.message.reply_text(HELP_TEXT, parse_mode="MarkdownV2")

# ── Cartera ───────────────────────────────────────────────────────────────────
async def cmd_cartera(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    portfolio = state["portfolio"]
    if not portfolio:
        await update.message.reply_text(
            "Tu cartera está vacía.\n\nUsa /compra BTC 0.5 para añadir una posición."
        )
        return

    msg = await update.message.reply_text("🔄 Obteniendo precios...")
    data = get_prices(list(portfolio.keys()))

    total_inv = total_cur = 0
    lines = ["💼 *Tu Cartera*\n"]

    for cid, pos in portfolio.items():
        units   = pos["units"]
        avg_buy = pos["avg_buy"]
        info    = data.get(cid, {})
        price   = info.get("usd", 0)
        chg24   = info.get("usd_24h_change", 0) or 0

        inv    = avg_buy * units
        cur    = price   * units
        profit = cur - inv
        pnl    = (profit / inv * 100) if inv else 0

        total_inv += inv
        total_cur += cur

        icon = "🟢" if profit >= 0 else "🔴"
        lines.append(
            f"{icon} *{sym(cid)}*\n"
            f"  {units} uds · Compra: {fp(avg_buy)} · Ahora: {fp(price)}\n"
            f"  24h: {pct(chg24)}\n"
            f"  Invertido: {fp(inv)} → Valor: {fp(cur)}\n"
            f"  P&L: {fp(profit)} ({pnl:+.2f}%)\n"
        )

    tp = total_cur - total_inv
    ti = (tp / total_inv * 100) if total_inv else 0
    lines += [
        "━━━━━━━━━━━━━━━━",
        f"{'🟢' if tp >= 0 else '🔴'} *TOTAL*",
        f"  Invertido: `{fp(total_inv)}`",
        f"  Valor actual: `{fp(total_cur)}`",
        f"  P&L: `{fp(tp)}` ({ti:+.2f}%)",
        f"\n_Actualizado: {datetime.now().strftime('%H:%M:%S')}_",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def cmd_compra(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: `/compra BTC 0.5` o `/compra BTC 0.5 65000`\n"
            "_Precio opcional: si lo omites se usa el precio de mercado._",
            parse_mode="Markdown",
        )
        return

    coin_id = resolve_coin(args[0])
    if not coin_id:
        await update.message.reply_text(
            f"❌ No reconozco '{args[0]}'.\n"
            "Prueba con el símbolo (BTC, ETH, SOL...) o el ID (bitcoin, ethereum...)"
        )
        return

    try:
        units = float(args[1].replace(",", "."))
        if units <= 0:
            raise ValueError
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
        info = get_prices([coin_id])
        if not info or coin_id not in info:
            await update.message.reply_text(
                f"⚠️ No pude obtener el precio actual de *{sym(coin_id)}*.\n\n"
                f"Puedes indicar el precio manualmente:\n"
                f"`/compra {sym(coin_id)} {units} <precio>`\n\n"
                f"Ejemplo: `/compra {sym(coin_id)} {units} 0.00001`",
                parse_mode="Markdown",
            )
            return
        buy_price = info[coin_id]["usd"]

    portfolio = state["portfolio"]
    if coin_id in portfolio:
        old_u = portfolio[coin_id]["units"]
        old_a = portfolio[coin_id]["avg_buy"]
        new_u = old_u + units
        new_a = (old_u * old_a + units * buy_price) / new_u
        portfolio[coin_id] = {"units": round(new_u, 8), "avg_buy": round(new_a, 8)}
        msg_extra = "Posición ampliada."
    else:
        portfolio[coin_id] = {"units": round(units, 8), "avg_buy": round(buy_price, 8)}
        msg_extra = "Nueva posición creada."

    save_state()
    await update.message.reply_text(
        f"✅ *Compra registrada*\n\n"
        f"  {sym(coin_id)}: `{units}` uds @ `{fp(buy_price)}`\n"
        f"  Invertido: `{fp(units * buy_price)}`\n"
        f"  Precio medio ahora: `{fp(portfolio[coin_id]['avg_buy'])}`\n\n"
        f"_{msg_extra}_",
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

    portfolio = state["portfolio"]
    if coin_id not in portfolio:
        await update.message.reply_text(
            f"⚠️ No tienes {sym(coin_id)} en cartera. Usa /cartera para ver tus posiciones."
        )
        return

    holding    = portfolio[coin_id]
    info       = get_prices([coin_id])
    sell_price = info.get(coin_id, {}).get("usd", holding["avg_buy"])
    profit     = (sell_price - holding["avg_buy"]) * units
    pnl_pct    = ((sell_price / holding["avg_buy"]) - 1) * 100 if holding["avg_buy"] else 0

    if units >= holding["units"]:
        del portfolio[coin_id]
        remaining = 0
    else:
        portfolio[coin_id]["units"] = round(holding["units"] - units, 8)
        remaining = portfolio[coin_id]["units"]

    save_state()
    icon = "💰" if profit >= 0 else "📉"
    await update.message.reply_text(
        f"{icon} *Venta registrada*\n\n"
        f"  {sym(coin_id)}: `{units}` uds @ `{fp(sell_price)}`\n"
        f"  Precio medio compra: `{fp(holding['avg_buy'])}`\n"
        f"  P&L esta venta: `{fp(profit)}` ({pnl_pct:+.2f}%)\n"
        f"  Unidades restantes: `{remaining}`\n",
        parse_mode="Markdown",
    )


async def cmd_precio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: `/precio BTC`", parse_mode="Markdown")
        return
    coin_id = resolve_coin(ctx.args[0])
    if not coin_id:
        await update.message.reply_text(f"❌ No reconozco '{ctx.args[0]}'.")
        return
    info = get_prices([coin_id])
    if not info or coin_id not in info:
        await update.message.reply_text(
            f"⚠️ No pude obtener el precio de *{sym(coin_id)}* ahora mismo.\n\n"
            f"CoinGecko puede estar limitando las peticiones. Espera 30 segundos e inténtalo de nuevo.\n\n"
            f"Si el problema persiste puede ser que esta moneda tenga muy poco volumen.",
            parse_mode="Markdown",
        )
        return
    d = info[coin_id]
    await update.message.reply_text(
        f"💰 *{sym(coin_id)}*\n\n"
        f"  Precio: *{fp(d['usd'])}*\n"
        f"  24h: {pct(d.get('usd_24h_change', 0) or 0)}\n"
        f"  7d:  {pct(d.get('usd_7d_change',  0) or 0)}\n"
        f"  Vol 24h: ${(d.get('usd_24h_vol',    0) or 0)/1e9:.2f}B\n"
        f"  Mkt cap: ${(d.get('usd_market_cap', 0) or 0)/1e9:.2f}B",
        parse_mode="Markdown",
    )

# ── Analizar ──────────────────────────────────────────────────────────────────
async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    portfolio = state["portfolio"]

    if ctx.args:
        coin_id = resolve_coin(ctx.args[0])
        if not coin_id:
            await update.message.reply_text(f"❌ No reconozco '{ctx.args[0]}'.")
            return
        msg = await update.message.reply_text(f"🔍 Analizando {sym(coin_id)}...")
        info = get_prices([coin_id])
        if not info or coin_id not in info:
            await msg.edit_text(
                f"⚠️ No pude obtener datos de *{sym(coin_id)}* ahora mismo.\n\n"
                f"CoinGecko puede estar limitando las peticiones \\(máx 30/min en plan gratuito\\)\\.\n"
                f"Espera 30\\-60 segundos e inténtalo de nuevo\\.",
                parse_mode="MarkdownV2",
            )
            return
        a = full_analysis(coin_id, info[coin_id])
        await msg.edit_text(
            build_analysis_msg(coin_id, a, show_portfolio=(coin_id in portfolio)),
            parse_mode="Markdown",
        )
        return

    if not portfolio:
        await update.message.reply_text(
            "Tu cartera está vacía.\n\nUsa /scanner para buscar oportunidades."
        )
        return

    msg = await update.message.reply_text(f"🔍 Analizando {len(portfolio)} activos de tu cartera...")
    data    = get_prices(list(portfolio.keys()))
    results = []

    for cid in portfolio:
        info = data.get(cid)
        if not info:
            continue
        a = full_analysis(cid, info)
        results.append((cid, a))
        await asyncio.sleep(0.4)

    if not results:
        await msg.edit_text("❌ No pude obtener datos del mercado.")
        return

    summary = ["📊 *Resumen de tu cartera*\n"]
    for cid, a in results:
        pos    = portfolio[cid]
        pnl    = ((a["price"] / pos["avg_buy"]) - 1) * 100 if pos["avg_buy"] else 0
        summary.append(
            f"{a['signal']} *{sym(cid)}*\n"
            f"  {fp(a['price'])} · RSI: {a['rsi']} · P&L: {pnl:+.1f}% · Conf: {a['confidence']}%\n"
        )
    await msg.edit_text("\n".join(summary), parse_mode="Markdown")

    for cid, a in results:
        await update.message.reply_text(
            build_analysis_msg(cid, a, show_portfolio=True),
            parse_mode="Markdown",
        )
        await asyncio.sleep(0.3)

# ── Scanner ───────────────────────────────────────────────────────────────────
async def cmd_scanner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_catalogue()
    scan_ids = SCANNER_TOP_IDS[:100] if SCANNER_TOP_IDS else []
    if not scan_ids:
        await update.message.reply_text("❌ No se pudo cargar la lista de monedas. Intenta de nuevo.")
        return

    msg = await update.message.reply_text(
        f"🔍 Escaneando top {len(scan_ids)} criptomonedas por capitalización...\n_Puede tardar hasta 40 seg._",
        parse_mode="Markdown",
    )

    # Obtener precios en lotes de 50 (límite CoinGecko)
    data = {}
    for i in range(0, len(scan_ids), 50):
        batch = scan_ids[i:i+50]
        data.update(get_prices(batch))
        await asyncio.sleep(1.2)

    opps = []
    for cid in scan_ids:
        info = data.get(cid)
        if not info:
            continue
        a = full_analysis(cid, info, days=14)
        opps.append((cid, a))
        await asyncio.sleep(0.1)

    opps.sort(key=lambda x: x[1]["score"], reverse=True)
    buy_ops  = [(c, a) for c, a in opps if a["score"] >= 2]
    sell_ops = [(c, a) for c, a in opps if a["score"] <= -3]

    lines = [f"🔍 *Scanner — {datetime.now().strftime('%H:%M')}* (top {len(scan_ids)} por mkt cap)\n"]

    if buy_ops:
        lines.append("🟢 *OPORTUNIDADES DE COMPRA*")
        for cid, a in buy_ops[:6]:
            tag = " 📦" if cid in state["portfolio"] else ""
            lines.append(
                f"  *{sym(cid)}{tag}* — {fp(a['price'])} — RSI: {a['rsi']} — Conf: {a['confidence']}%\n"
                f"    {a['signal']}"
            )
        lines.append("")

    if sell_ops:
        lines.append("🔴 *SEÑALES DE VENTA*")
        for cid, a in sell_ops[:4]:
            tag = " 📦" if cid in state["portfolio"] else ""
            lines.append(
                f"  *{sym(cid)}{tag}* — {fp(a['price'])} — RSI: {a['rsi']} — Conf: {a['confidence']}%\n"
                f"    {a['signal']}"
            )
        lines.append("")

    lines.append("_📦 = ya tienes en cartera_")
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

    if buy_ops:
        await update.message.reply_text("📋 *Detalle top 3 oportunidades:*", parse_mode="Markdown")
        for cid, a in buy_ops[:3]:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"📥 Registrar compra de {sym(cid)}",
                    callback_data=f"buy_prompt:{cid}",
                )
            ]])
            await update.message.reply_text(
                build_analysis_msg(cid, a, show_portfolio=(cid in state["portfolio"])),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            await asyncio.sleep(0.3)


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ensure_catalogue()
    top_ids = SCANNER_TOP_IDS[:20] if SCANNER_TOP_IDS else []
    msg  = await update.message.reply_text("⚡ Buscando top oportunidades...")
    data = get_prices(top_ids)
    scored = []
    for cid in top_ids:
        info = data.get(cid)
        if not info:
            continue
        a = full_analysis(cid, info, days=14)
        scored.append((cid, a))
        await asyncio.sleep(0.1)

    scored.sort(key=lambda x: x[1]["score"], reverse=True)
    lines = [f"⚡ *Top 5 Oportunidades — {datetime.now().strftime('%H:%M')}*\n"]
    for i, (cid, a) in enumerate(scored[:5], 1):
        tag = " 📦" if cid in state["portfolio"] else ""
        lines.append(
            f"*{i}. {sym(cid)}{tag}* — {fp(a['price'])}\n"
            f"  {a['signal']} · RSI: {a['rsi']} · Conf: {a['confidence']}%\n"
            f"  24h: {pct(a['chg24'])}\n"
        )
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /buscar PEPE  — busca la cripto y muestra info + botón de análisis.
    Sirve para encontrar cualquier moneda aunque no esté en el top.
    """
    if not ctx.args:
        await update.message.reply_text(
            "Uso: `/buscar PEPE` o `/buscar hedera`\n"
            "Busca cualquier criptomoneda por símbolo o nombre.",
            parse_mode="Markdown",
        )
        return

    query   = " ".join(ctx.args)
    msg     = await update.message.reply_text(f"🔍 Buscando '{query}'...")
    ensure_catalogue()
    coin_id = resolve_coin(query)

    if not coin_id:
        await msg.edit_text(
            f"❌ No encontré ninguna cripto con '{query}'.\n"
            "Prueba con el nombre completo en inglés (ej: pepe, hedera, vechain)."
        )
        return

    info = get_prices([coin_id])
    if not info or coin_id not in info:
        # Puede ser una moneda con volumen muy bajo — mostrar nombre igualmente
        entry = COIN_CATALOGUE.get(coin_id, {})
        await msg.edit_text(
            f"✅ Encontrada: *{entry.get('name', coin_id)}* ({sym(coin_id)})\n\n"
            f"ID CoinGecko: `{coin_id}`\n\n"
            f"⚠️ No hay datos de precio disponibles ahora mismo (posible moneda sin liquidez).",
            parse_mode="Markdown",
        )
        return

    entry = COIN_CATALOGUE.get(coin_id, {})
    d     = info[coin_id]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📊 Analizar {sym(coin_id)}", callback_data=f"analyse:{coin_id}"),
        InlineKeyboardButton(f"📥 Registrar compra",       callback_data=f"buy_prompt:{coin_id}"),
    ]])
    await msg.edit_text(
        f"✅ *{entry.get('name', coin_id)}* ({sym(coin_id)})\n\n"
        f"  💰 Precio: *{fp(d['usd'])}*\n"
        f"  24h: {pct(d.get('usd_24h_change', 0) or 0)}\n"
        f"  7d:  {pct(d.get('usd_7d_change',  0) or 0)}\n"
        f"  Vol 24h: ${(d.get('usd_24h_vol', 0) or 0)/1e9:.3f}B\n"
        f"  Mkt cap: ${(d.get('usd_market_cap', 0) or 0)/1e9:.3f}B\n\n"
        f"_ID: `{coin_id}`_",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

# ── Chat IA ───────────────────────────────────────────────────────────────────
async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Uso: `/chat ¿Debo vender mi ETH ahora?`\n\n"
            "También puedes escribir directamente sin /chat y te responderé.",
            parse_mode="Markdown",
        )
        return
    user_msg = " ".join(ctx.args)
    msg      = await update.message.reply_text("🧠 Pensando...")
    reply    = ask_claude(user_msg)
    await msg.edit_text(reply, parse_mode="Markdown")

async def cmd_reset_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["chat_history"] = []
    await update.message.reply_text("🗑️ Historial del chat borrado.")

# ── Texto libre ───────────────────────────────────────────────────────────────
async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_msg = (update.message.text or "").strip()
    if not user_msg:
        return
    msg   = await update.message.reply_text("🧠 Pensando...")
    reply = ask_claude(user_msg)
    await msg.edit_text(reply, parse_mode="Markdown")

# ── Callbacks inline ──────────────────────────────────────────────────────────
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("buy_prompt:"):
        cid = data.split(":")[1]
        await query.message.reply_text(
            f"Para registrar la compra de *{sym(cid)}* usa:\n\n"
            f"`/compra {sym(cid)} <cantidad>`\n\n"
            f"Ejemplo: `/compra {sym(cid)} 100`",
            parse_mode="Markdown",
        )

    elif data.startswith("analyse:"):
        cid  = data.split(":")[1]
        msg  = await query.message.reply_text(f"🔍 Analizando {sym(cid)}...")
        info = get_prices([cid])
        if not info or cid not in info:
            await msg.edit_text("❌ No pude obtener datos de precio.")
            return
        a = full_analysis(cid, info[cid])
        await msg.edit_text(
            build_analysis_msg(cid, a, show_portfolio=(cid in state["portfolio"])),
            parse_mode="Markdown",
        )

# ── Monitor automático ────────────────────────────────────────────────────────
async def monitor_loop(bot: Bot):
    log.info("Monitor automático iniciado")
    await asyncio.sleep(20)
    while True:
        try:
            await auto_check(bot)
        except Exception as e:
            log.error("monitor_loop: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)

async def auto_check(bot: Bot):
    portfolio = state["portfolio"]
    if not portfolio:
        return

    data   = get_prices(list(portfolio.keys()))
    now_ts = datetime.now().timestamp()

    for cid, pos in portfolio.items():
        info = data.get(cid)
        if not info:
            continue

        a     = full_analysis(cid, info)
        key   = f"{cid}_{a['score'] // 2}"
        last  = state["alerts_sent"].get(key, 0)

        if now_ts - last < ALERT_COOLDOWN:
            continue

        chg24 = info.get("usd_24h_change", 0) or 0
        text  = None

        if a["score"] >= 3 and a["confidence"] >= 65:
            text = f"🚨 *SEÑAL DE COMPRA — {sym(cid)}*\n\n" + build_analysis_msg(cid, a, show_portfolio=True)
        elif a["score"] <= -3 and a["confidence"] >= 65:
            text = f"🚨 *SEÑAL DE VENTA — {sym(cid)}*\n\n" + build_analysis_msg(cid, a, show_portfolio=True)
        elif abs(chg24) >= 10:
            text = (
                f"⚡ *MOVIMIENTO BRUSCO — {sym(cid)}*\n\n"
                f"  {pct(chg24)} en 24h · Precio: {fp(a['price'])}\n"
                f"  Señal: {a['signal']} · RSI: {a['rsi']}\n"
                f"  Confianza: {a['confidence']}%"
            )

        if text and CHAT_ID:
            await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
            state["alerts_sent"][key] = now_ts
            log.info("Alerta enviada: %s → %s", sym(cid), a["signal"])

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no configurado")
    if not CHAT_ID:
        raise RuntimeError("CHAT_ID no configurado")

    load_state()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("ayuda",     cmd_ayuda))
    app.add_handler(CommandHandler("cartera",   cmd_cartera))
    app.add_handler(CommandHandler("compra",    cmd_compra))
    app.add_handler(CommandHandler("venta",     cmd_venta))
    app.add_handler(CommandHandler("precio",    cmd_precio))
    app.add_handler(CommandHandler("analizar",  cmd_analizar))
    app.add_handler(CommandHandler("buscar",    cmd_buscar))
    app.add_handler(CommandHandler("scanner",   cmd_scanner))
    app.add_handler(CommandHandler("top",       cmd_top))
    app.add_handler(CommandHandler("chat",      cmd_chat))
    app.add_handler(CommandHandler("resetChat", cmd_reset_chat))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    async def post_init(application):
        asyncio.create_task(monitor_loop(application.bot))

    app.post_init = post_init

    log.info("Bot arrancado correctamente")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
