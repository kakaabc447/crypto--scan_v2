from datetime import datetime, timedelta, timezone
import time
import ccxt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="WEEX / OKX / Gate 实时合约盯盘", layout="wide")

st.markdown(
    """
    <style>
    @media (max-width: 768px) {
        .block-container {
            padding-top: 2.2rem !important;
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
            padding-bottom: 2rem !important;
        }
        h1 {
            font-size: 1.35rem !important;
            line-height: 1.3 !important;
            margin-bottom: 0.4rem !important;
        }
        h2, h3 {
            font-size: 1.05rem !important;
            line-height: 1.3 !important;
            margin-top: 0.35rem !important;
            margin-bottom: 0.35rem !important;
        }
        div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]),
        div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetricDelta"]) {
            display: grid !important;
            grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
            gap: 0.45rem !important;
            align-items: stretch !important;
        }
        div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="column"],
        div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetricDelta"]) > div[data-testid="column"] {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            flex: none !important;
            padding: 0 !important;
        }
        div[data-testid="stMetric"] {
            height: 100% !important;
            background: rgba(128, 128, 128, 0.06);
            border-radius: 8px;
            padding: 0.45rem 0.55rem !important;
        }
        div[data-testid="stMetricLabel"] {
            font-size: 0.75rem !important;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.05rem !important;
        }
        div[data-testid="stMetricDelta"] {
            font-size: 0.72rem !important;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("⚡ WEEX / OKX / Gate 实时盯盘与出单参考")

# 侧边栏：交易所、选币与刷新设置
st.sidebar.header("1. 交易所与币种")
exchange_choice = st.sidebar.selectbox("选择交易所", ["weex", "okx", "gate"], index=0)

if "symbol_input" not in st.session_state:
    st.session_state["symbol_input"] = "BTC/USDT"

quick_symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ZEC/USDT", "DOGE/USDT", "SUI/USDT"]
q_cols = st.sidebar.columns(3)
for idx, qs in enumerate(quick_symbols):
    if q_cols[idx % 3].button(qs.split("/")[0], key=f"quick_{qs}", use_container_width=True):
        st.session_state["symbol_input"] = qs

symbol_input = st.sidebar.text_input(
    "搜索/输入交易对",
    key="symbol_input",
    help="例如 BTC/USDT、ZEC/USDT、DOGE/USDT、SUI/USDT",
).strip().upper()
if "/" not in symbol_input and symbol_input.endswith("USDT"):
    symbol_input = f"{symbol_input[:-4]}/USDT"
elif "/" not in symbol_input and symbol_input:
    symbol_input = f"{symbol_input}/USDT"

timeframe = st.sidebar.selectbox("K线周期", ["1m", "5m", "15m", "1h"], index=1)
htf_choice = st.sidebar.selectbox("大级别参考周期", ["1h", "4h"], index=0, help="用于判断小周期信号是否顺大势或产生级别冲突")
limit = st.sidebar.slider("K线根数", min_value=50, max_value=200, value=120, step=10)

st.sidebar.header("2. 自动刷新与时区")
refresh_sec = st.sidebar.slider("刷新间隔(秒)", min_value=5, max_value=60, value=15, step=5)
auto_refresh = st.sidebar.checkbox("开启静默自动刷新", value=True)
tz_offset_hours = st.sidebar.number_input(
    "显示时区 (UTC偏移小时)",
    min_value=-12,
    max_value=14,
    value=8,
    step=1,
    help="默认 UTC+8（北京时间），如遇时间偏差可按所在时区调整",
)

st.sidebar.header("3. 策略过滤与去重")
enable_rsi_assist = st.sidebar.checkbox("启用 RSI(14) 辅助提示", value=True)
enable_sweep_branch = st.sidebar.checkbox("启用假突破扫损反转分支", value=True)
vol_mult_req = st.sidebar.slider(
    "盘中折算放量倍数 (相对20均量)",
    min_value=1.0,
    max_value=3.0,
    value=1.35,
    step=0.05,
    help="按当前 K 线已走过的时间比例折算放量速度，需达到近 20 根均量的该倍数才出信号",
)
breakout_buf_pct = st.sidebar.number_input(
    "均线突破缓冲 (%)",
    min_value=0.0,
    max_value=1.0,
    value=0.15,
    step=0.05,
    format="%.2f",
    help="现价站上/跌破均线需拉开至少该幅度，减少在均线边上来回贴线摩擦的假信号",
)
confirm_two_ticks = st.sidebar.checkbox(
    "连续两轮刷新确认",
    value=True,
    help="同方向信号连续两次刷新都出现才正式锁单，减少一秒钟插针闪现",
)
dedup_pct = st.sidebar.number_input(
    "价格去重间距 (%)",
    min_value=0.1,
    max_value=10.0,
    value=0.6,
    step=0.1,
    format="%.2f",
    help="同交易所、同币种、同档位、同方向下，新价格与上一条参考价差距小于该比例时不重复出单",
)
max_strategy_items = st.sidebar.slider("最多保留策略条数", min_value=3, max_value=15, value=6, step=1)

if "strategy_vault" not in st.session_state:
    st.session_state["strategy_vault"] = {}
if "global_strategy_history" not in st.session_state:
    st.session_state["global_strategy_history"] = []
if "pending_signal_streak" not in st.session_state:
    st.session_state["pending_signal_streak"] = {}
if "active_view" not in st.session_state:
    st.session_state["active_view"] = "live"
if "expander_states" not in st.session_state:
    st.session_state["expander_states"] = {}

st.sidebar.header("4. 持仓与出单提示")
direction = st.sidebar.selectbox("持仓方向", ["做多 (Long)", "做空 (Short)"], index=0)
entry_price = st.sidebar.number_input("开仓均价", min_value=0.0, value=0.0, step=0.1, format="%.4f")
sl_pct = st.sidebar.number_input("止损幅度 (%)", min_value=0.1, max_value=20.0, value=1.0, step=0.1, format="%.2f")
tp1_pct = st.sidebar.number_input("第一止盈 TP1 (%)", min_value=0.2, max_value=30.0, value=1.5, step=0.1, format="%.2f")
tp2_pct = st.sidebar.number_input("第二止盈 TP2 (%)", min_value=0.5, max_value=50.0, value=3.0, step=0.1, format="%.2f")
breakeven_trigger_pct = st.sidebar.number_input(
    "建议推保本浮盈门槛 (%)",
    min_value=0.1,
    max_value=20.0,
    value=0.8,
    step=0.1,
    format="%.2f",
    help="多单或空单浮盈达到该幅度且未到 TP1 时，提示建议将止损移至开仓价附近",
)

st.sidebar.divider()
st.sidebar.header("5. 视图切换")
if st.session_state["active_view"] == "live":
    if st.sidebar.button("📜 查看历史策略页", key="btn_to_history", use_container_width=True):
        st.session_state["active_view"] = "history"
        st.rerun()
else:
    if st.sidebar.button("⚡ 返回实时盯盘", key="btn_to_live", use_container_width=True):
        st.session_state["active_view"] = "live"
        st.rerun()


def _now_tz_str(offset_hours: int) -> str:
    tz = timezone(timedelta(hours=int(offset_hours)))
    return datetime.now(tz).strftime("%H:%M:%S")


def _fmt_price(val: float) -> str:
    if abs(val) >= 100:
        return f"${val:,.2f}"
    if abs(val) >= 1:
        return f"${val:,.4f}"
    return f"${val:,.6f}"


def _tf_to_seconds(tf: str) -> int:
    mapping = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
    return mapping.get(tf, 300)


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().bfill()


def _fetch_weex_contract_klines(coin: str, tf: str, lim: int) -> pd.DataFrame:
    symbol_raw = f"{coin.upper()}USDT"
    url = "https://api-contract.weex.com/capi/v3/market/klines"
    params = {"symbol": symbol_raw, "interval": tf, "limit": min(lim, 100)}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not rows:
        raise ValueError("WEEX 返回空 K 线数据")
    df_raw = pd.DataFrame(rows)
    if isinstance(rows[0], dict):
        df = pd.DataFrame({
            "ts": pd.to_numeric(df_raw.get("ts", df_raw.get("timestamp", 0))),
            "open": pd.to_numeric(df_raw["open"]),
            "high": pd.to_numeric(df_raw["high"]),
            "low": pd.to_numeric(df_raw["low"]),
            "close": pd.to_numeric(df_raw["close"]),
            "volume": pd.to_numeric(df_raw.get("volume", 0)),
        })
    else:
        df = pd.DataFrame({
            "ts": pd.to_numeric(df_raw[0]),
            "open": pd.to_numeric(df_raw[1]),
            "high": pd.to_numeric(df_raw[2]),
            "low": pd.to_numeric(df_raw[3]),
            "close": pd.to_numeric(df_raw[4]),
            "volume": pd.to_numeric(df_raw[5] if df_raw.shape[1] > 5 else 0),
        })
    df = df.sort_values("ts").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["ts"], unit="ms")
    return df


@st.cache_data(ttl=5)
def fetch_ohlcv_by_exchange(ex_name: str, symbol: str, tf: str, lim: int) -> pd.DataFrame:
    coin_u = symbol.split("/")[0].strip().upper()
    if ex_name == "weex":
        try:
            if hasattr(ccxt, "weex"):
                ex = getattr(ccxt, "weex")({"enableRateLimit": True})
                for sym in (f"{coin_u}/USDT:USDT", f"{coin_u}/USDT", symbol):
                    try:
                        raw = ex.fetch_ohlcv(sym, timeframe=tf, limit=lim)
                        if raw:
                            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
                            df["time"] = pd.to_datetime(df["ts"], unit="ms")
                            break
                    except Exception:
                        continue
                else:
                    df = _fetch_weex_contract_klines(coin_u, tf, lim)
            else:
                df = _fetch_weex_contract_klines(coin_u, tf, lim)
        except Exception:
            df = _fetch_weex_contract_klines(coin_u, tf, lim)
    else:
        ex_cls = getattr(ccxt, ex_name)
        ex = ex_cls({"enableRateLimit": True})
        candidates = [f"{coin_u}/USDT:USDT", f"{coin_u}/USDT", symbol]
        raw = None
        last_err = None
        for sym in candidates:
            try:
                raw = ex.fetch_ohlcv(sym, timeframe=tf, limit=lim)
                if raw:
                    break
            except Exception as e:
                last_err = e
        if not raw:
            raise RuntimeError(last_err or f"无法获取 {ex_name} {symbol} 行情")
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms")

    df["ema7"] = df["close"].ewm(span=7, adjust=False).mean()
    df["ema25"] = df["close"].ewm(span=25, adjust=False).mean()
    df["rsi14"] = _calc_rsi(df["close"], period=14)
    df["atr14"] = _calc_atr(df, period=14)
    df["vol_ma20"] = df["volume"].rolling(window=20, min_periods=1).mean()
    return df


def _infer_htf_bias(df_htf: pd.DataFrame | None) -> tuple[str, str]:
    """推断大级别（1h / 4h）方向倾向，返回 (标签, 简述)。"""
    if df_htf is None or len(df_htf) < 2:
        return ("中性", "大级别数据不足，暂按中性参考")
    c = float(df_htf["close"].iloc[-1])
    e7 = float(df_htf["ema7"].iloc[-1])
    e25 = float(df_htf["ema25"].iloc[-1])
    if c > e25 and e7 >= e25:
        return ("多头", "收盘高于 EMA25 且均线偏多")
    if c < e25 and e7 <= e25:
        return ("空头", "收盘低于 EMA25 且均线偏空")
    return ("震荡/中性", "价格围绕 EMA25 震荡")


def _build_conflict_note(side: str, htf_tf: str, htf_bias: str) -> str:
    """当小周期信号方向与大级别明显相反时输出级别冲突提示。"""
    is_long = side.startswith("做多")
    if is_long and htf_bias == "空头":
        return f"⚠️ {htf_tf} 为空头倾向，逆大级别短线博弈，注意轻仓与推保本/止盈"
    if (not is_long) and htf_bias == "多头":
        return f"⚠️ {htf_tf} 为多头倾向，逆大级别短线博弈，注意轻仓与推保本/止盈"
    return ""


def _build_dynamic_advice(
    is_long: bool,
    pnl: float,
    entry_p: float,
    last_p: float,
    prev_p: float,
    ema7_val: float,
    prev_ema7: float,
    ema25_val: float,
    be_trigger_pct: float,
    hit_sl: bool,
    hit_tp1: bool,
    hit_tp2: bool,
) -> tuple[str, str, str]:
    """返回 (提示级别, 主建议文案, 推保本状态标签)。"""
    ema7_rising = ema7_val > prev_ema7
    ema7_falling = ema7_val < prev_ema7
    price_rising = last_p > prev_p
    can_move_be = pnl >= float(be_trigger_pct) and not hit_sl
    be_tag = f"✅ 已达推保本门槛（建议止损移至 {_fmt_price(entry_p)}）" if can_move_be else "⏳ 未达推保本门槛"

    if hit_sl:
        return ("error", "🚨 已触及止损线：建议严格按止损出单，避免扛单扩大亏损。", "❌ 已触及止损")
    if hit_tp2:
        return ("success", "🎯 已到达 TP2 目标位：冲高/下跌到位，建议分批或大部分止盈出单，锁定利润。", be_tag)

    if is_long:
        if hit_tp1:
            if last_p < ema7_val or ema7_falling:
                return ("warning", f"✅ 已过 TP1 且短线动能减弱（跌破 EMA7 或 EMA7 拐头）：建议先减仓锁盈，剩余仓位止损上移至开仓价 {_fmt_price(entry_p)} 附近。", be_tag)
            return ("success", f"✅ 已到达 TP1 目标位：建议先分批减仓落袋，止损上移至保本/开仓价 {_fmt_price(entry_p)} 附近，剩余仓位博弈 TP2。", be_tag)
        if can_move_be and (last_p < ema7_val or ema7_falling):
            return ("warning", f"🛡️ 建议推保本 + 注意走弱：当前浮盈 {pnl:+.2f}% 已达推保本门槛，建议把止损移至开仓价 {_fmt_price(entry_p)} 附近，同时留意 EMA7 转弱风险。", be_tag)
        if can_move_be:
            return ("info", f"🛡️ 建议推保本损：当前浮盈 {pnl:+.2f}% 已达到设定门槛，可将止损移至开仓价 {_fmt_price(entry_p)} 附近锁定无损空间，继续看 TP1/TP2。", be_tag)
        if pnl > 0 and (last_p < ema7_val or ema7_falling):
            return ("warning", "⚠️ 多单小幅浮盈但短线动能减弱（跌破 EMA7 或 EMA7 拐头）：建议留意结构走弱，视情况减仓或收紧防守。", be_tag)
        if last_p < ema25_val:
            return ("warning", "⚠️ 价格处于 EMA25 下方且未到 TP1：多头结构偏弱，注意控制仓位与防守线。", be_tag)
        if last_p > ema7_val and ema7_rising and price_rising:
            return ("info", "📈 多头动能仍在（站稳 EMA7 上方且 EMA7 向上）：趋势尚可，可按计划持有并盯紧推保本门槛与 TP1。", be_tag)
        return ("info", "⏳ 多单持仓观察中：留意 EMA7 支撑、推保本触发点与 TP1/TP2 距离。", be_tag)
    else:
        if hit_tp1:
            if last_p > ema7_val or ema7_rising:
                return ("warning", f"✅ 已过 TP1 且下跌动能减弱（反弹站上 EMA7 或 EMA7 拐头向上）：建议先减仓锁盈，剩余空单止损下移至开仓价 {_fmt_price(entry_p)} 附近。", be_tag)
            return ("success", f"✅ 已到达 TP1 目标位：建议先分批减仓落袋，止损下移至保本/开仓价 {_fmt_price(entry_p)} 附近，剩余仓位博弈 TP2。", be_tag)
        if can_move_be and (last_p > ema7_val or ema7_rising):
            return ("warning", f"🛡️ 建议推保本 + 注意反弹：当前浮盈 {pnl:+.2f}% 已达推保本门槛，建议把止损移至开仓价 {_fmt_price(entry_p)} 附近，防反弹洗盘。", be_tag)
        if can_move_be:
            return ("info", f"🛡️ 建议推保本损：当前浮盈 {pnl:+.2f}% 已达到设定门槛，可将止损移至开仓价 {_fmt_price(entry_p)} 附近锁定无损空间，继续看 TP1/TP2。", be_tag)
        if pnl > 0 and (last_p > ema7_val or ema7_rising):
            return ("warning", "⚠️ 空单小幅浮盈但下跌动能减弱（反弹靠近/站上 EMA7 或均线走平偏升）：建议留意结构走弱，视情况减仓或下移防守。", be_tag)
        if last_p > ema25_val:
            return ("warning", "⚠️ 价格处于 EMA25 上方且未到 TP1：空头结构偏弱，注意反弹风险与止损位置。", be_tag)
        if last_p < ema7_val and ema7_falling and not price_rising:
            return ("info", "📉 空头动能仍在（运行在 EMA7 下方且 EMA7 向下）：顺势节奏尚可，继续盯紧推保本门槛与 TP1。", be_tag)
        return ("info", "⏳ 空单持仓观察中：留意 EMA7 阻力、推保本触发点与 TP1/TP2 距离。", be_tag)


def _rsi_note(side: str, rsi_val: float, use_rsi: bool) -> str:
    if not use_rsi:
        return ""
    if side.startswith("做多"):
        if rsi_val >= 72:
            return f" | RSI(14)={rsi_val:.1f} 偏高，注意短线冲高回落、别追太高"
        if rsi_val <= 35:
            return f" | RSI(14)={rsi_val:.1f} 偏低回升，顺势留意企稳"
        return f" | RSI(14)={rsi_val:.1f} 中性"
    if side.startswith("做空"):
        if rsi_val <= 28:
            return f" | RSI(14)={rsi_val:.1f} 偏低，防短线超跌反弹、别在地板追空"
        if rsi_val >= 65:
            return f" | RSI(14)={rsi_val:.1f} 偏高回落，顺势留意阻力"
        return f" | RSI(14)={rsi_val:.1f} 中性"
    return f" | RSI(14)={rsi_val:.1f}"


def _build_early_invalid_line(
    is_long: bool,
    coin_short: str,
    tf: str,
    entry_p: float,
    sl_p: float,
    ema25_p: float,
    recent_low: float,
    recent_high: float,
) -> tuple[float, str, str]:
    """根据均线或近期结构高低点生成提前失效线（放在硬止损之前）。"""
    if is_long:
        candidates = [p for p in (ema25_p, recent_low) if sl_p < p < entry_p]
        if candidates:
            inv_p = max(candidates)
            label = "EMA25 均线位" if abs(inv_p - ema25_p) <= abs(inv_p - recent_low) else "近期低点结构位"
        else:
            inv_p = sl_p + (entry_p - sl_p) * 0.45
            label = "结构过渡位"
        msg = f"🛡️ 若 {coin_short} {tf} 跌破 {_fmt_price(inv_p)}（{label}），多头结构走弱，此策略提前失效（未到硬止损先防）。"
        return (inv_p, "below", msg)
    else:
        candidates = [p for p in (ema25_p, recent_high) if entry_p < p < sl_p]
        if candidates:
            inv_p = min(candidates)
            label = "EMA25 均线位" if abs(inv_p - ema25_p) <= abs(inv_p - recent_high) else "近期高点结构位"
        else:
            inv_p = sl_p - (sl_p - entry_p) * 0.45
            label = "结构过渡位"
        msg = f"🛡️ 若 {coin_short} {tf} 站稳 {_fmt_price(inv_p)}（{label}），空头结构走弱，此策略提前失效（未到硬止损先防）。"
        return (inv_p, "above", msg)


def _calc_intraday_pace_ratio(df: pd.DataFrame, tf: str) -> tuple[float, float]:
    """计算当前盘中 K 线按时间进度折算后的放量倍数，返回 (折算放量倍数, 进度比例)。"""
    vol_val = float(df["volume"].iloc[-1]) if "volume" in df.columns else 0.0
    vol_ma = float(df["vol_ma20"].iloc[-1]) if "vol_ma20" in df.columns else 0.0
    if vol_ma <= 0 or vol_val <= 0:
        return (1.0, 1.0)

    bar_sec = _tf_to_seconds(tf)
    now_ms = int(time.time() * 1000)
    last_ts = int(df["ts"].iloc[-1]) if "ts" in df.columns else now_ms
    elapsed_sec = max(0.0, (now_ms - last_ts) / 1000.0)
    progress = min(1.0, max(0.20, elapsed_sec / bar_sec)) if bar_sec > 0 else 1.0

    projected_vol = vol_val / progress
    pace_ratio = projected_vol / vol_ma
    return (pace_ratio, progress)


def _evaluate_candidate_strategies(
    df: pd.DataFrame,
    coin_short: str,
    tf: str,
    htf_tf: str,
    htf_bias: str,
    use_rsi: bool,
    use_sweep: bool,
    vol_mult: float,
    buf_pct: float,
) -> list[dict]:
    """基于盘中最新价格（iloc[-1]）评估多分支候选策略：顺势突破/回踩 + 扫损反转 + 级别冲突提示。"""
    if len(df) < 25:
        return []

    last_p = float(df["close"].iloc[-1])
    last_o = float(df["open"].iloc[-1])
    last_h = float(df["high"].iloc[-1])
    last_l = float(df["low"].iloc[-1])
    prev_p = float(df["close"].iloc[-2])
    ema7 = float(df["ema7"].iloc[-1])
    prev_ema7 = float(df["ema7"].iloc[-2])
    ema25 = float(df["ema25"].iloc[-1])
    prev_ema25 = float(df["ema25"].iloc[-2])
    rsi_val = float(df["rsi14"].iloc[-1]) if "rsi14" in df.columns else 50.0
    atr_val = float(df["atr14"].iloc[-1]) if "atr14" in df.columns else max(last_p * 0.008, 1e-8)

    pace_ratio, progress = _calc_intraday_pace_ratio(df, tf)
    has_volume_confirm = pace_ratio >= vol_mult

    spread_pct = abs(ema7 - ema25) / ema25 * 100 if ema25 else 0.0
    win12 = df.tail(min(len(df), 12))
    range_pct = ((float(win12["high"].max()) - float(win12["low"].min())) / last_p * 100) if last_p else 0.0
    is_choppy = spread_pct < 0.25 or range_pct < 0.85

    win6 = df.tail(min(len(df), 6))
    recent_low = float(win6["low"].min())
    recent_high = float(win6["high"].max())

    prev20 = df.iloc[-21:-1]
    prev_high20 = float(prev20["high"].max())
    prev_low20 = float(prev20["low"].min())

    buf_ratio = max(0.0, float(buf_pct)) / 100.0
    above_ema7_buf = last_p >= ema7 * (1.0 + buf_ratio)
    below_ema7_buf = last_p <= ema7 * (1.0 - buf_ratio)

    ema7_up = ema7 > prev_ema7
    ema7_down = ema7 < prev_ema7
    ema25_up = ema25 >= prev_ema25
    ema25_down = ema25 <= prev_ema25
    cross_up = prev_ema7 <= prev_ema25 and ema7 > ema25
    cross_down = prev_ema7 >= prev_ema25 and ema7 < ema25

    pace_tag = f"折算放量{pace_ratio:.2f}x（进度{int(progress * 100)}%)"
    candidates: list[dict] = []

    if use_sweep:
        upper_wick = last_h - max(last_o, last_p)
        lower_wick = min(last_o, last_p) - last_l
        if last_h > prev_high20 and last_p < prev_high20 and upper_wick >= atr_val * 0.45 and pace_ratio >= max(1.1, vol_mult * 0.85):
            sweep_sl = max(last_h + atr_val * 0.25, last_p * 1.003)
            candidates.append({
                "mode": "扫损反转",
                "side": "做空",
                "badge": "🧲 扫高反转空",
                "reason": f"{pace_tag}，刺破前20高点({_fmt_price(prev_high20)})至{_fmt_price(last_h)}后收回，上影拒绝扫损反转" + _rsi_note("做空", rsi_val, use_rsi),
                "entry": last_p,
                "custom_sl_price": sweep_sl,
                "tp1_mult": 1.6,
                "tp2_mult": 2.8,
            })
        elif last_l < prev_low20 and last_p > prev_low20 and lower_wick >= atr_val * 0.45 and pace_ratio >= max(1.1, vol_mult * 0.85):
            sweep_sl = min(last_l - atr_val * 0.25, last_p * 0.997)
            candidates.append({
                "mode": "扫损反转",
                "side": "做多",
                "badge": "🧲 扫低反转多",
                "reason": f"{pace_tag}，刺破前20低点({_fmt_price(prev_low20)})至{_fmt_price(last_l)}后收回，下影拒绝扫损反转" + _rsi_note("做多", rsi_val, use_rsi),
                "entry": last_p,
                "custom_sl_price": sweep_sl,
                "tp1_mult": 1.6,
                "tp2_mult": 2.8,
            })

    if not is_choppy and has_volume_confirm:
        robust_cand: dict | None = None
        fast_cand: dict | None = None

        if ema7 > ema25 and ema7_up and ema25_up and last_p >= ema25 * (1.0 + buf_ratio * 0.5):
            pullback = abs(last_p - ema7) / ema7 <= 0.008 or last_l <= ema7 * 1.002
            mode_name = "均线回踩顺势" if pullback else "趋势突破顺势"
            badge_name = "🟢 回踩企稳多" if pullback else "🟢 顺势突破多"
            reason = (
                f"{pace_tag}，EMA7 高于 EMA25 且双均线向上，回踩 EMA7 附近企稳延续"
                if pullback
                else f"{pace_tag}，EMA7 高于 EMA25 且双均线向上，顺势突破延续"
            )
            robust_cand = {
                "mode": mode_name,
                "side": "做多",
                "badge": badge_name,
                "reason": reason + _rsi_note("做多", rsi_val, use_rsi),
                "entry": last_p,
                "sl_mult": 1.5,
                "tp1_mult": 2.4,
                "tp2_mult": 4.0,
            }
        elif ema7 < ema25 and ema7_down and ema25_down and last_p <= ema25 * (1.0 - buf_ratio * 0.5):
            rebound = abs(last_p - ema7) / ema7 <= 0.008 or last_h >= ema7 * 0.998
            mode_name = "均线回踩顺势" if rebound else "趋势突破顺势"
            badge_name = "🔴 反弹承压空" if rebound else "🔴 顺势跌破空"
            reason = (
                f"{pace_tag}，EMA7 低于 EMA25 且双均线向下，反弹靠近 EMA7 受阻延续"
                if rebound
                else f"{pace_tag}，EMA7 低于 EMA25 且双均线向下，顺势跌破延续"
            )
            robust_cand = {
                "mode": mode_name,
                "side": "做空",
                "badge": badge_name,
                "reason": reason + _rsi_note("做空", rsi_val, use_rsi),
                "entry": last_p,
                "sl_mult": 1.5,
                "tp1_mult": 2.4,
                "tp2_mult": 4.0,
            }

        if robust_cand is None:
            if (cross_up and above_ema7_buf) or (ema7_up and above_ema7_buf and last_p > prev_p):
                reason = (
                    f"{pace_tag}，EMA7 上穿 EMA25 且拉开 {buf_pct:.2f}% 缓冲，快节奏偏多"
                    if cross_up
                    else f"{pace_tag}，现价站稳 EMA7 之上（缓冲 {buf_pct:.2f}%）且均线向上，快节奏短多"
                )
                fast_cand = {
                    "mode": "快节奏短线",
                    "side": "做多",
                    "badge": "⚡ 快多脉冲",
                    "reason": reason + _rsi_note("做多", rsi_val, use_rsi),
                    "entry": last_p,
                    "sl_mult": 0.9,
                    "tp1_mult": 1.4,
                    "tp2_mult": 2.5,
                }
            elif (cross_down and below_ema7_buf) or (ema7_down and below_ema7_buf and last_p < prev_p):
                reason = (
                    f"{pace_tag}，EMA7 下穿 EMA25 且拉开 {buf_pct:.2f}% 缓冲，快节奏偏空"
                    if cross_down
                    else f"{pace_tag}，现价跌破 EMA7 之下（缓冲 {buf_pct:.2f}%）且均线向下，快节奏短空"
                )
                fast_cand = {
                    "mode": "快节奏短线",
                    "side": "做空",
                    "badge": "⚡ 快空脉冲",
                    "reason": reason + _rsi_note("做空", rsi_val, use_rsi),
                    "entry": last_p,
                    "sl_mult": 0.9,
                    "tp1_mult": 1.4,
                    "tp2_mult": 2.5,
                }

        for c in (robust_cand, fast_cand):
            if c is not None:
                candidates.append(c)

    eff_atr = max(atr_val, last_p * 0.0025)
    final_list: list[dict] = []

    for item in candidates:
        ep = float(item["entry"])
        is_l = item["side"].startswith("做多")
        tp1_dist = eff_atr * float(item["tp1_mult"])
        tp2_dist = eff_atr * float(item["tp2_mult"])

        if "custom_sl_price" in item:
            sl_p = float(item["custom_sl_price"])
        else:
            sl_dist = eff_atr * float(item.get("sl_mult", 1.2))
            sl_p = max(ep - sl_dist, ep * 0.80) if is_l else (ep + sl_dist)

        if is_l:
            if sl_p >= ep:
                sl_p = max(ep - eff_atr * 1.0, ep * 0.80)
            tp1_p = ep + tp1_dist
            tp2_p = ep + tp2_dist
        else:
            if sl_p <= ep:
                sl_p = ep + eff_atr * 1.0
            tp1_p = max(ep - tp1_dist, ep * 0.20)
            tp2_p = max(ep - tp2_dist, ep * 0.10)

        inv_p, inv_dir, inv_text = _build_early_invalid_line(
            is_long=is_l,
            coin_short=coin_short,
            tf=tf,
            entry_p=ep,
            sl_p=sl_p,
            ema25_p=ema25,
            recent_low=recent_low,
            recent_high=recent_high,
        )
        conflict_text = _build_conflict_note(item["side"], htf_tf, htf_bias)

        item["sl_pct"] = abs(sl_p - ep) / ep * 100
        item["tp1_pct"] = abs(tp1_p - ep) / ep * 100
        item["tp2_pct"] = abs(tp2_p - ep) / ep * 100
        item["sl_price"] = sl_p
        item["tp1_price"] = tp1_p
        item["tp2_price"] = tp2_p
        item["invalid_price"] = inv_p
        item["invalid_dir"] = inv_dir
        item["invalid_text"] = inv_text
        item["conflict_text"] = conflict_text
        item["htf_tag"] = f"{htf_tf}:{htf_bias}"
        final_list.append(item)

    return final_list


def _filter_by_two_tick_streak(vault_key: str, candidates: list[dict], require_two_ticks: bool) -> list[dict]:
    """若开启连续两轮刷新确认，则同方向候选信号连续出现两次才放行。"""
    streaks: dict[str, int] = dict(st.session_state["pending_signal_streak"])
    active_keys = {f"{vault_key}|{c['mode']}|{c['side']}" for c in candidates}

    prefix = f"{vault_key}|"
    for k in list(streaks.keys()):
        if k.startswith(prefix) and k not in active_keys:
            del streaks[k]

    confirmed: list[dict] = []
    for cand in candidates:
        sig_key = f"{vault_key}|{cand['mode']}|{cand['side']}"
        cnt = streaks.get(sig_key, 0) + 1
        streaks[sig_key] = cnt
        if not require_two_ticks or cnt >= 2:
            confirmed.append(cand)

    st.session_state["pending_signal_streak"] = streaks
    return confirmed


def _upsert_global_history(record: dict, max_global_items: int = 50) -> None:
    """按快照 ID 更新或插入全局历史记录，让 TP1 / TP2 / 止损到达时间也能在历史页看到。"""
    global_items: list[dict] = list(st.session_state["global_strategy_history"])
    replaced = False
    for i, item in enumerate(global_items):
        if item.get("id") == record.get("id"):
            global_items[i] = record
            replaced = True
            break
    if not replaced:
        global_items.insert(0, record)
    st.session_state["global_strategy_history"] = global_items[:max_global_items]


def _sync_strategy_vault(
    vault_key: str,
    ex_name: str,
    coin_short: str,
    tf: str,
    candidates: list[dict],
    min_gap_pct: float,
    max_items: int,
    tz_offset: int,
) -> int:
    """将符合条件且价格不重复的策略锁定快照存入独立策略区与全局历史记录，返回本次新增条数。"""
    items: list[dict] = list(st.session_state["strategy_vault"].get(vault_key, []))
    exp_map: dict[str, bool] = dict(st.session_state["expander_states"])
    added = 0

    for cand in candidates:
        cand_entry = float(cand["entry"])
        is_duplicate = False
        for existing in items:
            if existing["mode"] == cand["mode"] and existing["side"] == cand["side"]:
                prev_entry = float(existing["entry"])
                if prev_entry > 0:
                    gap = abs(cand_entry - prev_entry) / prev_entry * 100
                    if gap < min_gap_pct:
                        is_duplicate = True
                        break
        if not is_duplicate:
            now_str = _now_tz_str(tz_offset)
            snap_id = f"{int(time.time() * 1000)}_{added}_{cand['mode']}"
            snapshot = {
                "id": snap_id,
                "created_at": now_str,
                "ex": ex_name.upper(),
                "coin": coin_short,
                "tf": tf,
                "htf_tag": str(cand.get("htf_tag", "")),
                "mode": cand["mode"],
                "side": cand["side"],
                "badge": cand["badge"],
                "reason": cand["reason"],
                "conflict_text": str(cand.get("conflict_text", "")),
                "entry": cand_entry,
                "sl_pct": float(cand["sl_pct"]),
                "tp1_pct": float(cand["tp1_pct"]),
                "tp2_pct": float(cand["tp2_pct"]),
                "sl_price": float(cand["sl_price"]),
                "tp1_price": float(cand["tp1_price"]),
                "tp2_price": float(cand["tp2_price"]),
                "invalid_price": float(cand["invalid_price"]),
                "invalid_dir": str(cand["invalid_dir"]),
                "invalid_text": str(cand["invalid_text"]),
                "hit_tp1_at": None,
                "hit_tp2_at": None,
                "hit_sl_at": None,
            }
            items.insert(0, snapshot)
            exp_map[snap_id] = True
            _upsert_global_history(dict(snapshot))
            added += 1

    items = items[:max_items]
    st.session_state["strategy_vault"][vault_key] = items
    st.session_state["expander_states"] = exp_map
    return added


def _update_snapshot_hit_times(items: list[dict], last_p: float, tz_offset: int) -> list[dict]:
    """根据当前最新价记录快照首次到达止损 / TP1 / TP2 的时间，并同步到全局历史。"""
    now_str = _now_tz_str(tz_offset)
    for snap in items:
        is_long = str(snap.get("side", "")).startswith("做多")
        sl_p = float(snap.get("sl_price", 0.0))
        tp1_p = float(snap.get("tp1_price", 0.0))
        tp2_p = float(snap.get("tp2_price", 0.0))

        if is_long:
            if tp1_p > 0 and last_p >= tp1_p and not snap.get("hit_tp1_at"):
                snap["hit_tp1_at"] = now_str
            if tp2_p > 0 and last_p >= tp2_p and not snap.get("hit_tp2_at"):
                snap["hit_tp2_at"] = now_str
            if sl_p > 0 and last_p <= sl_p and not snap.get("hit_sl_at"):
                snap["hit_sl_at"] = now_str
        else:
            if tp1_p > 0 and last_p <= tp1_p and not snap.get("hit_tp1_at"):
                snap["hit_tp1_at"] = now_str
            if tp2_p > 0 and last_p <= tp2_p and not snap.get("hit_tp2_at"):
                snap["hit_tp2_at"] = now_str
            if sl_p > 0 and last_p >= sl_p and not snap.get("hit_sl_at"):
                snap["hit_sl_at"] = now_str
        _upsert_global_history(dict(snap))
    return items


def _delete_snapshot(vault_key: str, snap_id: str) -> None:
    items: list[dict] = list(st.session_state["strategy_vault"].get(vault_key, []))
    st.session_state["strategy_vault"][vault_key] = [x for x in items if x["id"] != snap_id]
    st.session_state["expander_states"].pop(snap_id, None)


def render_history_view() -> None:
    h_left, h_right = st.columns([4, 1])
    with h_left:
        st.subheader("📜 历史策略页（全币种/全周期快照汇总）")
    with h_right:
        if st.button("清空历史记录", key="clear_history_page", use_container_width=True):
            st.session_state["global_strategy_history"] = []
            st.rerun()

    hist_items: list[dict] = list(st.session_state.get("global_strategy_history", []))
    st.caption(f"当前已汇总 {len(hist_items)} 条历史策略快照（UTC{int(tz_offset_hours):+d} 时间；点左侧“返回实时盯盘”可切回主界面）")

    if not hist_items:
        st.info("暂无历史策略记录。回到实时盯盘后，生成的新策略快照会自动汇总到这里。")
        return

    for idx, item in enumerate(hist_items):
        with st.container(border=True):
            st.markdown(
                f"**{idx + 1} · [{item.get('mode', '')}] {item.get('coin', '')} {item.get('side', '')} "
                f"@{_fmt_price(float(item.get('entry', 0.0)))}** · {item.get('badge', '')} · "
                f"`{item.get('created_at', '--')}` · `{item.get('htf_tag', '')}` · `{item.get('ex', '')}`"
            )
            st.write(f"**触发依据**：{item.get('reason', '')}")
            if item.get("conflict_text"):
                st.write(f"**冲突提示**：{item['conflict_text']}")
            st.write(
                f"**关键点位**：入场 `{_fmt_price(float(item.get('entry', 0.0)))}` · "
                f"止损 `{_fmt_price(float(item.get('sl_price', 0.0)))} (-{float(item.get('sl_pct', 0.0)):.2f}%)` · "
                f"TP1 `{_fmt_price(float(item.get('tp1_price', 0.0)))} (+{float(item.get('tp1_pct', 0.0)):.2f}%)` · "
                f"TP2 `{_fmt_price(float(item.get('tp2_price', 0.0)))} (+{float(item.get('tp2_pct', 0.0)):.2f}%)`"
            )
            st.write(
                f"**跟进状态**：到 TP1 {item.get('hit_tp1_at') or '未到达'} · "
                f"到 TP2 {item.get('hit_tp2_at') or '未到达'} · "
                f"到止损 {item.get('hit_sl_at') or '未到达'}"
            )
            if item.get("invalid_text"):
                st.write(f"**提前预警**：{item['invalid_text']}")


if st.session_state["active_view"] == "history":
    render_history_view()
else:
    interval_arg = refresh_sec if auto_refresh else None

    @st.fragment(run_every=interval_arg)
    def render_live_panel() -> None:
        active_symbol = symbol_input or "BTC/USDT"
        coin_short = active_symbol.split("/")[0]
        vault_key = f"{exchange_choice}_{active_symbol}_{timeframe}"
        st.subheader(f"📌 当前选中币种实时卡片（{active_symbol} · {exchange_choice.upper()}）")

        try:
            df = fetch_ohlcv_by_exchange(exchange_choice, active_symbol, timeframe, limit)
            try:
                df_htf = fetch_ohlcv_by_exchange(exchange_choice, active_symbol, htf_choice, 60)
            except Exception:
                df_htf = None

            htf_bias, htf_desc = _infer_htf_bias(df_htf)

            last_p = float(df["close"].iloc[-1])
            prev_p = float(df["close"].iloc[-2]) if len(df) > 1 else last_p
            chg = ((last_p - prev_p) / prev_p) * 100 if prev_p else 0.0
            ema7_val = float(df["ema7"].iloc[-1])
            prev_ema7 = float(df["ema7"].iloc[-2]) if len(df) > 1 else ema7_val
            ema25_val = float(df["ema25"].iloc[-1])
            rsi_val = float(df["rsi14"].iloc[-1]) if "rsi14" in df.columns else 50.0

            c1, c2, c3, c4 = st.columns(4)
            c1.metric(f"{active_symbol} 最新价", _fmt_price(last_p), f"{chg:+.2f}%")
            c2.metric("EMA7 价格", _fmt_price(ema7_val), f"价差 {last_p - ema7_val:+.4f}")
            c3.metric(
                f"{htf_choice.upper()} 方向参考",
                htf_bias,
                f"RSI14 {rsi_val:.1f}" if enable_rsi_assist else f"EMA25 {_fmt_price(ema25_val)}",
            )
            c4.metric(
                "刷新状态",
                f"🟢 每 {refresh_sec}s" if auto_refresh else "⏸️ 已暂停",
                f"{exchange_choice.upper()} · {timeframe}",
            )
            st.caption(f"大级别参考（{htf_choice}）：{htf_desc}；本周期 EMA25：{_fmt_price(ema25_val)}")

            if entry_price > 0:
                st.divider()
                st.subheader("🚪 出单参考（止损 / TP1 / TP2、推保本与动态跟进）")
                is_long = direction.startswith("做多")
                if is_long:
                    pnl = ((last_p - entry_price) / entry_price) * 100
                    sl_p = entry_price * (1 - sl_pct / 100)
                    tp1_p = entry_price * (1 + tp1_pct / 100)
                    tp2_p = entry_price * (1 + tp2_pct / 100)
                    hit_sl = last_p <= sl_p
                    hit_tp2 = last_p >= tp2_p
                    hit_tp1 = last_p >= tp1_p
                else:
                    pnl = ((entry_price - last_p) / entry_price) * 100
                    sl_p = entry_price * (1 + sl_pct / 100)
                    tp1_p = entry_price * (1 - tp1_pct / 100)
                    tp2_p = entry_price * (1 - tp2_pct / 100)
                    hit_sl = last_p >= sl_p
                    hit_tp2 = last_p <= tp2_p
                    hit_tp1 = last_p <= tp1_p

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("当前浮盈亏", f"{pnl:+.2f}%")
                k2.metric("初始止损价", _fmt_price(sl_p))
                k3.metric("TP1 价", _fmt_price(tp1_p))
                k4.metric("TP2 价", _fmt_price(tp2_p))

                level, advice_text, be_status = _build_dynamic_advice(
                    is_long=is_long,
                    pnl=pnl,
                    entry_p=entry_price,
                    last_p=last_p,
                    prev_p=prev_p,
                    ema7_val=ema7_val,
                    prev_ema7=prev_ema7,
                    ema25_val=ema25_val,
                    be_trigger_pct=float(breakeven_trigger_pct),
                    hit_sl=hit_sl,
                    hit_tp1=hit_tp1,
                    hit_tp2=hit_tp2,
                )
                st.caption(f"保本状态：{be_status}（门槛 {breakeven_trigger_pct:.2f}%）")
                if level == "error":
                    st.error(advice_text)
                elif level == "success":
                    st.success(advice_text)
                elif level == "warning":
                    st.warning(advice_text)
                else:
                    st.info(advice_text)

                pos_conflict = _build_conflict_note(direction, htf_choice, htf_bias)
                if pos_conflict:
                    st.warning(pos_conflict)

            # 独立策略区：折叠卡片排列（新单默认展开，手动收起后保持收起）
            st.divider()
            hdr_l, hdr_r = st.columns([4, 1])
            with hdr_l:
                st.subheader(f"🎯 独立短线策略区（{active_symbol} · {timeframe} · 多分支+风控过滤）")
            with hdr_r:
                if st.button("清空本币策略", key=f"clear_vault_{vault_key}", use_container_width=True):
                    st.session_state["strategy_vault"][vault_key] = []
                    st.rerun(scope="fragment")

            raw_candidates = _evaluate_candidate_strategies(
                df,
                coin_short=coin_short,
                tf=timeframe,
                htf_tf=htf_choice,
                htf_bias=htf_bias,
                use_rsi=enable_rsi_assist,
                use_sweep=enable_sweep_branch,
                vol_mult=float(vol_mult_req),
                buf_pct=float(breakout_buf_pct),
            )
            confirmed_candidates = _filter_by_two_tick_streak(
                vault_key=vault_key,
                candidates=raw_candidates,
                require_two_ticks=bool(confirm_two_ticks),
            )
            added_cnt = _sync_strategy_vault(
                vault_key=vault_key,
                ex_name=exchange_choice,
                coin_short=coin_short,
                tf=timeframe,
                candidates=confirmed_candidates,
                min_gap_pct=float(dedup_pct),
                max_items=int(max_strategy_items),
                tz_offset=int(tz_offset_hours),
            )
            vault_items = _update_snapshot_hit_times(
                list(st.session_state["strategy_vault"].get(vault_key, [])),
                last_p=last_p,
                tz_offset=int(tz_offset_hours),
            )
            st.session_state["strategy_vault"][vault_key] = vault_items

            streak_tag = "两轮确认" if confirm_two_ticks else "单轮即出"
            sweep_tag = "扫损开" if enable_sweep_branch else "扫损关"
            status_note = (
                f"本次新增 {added_cnt} 条策略快照（{sweep_tag}｜折算放量≥{vol_mult_req:.2f}x｜缓冲{breakout_buf_pct:.2f}%｜{streak_tag}｜去重{dedup_pct:.2f}%）"
                if added_cnt > 0
                else f"当前无新不重复单据（{sweep_tag}｜折算放量≥{vol_mult_req:.2f}x｜缓冲{breakout_buf_pct:.2f}%｜{streak_tag}｜去重{dedup_pct:.2f}%）"
            )
            st.caption(status_note)

            if not vault_items:
                st.info(
                    "当前处于震荡过滤、折算量能不足、未过突破缓冲或等待连续两轮确认，暂未生成新单据。符合条件且拉开去重间距后会出现在这里。"
                )
            else:
                exp_map: dict[str, bool] = st.session_state["expander_states"]
                for idx, snap in enumerate(vault_items):
                    snap_id = snap["id"]
                    is_exp = bool(exp_map.get(snap_id, True))
                    header_line = (
                        f"{idx + 1} · [{snap['mode']}] {snap['coin']} {snap['side']} "
                        f"@{_fmt_price(float(snap['entry']))} · {snap['badge']} · "
                        f"`{snap['created_at']}` · `{snap.get('htf_tag', '')}`"
                    )
                    with st.expander(header_line, expanded=is_exp):
                        st.write(f"**触发依据**：{snap['reason']}")
                        if snap.get("conflict_text"):
                            st.write(f"**冲突提示**：{snap['conflict_text']}")
                        st.write(
                            f"**关键点位**：入场 `{_fmt_price(float(snap['entry']))}` · "
                            f"止损 `{_fmt_price(float(snap['sl_price']))} (-{float(snap['sl_pct']):.2f}%)` · "
                            f"TP1 `{_fmt_price(float(snap['tp1_price']))} (+{float(snap['tp1_pct']):.2f}%)` · "
                            f"TP2 `{_fmt_price(float(snap['tp2_price']))} (+{float(snap['tp2_pct']):.2f}%)`"
                        )
                        st.write(
                            f"**跟进状态**：到 TP1 {snap.get('hit_tp1_at') or '未到达'} · "
                            f"到 TP2 {snap.get('hit_tp2_at') or '未到达'} · "
                            f"到止损 {snap.get('hit_sl_at') or '未到达'}"
                        )

                        inv_p = float(snap.get("invalid_price", 0.0))
                        inv_dir = snap.get("invalid_dir", "")
                        inv_text = snap.get("invalid_text", "")
                        if inv_text and inv_p > 0:
                            triggered = (inv_dir == "below" and last_p < inv_p) or (inv_dir == "above" and last_p > inv_p)
                            if triggered:
                                st.warning(f"🚨 已越过提前失效线（现价 {_fmt_price(last_p)}）—— {inv_text}")
                            else:
                                st.write(f"**提前预警**：{inv_text}")

                        if st.button("删除本条策略", key=f"del_snap_{vault_key}_{snap_id}", use_container_width=True):
                            _delete_snapshot(vault_key, snap_id)
                            st.rerun(scope="fragment")

            st.divider()
            st.subheader(f"📉 {active_symbol} 价格与均线走势（EMA7 / EMA25）")

            fig = go.Figure(
                data=[
                    go.Candlestick(
                        x=df["time"],
                        open=df["open"],
                        high=df["high"],
                        low=df["low"],
                        close=df["close"],
                        name="K线",
                    ),
                    go.Scatter(
                        x=df["time"],
                        y=df["ema7"],
                        mode="lines",
                        name=f"EMA7 ({_fmt_price(ema7_val)})",
                        line=dict(width=1.4),
                    ),
                    go.Scatter(
                        x=df["time"],
                        y=df["ema25"],
                        mode="lines",
                        name=f"EMA25 ({_fmt_price(ema25_val)})",
                        line=dict(width=1.4),
                    ),
                ]
            )
            fig.update_layout(
                height=380,
                margin=dict(l=10, r=10, t=20, b=10),
                xaxis_rangeslider_visible=False,
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                f"{exchange_choice.upper()} · {active_symbol} · {timeframe}（{len(df)} 根）｜"
                f"最新 EMA7: {_fmt_price(ema7_val)} ｜最新 EMA25: {_fmt_price(ema25_val)}（点按或悬停 K 线可看各时点均线价格）"
            )

        except Exception as e:
            st.error(f"行情加载失败：{e}")

        if st.button("立即刷新"):
            st.cache_data.clear()
            st.rerun(scope="fragment")

    render_live_panel()
