"""
CRYPTO BOT PRO — ELITE SCALPER V5 (MAX ALPHA)
OKX USDC · eea.okx.com · 5m precision entries
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Exchange  : OKX SPOT · eea.okx.com · sandbox=False
Capital   : 25 USDC / trade · máx. 3 abiertas
Watchlist : NEAR · FET · RENDER · LINK · SOL

QUAD+ ENTRY SIGNAL (5 condiciones simultáneas):
  1. EMA200 (1h) — precio > EMA200: evita tendencia bajista macro
  2. RSI < 35 Y ASCENDENTE — rebote confirmado, no caída libre
  3. MACD cruce CONFIRMADO (h0 >= 0) — no entradas prematuras
  4. Volumen > media×1.2 — dinero real detrás
  5. Vela 5m alcista (close > open) — price action confirma el momento

SUPRESIONES ESTRATÉGICAS (anti-feedo):
  · MACD "turning" ELIMINADO — causaba entradas en falso antes del cruce
  · RSI sin dirección ELIMINADO — filtraba activos en caída sostenida
  · Cooldown 30 min/símbolo tras SL — evita reentrar en la misma trampa

ESCALATED TRAILING:
  · SL inicial: -2.2% (respira ante volatilidad cripto normal)
  · Step 1: max_pnl >= 1.5% → SL = +0.2% (break-even + fees)
  · Step 2: max_pnl >= 2.5% → SL = +1.0% (profit parcial asegurado)
  · Step 3: max_pnl >= 3.5% → SL = +2.0% (guarda el pico)
  · TP server-side: +4.5% (orden límite en OKX, fee maker)

TIMING:
  · Scan entradas: 5 min (evita overtrading y fee-bleeding)
  · Risk monitor:  15 s via batch fetch_tickers()

VARIABLES DE ENTORNO:
  TELEGRAM_TOKEN, CHAT_ID
  OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
  DRY_RUN=true
  DATA_PATH=/app/data
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
TRADE_LOOP_SEC    = 300    # 5 min — captura tendencias reales, evita fee-bleeding
RISK_LOOP_SEC     = 15     # 15 s — vigilancia rápida sin saturar OKX
DAILY_REPORT_HOUR = 16

# ── Capital (USDC) ────────────────────────────────────────────────────────────
TRADE_USDC     = 25.0
MAX_POSITIONS  = 3
MIN_TRADE_USDC = 5.0

# ── Riesgo ────────────────────────────────────────────────────────────────────
STOP_LOSS_PCT   = 2.2    # más holgura para respirar ante volatilidad normal
TAKE_PROFIT_PCT = 4.5    # TP como orden límite en servidor OKX

# Escalones trailing — SL solo sube, nunca baja
TRAIL_STEPS = [
    (1.5, 0.2),   # max_pnl >= +1.5% → SL = +0.2% (break-even + fees)
    (2.5, 1.0),   # max_pnl >= +2.5% → SL = +1.0% (profit parcial)
    (3.5, 2.0),   # max_pnl >= +3.5% → SL = +2.0% (guarda el pico)
]

KILL_SWITCH_PCT = 5.0

# ── Estrategia ────────────────────────────────────────────────────────────────
TIMEFRAME        = "5m"
OHLCV_LIMIT      = 80     # 80 velas × 5m = ~6.5h de historia (más contexto MACD)
EMA200_TF        = "1h"
EMA200_LIMIT     = 210
RSI_BUY          = 35
VOLUME_MULT      = 1.2
VOLUME_LOOKBACK  = 10
BTC_DROP_BLOCK   = 1.0    # pausa si BTC cae > 1% en 1h
SL_COOLDOWN_SEC  = 1800   # 30 min de cooldown por símbolo tras un SL

# ── Whitelist estricta ────────────────────────────────────────────────────────
ALLOWED_SYMBOLS = [
    "NEAR/USDC",
    "FET/USDC",
    "RENDER/USDC",
    "LINK/USDC",
    "SOL/USDC",
]
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
    # cooldown: {symbol: timestamp_unix_ultimo_SL}
    "sl_cooldown":        {},
}

def load_state():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE) as f:
                state.update(json.load(f))
            log.info("Estado cargado — %d posiciones", len(state["positions"]))
        except Exception as e:
            log.warning("Error cargando estado: %s", e)

def save_state():
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

def _rsi_series(closes: list, period: int = 14) -> list:
    """Devuelve la serie RSI completa (útil para detectar dirección)."""
    if len(closes) < period + 2:
        return [50.0, 50.0]
    result = []
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    for i in range(period, len(deltas)):
        g = [max(d, 0.0) for d in deltas[i - period:i]]
        l = [max(-d, 0.0) for d in deltas[i - period:i]]
        ag, al = sum(g) / period, sum(l) / period
        result.append(100.0 if al == 0 else round(100.0 - 100.0 / (1.0 + ag / al), 2))
    return result if result else [50.0, 50.0]

def _rsi(closes: list, period: int = 14) -> float:
    s = _rsi_series(closes, period)
    return s[-1] if s else 50.0

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

def _macd_confirmed_crossover(closes: list) -> bool:
    """
    SOLO cruce alcista CONFIRMADO: h[-1] >= 0 Y h[-2] < 0.
    
    SUPRESIÓN ESTRATÉGICA: eliminamos la condición "turning" (histograma
    negativo pero creciendo) que estaba en versiones anteriores.
    Esa condición generaba entradas prematuras antes de que el cruce
    se materializara, resultando en compras en medio de caídas.
    Ahora solo entramos cuando el MACD ha cruzado al alza de forma confirmada.
    """
    hist = _macd_histogram_series(closes)
    if len(hist) < 2:
        return False
    h0, h1 = hist[-1], hist[-2]
    return h1 < 0.0 and h0 >= 0.0   # cruce real: negativo → positivo

def _rsi_ascending(closes: list, period: int = 14) -> bool:
    """
    RSI ascendente: RSI[-1] > RSI[-2].
    
    ADICIÓN ESTRATÉGICA: filtra activos cuyo RSI está por debajo del umbral
    pero continúa cayendo (caída libre). Solo compramos cuando el RSI ya
    ha tocado suelo y está subiendo — confirma que el rebote ha comenzado.
    """
    s = _rsi_series(closes, period)
    if len(s) < 2:
        return False
    return s[-1] > s[-2]

def _bullish_candle(ohlcv: list) -> bool:
    """
    Vela 5m actual alcista: close > open.
    
    ADICIÓN ESTRATÉGICA: price action confirma el momento exacto de entrada.
    Evitamos entrar en medio de una vela roja aunque todos los demás
    indicadores sean positivos. Una vela verde en el momento de la señal
    es la confirmación final de que el precio está moviendo en nuestra dirección.
    """
    if not ohlcv:
        return False
    last = ohlcv[-1]
    return float(last[4]) > float(last[1])   # close > open

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
    """Precio > EMA200 en 1h: macro tendencia alcista."""
    if len(ohlcv_1h) < 200:
        return True   # sin suficientes datos, no bloqueamos
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
    market = get_exchange().markets.get(symbol, {})
    p = market.get("precision", {}).get("price")
    return p if p and p > 0 else 0.0001

def _fetch_usdc_free() -> float:
    bal = get_exchange().fetch_balance()
    return float((bal.get("USDC") or {}).get("free", 0.0) or 0.0)

def _fetch_total_portfolio_usdc() -> float:
    ex    = get_exchange()
    bal   = ex.fetch_balance()
    total = float((bal.get("USDC") or {}).get("total", 0.0) or 0.0)
    skip  = {"USDC", "USDT", "info", "free", "used", "total", "timestamp", "datetime"}
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
    """Una sola llamada API para todos los precios activos."""
    tickers = get_exchange().fetch_tickers(symbols)
    return {sym: float(tk.get("last") or 0.0) for sym, tk in tickers.items()}

def _fetch_ohlcv(symbol: str, tf: str = "5m", limit: int = OHLCV_LIMIT) -> list:
    ohlcv = get_exchange().fetch_ohlcv(symbol, tf, limit=limit)
    return ohlcv if ohlcv else []

def _fetch_price(symbol: str) -> float:
    return float((get_exchange().fetch_ticker(symbol).get("last")) or 0.0)

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

def _place_tp_limit_order(symbol: str, quantity: float, entry_price: float) -> str:
    """TP a +4.5% como orden límite en OKX — fee maker, ejecución instantánea."""
    tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT / 100.0), 8)
    tick     = _price_step(symbol)
    if tick > 0:
        tp_price = _round_step(tp_price, tick)

    if DRY_RUN:
        oid = f"DRY-TP-{int(time.time())}"
        log.info("[DRY RUN] TP limit: %s @ %.6f (+%.1f%%)", symbol, tp_price, TAKE_PROFIT_PCT)
        return oid

    try:
        order = get_exchange().create_limit_sell_order(
            symbol, quantity, tp_price,
            params={"tdMode": "cash"}
        )
        oid = str(order.get("id", ""))
        log.info("TP limit colocada: %s id=%s @ %.6f", symbol, oid, tp_price)
        return oid
    except Exception as e:
        log.warning("No pude colocar TP limit para %s: %s", symbol, e)
        return ""

def _cancel_order(symbol: str, order_id: str) -> bool:
    if not order_id or order_id.startswith("DRY-"):
        return True
    try:
        get_exchange().cancel_order(order_id, symbol)
        log.info("Orden cancelada: %s id=%s", symbol, order_id)
        return True
    except ccxt.OrderNotFound:
        return True   # ya ejecutada — no es error
    except Exception as e:
        log.warning("Error cancelando %s para %s: %s", order_id, symbol, e)
        return False

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
async def _notify(bot: Bot, text: str):
    if not CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        log.error("Telegram: %s", e)

def _coin_label(symbol: str) -> str:
    return COIN_NAMES.get(symbol, symbol.split("/")[0])

async def _msg_compra(bot: Bot, symbol: str, invested: float,
                      price: float, total_bal: float, reason: str, tp_price: float):
    dry = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry}👀 Compré {_coin_label(symbol)} — {invested:.0f} USDC\n"
        f"Entrada: {price:.5f}\n"
        f"Señal: {reason}\n"
        f"🎯 TP servidor: {tp_price:.5f} (+{TAKE_PROFIT_PCT}%)\n"
        f"🛑 SL: -{STOP_LOSS_PCT}%\n\n"
        f"💰 Total: {total_bal:.2f} USDC"
    )

async def _msg_venta_ganancia(bot: Bot, symbol: str, pnl: float,
                               total_bal: float, motivo: str):
    dry = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry}🎉 Ganancia en {_coin_label(symbol)}: +{pnl:.2f} USDC\n"
        f"({motivo})\n\n💰 Total: {total_bal:.2f} USDC"
    )

async def _msg_venta_perdida(bot: Bot, symbol: str, pnl: float,
                              total_bal: float, motivo: str):
    dry = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry}😔 Pérdida en {_coin_label(symbol)}: {pnl:.2f} USDC\n"
        f"({motivo})\n\n💰 Total: {total_bal:.2f} USDC"
    )

async def _msg_kill_switch(bot: Bot, total_bal: float, drawdown: float):
    await _notify(bot,
        f"🛑 Kill switch: bajé {drawdown:.1f}% hoy.\n"
        f"💰 Total: {total_bal:.2f} USDC\n\nReinicio mañana."
    )

async def _msg_error_grave(bot: Bot, motivo: str):
    await _notify(bot, f"⚠️ Error técnico — reintentando.\nMotivo: {motivo}")

async def _msg_sl_step(bot: Bot, symbol: str, step: int,
                        new_sl: float, max_pnl: float):
    dry = "⚠️ [SIMULACIÓN]\n\n" if DRY_RUN else ""
    await _notify(bot,
        f"{dry}🔒 Trailing Step {step} — {_coin_label(symbol)}\n"
        f"Pico: +{max_pnl:.2f}% → SL movido a +{new_sl:.1f}%"
    )

# ══════════════════════════════════════════════════════════════════════════════
# INFORME DE ESTADO
# ══════════════════════════════════════════════════════════════════════════════
async def _send_status_report(bot: Bot):
    log.info("Generando informe...")
    mode_str = "🧪 SIMULACIÓN" if DRY_RUN else "🔴 REAL"
    ks_str   = (f"⛔ Kill Switch — {state.get('kill_switch_reason','')}"
                if state.get("kill_switch") else "✅ Operando")

    try:
        total_bal = await _async(_fetch_total_portfolio_usdc)
        bal_str   = f"{total_bal:.2f} USDC"
    except Exception:
        total_bal, bal_str = 0.0, "No disponible"

    positions = state.get("positions", {})
    pos_lines = []
    for symbol, pos in positions.items():
        entry  = pos.get("entry_price", 0.0)
        inv    = pos.get("invested", 0.0)
        sl_pct = pos.get("sl_pct", -STOP_LOSS_PCT)
        tp_oid = pos.get("tp_order_id", "")
        try:
            p       = await _async(_fetch_price, symbol)
            pct     = ((p - entry) / entry * 100.0) if entry else 0.0
            icon    = "📈" if pct >= 0 else "📉"
            tp_tag  = " 🎯TP-svr" if tp_oid else ""
            pos_lines.append(
                f"  {icon} {_coin_label(symbol)}{tp_tag}\n"
                f"     {entry:.5f}→{p:.5f} | {inv:.2f}USDC | P&L:{pct:+.2f}%\n"
                f"     SL:{sl_pct:+.1f}%"
            )
        except Exception:
            pos_lines.append(f"  ⚪ {_coin_label(symbol)} | Sin precio")

    wins   = state.get("wins_today", 0)
    losses = state.get("losses_today", 0)
    tc     = wins + losses
    sr     = f"{wins}/{tc} ({wins/tc*100:.0f}%)" if tc else "—"
    hora   = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")

    msg = (
        f"📊 {hora}\n{'─'*32}\n"
        f"Modo: {mode_str} | {ks_str}\n"
        f"Equity: {bal_str} | P&L hoy: {state.get('daily_realized_pnl',0):+.2f}\n"
        f"Éxito: {sr}\n\n"
        f"Posiciones ({len(positions)}/{MAX_POSITIONS}):\n"
        + ("\n".join(pos_lines) if pos_lines else "  Sin posiciones.")
        + f"\n\nPersistencia: {os.path.abspath(POSITIONS_FILE)}"
    )
    await _notify(bot, msg)

async def daily_report_loop(bot: Bot):
    last = date.today().isoformat()
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(timezone.utc)
            if now.hour == DAILY_REPORT_HOUR and now.date().isoformat() != last:
                await _send_status_report(bot)
                last = now.date().isoformat()
        except Exception as e:
            log.error("daily_report_loop: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# GESTIÓN DE CAPITAL Y RIESGO
# ══════════════════════════════════════════════════════════════════════════════
def _slots_available() -> int:
    return MAX_POSITIONS - len(state["positions"])

def _in_cooldown(symbol: str) -> bool:
    """
    Cooldown de 30 min tras un Stop-Loss.
    Evita reentrar inmediatamente en la misma trampa de precio.
    """
    ts = state.get("sl_cooldown", {}).get(symbol)
    if not ts:
        return False
    remaining = SL_COOLDOWN_SEC - (time.time() - float(ts))
    if remaining > 0:
        log.info("COOLDOWN %s: %.0f s restantes", symbol, remaining)
        return True
    return False

def _set_cooldown(symbol: str):
    if "sl_cooldown" not in state:
        state["sl_cooldown"] = {}
    state["sl_cooldown"][symbol] = time.time()
    save_state()

def _reset_daily_if_needed(total: float):
    today = date.today().isoformat()
    if state.get("daily_date") == today:
        return
    log.info("Nuevo día (%s → %s) — reset", state.get("daily_date", "?"), today)
    state.update({
        "daily_date": today, "daily_start_bal": total,
        "daily_realized_pnl": 0.0, "trades_today": 0,
        "wins_today": 0, "losses_today": 0,
    })
    if state.get("kill_switch") and "Drawdown" in state.get("kill_switch_reason", ""):
        state["kill_switch"]        = False
        state["kill_switch_reason"] = ""
        log.info("Kill switch reseteado para el nuevo día.")
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
        log.warning("KILL SWITCH: %.2f%%", dd)
        return True, dd
    return False, dd

def _escalate_trailing(pos: dict) -> tuple[float, Optional[int]]:
    """SL solo sube — aplica el escalón más alto alcanzado."""
    max_pnl  = pos.get("max_pnl", 0.0)
    cur_sl   = pos.get("sl_pct", -STOP_LOSS_PCT)
    new_sl, step_hit = cur_sl, None
    for i, (trigger, floor) in enumerate(TRAIL_STEPS, 1):
        if max_pnl >= trigger and floor > cur_sl:
            new_sl, step_hit = floor, i
    return new_sl, step_hit

# ══════════════════════════════════════════════════════════════════════════════
# ANÁLISIS — 5 CONDICIONES SIMULTÁNEAS
# ══════════════════════════════════════════════════════════════════════════════
async def _analyze(symbol: str) -> Optional[dict]:
    """
    Señal de entrada de alta precisión — las 5 deben cumplirse:
    1. EMA200(1h)  — macro tendencia alcista
    2. RSI < 35    — sobreventa en 5m
    3. RSI↑        — rebote confirmado (no caída libre) [NUEVO]
    4. MACD cruce  — confirmado (h0>=0, no prematuro)   [OPTIMIZADO]
    5. Vol ×1.2    — dinero real detrás del movimiento
    + Vela alcista — price action confirma el momento   [NUEVO]
    """
    try:
        ohlcv_1h = await _async(_fetch_ohlcv, symbol, EMA200_TF, EMA200_LIMIT)
        ohlcv_5m = await _async(_fetch_ohlcv, symbol, TIMEFRAME, OHLCV_LIMIT)

        if len(ohlcv_5m) < max(35, VOLUME_LOOKBACK + 1):
            return None

        closes_5m = [float(c[4]) for c in ohlcv_5m]
        price     = closes_5m[-1]

        ema_ok    = _ema200_above(ohlcv_1h, price)
        rsi_val   = _rsi(closes_5m)
        rsi_up    = _rsi_ascending(closes_5m)          # RSI ascendente
        macd_ok   = _macd_confirmed_crossover(closes_5m)  # cruce confirmado
        vol_ok    = _volume_filter(ohlcv_5m)
        candle_ok = _bullish_candle(ohlcv_5m)           # vela alcista

        # Señal completa — las 6 condiciones
        signal = (ema_ok and rsi_val < RSI_BUY and rsi_up
                  and macd_ok and vol_ok and candle_ok)

        return {
            "price":     price,
            "rsi":       rsi_val,
            "rsi_up":    rsi_up,
            "macd_ok":   macd_ok,
            "vol_ok":    vol_ok,
            "ema_ok":    ema_ok,
            "candle_ok": candle_ok,
            "signal":    signal,
        }
    except Exception as e:
        log.debug("Error analizando %s: %s", symbol, e)
        return None

# ══════════════════════════════════════════════════════════════════════════════
# ÓRDENES
# ══════════════════════════════════════════════════════════════════════════════
def _execute_buy(symbol: str, usdc_amount: float) -> dict:
    ex     = get_exchange()
    market = ex.markets.get(symbol, {})
    price  = _fetch_price(symbol)
    if price <= 0:
        raise ValueError(f"Precio inválido: {symbol}")

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
        oid, exec_price = f"DRY-BUY-{int(time.time())}", price
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
        oid        = str(order.get("id", ""))
        exec_price = float(order.get("average") or order.get("price") or price)
        quantity   = float(order.get("filled") or order.get("amount") or quantity)
        log.info("BUY OKX: %s id=%s qty=%.8f @ %.6f", symbol, oid, quantity, exec_price)

    return {
        "symbol":      symbol,
        "entry_price": exec_price,
        "quantity":    quantity,
        "invested":    round(quantity * exec_price, 4),
        "peak_price":  exec_price,
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "order_id":    oid,
        "sl_pct":      -STOP_LOSS_PCT,
        "max_pnl":     0.0,
        "tp_order_id": "",
    }

def _execute_sell(symbol: str, quantity: float) -> dict:
    """Zero-Dust: usa saldo real del exchange."""
    ex   = get_exchange()
    coin = symbol.split("/")[0]

    if DRY_RUN:
        price = _fetch_price(symbol)
        oid   = f"DRY-SELL-{int(time.time())}"
        log.info("[DRY RUN] SELL %s qty=%.8f @ %.6f", symbol, quantity, price)
    else:
        try:
            bal      = ex.fetch_balance()
            real_qty = float((bal.get(coin) or {}).get("free", 0.0) or 0.0)
            if real_qty <= 0:
                raise ValueError(f"Saldo {coin} = 0")
            sell_qty = real_qty * 0.999
            if abs(sell_qty - quantity) > 1e-6:
                log.info("Zero-dust %s: mem=%.8f real=%.8f venta=%.8f",
                         symbol, quantity, real_qty, sell_qty)
        except Exception as e:
            log.warning("No pude leer saldo real de %s: %s", coin, e)
            sell_qty = quantity

        order = ex.create_market_sell_order(
            symbol, sell_qty, params={"tdMode": "cash"}
        )
        oid   = str(order.get("id", ""))
        ep    = order.get("average") or order.get("price")
        price = float(ep) if ep else _fetch_price(symbol)
        quantity = sell_qty
        log.info("SELL OKX: %s id=%s qty=%.8f @ %.6f", symbol, oid, quantity, price)

    return {"price": price, "proceeds": round(price * quantity, 4), "order_id": oid}

# ══════════════════════════════════════════════════════════════════════════════
# OPERACIONES CON NOTIFICACIÓN
# ══════════════════════════════════════════════════════════════════════════════
async def _buy(symbol: str, bot: Bot, reason: str) -> bool:
    if symbol not in ALLOWED_SYMBOLS:
        log.critical("CRITICAL: compra no autorizada — %s", symbol)
        return False

    if symbol in state["positions"] or _slots_available() <= 0:
        return False

    if _in_cooldown(symbol):
        return False

    try:
        free = await _async(_fetch_usdc_free)
    except Exception as e:
        log.error("Error USDC libre: %s", e)
        return False

    amount = min(TRADE_USDC, free * 0.98)
    if amount < MIN_TRADE_USDC:
        log.warning("USDC insuficiente: %.2f", free)
        return False

    try:
        result = await _async(_execute_buy, symbol, amount)
    except Exception as e:
        log.error("Fallo comprando %s: %s", symbol, e)
        await _msg_error_grave(bot, f"No pude comprar {_coin_label(symbol)}: {e}")
        return False

    if symbol not in ALLOWED_SYMBOLS:
        log.critical("CRITICAL: activo no autorizado post-buy — %s", symbol)
        await _async(_execute_sell, symbol, result["quantity"])
        return False

    tp_oid = await _async(
        _place_tp_limit_order, symbol, result["quantity"], result["entry_price"]
    )
    result["tp_order_id"] = tp_oid

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
    log.info("COMPRA: %s %.2f USDC @ %.6f | TP=%s",
             symbol, result["invested"], result["entry_price"], tp_oid)
    return True

async def _sell(symbol: str, pos: dict, bot: Bot, motivo: str, is_sl: bool = False):
    tp_oid = pos.get("tp_order_id", "")
    if tp_oid:
        await _async(_cancel_order, symbol, tp_oid)

    try:
        result = await _async(_execute_sell, symbol, pos["quantity"])
    except Exception as e:
        log.error("Fallo vendiendo %s: %s", symbol, e)
        await _msg_error_grave(bot, f"No pude vender {_coin_label(symbol)}: {e}")
        return

    pnl = result["proceeds"] - pos["invested"]
    state["positions"].pop(symbol, None)
    state["daily_realized_pnl"] = round(
        state.get("daily_realized_pnl", 0.0) + pnl, 4
    )
    if pnl >= 0:
        state["wins_today"]   = state.get("wins_today", 0) + 1
    else:
        state["losses_today"] = state.get("losses_today", 0) + 1

    # Activar cooldown si fue un stop-loss
    if is_sl:
        _set_cooldown(symbol)

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
    now = datetime.now(timezone.utc)
    log.info("━━━ CICLO %s ━━━", now.strftime("%d/%m %H:%M"))
    state["last_scan"] = now.isoformat()

    try:
        total = await _async(_fetch_total_portfolio_usdc)
    except Exception as e:
        log.error("Error portfolio: %s", e)
        return

    _reset_daily_if_needed(total)

    killed, dd = _check_kill_switch(total)
    if killed:
        if dd > 0:
            log.warning("Kill switch: drawdown %.2f%%", dd)
            await _msg_kill_switch(bot, total, dd)
        else:
            log.warning("Kill switch activo: %s",
                        state.get("kill_switch_reason", "?"))
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
        log.warning("Error BTC guard: %s", e)
        btc_ok = True

    if not btc_ok:
        log.info("BTC Guard activo (%.2f%%)", btc_chg)
        return

    # Escaneo con cuádruple confirmación
    for symbol in ALLOWED_SYMBOLS:
        if _slots_available() <= 0:
            break
        if symbol in state["positions"]:
            continue
        if _in_cooldown(symbol):
            continue

        a = await _analyze(symbol)
        await asyncio.sleep(0.4)

        if not a:
            continue

        log.info("%s EMA=%s RSI=%.1f(↑%s) MACD=%s VOL=%s VELA=%s → %s",
                 symbol,
                 "✓" if a["ema_ok"]    else "✗",
                 a["rsi"],
                 "✓" if a["rsi_up"]    else "✗",
                 "✓" if a["macd_ok"]   else "✗",
                 "✓" if a["vol_ok"]    else "✗",
                 "✓" if a["candle_ok"] else "✗",
                 "✅" if a["signal"]    else "❌")

        if a["signal"]:
            reason = (f"EMA200✓ RSI{a['rsi']:.0f}↑ MACD-X✓ Vol✓ Vela✓")
            await _buy(symbol, bot, reason)
            await asyncio.sleep(1.0)

    log.info("━━━ FIN — %d/%d posiciones ━━━",
             len(state["positions"]), MAX_POSITIONS)

# ══════════════════════════════════════════════════════════════════════════════
# BUCLE DE RIESGO — cada 15 segundos
# ══════════════════════════════════════════════════════════════════════════════
async def risk_loop(bot: Bot):
    if not state["positions"]:
        return

    symbols = list(state["positions"].keys())
    try:
        prices = await _async(_fetch_tickers_batch, symbols)
    except Exception as e:
        log.warning("risk_loop batch error: %s", e)
        return

    for symbol, pos in list(state["positions"].items()):
        try:
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue

            entry   = pos["entry_price"]
            pnl_pct = (price - entry) / entry * 100.0

            if price > pos.get("peak_price", entry):
                state["positions"][symbol]["peak_price"] = price
            max_pnl = max(pos.get("max_pnl", 0.0), pnl_pct)
            state["positions"][symbol]["max_pnl"] = max_pnl

            # Escalado trailing
            new_sl, step_hit = _escalate_trailing({**pos, "max_pnl": max_pnl})
            if step_hit is not None:
                state["positions"][symbol]["sl_pct"] = new_sl
                save_state()
                log.info("TRAILING Step %d: %s pico=+%.2f%% → SL=+%.1f%%",
                         step_hit, symbol, max_pnl, new_sl)
                await _msg_sl_step(bot, symbol, step_hit, new_sl, max_pnl)

            sl_pct = state["positions"][symbol].get("sl_pct", -STOP_LOSS_PCT)

            # Stop-Loss
            if pnl_pct <= sl_pct:
                is_sl = sl_pct < 0   # solo el SL inicial activa cooldown
                if sl_pct >= 0:
                    motivo = (f"Trailing Step SL={sl_pct:+.1f}% "
                              f"(pico +{max_pnl:.2f}%, ahora {pnl_pct:+.2f}%)")
                else:
                    motivo = f"Stop-Loss -{STOP_LOSS_PCT}% ({pnl_pct:+.2f}%)"
                log.warning("SL: %s pnl=%.2f%% sl=%.2f%%", symbol, pnl_pct, sl_pct)
                await _sell(symbol, pos, bot, motivo, is_sl=is_sl)
                continue

            # TP fallback (si la limit order no se colocó o fue cancelada)
            if pnl_pct >= TAKE_PROFIT_PCT and not pos.get("tp_order_id"):
                motivo = f"TP fallback +{pnl_pct:.2f}%"
                log.info("TP fallback: %s", symbol)
                await _sell(symbol, pos, bot, motivo, is_sl=False)
                continue

        except Exception as e:
            log.error("risk_loop %s: %s", symbol, e)

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
            log.warning("Trading loop — red: %s. Siguiente ciclo.", type(e).__name__)
        except Exception as e:
            log.error("Error trading_loop: %s", e)
            await _msg_error_grave(bot, str(e))
        await asyncio.sleep(TRADE_LOOP_SEC)

async def _run_risk_loop(bot: Bot):
    await asyncio.sleep(30)
    log.info("Risk loop activo — cada %ds (batch)", RISK_LOOP_SEC)
    while True:
        try:
            await risk_loop(bot)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            log.warning("Risk loop — red: %s. Siguiente tick.", type(e).__name__)
        except Exception as e:
            log.error("Error risk_loop: %s", e)
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

    log.info("════ ELITE SCALPER V5 MAX ALPHA — OKX EEA ════")
    log.info("Persistencia: %s", os.path.abspath(POSITIONS_FILE))

    bot = Bot(token=TELEGRAM_TOKEN)
    connected = False

    try:
        ex = get_exchange()
        log.info("[1/3] Cargando mercados OKX (eea.okx.com)...")
        await _async(ex.load_markets)
        log.info("✅ [1/3] %d mercados", len(ex.markets))

        log.info("[2/3] Auth y saldo USDC...")
        total_bal = await _async(_fetch_total_portfolio_usdc)
        free_usdc = await _async(_fetch_usdc_free)
        log.info("✅ [2/3] OK — libre: %.2f / total: %.2f", free_usdc, total_bal)
        connected = True

        log.info("[3/3] ALLOWED_SYMBOLS en OKX...")
        for sym in ALLOWED_SYMBOLS:
            if sym in ex.markets:
                log.info("  ✅ %s", sym)
            else:
                log.warning("  ⚠️ %s NO disponible", sym)
        btc_chg = await _async(_fetch_btc_1h_change)
        log.info("✅ [3/3] BTC 1h: %+.2f%%", btc_chg)

    except ccxt.AuthenticationError as e:
        log.error("❌ Auth OKX: %s", e)
        await bot.send_message(chat_id=CHAT_ID,
            text="❌ Auth OKX fallida. Revisa OKX_API_KEY, SECRET y PASSPHRASE.")
        return

    except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
        log.error("❌ Red OKX: %s — continuando", e)

    except Exception as e:
        log.error("❌ Error arranque: %s", e)

    load_state()

    asyncio.create_task(_run_trading_loop(bot))
    asyncio.create_task(_run_risk_loop(bot))
    asyncio.create_task(_run_daily_report_loop(bot))

    if connected:
        await _send_status_report(bot)
    else:
        await _notify(bot,
            "Arrancado con problemas de red — reintentando automáticamente.")

    log.info("════ BOT LISTO — %s ════", "DRY RUN" if DRY_RUN else "MODO REAL")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
