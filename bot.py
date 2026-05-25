"""
CRYPTO BOT PRO — ELITE HFT SCALPER V4
OKX USDC · eea.okx.com · 1m scan / 10s risk
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Exchange  : OKX SPOT · eea.okx.com · sandbox=False
Moneda    : USDC
Capital   : 25 USDC por operación · máx. 3 abiertas
Watchlist : NEAR · FET · RENDER · LINK · SOL (whitelist estricta)
Timeframe : 5m análisis / 1m escaneo / 10s risk

ESTRATEGIA — CUÁDRUPLE CONFIRMACIÓN:
  1. EMA 200 (1h): precio > EMA200 en 1h (macro tendencia alcista)
  2. RSI < 35 en 5m (sobreventa a corto plazo)
  3. MACD bullish crossover en 5m
  4. Volumen actual > promedio 10 velas × 1.2

RIESGO ESCALADO:
  · SL inicial: -1.5%
  · Step 1: max_pnl >= +1.0% → SL sube a +0.1% (break-even)
  · Step 2: max_pnl >= +2.5% → SL sube a +1.2%
  · Step 3: max_pnl >= +3.8% → SL sube a +2.5%
  · TP servidor: orden límite a +4.5% colocada en OKX al instante
    (0ms delay, comisiones maker más bajas)
  · Kill Switch diario: -5%
  · BTC Guard: pausa si BTC cae > 1% en 1h

VELOCIDAD:
  · Scan de entradas: cada 60 s
  · Monitor de riesgo: cada 10 s (fetch_tickers batch, mínimas llamadas)

VARIABLES DE ENTORNO:
  TELEGRAM_TOKEN, CHAT_ID
  OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
  DRY_RUN=true
  DATA_PATH=/app/data  (Railway Volume — opcional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.environ.get("CHAT_ID", "").strip()
OKX_API_KEY    = os.environ.get("OKX_API_KEY", "").strip()
OKX_SECRET     = os.environ.get("OKX_SECRET_KEY", "").strip()
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "").strip()
DRY_RUN        = os.environ.get("DRY_RUN", "true").strip().lower() == "true"

# ── Persistencia ──────────────────────────────────────────────────────────────
_DATA_PATH     = os.environ.get("DATA_PATH", "").strip()
POSITIONS_FILE = os.path.join(_DATA_PATH, "positions.json") if _DATA_PATH \
                 else "positions.json"

# ── Timing ────────────────────────────────────────────────────────────────────
TRADE_LOOP_SEC    = 60     # escaneo de entradas cada 1 minuto
RISK_LOOP_SEC     = 10     # monitor de riesgo cada 10 segundos
DAILY_REPORT_HOUR = 16     # hora UTC del informe diario

# ── Capital (en USDC) ─────────────────────────────────────────────────────────
TRADE_USDC     = 25.0
MAX_POSITIONS  = 3
MIN_TRADE_USDC = 5.0

# ── Riesgo escalado ───────────────────────────────────────────────────────────
STOP_LOSS_PCT   = 1.5    # SL inicial más ajustado para HFT
TAKE_PROFIT_PCT = 4.5    # TP colocado como orden límite en el servidor

# Escalones del trailing stop (max_pnl alcanzado → nuevo SL)
TRAIL_STEPS = [
    (1.0, 0.1),   # max_pnl >= 1.0% → SL = +0.1% (break-even)
    (2.5, 1.2),   # max_pnl >= 2.5% → SL = +1.2%
    (3.8, 2.5),   # max_pnl >= 3.8% → SL = +2.5%
]

KILL_SWITCH_PCT = 5.0    # kill switch si drawdown diario > 5%

# ── Estrategia ────────────────────────────────────────────────────────────────
TIMEFRAME       = "5m"
OHLCV_LIMIT     = 60     # 60 velas × 5m = 5h de historia
EMA200_TF       = "1h"   # timeframe del filtro macro EMA200
EMA200_LIMIT    = 210    # velas 1h para calcular la EMA200 con precisión
RSI_BUY         = 35
VOLUME_MULT     = 1.2
VOLUME_LOOKBACK = 10
BTC_DROP_BLOCK  = 1.0    # no comprar si BTC cae > 1% en 1h

# ── Whitelist estricta ────────────────────────────────────────────────────────
ALLOWED_SYMBOLS = [
    "NEAR/USDC",
    "FET/USDC",
    "RENDER/USDC",
    "LINK/USDC",
    "SOL/USDC",
]
WATCHLIST = ALLOWED_SYMBOLS   # alias

COIN_NAMES = {
    "SOL/USDC":    "Solana",
    "FET/USDC":    "Fetch.AI",
    "RENDER/USDC": "Render",
    "NEAR/USDC":   "NEAR Protocol",
    "LINK/USDC":   "Chainlink",
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
    # symbol → {entry_price, quantity, invested, peak_price, entry_time,
    #            order_id, sl_pct, max_pnl, tp_order_id}
    "positions":          {},
    "kill_switch":        False,
    "kill_switch_reason": "",
    "daily_start_bal":    None,
    "daily_date":         None,
    "daily_realized_pnl": 0.0,
    "trades_today":       0,
    "wins_today":         0,
    "losses_today":       0,
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
    """Escritura atómica — nunca deja el JSON corrupto."""
    try:
        target_dir = os.path.dirname(os.path.abspath(POSITIONS_FILE))
        os.makedirs(target_dir, exist_ok=True)
        tmp = POSITIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, POSITIONS_FILE)
    except Exception as e:
        log.error("Error guardando estado: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGE — OKX EEA
# ══════════════════════════════════════════════════════════════════════════════
_exchange: Optional[ccxt.okx] = None

def get_exchange() -> ccxt.okx:
    """
    OKX SPOT EEA con:
    · retries=5          → reintenta si la red cae
    · networkTimeout=15s → espera hasta 15 s por respuesta EEA
    · enableRateLimit    → CCXT gestiona spacing entre llamadas
    """
    global _exchange
    if _exchange is None:
        _exchange = ccxt.okx({
            "apiKey":   OKX_API_KEY,
            "secret":   OKX_SECRET,
            "password": OKX_PASSPHRASE,
            "sandbox":  False,
            "hostname": "eea.okx.com",
            "enableRateLimit": True,
            "retries":         5,
            "options": {
                "defaultType":             "spot",
                "adjustForTimeDifference": True,
                "networkTimeout":          15000,
            },
        })
    return _exchange

async def _async(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

# ══════════════════════════════════════════════════════════════════════════════
# INDICADORES TÉCNICOS
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

def _macd_bullish(closes: list) -> bool:
    hist = _macd_histogram_series(closes)
    if len(hist) < 3:
        return False
    h0, h1, h2 = hist[-1], hist[-2], hist[-3]
    cross_up = h1 < 0.0 <= h0
    turning  = h0 < 0.0 and h0 > h1 > h2
    return cross_up or turning

def _volume_filter(ohlcv: list) -> bool:
    if len(ohlcv) < VOLUME_LOOKBACK + 1:
        return False
    volumes = [float(c[5]) for c in ohlcv]
    cur_vol = volumes[-1]
    avg_vol = sum(volumes[-(VOLUME_LOOKBACK + 1):-1]) / VOLUME_LOOKBACK
    if avg_vol <= 0:
        return False
    passes = cur_vol >= avg_vol * VOLUME_MULT
    log.debug("Vol: cur=%.0f avg=%.0f ratio=%.2f → %s",
              cur_vol, avg_vol, cur_vol / avg_vol, "OK" if passes else "NO")
    return passes

def _ema200_above(ohlcv_1h: list, current_price: float) -> bool:
    """
    True si current_price está ESTRICTAMENTE por encima de la EMA200 en 1h.
    Requiere al menos 200 velas de 1h. Si no hay suficientes datos,
    se permite la operación por defecto (conservador ante la falta de datos).
    """
    if len(ohlcv_1h) < 200:
        log.debug("EMA200: datos insuficientes (%d velas) — omitiendo filtro",
                  len(ohlcv_1h))
        return True
    closes_1h = [float(c[4]) for c in ohlcv_1h]
    ema200    = _ema(closes_1h, 200)
    above     = current_price > ema200
    log.debug("EMA200(1h): precio=%.5f ema200=%.5f → %s",
              current_price, ema200, "SOBRE" if above else "BAJO")
    return above

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════
def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1.0 else 0
    return round(math.floor(value / step) * step, dec)

def _price_step(symbol: str) -> float:
    """Paso mínimo de precio (tick size) del mercado."""
    ex = get_exchange()
    market = ex.markets.get(symbol, {})
    p = market.get("precision", {}).get("price")
    return p if p and p > 0 else 0.0001

def _fetch_usdc_free() -> float:
    ex  = get_exchange()
    bal = ex.fetch_balance()
    return float((bal.get("USDC") or {}).get("free", 0.0) or 0.0)

def _fetch_total_portfolio_usdc() -> float:
    ex    = get_exchange()
    bal   = ex.fetch_balance()
    total = float((bal.get("USDC") or {}).get("total", 0.0) or 0.0)
    skip  = {"USDC", "USDT", "info", "free", "used", "total",
             "timestamp", "datetime"}
    for coin, amounts in bal.items():
        if coin in skip:
            continue
        qty = float((amounts or {}).get("total", 0.0) or 0.0)
        if qty < 1e-8:
            continue
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

def _fetch_tickers_batch(symbols: list) -> dict:
    """
    fetch_tickers() para un conjunto de símbolos en una sola llamada.
    Retorna {symbol: last_price}. Más eficiente que N×fetch_ticker().
    """
    ex      = get_exchange()
    tickers = ex.fetch_tickers(symbols)
    return {sym: float(tk.get("last") or 0.0)
            for sym, tk in tickers.items()}

def _fetch_ohlcv(symbol: str, tf: str = "5m",
                  limit: int = OHLCV_LIMIT) -> list:
    ex    = get_exchange()
    ohlcv = ex.fetch_ohlcv(symbol, tf, limit=limit)
    return ohlcv if ohlcv else []

def _fetch_price(symbol: str) -> float:
    ex = get_exchange()
    return float((ex.fetch_ticker(symbol).get("last")) or 0.0)

def _fetch_btc_1h_change() -> float:
    ex = get_exchange()
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

def _place_tp_limit_order(symbol: str, quantity: float,
                           entry_price: float) -> str:
    """
    Coloca una orden límite de VENTA a +TAKE_PROFIT_PCT% directamente en OKX.
    Garantiza ejecución instantánea con fee de maker (más barato que taker).
    Devuelve el order_id o "" si DRY_RUN o si falla.
    """
    tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT / 100.0),
                     8)
    # Redondear al tick size del mercado
    tick = _price_step(symbol)
    if tick > 0:
        tp_price = _round_step(tp_price, tick)

    if DRY_RUN:
        fake_id = f"DRY-TP-{int(time.time())}"
        log.info("[DRY RUN] TP limit order: %s qty=%.8f @ %.6f (+%.1f%%)",
                 symbol, quantity, tp_price, TAKE_PROFIT_PCT)
        return fake_id

    try:
        ex    = get_exchange()
        order = ex.create_limit_sell_order(
            symbol, quantity, tp_price,
            params={"tdMode": "cash"}
        )
        oid = str(order.get("id", ""))
        log.info("TP limit order colocada: %s id=%s @ %.6f", symbol, oid, tp_price)
        return oid
    except Exception as e:
        log.warning("No pude colocar TP limit order para %s: %s", symbol, e)
        return ""

def _cancel_order(symbol: str, order_id: str) -> bool:
    """
    Cancela una orden abierta en OKX. Necesario antes de hacer market sell
    cuando el trailing stop se activa (evita doble venta).
    Devuelve True si se canceló correctamente.
    """
    if not order_id or order_id.startswith("DRY-"):
        return True   # en DRY_RUN no hay nada real que cancelar

    try:
        ex = get_exchange()
        ex.cancel_order(order_id, symbol)
        log.info("Orden cancelada: %s id=%s", symbol, order_id)
        return True
    except ccxt.OrderNotFound:
        log.info("Orden %s ya ejecutada o no existe — continuando", order_id)
        return True   # puede que el TP ya se ejecutó — no es error
    except Exception as e:
        log.warning("Error cancelando orden %s para %s: %s", order_id, symbol, e)
        return False

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — MENSAJES
# ══════════════════════════════════════════════════════════════════════════════
async def _notify(bot: Bot, text: str):
    if not CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        log.error("Error enviando Telegram: %s", e)

def _coin_label(symbol: str) -> str:
    return COIN_NAMES.get(symbol, symbol.split("/")[0])

async def _msg_compra(bot: Bot, symbol: str, invested: float,
                      price: float, total_bal: float, reason: str,
                      tp_price: float):
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"👀 He comprado {label} usando {invested:.0f} dólares.\n"
        f"Precio entrada: {price:.5f} USDC\n"
        f"Señal: {reason}\n"
        f"🎯 TP en servidor: {tp_price:.5f} (+{TAKE_PROFIT_PCT}%)\n"
        f"🛑 SL inicial: -{STOP_LOSS_PCT}%\n\n"
        f"💰 Total ahora: {total_bal:.2f} USDC"
    )

async def _msg_venta_ganancia(bot: Bot, symbol: str, pnl: float,
                               total_bal: float, motivo: str):
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"🎉 ¡Ganancia! Vendí {label} y hemos ganado {pnl:.2f} dólares.\n"
        f"({motivo})\n\n"
        f"💰 Total ahora: {total_bal:.2f} USDC"
    )

async def _msg_venta_perdida(bot: Bot, symbol: str, pnl: float,
                              total_bal: float, motivo: str):
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"😔 Vendí {label} con pérdida de {abs(pnl):.2f} dólares.\n"
        f"({motivo})\n\n"
        f"💰 Total ahora: {total_bal:.2f} USDC"
    )

async def _msg_kill_switch(bot: Bot, total_bal: float, drawdown: float):
    await _notify(bot,
        f"🛑 He pausado las compras. El dinero bajó {drawdown:.1f}% hoy.\n\n"
        f"💰 Total ahora: {total_bal:.2f} USDC\n\n"
        f"Se reiniciará mañana automáticamente."
    )

async def _msg_error_grave(bot: Bot, motivo: str):
    await _notify(bot,
        f"⚠️ Problema técnico — siguiente ciclo lo reintentará.\n\n"
        f"Motivo: {motivo}"
    )

async def _msg_sl_step(bot: Bot, symbol: str, step: int,
                        new_sl: float, pnl_pct: float):
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"🔒 Step {step} trailing en {label}\n"
        f"P&L máximo: +{pnl_pct:.2f}%\n"
        f"SL movido a: +{new_sl:.1f}% (protegiendo capital)"
    )

# ══════════════════════════════════════════════════════════════════════════════
# INFORME DE ESTADO
# ══════════════════════════════════════════════════════════════════════════════
async def _send_status_report(bot: Bot):
    log.info("Generando informe de estado...")

    mode_str = "🧪 SIMULACIÓN" if DRY_RUN else "🔴 MODO REAL"
    ks_str   = (
        f"⛔ Kill Switch ACTIVO — {state.get('kill_switch_reason', '')}"
        if state.get("kill_switch") else "✅ Operando"
    )

    try:
        total_bal = await _async(_fetch_total_portfolio_usdc)
        bal_str   = f"{total_bal:.2f} USDC"
    except Exception as e:
        log.warning("Error obteniendo balance: %s", e)
        total_bal, bal_str = 0.0, "No disponible"

    positions = state.get("positions", {})
    if positions:
        pos_lines = []
        for symbol, pos in positions.items():
            entry  = pos.get("entry_price", 0.0)
            inv    = pos.get("invested", 0.0)
            sl_pct = pos.get("sl_pct", -STOP_LOSS_PCT)
            tp_oid = pos.get("tp_order_id", "")
            try:
                cur_price = await _async(_fetch_price, symbol)
                pnl_pct   = ((cur_price - entry) / entry * 100.0) if entry else 0.0
                icon      = "📈" if pnl_pct >= 0 else "📉"
                tp_tag    = " 🎯TP-server" if tp_oid else ""
                pos_lines.append(
                    f"  {icon} {_coin_label(symbol)}{tp_tag}\n"
                    f"     Entrada: {entry:.5f} | Ahora: {cur_price:.5f}\n"
                    f"     Invertido: {inv:.2f} USDC | P&L: {pnl_pct:+.2f}%\n"
                    f"     SL activo: {sl_pct:+.1f}%"
                )
            except Exception:
                pos_lines.append(f"  ⚪ {_coin_label(symbol)} | Sin precio")
        positions_str = "\n".join(pos_lines)
    else:
        positions_str = "  Sin posiciones abiertas."

    wins         = state.get("wins_today", 0)
    losses       = state.get("losses_today", 0)
    total_closed = wins + losses
    success_rate = (
        f"{wins}/{total_closed} ({wins/total_closed*100:.0f}%)"
        if total_closed > 0 else "Sin operaciones cerradas hoy"
    )
    abs_path    = os.path.abspath(POSITIONS_FILE)
    persist_str = (
        f"✅ {abs_path}" if os.path.exists(POSITIONS_FILE)
        else f"⚠️ No encontrado — {abs_path}"
    )
    hora = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    msg = (
        f"📊 Informe — {hora}\n"
        f"{'─' * 34}\n"
        f"Modo       : {mode_str}\n"
        f"Estado     : {ks_str}\n"
        f"Equity     : {bal_str}\n"
        f"P&L hoy    : {state.get('daily_realized_pnl', 0.0):+.2f} USDC\n"
        f"Éxito hoy  : {success_rate}\n"
        f"Últ. ciclo : {state.get('last_scan', 'Nunca')}\n\n"
        f"Posiciones ({len(positions)}/{MAX_POSITIONS}):\n"
        f"{positions_str}\n\n"
        f"Persistencia: {persist_str}"
    )
    await _notify(bot, msg)
    log.info("Informe enviado.")

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE INFORME DIARIO
# ══════════════════════════════════════════════════════════════════════════════
async def daily_report_loop(bot: Bot):
    last_report_date = date.today().isoformat()
    log.info("Daily report loop activo — informe a las %02d:00 UTC", DAILY_REPORT_HOUR)
    while True:
        await asyncio.sleep(60)
        try:
            now       = datetime.now(timezone.utc)
            today_str = now.date().isoformat()
            if now.hour == DAILY_REPORT_HOUR and today_str != last_report_date:
                await _send_status_report(bot)
                last_report_date = today_str
        except Exception as e:
            log.error("Error en daily_report_loop: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# EJECUCIÓN DE ÓRDENES
# ══════════════════════════════════════════════════════════════════════════════
def _execute_buy(symbol: str, usdc_amount: float) -> dict:
    """Compra MARKET en OKX SPOT. Devuelve dict de posición."""
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
        log.info("[DRY RUN] BUY %s qty=%.8f @ %.6f", symbol, quantity, price)
    else:
        try:
            order = ex.createMarketBuyOrderWithCost(
                symbol, usdc_amount, params={"tdMode": "cash"}
            )
        except (ccxt.NotSupported, AttributeError):
            order = ex.create_market_buy_order(
                symbol, quantity, params={"tdMode": "cash"}
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
        "sl_pct":      -STOP_LOSS_PCT,   # SL inicial -1.5%
        "max_pnl":     0.0,              # máximo P&L alcanzado
        "tp_order_id": "",               # se rellena después del buy
    }

def _execute_sell(symbol: str, quantity: float) -> dict:
    """
    Venta MARKET — Zero-Dust: lee saldo real antes de vender.
    Cancelar la TP limit order antes de llamar a esta función.
    """
    ex   = get_exchange()
    coin = symbol.split("/")[0]

    if DRY_RUN:
        price    = _fetch_price(symbol)
        order_id = f"DRY-SELL-{int(time.time())}"
        log.info("[DRY RUN] SELL %s qty=%.8f @ %.6f", symbol, quantity, price)
    else:
        try:
            bal      = ex.fetch_balance()
            real_qty = float((bal.get(coin) or {}).get("free", 0.0) or 0.0)
            if real_qty <= 0:
                raise ValueError(f"Saldo real de {coin} es 0")
            sell_qty = real_qty * 0.999
            if abs(sell_qty - quantity) > 1e-6:
                log.info("SELL %s zero-dust: mem=%.8f real=%.8f venta=%.8f",
                         symbol, quantity, real_qty, sell_qty)
        except Exception as e:
            log.warning("No pude leer saldo real de %s: %s", coin, e)
            sell_qty = quantity

        order    = ex.create_market_sell_order(
            symbol, sell_qty, params={"tdMode": "cash"}
        )
        order_id = str(order.get("id", ""))
        exec_p   = order.get("average") or order.get("price")
        price    = float(exec_p) if exec_p else _fetch_price(symbol)
        quantity = sell_qty
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
    if state.get("daily_date") == today:
        return
    log.info("Nuevo día (%s → %s) — reseteando estadísticas",
             state.get("daily_date", "ninguna"), today)
    state["daily_date"]         = today
    state["daily_start_bal"]    = total
    state["daily_realized_pnl"] = 0.0
    state["trades_today"]       = 0
    state["wins_today"]         = 0
    state["losses_today"]       = 0
    if state.get("kill_switch") and "Drawdown" in state.get("kill_switch_reason", ""):
        log.info("Kill switch de drawdown reseteado para el nuevo día.")
        state["kill_switch"]        = False
        state["kill_switch_reason"] = ""
    log.info("Reset diario — saldo inicial: %.2f USDC", total)
    save_state()

def _check_kill_switch(total: float) -> tuple[bool, float]:
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
        log.warning("KILL SWITCH: drawdown %.2f%%", dd)
        return True, dd
    return False, dd

def _escalate_trailing(pos: dict) -> tuple[float, Optional[int]]:
    """
    Calcula el nuevo SL según los escalones del trailing.
    Devuelve (new_sl_pct, step_number_if_changed_else_None).
    El SL solo sube, nunca baja.
    """
    max_pnl  = pos.get("max_pnl", 0.0)
    cur_sl   = pos.get("sl_pct", -STOP_LOSS_PCT)
    new_sl   = cur_sl
    step_hit = None

    for i, (trigger, floor) in enumerate(TRAIL_STEPS, start=1):
        if max_pnl >= trigger and floor > cur_sl:
            new_sl   = floor
            step_hit = i

    return new_sl, step_hit

# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS — CUÁDRUPLE CONFIRMACIÓN
# ══════════════════════════════════════════════════════════════════════════════
async def _analyze(symbol: str) -> Optional[dict]:
    """
    1. EMA200 (1h): precio > EMA200 → macro tendencia alcista
    2. RSI < 35 (5m)
    3. MACD bullish (5m)
    4. Volumen > avg×1.2 (5m)
    Devuelve dict o None si datos insuficientes / señal no activa.
    """
    try:
        # Obtener datos 1h para EMA200 y datos 5m para señales
        ohlcv_1h = await _async(_fetch_ohlcv, symbol, EMA200_TF, EMA200_LIMIT)
        ohlcv_5m = await _async(_fetch_ohlcv, symbol, TIMEFRAME, OHLCV_LIMIT)

        if len(ohlcv_5m) < max(35, VOLUME_LOOKBACK + 1):
            return None

        closes_5m    = [float(c[4]) for c in ohlcv_5m]
        current_price = closes_5m[-1]

        # Filtro macro EMA200 — primera barrera
        ema_ok  = _ema200_above(ohlcv_1h, current_price)
        rsi     = _rsi(closes_5m)
        bullish = _macd_bullish(closes_5m)
        vol_ok  = _volume_filter(ohlcv_5m)

        return {
            "price":   current_price,
            "rsi":     rsi,
            "bullish": bullish,
            "vol_ok":  vol_ok,
            "ema_ok":  ema_ok,
            # Señal solo si las 4 condiciones se cumplen
            "signal":  ema_ok and rsi < RSI_BUY and bullish and vol_ok,
        }
    except Exception as e:
        log.debug("Error analizando %s: %s", symbol, e)
        return None

# ══════════════════════════════════════════════════════════════════════════════
# OPERACIONES CON NOTIFICACIÓN
# ══════════════════════════════════════════════════════════════════════════════
async def _buy(symbol: str, bot: Bot, reason: str) -> bool:
    """Compra, coloca TP limit order en servidor y notifica."""
    # Whitelist pre-check
    if symbol not in ALLOWED_SYMBOLS:
        log.critical("CRITICAL: compra NO autorizada: %s — cancelada", symbol)
        await _notify(bot, f"🚨 Intento de compra no autorizada: {symbol} — CANCELADA")
        return False

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

    # Whitelist post-check
    if symbol not in ALLOWED_SYMBOLS:
        log.critical("CRITICAL: Buying unauthorized asset! %s — venta inmediata", symbol)
        await _async(_execute_sell, symbol, result["quantity"])
        return False

    # Colocar TP limit order en el servidor OKX (0ms delay, fee maker)
    tp_order_id = await _async(
        _place_tp_limit_order,
        symbol, result["quantity"], result["entry_price"]
    )
    result["tp_order_id"] = tp_order_id

    state["positions"][symbol] = result
    state["trades_today"]      = state.get("trades_today", 0) + 1
    save_state()

    try:
        total_bal = await _async(_fetch_total_portfolio_usdc)
    except Exception:
        total_bal = 0.0

    tp_price = result["entry_price"] * (1 + TAKE_PROFIT_PCT / 100.0)
    await _msg_compra(bot, symbol, result["invested"], result["entry_price"],
                      total_bal, reason, tp_price)
    log.info("COMPRA: %s %.2f USDC @ %.6f | TP order: %s",
             symbol, result["invested"], result["entry_price"], tp_order_id)
    return True

async def _sell(symbol: str, pos: dict, bot: Bot, motivo: str):
    """
    Cancela la TP limit order del servidor, luego vende a mercado.
    Actualiza P&L y envía notificación.
    """
    # Cancelar la TP limit order antes de vender a mercado
    tp_oid = pos.get("tp_order_id", "")
    if tp_oid:
        cancelled = await _async(_cancel_order, symbol, tp_oid)
        if not cancelled:
            log.warning("No pude cancelar TP order %s — posible doble venta en %s",
                        tp_oid, symbol)

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
    if pnl >= 0:
        state["wins_today"]   = state.get("wins_today", 0) + 1
    else:
        state["losses_today"] = state.get("losses_today", 0) + 1
    save_state()

    try:
        total_bal = await _async(_fetch_total_portfolio_usdc)
    except Exception:
        total_bal = 0.0

    if pnl >= 0:
        await _msg_venta_ganancia(bot, symbol, pnl, total_bal, motivo)
    else:
        await _msg_venta_perdida(bot, symbol, pnl, total_bal, motivo)
    log.info("VENTA: %s P&L %+.2f USDC (%s)", symbol, pnl, motivo)

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE TRADING — cada 60 segundos
# ══════════════════════════════════════════════════════════════════════════════
async def trading_loop(bot: Bot):
    """
    Cuádruple confirmación: EMA200(1h) + RSI(5m) + MACD(5m) + Vol(5m).
    """
    now = datetime.now(timezone.utc)
    log.info("━━━ CICLO %s ━━━", now.strftime("%d/%m %H:%M:%S"))
    state["last_scan"] = now.isoformat()

    try:
        total = await _async(_fetch_total_portfolio_usdc)
    except Exception as e:
        log.error("Error obteniendo portfolio: %s", e)
        return

    _reset_daily_if_needed(total)

    killed, dd = _check_kill_switch(total)
    if killed:
        if dd > 0:
            log.warning("Kill switch: pérdida diaria %.2f%% (límite %.1f%%)",
                        dd, KILL_SWITCH_PCT)
            await _msg_kill_switch(bot, total, dd)
        else:
            log.warning("Kill switch activo: %s — ciclo omitido.",
                        state.get("kill_switch_reason", "razón desconocida"))
        return

    if _slots_available() <= 0:
        log.info("Posiciones llenas (%d/%d)", len(state["positions"]), MAX_POSITIONS)
        return

    # BTC Guard
    try:
        btc_chg = await _async(_fetch_btc_1h_change)
        btc_ok  = btc_chg > -BTC_DROP_BLOCK
        log.info("BTC 1h: %+.2f%% — %s", btc_chg, "OK" if btc_ok else "BLOQUEADO")
    except Exception as e:
        log.warning("Error filtro BTC: %s", e)
        btc_ok = True

    if not btc_ok:
        log.info("BTC Guard: compras pausadas (%.2f%%)", btc_chg)
        return

    # Escaneo
    for symbol in ALLOWED_SYMBOLS:
        if symbol not in ALLOWED_SYMBOLS:
            continue
        if _slots_available() <= 0:
            break
        if symbol in state["positions"]:
            continue

        a = await _analyze(symbol)
        await asyncio.sleep(0.4)

        if not a:
            continue

        log.info("%s EMA=%s RSI=%.1f MACD=%s VOL=%s → %s",
                 symbol,
                 "OK" if a["ema_ok"] else "BAJO",
                 a["rsi"],
                 "SI" if a["bullish"] else "NO",
                 "SI" if a["vol_ok"] else "NO",
                 "✅ SEÑAL" if a["signal"] else "❌")

        if a["signal"]:
            reason = (
                f"EMA200(1h)✅ RSI {a['rsi']:.1f} MACD✅ Vol✅"
            )
            await _buy(symbol, bot, reason)
            await asyncio.sleep(1.0)

    log.info("━━━ FIN CICLO — %d/%d posiciones ━━━",
             len(state["positions"]), MAX_POSITIONS)

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE RIESGO — cada 10 segundos
# ══════════════════════════════════════════════════════════════════════════════
async def risk_loop(bot: Bot):
    """
    Usa fetch_tickers() batch para obtener todos los precios en 1 llamada.
    Aplica trailing escalado y gestiona la TP limit order del servidor.
    """
    if not state["positions"]:
        return

    symbols = list(state["positions"].keys())

    try:
        prices = await _async(_fetch_tickers_batch, symbols)
    except Exception as e:
        log.warning("risk_loop: error obteniendo precios batch: %s", e)
        return

    for symbol, pos in list(state["positions"].items()):
        try:
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue

            entry   = pos["entry_price"]
            pnl_pct = (price - entry) / entry * 100.0

            # Actualizar peak_price y max_pnl
            if price > pos.get("peak_price", entry):
                state["positions"][symbol]["peak_price"] = price
            max_pnl = max(pos.get("max_pnl", 0.0), pnl_pct)
            state["positions"][symbol]["max_pnl"] = max_pnl

            # Escalado del trailing stop
            new_sl, step_hit = _escalate_trailing({**pos, "max_pnl": max_pnl})
            if step_hit is not None:
                state["positions"][symbol]["sl_pct"] = new_sl
                save_state()
                log.info("TRAILING Step %d: %s max_pnl=+%.2f%% → SL=+%.1f%%",
                         step_hit, symbol, max_pnl, new_sl)
                await _msg_sl_step(bot, symbol, step_hit, new_sl, max_pnl)

            sl_pct = state["positions"][symbol].get("sl_pct", -STOP_LOSS_PCT)

            # Stop-Loss
            if pnl_pct <= sl_pct:
                if sl_pct >= 0:
                    motivo = (f"Trailing Step (pico +{max_pnl:.2f}% → "
                              f"SL {sl_pct:+.1f}%, ahora {pnl_pct:+.2f}%)")
                else:
                    motivo = f"Stop-Loss -{STOP_LOSS_PCT}% ({pnl_pct:+.2f}%)"
                log.warning("SL: %s pnl=%.2f%% sl=%.2f%%", symbol, pnl_pct, sl_pct)
                await _sell(symbol, pos, bot, motivo)
                continue

            # TP comprobación extra (por si la limit order fue cancelada o no se colocó)
            if pnl_pct >= TAKE_PROFIT_PCT and not pos.get("tp_order_id"):
                motivo = f"Take-Profit +{TAKE_PROFIT_PCT}% ({pnl_pct:+.2f}%)"
                log.info("TP fallback: %s +%.2f%%", symbol, pnl_pct)
                await _sell(symbol, pos, bot, motivo)
                continue

        except Exception as e:
            log.error("Error risk_loop %s: %s", symbol, e)

    save_state()

# ══════════════════════════════════════════════════════════════════════════════
# ORQUESTADOR
# ══════════════════════════════════════════════════════════════════════════════
async def _run_trading_loop(bot: Bot):
    await asyncio.sleep(15)
    log.info("Trading loop activo — cada %ds", TRADE_LOOP_SEC)
    while True:
        try:
            await trading_loop(bot)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            log.warning("Trading loop — red transitoria: %s. Siguiente ciclo.",
                        type(e).__name__)
        except Exception as e:
            log.error("Error crítico en trading_loop: %s", e)
            await _msg_error_grave(bot, str(e))
        await asyncio.sleep(TRADE_LOOP_SEC)

async def _run_risk_loop(bot: Bot):
    await asyncio.sleep(30)
    log.info("Risk loop activo — cada %ds (batch tickers)", RISK_LOOP_SEC)
    while True:
        try:
            await risk_loop(bot)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            log.warning("Risk loop — red transitoria: %s. Siguiente tick.",
                        type(e).__name__)
        except Exception as e:
            log.error("Error crítico en risk_loop: %s", e)
        await asyncio.sleep(RISK_LOOP_SEC)

async def _run_daily_report_loop(bot: Bot):
    await asyncio.sleep(60)
    await daily_report_loop(bot)

# ══════════════════════════════════════════════════════════════════════════════
# ARRANQUE
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN no configurado")

    log.info("════ ELITE HFT SCALPER V4 — OKX EEA ════")
    log.info("Persistencia: %s", os.path.abspath(POSITIONS_FILE))

    bot = Bot(token=TELEGRAM_TOKEN)
    connected = False

    try:
        ex = get_exchange()

        log.info("[1/3] Cargando mercados OKX (eea.okx.com)...")
        await _async(ex.load_markets)
        log.info("✅ [1/3] %d mercados", len(ex.markets))

        log.info("[2/3] Auth y saldo...")
        total_bal = await _async(_fetch_total_portfolio_usdc)
        free_usdc = await _async(_fetch_usdc_free)
        log.info("✅ [2/3] Auth OK — libre: %.2f / total: %.2f", free_usdc, total_bal)
        connected = True

        log.info("[3/3] Verificando ALLOWED_SYMBOLS...")
        for sym in ALLOWED_SYMBOLS:
            if sym in ex.markets:
                log.info("  ✅ %s", sym)
            else:
                log.warning("  ⚠️ %s NO disponible en OKX", sym)

        btc_chg = await _async(_fetch_btc_1h_change)
        log.info("✅ [3/3] BTC 1h: %+.2f%%", btc_chg)

    except ccxt.AuthenticationError as e:
        log.error("❌ Auth OKX fallida: %s", e)
        await bot.send_message(
            chat_id=CHAT_ID,
            text="❌ Error de autenticación OKX.\n\n"
                 "Comprueba OKX_API_KEY, OKX_SECRET_KEY y OKX_PASSPHRASE en Railway."
        )
        return

    except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
        log.error("❌ Error de red al arrancar: %s — continuando", e)

    except Exception as e:
        log.error("❌ Error inesperado al arrancar: %s", e)

    load_state()

    asyncio.create_task(_run_trading_loop(bot))
    asyncio.create_task(_run_risk_loop(bot))
    asyncio.create_task(_run_daily_report_loop(bot))

    if connected:
        await _send_status_report(bot)
    else:
        await _notify(bot,
            "He arrancado pero tengo problemas de conexión con OKX.\n"
            "Lo reintentaré automáticamente."
        )

    log.info("════ BOT LISTO — %s ════", "DRY RUN" if DRY_RUN else "MODO REAL")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
