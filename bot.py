"""
CRYPTO BOT PRO v20.2.2-fix — OKX USDC Watcher (Mainnet forzada)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Exchange  : OKX SPOT · Mainnet · sandbox=False · hostname=www.okx.com
Moneda    : USDC (no USDT)
Capital   : ~91.94 USDC → 25 USDC por operación · máx. 3 abiertas
Vigilancia: TAO/USDC · RENDER/USDC · FET/USDC · SOL/USDC
Señal     : RSI < 32 + MACD girando al alza + BTC estable (no cae >1%/1h)
Riesgo    : SL -2.5% · TP +4% · Kill Switch -5%/día
Telegram  : Solo notificaciones · sin comandos · lenguaje llano

CORRECCIONES vs v20.2.2:
  · sandbox=False forzado explícitamente (evita error OKX 50119)
  · hostname="www.okx.com" fija el endpoint de Mainnet
  · Diagnóstico de credenciales en log al arrancar
  · Primeros 4 chars de API key en log si falla autenticación

VARIABLES DE ENTORNO:
  TELEGRAM_TOKEN, CHAT_ID
  OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
  DRY_RUN=true
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, json, asyncio, logging, time, math
from datetime import datetime, timezone, date
from functools import partial
from typing import Optional

import ccxt
from telegram import Bot

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET     = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
DRY_RUN        = os.environ.get("DRY_RUN", "true").lower() == "true"

# ── Persistencia ──────────────────────────────────────────────────────────────
POSITIONS_FILE = "positions.json"

# ── Timing ────────────────────────────────────────────────────────────────────
TRADE_LOOP_SEC = 15 * 60   # análisis cada 15 minutos
RISK_LOOP_SEC  = 60        # vigilancia de riesgo cada 60 segundos

# ── Capital (en USDC) ─────────────────────────────────────────────────────────
TRADE_USDC     = 25.0      # dólares por compra
MAX_POSITIONS  = 3         # máximo de compras simultáneas
MIN_TRADE_USDC = 5.0       # mínimo operacional

# ── Riesgo ────────────────────────────────────────────────────────────────────
STOP_LOSS_PCT    = 2.5
TAKE_PROFIT_PCT  = 4.0
KILL_SWITCH_PCT  = 5.0     # parar todo si el total cae > 5% en el día

# ── Estrategia ────────────────────────────────────────────────────────────────
TIMEFRAME        = "15m"
OHLCV_LIMIT      = 100
RSI_BUY          = 32
BTC_DROP_BLOCK   = 1.0     # no comprar si BTC bajó > 1% en la última hora

# ── Lista fija de monedas a vigilar (en USDC) ─────────────────────────────────
WATCHLIST = [
    "TAO/USDC",
    "RENDER/USDC",
    "FET/USDC",
    "SOL/USDC",
]

# Nombres amigables para los mensajes de Telegram
COIN_NAMES = {
    "TAO/USDC":    "TAO (Bittensor)",
    "RENDER/USDC": "Render",
    "FET/USDC":    "Fetch.AI",
    "SOL/USDC":    "Solana",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════════════
state: dict = {
    "positions":          {},
    "kill_switch":        False,
    "kill_switch_reason": "",
    "daily_start_bal":    None,
    "daily_date":         None,
    "daily_realized_pnl": 0.0,
    "trades_today":       0,
    "last_scan":          "Nunca",
}

def load_state():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE) as f:
                state.update(json.load(f))
            log.info("Estado cargado — %d posiciones abiertas",
                     len(state["positions"]))
        except Exception as e:
            log.warning("Error cargando estado: %s", e)

def save_state():
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error("Error guardando estado: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGE — OKX via CCXT
# ══════════════════════════════════════════════════════════════════════════════
_exchange: Optional[ccxt.okx] = None

def get_exchange() -> ccxt.okx:
    """
    Conector OKX SPOT — Mainnet forzada.

    Parámetros críticos para evitar el error 50119 ("API key doesn't exist"):
    ─────────────────────────────────────────────────────────────────────────
    · sandbox=False          → fuerza explícitamente la red real (Mainnet).
                               Sin este flag, algunas versiones de CCXT pueden
                               intentar conectar al entorno de demo de OKX,
                               donde las claves reales no existen.

    · hostname="www.okx.com" → fija el endpoint HTTP a la URL de producción.
                               Equivalente a escribir directamente la dirección
                               de la Mainnet, sin depender de la lógica interna
                               de CCXT para elegir el host.

    · adjustForTimeDifference=True → OKX rechaza peticiones con timestamp
                               desviado >30 s del servidor. Railway usa IPs
                               dinámicas cuyo reloj puede desviarse; este flag
                               hace que CCXT consulte el tiempo del servidor
                               antes de firmar la petición.

    · nonce → función personalizada basada en time.time_ns() para máxima
               precisión del timestamp en nanosegundos, lo que reduce aún más
               los rechazos por firma inválida.
    """
    global _exchange
    if _exchange is None:
        # ── Diagnóstico de credenciales (visible en los logs de Railway) ──────
        key_preview        = (OKX_API_KEY[:4]    + "****") if OKX_API_KEY    else "❌ VACÍA"
        secret_preview     = (OKX_SECRET[:4]     + "****") if OKX_SECRET     else "❌ VACÍA"
        passphrase_preview = ("****" + OKX_PASSPHRASE[-2:]) if OKX_PASSPHRASE else "❌ VACÍA"
        log.info("═══ DIAGNÓSTICO DE CREDENCIALES OKX ═══")
        log.info("  OKX_API_KEY       → empieza por: %s", key_preview)
        log.info("  OKX_SECRET_KEY    → empieza por: %s", secret_preview)
        log.info("  OKX_PASSPHRASE    → termina en:  %s", passphrase_preview)
        log.info("  DRY_RUN           → %s", DRY_RUN)
        log.info("  Endpoint forzado  → https://www.okx.com (Mainnet)")
        log.info("════════════════════════════════════════")

        _exchange = ccxt.okx({
            "apiKey":   OKX_API_KEY,
            "secret":   OKX_SECRET,
            "password": OKX_PASSPHRASE,   # passphrase — tercer factor de OKX

            # ── MAINNET FORZADA ───────────────────────────────────────────────
            "sandbox":  False,            # jamás usar la red de demo
            "hostname": "www.okx.com",    # endpoint de producción explícito

            "options": {
                "defaultType":             "spot",   # cuenta Trading, mercado Spot
                "adjustForTimeDifference": True,     # sincroniza timestamp con OKX
            },

            "enableRateLimit": True,      # CCXT gestiona el rate limit automáticamente
        })
    return _exchange

async def _async(fn, *args, **kwargs):
    """Ejecuta función síncrona de CCXT en el threadpool sin bloquear asyncio."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

# ══════════════════════════════════════════════════════════════════════════════
# INDICADORES TÉCNICOS (puro Python, sin librerías externas)
# ══════════════════════════════════════════════════════════════════════════════
def _ema(prices: list, period: int) -> float:
    if not prices:
        return 0.0
    k, e = 2.0 / (period + 1), float(prices[0])
    for p in prices[1:]:
        e = float(p) * k + e * (1.0 - k)
    return e

def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d,  0.0) for d in deltas[-period:]]
    losses = [max(-d, 0.0) for d in deltas[-period:]]
    ag, al = sum(gains) / period, sum(losses) / period
    return 100.0 if al == 0 else round(100.0 - 100.0 / (1.0 + ag / al), 2)

def _macd_histogram_series(closes: list) -> list:
    """Serie de histogramas MACD para detectar el giro alcista."""
    if len(closes) < 35:
        return []
    macd_vals = [
        _ema(closes[:i + 1], 12) - _ema(closes[:i + 1], 26)
        for i in range(26, len(closes))
    ]
    signal_vals = [
        _ema(macd_vals[:i + 1], 9)
        for i in range(8, len(macd_vals))
    ]
    if len(signal_vals) < 2:
        return []
    return [m - s for m, s in zip(macd_vals[8:], signal_vals)]

def _macd_turning_bullish(closes: list) -> bool:
    """
    True si el MACD muestra un giro alcista:
    · Cruce confirmado: histograma pasó de negativo a ≥0
    · Giro inminente:   histograma negativo pero creciendo 2 velas seguidas
    """
    hist = _macd_histogram_series(closes)
    if len(hist) < 3:
        return False
    h0, h1, h2 = hist[-1], hist[-2], hist[-3]
    cross_up = h1 < 0.0 <= h0
    turning  = h0 < 0.0 and h0 > h1 > h2
    return cross_up or turning

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════
def _round_step(value: float, step: float) -> float:
    """Trunca al múltiplo de step más cercano por debajo (nunca superar el saldo)."""
    if step <= 0:
        return value
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1.0 else 0
    return round(math.floor(value / step) * step, dec)

def _fetch_usdc_free() -> float:
    """Saldo libre de USDC en la cuenta Trading (Spot) de OKX."""
    ex  = get_exchange()
    bal = ex.fetch_balance()
    return float((bal.get("USDC") or {}).get("free", 0.0) or 0.0)

def _fetch_total_portfolio_usdc() -> float:
    """
    Valoración total en USDC:
    USDC libre + valor de mercado de los tokens en posiciones abiertas.
    """
    ex    = get_exchange()
    bal   = ex.fetch_balance()
    total = float((bal.get("USDC") or {}).get("total", 0.0) or 0.0)

    skip = {"USDC", "USDT", "info", "free", "used", "total",
            "timestamp", "datetime"}
    for coin, amounts in bal.items():
        if coin in skip:
            continue
        qty = float((amounts or {}).get("total", 0.0) or 0.0)
        if qty < 1e-8:
            continue
        # Intentar primero el par /USDC, luego /USDT como fallback
        for quote in ("USDC", "USDT"):
            try:
                tk    = ex.fetch_ticker(f"{coin}/{quote}")
                price = float(tk.get("last") or 0.0)
                if price > 0:
                    total += qty * price
                    break
            except Exception:
                continue
    return total

def _fetch_ohlcv_closes(symbol: str, tf: str = "15m",
                         limit: int = OHLCV_LIMIT) -> list:
    ex    = get_exchange()
    ohlcv = ex.fetch_ohlcv(symbol, tf, limit=limit)
    return [float(c[4]) for c in ohlcv] if ohlcv else []

def _fetch_price(symbol: str) -> float:
    ex = get_exchange()
    return float((ex.fetch_ticker(symbol).get("last")) or 0.0)

def _fetch_btc_1h_change() -> float:
    """Cambio % del BTC en la última hora completa (vela 1h)."""
    ex    = get_exchange()
    # Usamos BTC/USDT ya que BTC/USDC puede no tener liquidez en OKX
    for pair in ("BTC/USDC", "BTC/USDT"):
        try:
            ohlcv = ex.fetch_ohlcv(pair, "1h", limit=2)
            if ohlcv:
                c = ohlcv[-1]
                o, cl = float(c[1]), float(c[4])
                return ((cl - o) / o * 100.0) if o else 0.0
        except Exception:
            continue
    return 0.0

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — MENSAJES EN LENGUAJE LLANO
# ══════════════════════════════════════════════════════════════════════════════
async def _notify(bot: Bot, text: str):
    """Envía mensaje de texto plano (sin HTML técnico) al usuario."""
    if not CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        log.error("Error enviando Telegram: %s", e)

def _coin_label(symbol: str) -> str:
    """Nombre amigable de la moneda para los mensajes."""
    return COIN_NAMES.get(symbol, symbol.split("/")[0])

async def _msg_compra(bot: Bot, symbol: str, invested: float,
                      price: float, total_bal: float):
    """Mensaje de compra en lenguaje completamente llano."""
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN - sin dinero real]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"👀 He visto una oportunidad y he comprado {label} "
        f"usando {invested:.0f} dólares. ¡A ver cómo sale!\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC"
    )

async def _msg_venta_ganancia(bot: Bot, symbol: str, pnl: float,
                               total_bal: float):
    """Mensaje de venta con ganancias en lenguaje llano."""
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN - sin dinero real]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"🎉 ¡Buenas noticias! He vendido {label} y hemos ganado "
        f"{pnl:.2f} dólares. ¡Tu dinero total ahora es mayor!\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC"
    )

async def _msg_venta_perdida(bot: Bot, symbol: str, pnl: float,
                              total_bal: float):
    """Mensaje de venta con pérdidas en lenguaje llano."""
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN - sin dinero real]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"😔 Hoy no ha podido ser. He tenido que vender {label} "
        f"para proteger el dinero y hemos perdido {abs(pnl):.2f} dólares. "
        f"Seguimos buscando la próxima.\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC"
    )

async def _msg_kill_switch(bot: Bot, total_bal: float, drawdown: float):
    """Aviso de kill switch en lenguaje llano."""
    await _notify(bot,
        f"🛑 He pausado todas las compras porque el dinero ha bajado "
        f"un {drawdown:.1f}% hoy, que es más de lo que me has dicho que tolere.\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC\n\n"
        f"No haré nada hasta mañana que se reinicie el contador."
    )

async def _msg_arranque(bot: Bot, total_bal: float, btc_ok: bool):
    """Mensaje de arranque en lenguaje llano."""
    dry_note = "⚠️ Estoy en modo SIMULACIÓN — no se mueve dinero real.\n\n" if DRY_RUN else ""
    btc_msg  = "Bitcoin está estable hoy ✅" if btc_ok else "Bitcoin no está bien ahora ⛔"
    await _notify(bot,
        f"{dry_note}"
        f"¡Hola! 👋 Acabo de arrancar y estoy listo para vigilar el mercado.\n\n"
        f"Voy a estar pendiente de:\n"
        f"  · TAO (Bittensor)\n"
        f"  · Render\n"
        f"  · Fetch.AI\n"
        f"  · Solana\n\n"
        f"Usaré 25 dólares por cada compra y no haré más de 3 a la vez.\n"
        f"{btc_msg}\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC"
    )

async def _msg_error_grave(bot: Bot, motivo: str):
    """Aviso de error grave en lenguaje llano."""
    await _notify(bot,
        f"⚠️ Ha ocurrido un problema técnico y no puedo operar ahora mismo.\n\n"
        f"Motivo técnico (para tu información): {motivo}\n\n"
        f"Lo seguiré intentando en el próximo ciclo."
    )

# ══════════════════════════════════════════════════════════════════════════════
# EJECUCIÓN DE ÓRDENES
# ══════════════════════════════════════════════════════════════════════════════
def _execute_buy(symbol: str, usdc_amount: float) -> dict:
    """
    Compra MARKET en OKX SPOT pagando `usdc_amount` USDC.
    tdMode='cash' es obligatorio para órdenes Spot en OKX.
    """
    ex     = get_exchange()
    market = ex.markets.get(symbol, {})
    price  = _fetch_price(symbol)
    if price <= 0:
        raise ValueError(f"Precio inválido para {symbol}")

    lot_step = (
        market.get("precision", {}).get("amount")
        or (market.get("limits") or {}).get("amount", {}).get("min", 0.0001)
        or 0.0001
    )
    quantity = _round_step(usdc_amount / price, lot_step)
    min_qty  = ((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0
    if quantity < min_qty:
        quantity = _round_step(min_qty * 1.05, lot_step)

    if DRY_RUN:
        order_id   = f"DRY-BUY-{int(time.time())}"
        exec_price = price
        log.info("[DRY RUN] BUY %s qty=%.8f @ %.6f USDC", symbol, quantity, price)
    else:
        try:
            order = ex.createMarketBuyOrderWithCost(
                symbol, usdc_amount,
                params={"tdMode": "cash"}
            )
        except (ccxt.NotSupported, AttributeError):
            order = ex.create_market_buy_order(
                symbol, quantity,
                params={"tdMode": "cash"}
            )
        order_id   = str(order.get("id", ""))
        exec_price = float(order.get("average") or order.get("price") or price)
        quantity   = float(order.get("filled") or order.get("amount") or quantity)
        log.info("BUY OKX: %s id=%s qty=%.8f @ %.6f", symbol, order_id, quantity, exec_price)

    return {
        "symbol":      symbol,
        "entry_price": exec_price,
        "quantity":    quantity,
        "invested":    round(quantity * exec_price, 4),
        "peak_price":  exec_price,
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "order_id":    order_id,
    }

def _execute_sell(symbol: str, quantity: float) -> dict:
    """Venta MARKET en OKX SPOT con tdMode='cash'."""
    ex = get_exchange()
    if DRY_RUN:
        price    = _fetch_price(symbol)
        order_id = f"DRY-SELL-{int(time.time())}"
        log.info("[DRY RUN] SELL %s qty=%.8f @ %.6f USDC", symbol, quantity, price)
    else:
        order    = ex.create_market_sell_order(
            symbol, quantity,
            params={"tdMode": "cash"}
        )
        order_id = str(order.get("id", ""))
        exec_p   = order.get("average") or order.get("price")
        price    = float(exec_p) if exec_p else _fetch_price(symbol)
        log.info("SELL OKX: %s id=%s qty=%.8f @ %.6f", symbol, order_id, quantity, price)

    return {
        "price":    price,
        "proceeds": round(price * quantity, 4),
        "order_id": order_id,
    }

# ══════════════════════════════════════════════════════════════════════════════
# GESTIÓN DE CAPITAL Y RIESGO
# ══════════════════════════════════════════════════════════════════════════════
def _slots_available() -> int:
    return MAX_POSITIONS - len(state["positions"])

def _reset_daily_if_needed(total: float):
    today = date.today().isoformat()
    if state.get("daily_date") != today:
        state["daily_date"]         = today
        state["daily_start_bal"]    = total
        state["daily_realized_pnl"] = 0.0
        state["trades_today"]       = 0
        log.info("Reset diario — saldo inicial: %.2f USDC", total)
        save_state()

def _check_kill_switch(total: float) -> tuple[bool, float]:
    """Devuelve (activado, drawdown_pct)."""
    if state["kill_switch"]:
        return True, 0.0
    start = state.get("daily_start_bal") or 0.0
    if start <= 0:
        return False, 0.0
    dd = (start - total) / start * 100.0
    if dd >= KILL_SWITCH_PCT:
        state["kill_switch"]        = True
        state["kill_switch_reason"] = f"Drawdown {dd:.2f}% > {KILL_SWITCH_PCT}%"
        save_state()
        log.warning("⛔ KILL SWITCH: drawdown %.2f%%", dd)
        return True, dd
    return False, dd

# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS
# ══════════════════════════════════════════════════════════════════════════════
async def _analyze(symbol: str) -> Optional[dict]:
    """Descarga velas 15m y calcula indicadores. Devuelve None si falla."""
    try:
        closes = await _async(_fetch_ohlcv_closes, symbol, "15m", OHLCV_LIMIT)
        if len(closes) < 35:
            return None
        return {
            "rsi":     _rsi(closes),
            "bullish": _macd_turning_bullish(closes),
            "price":   closes[-1],
        }
    except Exception as e:
        log.debug("Error analizando %s: %s", symbol, e)
        return None

# ══════════════════════════════════════════════════════════════════════════════
# OPERACIONES CON NOTIFICACIÓN LLANA
# ══════════════════════════════════════════════════════════════════════════════
async def _buy(symbol: str, bot: Bot) -> bool:
    """Ejecuta la compra y envía mensaje amigable por Telegram."""
    if symbol in state["positions"] or _slots_available() <= 0:
        return False

    try:
        free = await _async(_fetch_usdc_free)
    except Exception as e:
        log.error("Error obteniendo USDC libre: %s", e)
        return False

    amount = min(TRADE_USDC, free * 0.98)
    if amount < MIN_TRADE_USDC:
        log.warning("USDC insuficiente para %s (%.2f disponible)", symbol, free)
        return False

    try:
        result = await _async(_execute_buy, symbol, amount)
    except Exception as e:
        log.error("Fallo comprando %s: %s", symbol, e)
        await _msg_error_grave(bot, f"No pude comprar {_coin_label(symbol)}: {e}")
        return False

    state["positions"][symbol] = result
    state["trades_today"]      = state.get("trades_today", 0) + 1
    save_state()

    try:
        total_bal = await _async(_fetch_total_portfolio_usdc)
    except Exception:
        total_bal = 0.0

    await _msg_compra(bot, symbol, result["invested"], result["entry_price"], total_bal)
    log.info("COMPRA: %s %.2f USDC @ %.6f", symbol, result["invested"], result["entry_price"])
    return True

async def _sell(symbol: str, pos: dict, bot: Bot):
    """Ejecuta la venta y envía el mensaje amigable correspondiente."""
    try:
        result = await _async(_execute_sell, symbol, pos["quantity"])
    except Exception as e:
        log.error("Fallo vendiendo %s: %s", symbol, e)
        await _msg_error_grave(bot, f"No pude vender {_coin_label(symbol)}: {e}")
        return

    invested = pos["invested"]
    proceeds = result["proceeds"]
    pnl      = proceeds - invested

    state["positions"].pop(symbol, None)
    state["daily_realized_pnl"] = round(
        state.get("daily_realized_pnl", 0.0) + pnl, 4
    )
    save_state()

    try:
        total_bal = await _async(_fetch_total_portfolio_usdc)
    except Exception:
        total_bal = 0.0

    if pnl >= 0:
        await _msg_venta_ganancia(bot, symbol, pnl, total_bal)
    else:
        await _msg_venta_perdida(bot, symbol, pnl, total_bal)

    log.info("VENTA: %s P&L %+.2f USDC", symbol, pnl)

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE TRADING — cada 15 minutos
# ══════════════════════════════════════════════════════════════════════════════
async def trading_loop(bot: Bot):
    """
    1. Comprobar saldo y kill switch
    2. Filtro BTC (protector)
    3. Analizar cada moneda del WATCHLIST
    4. Comprar si RSI < 32 + MACD alcista
    """
    now = datetime.now(timezone.utc)
    log.info("━━━ CICLO %s ━━━", now.strftime("%d/%m %H:%M"))
    state["last_scan"] = now.isoformat()

    # ── Saldo total ───────────────────────────────────────────────────────────
    try:
        total = await _async(_fetch_total_portfolio_usdc)
    except Exception as e:
        log.error("Error obteniendo portfolio: %s", e)
        return

    _reset_daily_if_needed(total)

    killed, dd = _check_kill_switch(total)
    if killed:
        if dd > 0:
            await _msg_kill_switch(bot, total, dd)
        log.warning("Kill switch activo — ciclo omitido")
        return

    if _slots_available() <= 0:
        log.info("Posiciones llenas (%d/%d)", len(state["positions"]), MAX_POSITIONS)
        return

    # ── Filtro BTC ────────────────────────────────────────────────────────────
    try:
        btc_chg = await _async(_fetch_btc_1h_change)
        btc_ok  = btc_chg > -BTC_DROP_BLOCK
        log.info("BTC 1h: %+.2f%% — %s", btc_chg, "OK" if btc_ok else "BLOQUEADO")
    except Exception as e:
        log.warning("Error filtro BTC: %s", e)
        btc_ok = True   # si no podemos saber, seguimos (conservador)

    if not btc_ok:
        log.info("Filtro BTC: compras pausadas (BTC %.2f%%)", btc_chg)
        return

    # ── Analizar WATCHLIST ────────────────────────────────────────────────────
    for symbol in WATCHLIST:
        if _slots_available() <= 0:
            break
        if symbol in state["positions"]:
            continue

        a = await _analyze(symbol)
        await asyncio.sleep(0.5)   # cortesía con el rate limit de OKX

        if not a:
            log.debug("%s — sin datos suficientes", symbol)
            continue

        log.info("%s RSI=%.1f MACD_bull=%s", symbol, a["rsi"], a["bullish"])

        if a["rsi"] < RSI_BUY and a["bullish"]:
            log.info("Señal de compra: %s", symbol)
            await _buy(symbol, bot)
            await asyncio.sleep(1.0)

    log.info("━━━ FIN CICLO — posiciones: %d/%d ━━━",
             len(state["positions"]), MAX_POSITIONS)

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE RIESGO — cada 60 segundos
# ══════════════════════════════════════════════════════════════════════════════
async def risk_loop(bot: Bot):
    """Vigila SL y TP de todas las posiciones abiertas."""
    if not state["positions"]:
        return

    for symbol, pos in list(state["positions"].items()):
        try:
            price = await _async(_fetch_price, symbol)
            if price <= 0:
                continue

            entry   = pos["entry_price"]
            pnl_pct = (price - entry) / entry * 100.0

            if price > pos.get("peak_price", entry):
                state["positions"][symbol]["peak_price"] = price

            if pnl_pct <= -STOP_LOSS_PCT:
                log.warning("SL: %s %.2f%%", symbol, pnl_pct)
                await _sell(symbol, pos, bot)

            elif pnl_pct >= TAKE_PROFIT_PCT:
                log.info("TP: %s +%.2f%%", symbol, pnl_pct)
                await _sell(symbol, pos, bot)

        except Exception as e:
            log.error("Error risk_loop %s: %s", symbol, e)

    save_state()

# ══════════════════════════════════════════════════════════════════════════════
# ORQUESTADOR
# ══════════════════════════════════════════════════════════════════════════════
async def _run_trading_loop(bot: Bot):
    await asyncio.sleep(15)
    log.info("Trading loop activo — cada %d min", TRADE_LOOP_SEC // 60)
    while True:
        try:
            await trading_loop(bot)
        except Exception as e:
            log.error("Error crítico en trading_loop: %s", e)
            await _msg_error_grave(bot, str(e))
        await asyncio.sleep(TRADE_LOOP_SEC)

async def _run_risk_loop(bot: Bot):
    await asyncio.sleep(30)
    log.info("Risk loop activo — cada %ds", RISK_LOOP_SEC)
    while True:
        try:
            await risk_loop(bot)
        except Exception as e:
            log.error("Error crítico en risk_loop: %s", e)
        await asyncio.sleep(RISK_LOOP_SEC)

# ══════════════════════════════════════════════════════════════════════════════
# ARRANQUE
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    """
    Punto de entrada principal.
    Verifica OKX en 3 pasos, arranca los bucles y envía el mensaje de bienvenida.
    """
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no configurado")

    log.info("════ INICIANDO BOT v20.2.2-fix — OKX USDC WATCHER (Mainnet) ════")

    # Construir el objeto Bot de Telegram (sin Application — no necesitamos comandos)
    bot = Bot(token=TELEGRAM_TOKEN)

    # ── Verificación OKX ──────────────────────────────────────────────────────
    total_bal = 0.0
    btc_ok    = True
    connected = False

    try:
        ex = get_exchange()

        log.info("[1/3] Cargando mercados OKX...")
        await _async(ex.load_markets)
        log.info("✅ [1/3] %d mercados disponibles", len(ex.markets))

        log.info("[2/3] Verificando autenticación y saldo USDC...")
        total_bal = await _async(_fetch_total_portfolio_usdc)
        free_usdc = await _async(_fetch_usdc_free)
        log.info("✅ [2/3] Auth OK — USDC libre: %.2f / total: %.2f",
                 free_usdc, total_bal)
        connected = True

        log.info("[3/3] Verificando pares del WATCHLIST en OKX...")
        for sym in WATCHLIST:
            if sym in ex.markets:
                log.info("  ✅ %s disponible", sym)
            else:
                log.warning("  ⚠️ %s NO encontrado en OKX — revisar símbolo", sym)

        # Comprobar BTC para el mensaje de arranque
        btc_chg = await _async(_fetch_btc_1h_change)
        btc_ok  = btc_chg > -BTC_DROP_BLOCK
        log.info("✅ [3/3] BTC 1h: %+.2f%%", btc_chg)

    except ccxt.AuthenticationError as e:
        # Error 50119 = "API key doesn't exist" en OKX
        # Las causas más frecuentes son:
        #   1. La key apunta al entorno demo en lugar de Mainnet
        #   2. La variable de entorno llega vacía o con espacios
        #   3. El passphrase no coincide con el de la key
        key_preview = (OKX_API_KEY[:4] + "****") if OKX_API_KEY else "❌ VACÍA"
        log.critical("❌ Error de autenticación OKX (código 50119 o similar)")
        log.critical("   Mensaje original: %s", e)
        log.critical("   OKX_API_KEY  → empieza por: %s  (longitud: %d)",
                     key_preview, len(OKX_API_KEY))
        log.critical("   OKX_SECRET   → longitud: %d  (esperada: 32 chars aprox.)",
                     len(OKX_SECRET))
        log.critical("   OKX_PASSPHRASE → longitud: %d  (debe ser > 0)",
                     len(OKX_PASSPHRASE))
        log.critical("   Endpoint usado → www.okx.com (Mainnet, sandbox=False)")
        log.critical("   ─── POSIBLES CAUSAS ───────────────────────────────")
        log.critical("   · La key fue creada en el entorno DEMO de OKX")
        log.critical("     → Crea una nueva key en https://www.okx.com > API")
        log.critical("   · OKX_API_KEY llega vacía desde Railway")
        log.critical("     → Revisa que NO haya espacios antes/después del valor")
        log.critical("   · El Passphrase no coincide con el registrado en la key")
        log.critical("     → Recrea la API key y copia el passphrase al crearla")
        log.critical("   ────────────────────────────────────────────────────")
        await bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"⚠️ No puedo conectar con OKX. Error de clave (código 50119).\n\n"
                f"He guardado el diagnóstico en los logs de Railway.\n"
                f"Posibles causas:\n"
                f"  · La API key fue creada en el entorno de DEMO\n"
                f"  · Alguna variable de entorno llegó vacía\n"
                f"  · El passphrase no coincide con el de la key\n\n"
                f"Clave leída (primeros 4 caracteres): {key_preview}"
            )
        )
        return   # no arrancar sin autenticación válida

    except ccxt.NetworkError as e:
        log.error("❌ Error de red OKX: %s — intentando arrancar de todas formas", e)

    except Exception as e:
        # A veces OKX devuelve el error 50119 envuelto en una ExchangeError genérica
        key_preview = (OKX_API_KEY[:4] + "****") if OKX_API_KEY else "❌ VACÍA"
        log.error("❌ Error inesperado al conectar con OKX: %s", e)
        log.error("   Tipo de error: %s", type(e).__name__)
        log.error("   OKX_API_KEY → empieza por: %s (longitud: %d)",
                  key_preview, len(OKX_API_KEY))
        log.error("   OKX_PASSPHRASE → longitud: %d", len(OKX_PASSPHRASE))
        log.error("   Si ves '50119' en el mensaje anterior, la key apunta al DEMO.")

    # ── Cargar estado previo ───────────────────────────────────────────────────
    load_state()

    # ── Arrancar bucles ───────────────────────────────────────────────────────
    asyncio.create_task(_run_trading_loop(bot))
    asyncio.create_task(_run_risk_loop(bot))

    # ── Mensaje de bienvenida ─────────────────────────────────────────────────
    if connected:
        await _msg_arranque(bot, total_bal, btc_ok)
    else:
        await _notify(bot,
            "He arrancado pero tengo problemas para conectar con el exchange. "
            "Lo seguiré intentando automáticamente."
        )

    log.info("════ BOT LISTO — %s ════", "DRY RUN" if DRY_RUN else "MODO REAL")

    # ── Mantener el proceso vivo ───────────────────────────────────────────────
    # (no usamos Application porque no hay comandos de Telegram)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
