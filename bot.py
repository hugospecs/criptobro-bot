"""
╔══════════════════════════════════════════════════╗
║        CRYPTO ALERT BOT — Telegram               ║
║  Monitoriza criptos y avisa cuándo comprar/vender ║
╚══════════════════════════════════════════════════╝
"""

import os
import asyncio
import logging
import requests
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Configuración ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")   # Tu token del bot
CHAT_ID         = os.environ.get("CHAT_ID", "")          # Tu chat ID
COINGECKO_URL   = "https://api.coingecko.com/api/v3"
CHECK_INTERVAL  = 300   # segundos entre cada análisis (5 minutos)

# Criptos monitorizadas por defecto
DEFAULT_CRYPTOS = ["bitcoin", "ethereum", "solana", "ripple", "cardano", "dogecoin"]

# Estado en memoria
monitored   = list(DEFAULT_CRYPTOS)   # activos que el usuario vigila
price_cache = {}                       # último precio conocido por activo
alerted     = {}                       # evita spam de alertas repetidas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Nombres legibles ──────────────────────────────────────────────────────────
SYMBOLS = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "ripple": "XRP", "cardano": "ADA", "dogecoin": "DOGE",
    "avalanche-2": "AVAX", "chainlink": "LINK", "polkadot": "DOT",
    "binancecoin": "BNB", "the-open-network": "TON",
}

def sym(coin_id):
    return SYMBOLS.get(coin_id, coin_id.upper())

# ── Obtener precios reales ────────────────────────────────────────────────────
def get_prices(coin_ids: list) -> dict:
    ids = ",".join(coin_ids)
    try:
        r = requests.get(
            f"{COINGECKO_URL}/simple/price",
            params={
                "ids": ids,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_7d_change": "true",
                "include_24hr_vol": "true",
                "include_market_cap": "true",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Error obteniendo precios: {e}")
        return {}

def get_history(coin_id: str, days: int = 30) -> list:
    try:
        r = requests.get(
            f"{COINGECKO_URL}/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": days, "interval": "daily"},
            timeout=10,
        )
        r.raise_for_status()
        return [p[1] for p in r.json().get("prices", [])]
    except Exception as e:
        log.error(f"Error historial {coin_id}: {e}")
        return []

# ── Indicadores técnicos ──────────────────────────────────────────────────────
def calc_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)

def calc_ema(prices: list, period: int) -> float:
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices: list) -> float:
    if len(prices) < 26:
        return 0.0
    return calc_ema(prices, 12) - calc_ema(prices, 26)

def analyse(coin_id: str, current_price: float, change_24h: float) -> dict:
    """Devuelve señal técnica para un activo."""
    history = get_history(coin_id, 30)
    prices  = history + [current_price]

    rsi     = calc_rsi(prices)
    ema7    = calc_ema(prices, 7)
    ema14   = calc_ema(prices, 14)
    macd    = calc_macd(prices)
    vs_ema7 = (current_price - ema7) / ema7 * 100

    # ── Lógica de señal ───────────────────────────────────────────────────────
    score = 0
    reasons = []

    # RSI
    if rsi < 30:
        score += 2
        reasons.append(f"RSI en sobreventa ({rsi})")
    elif rsi < 45:
        score += 1
        reasons.append(f"RSI bajo ({rsi}), zona de acumulación")
    elif rsi > 70:
        score -= 2
        reasons.append(f"RSI en sobrecompra ({rsi})")
    elif rsi > 60:
        score -= 1
        reasons.append(f"RSI elevado ({rsi})")

    # MACD
    if macd > 0:
        score += 1
        reasons.append("MACD positivo (momentum alcista)")
    else:
        score -= 1
        reasons.append("MACD negativo (momentum bajista)")

    # Precio vs EMA7
    if vs_ema7 < -3:
        score += 1
        reasons.append(f"Precio {abs(vs_ema7):.1f}% por debajo de EMA7")
    elif vs_ema7 > 5:
        score -= 1
        reasons.append(f"Precio {vs_ema7:.1f}% por encima de EMA7 (extendido)")

    # Cambio 24h
    if change_24h < -8:
        score += 1
        reasons.append(f"Caída brusca 24h ({change_24h:.1f}%) → posible sobrerreacción")
    elif change_24h > 10:
        score -= 1
        reasons.append(f"Subida fuerte 24h ({change_24h:.1f}%) → posible toma de beneficios")

    # Señal final
    if score >= 3:
        signal, conf = "COMPRAR 🟢", min(90, 60 + score * 5)
    elif score >= 1:
        signal, conf = "POSIBLE COMPRA 🟡", min(75, 55 + score * 4)
    elif score <= -3:
        signal, conf = "VENDER 🔴", min(90, 60 + abs(score) * 5)
    elif score <= -1:
        signal, conf = "POSIBLE VENTA 🟠", min(75, 55 + abs(score) * 4)
    else:
        signal, conf = "ESPERAR ⚪", 50

    return {
        "signal":    signal,
        "confidence": conf,
        "score":     score,
        "rsi":       rsi,
        "ema7":      ema7,
        "macd":      macd,
        "vs_ema7":   vs_ema7,
        "reasons":   reasons,
    }

# ── Formato de mensaje Telegram ───────────────────────────────────────────────
def fmt_price(p: float) -> str:
    if p >= 1000:  return f"${p:,.2f}"
    if p >= 1:     return f"${p:.4f}"
    return f"${p:.6f}"

def build_alert_msg(coin_id: str, price: float, change_24h: float, result: dict, alert_type: str) -> str:
    s = sym(coin_id)
    now = datetime.now().strftime("%H:%M")
    icon = "🚨" if "COMPRAR" in result["signal"] or "VENDER" in result["signal"] else "⚠️"

    lines = [
        f"{icon} *ALERTA {alert_type} — {s}*",
        f"🕐 {now}  |  💰 {fmt_price(price)}",
        f"📊 Cambio 24h: {'🟢' if change_24h>=0 else '🔴'} {change_24h:+.2f}%",
        f"",
        f"📈 *Señal:* {result['signal']}",
        f"🎯 *Confianza:* {result['confidence']}%",
        f"",
        f"*Indicadores:*",
        f"  • RSI: {result['rsi']}",
        f"  • MACD: {'positivo ↑' if result['macd']>0 else 'negativo ↓'}",
        f"  • vs EMA7: {result['vs_ema7']:+.2f}%",
        f"",
        f"*Razones:*",
    ]
    for r in result["reasons"]:
        lines.append(f"  → {r}")
    lines.append("")
    lines.append("_⚠️ Solo informativo. No es asesoramiento financiero._")
    return "\n".join(lines)

# ── Loop de monitorización ────────────────────────────────────────────────────
async def monitor_loop(bot: Bot):
    log.info("🚀 Monitor iniciado")
    while True:
        try:
            await run_analysis(bot)
        except Exception as e:
            log.error(f"Error en monitor_loop: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

async def run_analysis(bot: Bot):
    if not monitored:
        return

    prices_data = get_prices(monitored)
    if not prices_data:
        return

    now = datetime.now().strftime("%H:%M:%S")
    log.info(f"[{now}] Analizando {len(monitored)} activos...")

    for coin_id in monitored:
        info = prices_data.get(coin_id)
        if not info:
            continue

        price     = info.get("usd", 0)
        change_24 = info.get("usd_24h_change", 0) or 0
        prev      = price_cache.get(coin_id)

        # ── Detectar cambio brusco en precio (vs tick anterior) ───────────────
        spike_alert = False
        if prev and prev > 0:
            pct_change = (price - prev) / prev * 100
            if abs(pct_change) >= 3:          # ±3% en 5 minutos = alerta de spike
                spike_alert = True

        price_cache[coin_id] = price

        # ── Análisis técnico ──────────────────────────────────────────────────
        result = analyse(coin_id, price, change_24)
        key    = f"{coin_id}_{result['signal']}"

        # No repetir la misma señal más de una vez cada 2 horas
        last_alert = alerted.get(key, 0)
        now_ts     = datetime.now().timestamp()
        if now_ts - last_alert < 7200:
            continue

        should_alert = False
        alert_type   = ""

        if "COMPRAR" in result["signal"] and result["confidence"] >= 65:
            should_alert = True
            alert_type   = "COMPRA"
        elif "VENTA" in result["signal"] or "VENDER" in result["signal"]:
            if result["confidence"] >= 65:
                should_alert = True
                alert_type   = "VENTA"
        elif spike_alert:
            should_alert = True
            alert_type   = f"MOVIMIENTO BRUSCO ({pct_change:+.1f}% en 5 min)"

        if should_alert and CHAT_ID:
            msg = build_alert_msg(coin_id, price, change_24, result, alert_type)
            await bot.send_message(
                chat_id    = CHAT_ID,
                text       = msg,
                parse_mode = "Markdown",
            )
            alerted[key] = now_ts
            log.info(f"  ✅ Alerta enviada: {sym(coin_id)} → {result['signal']}")
        else:
            log.info(f"  {sym(coin_id)}: {result['signal']} ({result['confidence']}%) — sin alerta")

# ── Comandos Telegram ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Bienvenido a tu Crypto Alert Bot*\n\n"
        "Te avisaré automáticamente cuando detecte señales de *compra o venta*.\n\n"
        "*Comandos disponibles:*\n"
        "  /estado — Ver activos monitorizados\n"
        "  /añadir bitcoin — Añadir una cripto\n"
        "  /quitar bitcoin — Quitar una cripto\n"
        "  /analizar — Análisis inmediato de todo\n"
        "  /precio bitcoin — Precio actual\n"
        "  /ayuda — Ver todos los comandos\n\n"
        "📡 Monitorización activa cada 5 minutos."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not monitored:
        await update.message.reply_text("No hay activos monitorizados. Usa /añadir bitcoin")
        return
    lines = ["📡 *Activos monitorizados:*\n"]
    prices_data = get_prices(monitored)
    for coin in monitored:
        info  = prices_data.get(coin, {})
        price = info.get("usd", 0)
        chg   = info.get("usd_24h_change", 0) or 0
        emoji = "🟢" if chg >= 0 else "🔴"
        lines.append(f"  {emoji} *{sym(coin)}* — {fmt_price(price)} ({chg:+.2f}%)")
    lines.append(f"\n_Próximo análisis en ~{CHECK_INTERVAL//60} min_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_añadir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /añadir bitcoin\nEjemplos: bitcoin, ethereum, solana, ripple, cardano")
        return
    coin = ctx.args[0].lower().strip()
    if coin in monitored:
        await update.message.reply_text(f"⚠️ {coin} ya está en la lista.")
        return
    # Verificar que existe en CoinGecko
    test = get_prices([coin])
    if not test:
        await update.message.reply_text(f"❌ No encontré '{coin}'. Usa el ID de CoinGecko (ej: bitcoin, ethereum, solana)")
        return
    monitored.append(coin)
    await update.message.reply_text(f"✅ *{sym(coin)}* añadido a la monitorización.", parse_mode="Markdown")

async def cmd_quitar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /quitar bitcoin")
        return
    coin = ctx.args[0].lower().strip()
    if coin not in monitored:
        await update.message.reply_text(f"⚠️ {coin} no está en la lista.")
        return
    monitored.remove(coin)
    await update.message.reply_text(f"🗑️ *{sym(coin)}* eliminado de la monitorización.", parse_mode="Markdown")

async def cmd_precio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    coin = ctx.args[0].lower().strip() if ctx.args else "bitcoin"
    data = get_prices([coin])
    if not data or coin not in data:
        await update.message.reply_text(f"❌ No encontré '{coin}'.")
        return
    info  = data[coin]
    price = info.get("usd", 0)
    chg24 = info.get("usd_24h_change", 0) or 0
    chg7  = info.get("usd_7d_change",  0) or 0
    vol   = info.get("usd_24h_vol",    0) or 0
    msg = (
        f"💰 *{sym(coin)} — Precio actual*\n\n"
        f"  Precio: *{fmt_price(price)}*\n"
        f"  Cambio 24h: {'🟢' if chg24>=0 else '🔴'} {chg24:+.2f}%\n"
        f"  Cambio 7d:  {'🟢' if chg7>=0 else '🔴'} {chg7:+.2f}%\n"
        f"  Volumen 24h: ${vol/1e9:.2f}B\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_analizar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analizando todos los activos, espera un momento...")
    prices_data = get_prices(monitored)
    lines = ["📊 *Análisis completo:*\n"]
    for coin in monitored:
        info  = prices_data.get(coin, {})
        price = info.get("usd", 0)
        chg24 = info.get("usd_24h_change", 0) or 0
        result = analyse(coin, price, chg24)
        lines.append(
            f"*{sym(coin)}* {fmt_price(price)}\n"
            f"  → {result['signal']} ({result['confidence']}%) | RSI: {result['rsi']}\n"
        )
    lines.append("_⚠️ Solo informativo._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_ayuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 *Comandos disponibles:*\n\n"
        "/start — Bienvenida\n"
        "/estado — Ver activos y precios actuales\n"
        "/analizar — Análisis técnico inmediato de todo\n"
        "/precio bitcoin — Precio de una cripto\n"
        "/añadir bitcoin — Añadir cripto a la lista\n"
        "/quitar bitcoin — Quitar cripto de la lista\n"
        "/ayuda — Este mensaje\n\n"
        "*IDs válidos:* bitcoin, ethereum, solana, ripple, cardano, "
        "dogecoin, avalanche-2, chainlink, polkadot, binancecoin\n\n"
        "_El bot analiza automáticamente cada 5 minutos._"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("❌ Falta TELEGRAM_TOKEN en las variables de entorno")
    if not CHAT_ID:
        raise ValueError("❌ Falta CHAT_ID en las variables de entorno")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("estado",   cmd_estado))
    app.add_handler(CommandHandler("analizar", cmd_analizar))
    app.add_handler(CommandHandler("precio",   cmd_precio))
    app.add_handler(CommandHandler("anadir",   cmd_añadir))
    app.add_handler(CommandHandler("quitar",   cmd_quitar))
    app.add_handler(CommandHandler("ayuda",    cmd_ayuda))

    # Arrancar monitor en background
    loop = asyncio.get_event_loop()
    loop.create_task(monitor_loop(app.bot))

    log.info("✅ Bot arrancado correctamente")
    app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
