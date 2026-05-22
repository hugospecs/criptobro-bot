"""
CRYPTO BOT PRO — AGGRESSIVE V3
OKX USDC · eea.okx.com · 5m Scalping
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Exchange  : OKX SPOT · eea.okx.com · sandbox=False
Moneda    : USDC
Capital   : 25 USDC por operación · máx. 3 abiertas
Watchlist : NEAR · FET · RENDER · LINK · SOL (whitelist estricta)
Timeframe : 5m (scalping agresivo)

ESTRATEGIA — TRIPLE CONFIRMACIÓN:
  1. RSI < 35 (sobreventa)
  2. MACD bullish crossover (histograma > 0 o giro fuerte)
  3. Volumen actual > promedio 10 velas × 1.5 (dinero real detrás)

RIESGO DINÁMICO:
  · Stop-Loss inicial: -2.5%
  · Trailing: si P&L >= +2% → SL sube a +0.2% (break-even + fees)
  · Take-Profit agresivo: +4.5%
  · Kill Switch diario: -5%
  · BTC Guard: pausa compras si BTC cae > 1% en 1h

INFORMES:
  · Informe de estado al arrancar
  · Informe diario a las 16:00 UTC (equity + P&L + tasa de éxito)

VARIABLES DE ENTORNO:
  TELEGRAM_TOKEN, CHAT_ID
  OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
  DRY_RUN=true
  DATA_PATH=/app/data   (Railway Volume — opcional)
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
TRADE_LOOP_SEC    = 300    # escaneo cada 5 minutos (scalping agresivo)
RISK_LOOP_SEC     = 60     # vigilancia de riesgo cada 60 s — suficiente para 5m
DAILY_REPORT_HOUR = 16     # hora UTC del informe diario

# ── Capital (en USDC) ─────────────────────────────────────────────────────────
TRADE_USDC     = 25.0
MAX_POSITIONS  = 3
MIN_TRADE_USDC = 5.0

# ── Riesgo dinámico ───────────────────────────────────────────────────────────
STOP_LOSS_PCT       = 2.5   # stop-loss inicial
TRAILING_TRIGGER    = 2.0   # activar trailing cuando P&L >= +2%
TRAILING_FLOOR      = 0.2   # SL se mueve a +0.2% (break-even + fees)
TAKE_PROFIT_PCT     = 4.5   # take-profit agresivo
KILL_SWITCH_PCT     = 5.0   # kill switch diario

# ── Estrategia — Triple Confirmación ─────────────────────────────────────────
TIMEFRAME          = "5m"
OHLCV_LIMIT        = 60     # 60 velas de 5m = 5 horas de historia
RSI_BUY            = 35     # RSI < 35 (más permisivo que v20.3)
VOLUME_MULT        = 1.2    # volumen actual > avg_10 × 1.2 (suavizado para 5m)
VOLUME_LOOKBACK    = 10     # velas para calcular el volumen promedio
BTC_DROP_BLOCK     = 1.0    # no comprar si BTC bajó > 1% en 1h

# ── Whitelist estricta — ÚNICOS símbolos que el bot puede comprar ─────────────
# Modificar aquí para añadir o quitar monedas. El bot NUNCA comprará
# ningún símbolo que no esté en esta lista.
ALLOWED_SYMBOLS = [
    "NEAR/USDC",
    "FET/USDC",
    "RENDER/USDC",
    "LINK/USDC",
    "SOL/USDC",
]

# Alias para compatibilidad — el bucle itera sobre ALLOWED_SYMBOLS
WATCHLIST = ALLOWED_SYMBOLS

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
    "positions":          {},   # symbol → {entry_price, quantity, invested,
    #                                        peak_price, entry_time, order_id,
    #                                        trailing_active, trailing_sl_pct}
    "kill_switch":        False,
    "kill_switch_reason": "",
    "daily_start_bal":    None,
    "daily_date":         None,
    "daily_realized_pnl": 0.0,
    "trades_today":       0,
    "wins_today":         0,    # operaciones cerradas con ganancia
    "losses_today":       0,    # operaciones cerradas con pérdida
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
    """Escritura atómica con fichero temporal para evitar corrupción."""
    try:
        target_dir = os.path.dirname(os.path.abspath(POSITIONS_FILE))
        os.makedirs(target_dir, exist_ok=True)
        tmp_file = POSITIONS_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_file, POSITIONS_FILE)
    except Exception as e:
        log.error("Error guardando estado: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# EXCHANGE — OKX (EEA) via CCXT
# ══════════════════════════════════════════════════════════════════════════════
_exchange: Optional[ccxt.okx] = None

def get_exchange() -> ccxt.okx:
    """
    Conector OKX SPOT — Mainnet EEA (Europa).
    · hostname="eea.okx.com"           → endpoint regulado para usuarios europeos
    · sandbox=False                    → red real, nunca demo
    · adjustForTimeDifference=True     → sincroniza timestamp con OKX
    · enableRateLimit=True             → CCXT impone espaciado estricto entre llamadas
    · retries=5                        → reintenta silenciosamente hasta 5 veces
                                         si un paquete se pierde o el servidor tarda
    · networkTimeout=15000             → espera hasta 15 s por respuesta del server EEA
                                         (el endpoint europeo puede ser más lento
                                         que el global en horas de alta carga)
    · defaultType="spot"               → cuenta Trading, mercado Spot
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
    """Ejecuta función síncrona de CCXT en el threadpool sin bloquear asyncio."""
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
    """
    Cruce alcista confirmado: histograma > 0 (MACD cruzó la línea de señal al alza)
    O giro de momentum fuerte: histograma negativo pero creciendo 2 velas.
    """
    hist = _macd_histogram_series(closes)
    if len(hist) < 3:
        return False
    h0, h1, h2 = hist[-1], hist[-2], hist[-3]
    cross_up   = h1 < 0.0 <= h0          # cruce alcista confirmado
    turning    = h0 < 0.0 and h0 > h1 > h2  # momentum girando fuerte
    return cross_up or turning

def _volume_filter(ohlcv: list) -> bool:
    """
    Volumen actual > promedio de las últimas VOLUME_LOOKBACK velas × VOLUME_MULT.
    Filtra entradas sin respaldo de dinero real.
    ohlcv: lista de [ts, open, high, low, close, volume]
    """
    if len(ohlcv) < VOLUME_LOOKBACK + 1:
        return False
    volumes    = [float(c[5]) for c in ohlcv]
    cur_vol    = volumes[-1]
    avg_vol    = sum(volumes[-(VOLUME_LOOKBACK + 1):-1]) / VOLUME_LOOKBACK
    if avg_vol <= 0:
        return False
    passes = cur_vol >= avg_vol * VOLUME_MULT
    log.debug("Volumen: actual=%.0f avg=%.0f ratio=%.2f req=%.1f → %s",
              cur_vol, avg_vol, cur_vol / avg_vol, VOLUME_MULT,
              "OK" if passes else "INSUF")
    return passes

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════
def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1.0 else 0
    return round(math.floor(value / step) * step, dec)

def _fetch_usdc_free() -> float:
    ex  = get_exchange()
    bal = ex.fetch_balance()
    return float((bal.get("USDC") or {}).get("free", 0.0) or 0.0)

def _fetch_total_portfolio_usdc() -> float:
    """USDC libre + valor de mercado de posiciones abiertas."""
    ex  = get_exchange()
    bal = ex.fetch_balance()
    total = float((bal.get("USDC") or {}).get("total", 0.0) or 0.0)
    skip = {"USDC", "USDT", "info", "free", "used", "total",
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

def _fetch_ohlcv(symbol: str, tf: str = "5m",
                  limit: int = OHLCV_LIMIT) -> list:
    """Devuelve las velas completas: [ts, open, high, low, close, volume]."""
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

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — MENSAJES EN LENGUAJE LLANO
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
                      price: float, total_bal: float, reason: str):
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN - sin dinero real]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"👀 He visto una oportunidad y he comprado {label} "
        f"usando {invested:.0f} dólares.\n"
        f"Señal: {reason}\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC"
    )

async def _msg_venta_ganancia(bot: Bot, symbol: str, pnl: float,
                               total_bal: float, motivo: str):
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN - sin dinero real]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"🎉 ¡Buenas noticias! He vendido {label} y hemos ganado "
        f"{pnl:.2f} dólares. ({motivo})\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC"
    )

async def _msg_venta_perdida(bot: Bot, symbol: str, pnl: float,
                              total_bal: float, motivo: str):
    label    = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN - sin dinero real]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"😔 Hoy no ha podido ser. He tenido que vender {label} "
        f"para proteger el dinero y hemos perdido {abs(pnl):.2f} dólares. "
        f"({motivo}) Seguimos buscando la próxima.\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC"
    )

async def _msg_kill_switch(bot: Bot, total_bal: float, drawdown: float):
    await _notify(bot,
        f"🛑 He pausado todas las compras porque el dinero ha bajado "
        f"un {drawdown:.1f}% hoy, que es más de lo que me has dicho que tolere.\n\n"
        f"💰 Tu dinero total ahora mismo: {total_bal:.2f} USDC\n\n"
        f"No haré nada hasta mañana que se reinicie el contador."
    )

async def _msg_error_grave(bot: Bot, motivo: str):
    await _notify(bot,
        f"⚠️ Ha ocurrido un problema técnico y no puedo operar ahora mismo.\n\n"
        f"Motivo: {motivo}\n\n"
        f"Lo seguiré intentando en el próximo ciclo."
    )

async def _msg_trailing_activated(bot: Bot, symbol: str, pnl_pct: float,
                                   new_sl_pct: float):
    label = _coin_label(symbol)
    dry_note = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry_note}"
        f"🔒 Trailing Stop activado en {label}\n"
        f"Beneficio alcanzado: +{pnl_pct:.2f}%\n"
        f"Stop-Loss movido a: +{new_sl_pct:.1f}% (protegiendo capital)"
    )

# ══════════════════════════════════════════════════════════════════════════════
# INFORME DE ESTADO
# ══════════════════════════════════════════════════════════════════════════════
async def _send_status_report(bot: Bot):
    """
    Informe completo: equity, P&L, posiciones abiertas, tasa de éxito,
    persistencia. Se envía al arrancar y cada día a las 16:00 UTC.
    """
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
        log.warning("Error obteniendo balance para informe: %s", e)
        total_bal = 0.0
        bal_str   = "No disponible"

    positions = state.get("positions", {})
    if positions:
        pos_lines = []
        for symbol, pos in positions.items():
            entry    = pos.get("entry_price", 0.0)
            inv      = pos.get("invested", 0.0)
            trailing = pos.get("trailing_active", False)
            sl_pct   = pos.get("trailing_sl_pct", -STOP_LOSS_PCT)
            try:
                cur_price = await _async(_fetch_price, symbol)
                pnl_pct   = ((cur_price - entry) / entry * 100.0) if entry else 0.0
                icon      = "📈" if pnl_pct >= 0 else "📉"
                trail_tag = " 🔒Trailing" if trailing else ""
                pos_lines.append(
                    f"  {icon} {_coin_label(symbol)}{trail_tag}\n"
                    f"     Entrada: {entry:.5f} | Ahora: {cur_price:.5f}\n"
                    f"     Invertido: {inv:.2f} USDC | P&L: {pnl_pct:+.2f}%\n"
                    f"     SL activo: {sl_pct:+.1f}%"
                )
            except Exception:
                pos_lines.append(f"  ⚪ {_coin_label(symbol)} | Sin precio")
        positions_str = "\n".join(pos_lines)
    else:
        positions_str = "  Sin posiciones abiertas."

    # Tasa de éxito
    wins   = state.get("wins_today", 0)
    losses = state.get("losses_today", 0)
    total_closed = wins + losses
    if total_closed > 0:
        success_rate = f"{wins}/{total_closed} ({wins/total_closed*100:.0f}%)"
    else:
        success_rate = "Sin operaciones cerradas hoy"

    abs_path    = os.path.abspath(POSITIONS_FILE)
    persist_str = (
        f"✅ {abs_path}" if os.path.exists(POSITIONS_FILE)
        else f"⚠️ No encontrado — {abs_path}"
    )

    pnl_hoy  = state.get("daily_realized_pnl", 0.0)
    last_scan = state.get("last_scan", "Nunca")
    hora      = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    msg = (
        f"📊 Informe — {hora}\n"
        f"{'─' * 34}\n"
        f"Modo       : {mode_str}\n"
        f"Estado     : {ks_str}\n"
        f"Equity     : {bal_str}\n"
        f"P&L hoy    : {pnl_hoy:+.2f} USDC\n"
        f"Éxito hoy  : {success_rate}\n"
        f"Últ. ciclo : {last_scan}\n"
        f"\n"
        f"Posiciones ({len(positions)}/{MAX_POSITIONS}):\n"
        f"{positions_str}\n"
        f"\n"
        f"Persistencia: {persist_str}"
    )

    await _notify(bot, msg)
    log.info("Informe enviado.")

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE INFORME DIARIO
# ══════════════════════════════════════════════════════════════════════════════
async def daily_report_loop(bot: Bot):
    """Dispara exactamente una vez al día a las DAILY_REPORT_HOUR UTC."""
    last_report_date = date.today().isoformat()
    log.info("Daily report loop activo — informe a las %02d:00 UTC",
             DAILY_REPORT_HOUR)
    while True:
        await asyncio.sleep(60)
        try:
            now       = datetime.now(timezone.utc)
            today_str = now.date().isoformat()
            if now.hour == DAILY_REPORT_HOUR and today_str != last_report_date:
                log.info("Informe diario (%02d:00 UTC)", DAILY_REPORT_HOUR)
                await _send_status_report(bot)
                last_report_date = today_str
        except Exception as e:
            log.error("Error en daily_report_loop: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# EJECUCIÓN DE ÓRDENES
# ══════════════════════════════════════════════════════════════════════════════
def _execute_buy(symbol: str, usdc_amount: float) -> dict:
    """Compra MARKET en OKX SPOT pagando `usdc_amount` USDC."""
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
        "symbol":          symbol,
        "entry_price":     exec_price,
        "quantity":        quantity,
        "invested":        round(quantity * exec_price, 4),
        "peak_price":      exec_price,
        "entry_time":      datetime.now(timezone.utc).isoformat(),
        "order_id":        order_id,
        "trailing_active": False,
        "trailing_sl_pct": -STOP_LOSS_PCT,   # SL inicial
    }

def _execute_sell(symbol: str, quantity: float) -> dict:
    """
    Venta MARKET en OKX SPOT.
    Zero-Dust: consulta el saldo real en el exchange antes de vender.
    Usa el total disponible en lugar de la cantidad en memoria, eliminando
    errores de "insufficient balance" causados por comisiones de la compra.
    """
    ex   = get_exchange()
    coin = symbol.split("/")[0]

    if DRY_RUN:
        price    = _fetch_price(symbol)
        order_id = f"DRY-SELL-{int(time.time())}"
        log.info("[DRY RUN] SELL %s qty=%.8f @ %.6f USDC", symbol, quantity, price)
    else:
        # ── Zero-Dust: leer saldo real del exchange ───────────────────────────
        try:
            bal      = ex.fetch_balance()
            real_qty = float((bal.get(coin) or {}).get("free", 0.0) or 0.0)
            if real_qty <= 0:
                raise ValueError(f"Saldo real de {coin} es 0 en el exchange")
            # Vender el 100% del saldo real disponible (sin dejar dust)
            # Aplicar 0.1% de margen por redondeos de precisión del exchange
            sell_qty = real_qty * 0.999
            if abs(sell_qty - quantity) > 1e-6:
                log.info("SELL %s — zero-dust: memoria=%.8f  real=%.8f  venta=%.8f",
                         symbol, quantity, real_qty, sell_qty)
        except Exception as e:
            log.warning("No pude leer saldo real de %s, usando cantidad de memoria: %s",
                        coin, e)
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
    """
    Resetea las estadísticas diarias a las 00:00 UTC.

    Fix persistencia / Railway restart:
    Si el bot se reinicia en un día diferente al que tiene guardado en
    state["daily_date"], sobreescribe daily_start_bal con el saldo actual.
    Esto evita que el kill switch permanezca bloqueado indefinidamente
    porque compara contra un saldo de ayer (o de hace varios días).

    También resetea kill_switch=False al inicio de cada nuevo día para que
    el bot pueda operar aunque ayer hubiera alcanzado el límite de pérdida.
    """
    today = date.today().isoformat()
    if state.get("daily_date") == today:
        return   # mismo día — no hacer nada

    log.info("Nuevo día detectado (anterior: %s → hoy: %s) — reseteando estadísticas",
             state.get("daily_date", "ninguna"), today)

    state["daily_date"]         = today
    state["daily_start_bal"]    = total   # saldo real de este momento como referencia
    state["daily_realized_pnl"] = 0.0
    state["trades_today"]       = 0
    state["wins_today"]         = 0
    state["losses_today"]       = 0

    # Reset del kill switch diario: el límite de pérdida es por día,
    # no permanente. Al empezar un nuevo día se borra automáticamente.
    if state.get("kill_switch") and "Drawdown" in state.get("kill_switch_reason", ""):
        log.info("Kill switch de drawdown diario reseteado al inicio del nuevo día.")
        state["kill_switch"]        = False
        state["kill_switch_reason"] = ""

    log.info("Reset diario completado — saldo inicial: %.2f USDC", total)
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

# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS — TRIPLE CONFIRMACIÓN
# ══════════════════════════════════════════════════════════════════════════════
async def _analyze(symbol: str) -> Optional[dict]:
    """
    Triple confirmación antes de comprar:
    1. RSI < RSI_BUY  (sobreventa)
    2. MACD bullish   (cruce al alza o momentum girando)
    3. Volumen actual > avg_10_velas × VOLUME_MULT  (dinero real detrás)
    Devuelve dict con indicadores o None si los datos son insuficientes.
    """
    try:
        ohlcv = await _async(_fetch_ohlcv, symbol, TIMEFRAME, OHLCV_LIMIT)
        if len(ohlcv) < max(35, VOLUME_LOOKBACK + 1):
            return None

        closes = [float(c[4]) for c in ohlcv]
        rsi     = _rsi(closes)
        bullish = _macd_bullish(closes)
        vol_ok  = _volume_filter(ohlcv)
        price   = closes[-1]

        return {
            "rsi":     rsi,
            "bullish": bullish,
            "vol_ok":  vol_ok,
            "price":   price,
            # Señal completa solo si las 3 condiciones se cumplen
            "signal":  rsi < RSI_BUY and bullish and vol_ok,
        }
    except Exception as e:
        log.debug("Error analizando %s: %s", symbol, e)
        return None

# ══════════════════════════════════════════════════════════════════════════════
# OPERACIONES CON NOTIFICACIÓN
# ══════════════════════════════════════════════════════════════════════════════
async def _buy(symbol: str, bot: Bot, reason: str) -> bool:
    """Ejecuta la compra y envía notificación."""
    # ── Safety check previo: whitelist enforcement ────────────────────────────
    if symbol not in ALLOWED_SYMBOLS:
        log.critical("CRITICAL: Intento de compra de activo NO AUTORIZADO: %s — "
                     "operación cancelada. Solo se permiten: %s",
                     symbol, ALLOWED_SYMBOLS)
        await _notify(bot,
            f"🚨 ALERTA DE SEGURIDAD\n\n"
            f"El bot intentó comprar {symbol}, que NO está en la whitelist.\n"
            f"Operación CANCELADA automáticamente.\n"
            f"Símbolos permitidos: {', '.join(ALLOWED_SYMBOLS)}"
        )
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

    state["positions"][symbol] = result
    state["trades_today"]      = state.get("trades_today", 0) + 1
    save_state()

    # ── Post-buy safety check ────────────────────────────────────────────────
    # Última línea de defensa: si por algún motivo el símbolo comprado no
    # está en ALLOWED_SYMBOLS, vender inmediatamente y alertar.
    if symbol not in ALLOWED_SYMBOLS:
        log.critical("CRITICAL: Buying unauthorized asset! %s — iniciando venta inmediata.",
                     symbol)
        await _notify(bot,
            f"🚨 COMPRA DE ACTIVO NO AUTORIZADO DETECTADA\n\n"
            f"Se compró {symbol} sin estar en la whitelist.\n"
            f"Iniciando VENTA INMEDIATA para proteger el capital."
        )
        await _sell(symbol, result, bot, "Venta de emergencia: activo no autorizado")
        return False

    try:
        total_bal = await _async(_fetch_total_portfolio_usdc)
    except Exception:
        total_bal = 0.0

    await _msg_compra(bot, symbol, result["invested"],
                      result["entry_price"], total_bal, reason)
    log.info("COMPRA: %s %.2f USDC @ %.6f", symbol, result["invested"], result["entry_price"])
    return True

async def _sell(symbol: str, pos: dict, bot: Bot, motivo: str):
    """Ejecuta la venta, actualiza P&L y envía notificación."""
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
    # Actualizar tasa de éxito
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
# BUCLE DE TRADING — cada 5 minutos
# ══════════════════════════════════════════════════════════════════════════════
async def trading_loop(bot: Bot):
    """
    1. Saldo y kill switch
    2. BTC Guard (filtro protector)
    3. Triple confirmación por cada moneda del WATCHLIST
    """
    now = datetime.now(timezone.utc)
    log.info("━━━ CICLO %s ━━━", now.strftime("%d/%m %H:%M"))
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
            # Kill switch activado AHORA por pérdida diaria
            log.warning("Kill switch activo: Límite de pérdida diaria superado "
                        "(drawdown %.2f%% hoy, límite: %.1f%%).", dd, KILL_SWITCH_PCT)
            await _msg_kill_switch(bot, total, dd)
        else:
            # Kill switch ya estaba activo (cargado desde persistencia)
            reason = state.get("kill_switch_reason", "razón desconocida")
            log.warning("Kill switch activo: %s — ciclo omitido.", reason)
        return

    if _slots_available() <= 0:
        log.info("Posiciones llenas (%d/%d)", len(state["positions"]), MAX_POSITIONS)
        return

    # ── BTC Guard ─────────────────────────────────────────────────────────────
    try:
        btc_chg = await _async(_fetch_btc_1h_change)
        btc_ok  = btc_chg > -BTC_DROP_BLOCK
        log.info("BTC 1h: %+.2f%% — %s", btc_chg, "OK" if btc_ok else "BLOQUEADO")
    except Exception as e:
        log.warning("Error filtro BTC: %s", e)
        btc_ok = True

    if not btc_ok:
        log.info("BTC Guard: compras pausadas (BTC %.2f%%)", btc_chg)
        return

    # ── Escaneo ALLOWED_SYMBOLS ───────────────────────────────────────────────
    for symbol in ALLOWED_SYMBOLS:
        # Guard de whitelist: rechazar cualquier símbolo no autorizado
        # (defensa en profundidad por si ALLOWED_SYMBOLS fuera modificada
        # accidentalmente o el estado cargara posiciones de sesiones anteriores)
        if symbol not in ALLOWED_SYMBOLS:
            log.warning("WHITELIST GUARD: símbolo %s ignorado — no está en ALLOWED_SYMBOLS",
                        symbol)
            continue

        if _slots_available() <= 0:
            break
        if symbol in state["positions"]:
            continue

        a = await _analyze(symbol)
        await asyncio.sleep(0.4)

        if not a:
            log.debug("%s — datos insuficientes", symbol)
            continue

        log.info("%s RSI=%.1f MACD=%s VOL=%s SIGNAL=%s",
                 symbol, a["rsi"],
                 "SI" if a["bullish"] else "NO",
                 "SI" if a["vol_ok"] else "NO",
                 "✅" if a["signal"] else "❌")

        if a["signal"]:
            reason = (
                f"RSI {a['rsi']:.1f} + MACD alcista + "
                f"Volumen x{VOLUME_MULT}"
            )
            log.info("Triple confirmación — comprando %s", symbol)
            await _buy(symbol, bot, reason)
            await asyncio.sleep(1.0)

    log.info("━━━ FIN CICLO — posiciones: %d/%d ━━━",
             len(state["positions"]), MAX_POSITIONS)

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE RIESGO — cada 30 segundos, con Trailing Stop dinámico
# ══════════════════════════════════════════════════════════════════════════════
async def risk_loop(bot: Bot):
    """
    Para cada posición abierta:
    1. Si P&L >= TRAILING_TRIGGER (+2%) y trailing no activo:
       → Activar trailing: mover SL a +TRAILING_FLOOR (+0.2%)
       → Notificar al usuario
    2. Si trailing activo: verificar que el precio no haya caído
       por debajo del trailing SL.
    3. Si P&L <= SL actual: vender (stop-loss).
    4. Si P&L >= TAKE_PROFIT_PCT (+4.5%): vender (take-profit).
    """
    if not state["positions"]:
        return

    for symbol, pos in list(state["positions"].items()):
        try:
            price = await _async(_fetch_price, symbol)
            if price <= 0:
                continue

            entry   = pos["entry_price"]
            pnl_pct = (price - entry) / entry * 100.0

            # Actualizar pico máximo
            if price > pos.get("peak_price", entry):
                state["positions"][symbol]["peak_price"] = price

            # ── Trailing Stop — activación ────────────────────────────────────
            if (not pos.get("trailing_active", False)
                    and pnl_pct >= TRAILING_TRIGGER):
                state["positions"][symbol]["trailing_active"] = True
                state["positions"][symbol]["trailing_sl_pct"] = TRAILING_FLOOR
                save_state()
                log.info("TRAILING activado: %s P&L=+%.2f%% → SL movido a +%.1f%%",
                         symbol, pnl_pct, TRAILING_FLOOR)
                await _msg_trailing_activated(bot, symbol, pnl_pct, TRAILING_FLOOR)

            # ── Determinar el SL efectivo ─────────────────────────────────────
            sl_pct = pos.get("trailing_sl_pct", -STOP_LOSS_PCT)

            # ── Stop-Loss (inicial o trailing) ────────────────────────────────
            if pnl_pct <= sl_pct:
                if sl_pct >= 0:
                    motivo = f"Trailing Stop activado ({pnl_pct:+.2f}% / SL {sl_pct:+.1f}%)"
                else:
                    motivo = f"Stop-Loss -{STOP_LOSS_PCT}% ({pnl_pct:+.2f}%)"
                log.warning("SL/TRAILING: %s pnl=%.2f%% sl=%.2f%%",
                            symbol, pnl_pct, sl_pct)
                await _sell(symbol, pos, bot, motivo)
                continue

            # ── Take-Profit agresivo ──────────────────────────────────────────
            if pnl_pct >= TAKE_PROFIT_PCT:
                motivo = f"Take-Profit +{TAKE_PROFIT_PCT}% ({pnl_pct:+.2f}%)"
                log.info("TP: %s +%.2f%%", symbol, pnl_pct)
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
    log.info("Trading loop activo — cada %d s (%dm)", TRADE_LOOP_SEC, TRADE_LOOP_SEC // 60)
    while True:
        try:
            await trading_loop(bot)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            # Error de red transitorio — no avisar por Telegram, solo log y continuar
            log.warning("Trading loop — error de red transitorio (eea.okx.com): %s. "
                        "Reintentando en el próximo ciclo.", type(e).__name__)
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
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            # Error de red transitorio — las posiciones siguen abiertas y seguras,
            # el SL/TP se comprobará en el próximo tick (60 s)
            log.warning("Risk loop — error de red transitorio (eea.okx.com): %s. "
                        "SL/TP se comprobará en el próximo tick.", type(e).__name__)
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

    log.info("════ AGGRESSIVE V3 — OKX USDC SCALPER 5m ════")
    log.info("Persistencia: %s", os.path.abspath(POSITIONS_FILE))

    bot = Bot(token=TELEGRAM_TOKEN)
    connected = False

    try:
        ex = get_exchange()

        log.info("[1/3] Cargando mercados OKX (eea.okx.com)...")
        await _async(ex.load_markets)
        log.info("✅ [1/3] %d mercados disponibles", len(ex.markets))

        log.info("[2/3] Verificando auth y saldo USDC...")
        total_bal = await _async(_fetch_total_portfolio_usdc)
        free_usdc = await _async(_fetch_usdc_free)
        log.info("✅ [2/3] Auth OK — USDC libre: %.2f / total: %.2f",
                 free_usdc, total_bal)
        connected = True

        log.info("[3/3] Verificando ALLOWED_SYMBOLS en OKX...")
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
            text=(
                "❌ No puedo conectar con OKX — error de autenticación.\n\n"
                "Comprueba OKX_API_KEY, OKX_SECRET_KEY y OKX_PASSPHRASE en Railway."
            )
        )
        return

    except ccxt.NetworkError as e:
        log.error("❌ Error de red OKX: %s — arrancando de todas formas", e)

    except Exception as e:
        log.error("❌ Error inesperado: %s", e)

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
