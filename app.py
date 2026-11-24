import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timezone, timedelta

st.set_page_config(page_title="短线策略与持仓跟进面板", layout="wide")
st.title("⚡ 短线策略与持仓跟进面板（BTC / ETH / SOL / ZEC）")

# ================= 侧边栏配置 =================
with st.sidebar:
    st.header("⚙️ 策略与风控参数")
    webhook_url = st.text_input(
        "企业微信 Webhook（可选）",
        placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
    )
    min_rr = st.number_input("最小盈亏比 (RR)", min_value=1.0, max_value=10.0, value=1.5, step=0.1)
    cooldown_min = st.number_input("同源信号冷却（分钟）", min_value=5, max_value=240, value=30, step=5)
    auto_track_signals = st.toggle("触发策略自动加入持仓跟进", value=True)
    refresh_sec = st.number_input("自动刷新间隔（秒）", min_value=10, max_value=300, value=20, step=5)
    auto_run = st.toggle("开启自动刷新监控", value=True)

    if st.button("🔔 发送测试推送"):
        st.toast("已生成本地测试提示")
        if webhook_url.startswith("http"):
            try:
                requests.post(
                    webhook_url,
                    json={"msgtype": "text", "text": {"content": "[测试推送] 短线策略监控面板连接正常"}},
                    timeout=5
                )
                st.success("企业微信推送成功")
            except Exception as e:
                st.error(f"推送失败: {e}")

    if st.button("🧹 清空已触发策略记录"):
        st.session_state.alerts = []
        st.rerun()

COINS = [
    {"name": "BTC", "okx": "BTC-USDT-SWAP", "gate": "BTC_USDT"},
    {"name": "ETH", "okx": "ETH-USDT-SWAP", "gate": "ETH_USDT"},
    {"name": "SOL", "okx": "SOL-USDT-SWAP", "gate": "SOL_USDT"},
    {"name": "ZEC", "okx": "ZEC-USDT-SWAP", "gate": "ZEC_USDT"},
]

if "alerts" not in st.session_state:
    st.session_state.alerts = []
if "cooldown" not in st.session_state:
    st.session_state.cooldown = {}
if "positions" not in st.session_state:
    st.session_state.positions = []
if "pos_notices" not in st.session_state:
    st.session_state.pos_notices = {}


def now_bj():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%H:%M:%S")


def fetch_klines(coin, bar="15m", limit=80):
    """优先拉取 OKX 永续合约 K 线，失败自动回退 Gate 合约"""
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        r = requests.get(url, params={"instId": coin["okx"], "bar": bar, "limit": limit}, timeout=6).json()
        if r.get("code") == "0" and len(r.get("data", [])) >= 50:
            df = pd.DataFrame(r["data"]).iloc[:, :6]
            df.columns = ["ts", "open", "high", "low", "close", "vol"]
            for c in ["ts", "open", "high", "low", "close", "vol"]:
                df[c] = df[c].astype(float)
            return df.sort_values("ts").reset_index(drop=True), "OKX"
    except Exception:
        pass

    try:
        url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        r = requests.get(url, params={"contract": coin["gate"], "interval": bar, "limit": limit}, timeout=6).json()
        if isinstance(r, list) and len(r) >= 50:
            df = pd.DataFrame(r)
            df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "vol"})
            for c in ["ts", "open", "high", "low", "close", "v" if "v" in df.columns else "vol"]:
                pass
            for c in ["ts", "open", "high", "low", "close", "vol"]:
                df[c] = df[c].astype(float)
            return df.sort_values("ts").reset_index(drop=True), "Gate"
    except Exception:
        pass

    return None, "失败"


def add_indicators(df):
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    hl = df["high"] - df["low"]
    hc = np.abs(df["high"] - df["close"].shift())
    lc = np.abs(df["low"] - df["close"].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    return df


def push_wechat(text):
    if webhook_url.startswith("http"):
        try:
            requests.post(webhook_url, json={"msgtype": "text", "text": {"content": text}}, timeout=5)
        except Exception:
            pass


def add_tracked_position(coin_name, side, entry, sl, tp1, source_tag):
    # 避免同一品种同方向重复挂太多同源跟进单
    for p in st.session_state.positions:
        if p["coin"] == coin_name and p["side"] == side and p["active"]:
            return
    st.session_state.positions.insert(0, {
        "id": f"{coin_name}_{side}_{int(time.time())}",
        "coin": coin_name,
        "side": side,  # "LONG" or "SHORT"
        "entry": float(entry),
        "sl": float(sl),
        "tp1": float(tp1),
        "be_done": False,
        "tp1_done": False,
        "active": True,
        "tag": source_tag,
        "opened_at": now_bj(),
    })


def record_alert(coin_name, tag, side, trend_1h, title, detail, entry, sl, tp1):
    key = f"{coin_name}_{tag}"
    now_ts = time.time()
    if now_ts - st.session_state.cooldown.get(key, 0) < cooldown_min * 60:
        return
    st.session_state.cooldown[key] = now_ts

    conflict_text = ""
    if side == "LONG" and trend_1h == "空头":
        conflict_text = "⚠️ 级别冲突提示：当前 1H 为空头结构，属于逆大势/短线博弈，注意控仓与快进快出。"
    elif side == "SHORT" and trend_1h == "多头":
        conflict_text = "⚠️ 级别冲突提示：当前 1H 为多头结构，属于逆大势/短线博弈，注意控仓与快进快出。"

    full_detail = f"{detail}\n{conflict_text}".strip()
    t_str = now_bj()

    st.session_state.alerts.insert(0, {
        "time": t_str,
        "side": side,
        "title": title,
        "detail": full_detail,
        "has_conflict": bool(conflict_text),
    })

    if auto_track_signals:
        add_tracked_position(coin_name, side, entry, sl, tp1, title)

    push_wechat(f"{title}\n时间: {t_str}\n{full_detail}")


def evaluate_positions(latest_market):
    """根据最新行情评估跟进中的单子：推保本 / TP1减仓 / 结构走坏"""
    updates = []
    for pos in st.session_state.positions:
        if not pos["active"]:
            continue
        coin = pos["coin"]
        info = latest_market.get(coin)
        if not info:
            continue

        price = info["price"]
        ema21_15m = info["ema21_15m"]
        atr_15m = info["atr_15m"]
        entry = pos["entry"]
        risk = abs(entry - pos["sl"]) if abs(entry - pos["sl"]) > 0 else atr_15m

        if pos["side"] == "LONG":
            pnl_pts = price - entry
            hit_tp1 = price >= pos["tp1"]
            bad_struct = (price < ema21_15m) and (pnl_pts < 0)
        else:
            pnl_pts = entry - price
            hit_tp1 = price <= pos["tp1"]
            bad_struct = (price > ema21_15m) and (pnl_pts < 0)

        rr_now = round(pnl_pts / risk, 2) if risk > 0 else 0.0

        # 1. 推保本提醒（浮盈 >= 1R）
        if (not pos["be_done"]) and pnl_pts >= risk:
            pos["be_done"] = True
            pos["sl"] = entry
            msg = f"🛡️ [推保本提醒] {coin} {'做多' if pos['side']=='LONG' else '做空'} @{entry} 当前浮盈约 {rr_now}R，建议止损移至入场价 {entry}。"
            updates.append((coin, "BE", msg))

        # 2. TP1 减仓提醒
        if (not pos["tp1_done"]) and hit_tp1:
            pos["tp1_done"] = True
            msg = f"🎯 [TP1已到] {coin} {'做多' if pos['side']=='LONG' else '做空'} @{entry} 触及目标 {pos['tp1']}（现价 {price}），建议分批减仓/锁盈。"
            updates.append((coin, "TP1", msg))

        # 3. 结构走坏提醒
        if bad_struct:
            notice_key = f"{pos['id']}_BAD_STRUCT"
            if time.time() - st.session_state.pos_notices.get(notice_key, 0) > cooldown_min * 60:
                st.session_state.pos_notices[notice_key] = time.time()
                msg = f"⚠️ [结构走弱] {coin} {'做多' if pos['side']=='LONG' else '做空'} @{entry} 现价 {price} 已转弱于 15m EMA21({round(ema21_15m, 4)})，注意评估是否提前离场。"
                updates.append((coin, "WARN", msg))

    for _, _, m in updates:
        push_wechat(f"{m}\n时间: {now_bj()}")
    return updates


# ================= 主扫描流程 =================
latest_market = {}
cols = st.columns(4)

for idx, coin in enumerate(COINS):
    df15, src15 = fetch_klines(coin, "15m", 80)
    df1h, _ = fetch_klines(coin, "1h", 60)

    with cols[idx]:
        if df15 is None or df1h is None:
            st.error(f"**{coin['name']}** K线拉取失败")
            continue

        df15 = add_indicators(df15)
        df1h = add_indicators(df1h)

        c1h = df1h.iloc[-1]
        trend_1h = "多头" if c1h["close"] > c1h["ema50"] else "空头"

        curr = df15.iloc[-1]
        prev20 = df15.iloc[-21:-1]
        sweep30 = df15.iloc[-31:-1]

        price = float(curr["close"])
        atr = float(curr["atr"])
        ema21 = float(curr["ema21"])
        if np.isnan(atr) or atr <= 0:
            continue

        latest_market[coin["name"]] = {
            "price": price,
            "atr_15m": atr,
            "ema21_15m": ema21,
            "trend_1h": trend_1h,
        }

        st.metric(
            label=f"{coin['name']} ({src15})",
            value=f"{price}",
            delta=f"1H {trend_1h} | ATR {atr:.4f}",
            delta_color="normal" if trend_1h == "多头" else "inverse"
        )

        high20, low20 = float(prev20["high"].max()), float(prev20["low"].min())
        high30, low30 = float(sweep30["high"].max()), float(sweep30["low"].min())
        vol_ma20 = float(prev20["vol"].mean())

        # 分支A1：15m 放量突破做多（顺 1H 多头）
        if trend_1h == "多头" and price > high20 and float(curr["vol"]) > vol_ma20 * 1.1:
            sl = round(price - 1.5 * atr, 4)
            tp1 = round(price + 2.0 * atr, 4)
            tp2 = round(price + 3.0 * atr, 4)
            if price > sl:
                rr = round((tp1 - price) / (price - sl), 2)
                if rr >= min_rr:
                    record_alert(
                        coin["name"], "A_LONG_BREAK", "LONG", trend_1h,
                        f"[分支A·突破做多] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 1H多头，15m放量突破前20高点({round(high20, 4)})\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

        # 分支A2：15m 放量跌破做空（顺 1H 空头）
        elif trend_1h == "空头" and price < low20 and float(curr["vol"]) > vol_ma20 * 1.1:
            sl = round(price + 1.5 * atr, 4)
            tp1 = round(price - 2.0 * atr, 4)
            tp2 = round(price - 3.0 * atr, 4)
            if sl > price:
                rr = round((price - tp1) / (sl - price), 2)
                if rr >= min_rr:
                    record_alert(
                        coin["name"], "A_SHORT_BREAK", "SHORT", trend_1h,
                        f"[分支A·跌破做空] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 1H空头，15m放量跌破前20低点({round(low20, 4)})\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

        # 分支B1：假突破扫损反转做空（刺破前30高点后收回 + 长上影）
        upper_shadow = float(curr["high"]) - max(float(curr["open"]), price)
        if float(curr["high"]) > high30 and price < high30 and upper_shadow > 0.6 * atr:
            sl = round(float(curr["high"]) + 0.3 * atr, 4)
            tp1 = round(price - 1.8 * atr, 4)
            tp2 = round(price - 2.8 * atr, 4)
            if sl > price:
                rr = round((price - tp1) / (sl - price), 2)
                if rr >= min_rr:
                    record_alert(
                        coin["name"], "B_SHORT_SWEEP", "SHORT", trend_1h,
                        f"[分支B·扫损反转空] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 15m刺破前30高点({round(high30, 4)})至{round(float(curr['high']), 4)}后收回\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

        # 分支B2：假跌破扫损反转做多（刺破前30低点后收回 + 长下影）
        lower_shadow = min(float(curr["open"]), price) - float(curr["low"])
        if float(curr["low"]) < low30 and price > low30 and lower_shadow > 0.6 * atr:
            sl = round(float(curr["low"]) - 0.3 * atr, 4)
            tp1 = round(price + 1.8 * atr, 4)
            tp2 = round(price + 2.8 * atr, 4)
            if price > sl:
                rr = round((tp1 - price) / (price - sl), 2)
                if rr >= min_rr:
                    record_alert(
                        coin["name"], "B_LONG_SWEEP", "LONG", trend_1h,
                        f"[分支B·扫损反转多] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 15m刺破前30低点({round(low30, 4)})至{round(float(curr['low']), 4)}后收回\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

pos_updates = evaluate_positions(latest_market)

st.divider()

# ================= 持仓跟进区 =================
st.subheader("📌 短线持仓跟进（推保本 / TP1减仓 / 结构提示）")
for _, _, m in pos_updates:
    st.warning(m)

with st.expander("➕ 手动添加一笔跟进单"):
    c1, c2, c3, c4, c5 = st.columns(5)
    m_coin = c1.selectbox("品种", ["BTC", "ETH", "SOL", "ZEC"])
    m_side = c2.selectbox("方向", ["LONG", "SHORT"])
    m_entry = c3.number_input("入场价", min_value=0.0, value=0.0, format="%.4f")
    m_sl = c4.number_input("止损价", min_value=0.0, value=0.0, format="%.4f")
    m_tp1 = c5.number_input("TP1目标价", min_value=0.0, value=0.0, format="%.4f")
    if st.button("加入跟进"):
        if m_entry > 0 and m_sl > 0 and m_tp1 > 0:
            add_tracked_position(m_coin, m_side, m_entry, m_sl, m_tp1, "手动跟进")
            st.rerun()
        else:
            st.error("请填写大于 0 的入场价、止损价和 TP1 目标价")

active_pos = [p for p in st.session_state.positions if p["active"]]
if not active_pos:
    st.caption("当前没有跟进中的单子（开启“触发策略自动加入持仓跟进”后，出信号会自动进这里）。")
else:
    for p in active_pos:
        cur_p = latest_market.get(p["coin"], {}).get("price", p["entry"])
        pnl = (cur_p - p["entry"]) if p["side"] == "LONG" else (p["entry"] - cur_p)
        be_badge = "✅已推保本" if p["be_done"] else "⏳未推保本"
        tp_badge = "✅已过TP1" if p["tp1_done"] else "⏳待过TP1"
        col_a, col_b = st.columns([6, 1])
        col_a.info(
            f"**{p['coin']}** | {'做多' if p['side']=='LONG' else '做空'} | 开仓: `{p['entry']}` | 现价: `{cur_p}` | "
            f"浮动点数: `{round(pnl, 4)}` | 止损: `{p['sl']}` | TP1: `{p['tp1']}` | {be_badge} | {tp_badge}"
        )
        if col_b.button("结束", key=f"close_{p['id']}"):
            p["active"] = False
            st.rerun()

st.divider()

# ================= 策略预警记录区 =================
st.subheader("🚨 筛选后的短线策略预警")
if not st.session_state.alerts:
    st.info("暂无符合 RR 门槛且未在冷却期内的策略信号。")
else:
    for item in st.session_state.alerts[:30]:
        body_text = f"**{item['title']}** （{item['time']}）\n\n{item['detail']}"
        if item["has_conflict"]:
            st.warning(body_text)
        elif item["side"] == "LONG":
            st.success(body_text)
        else:
            st.error(body_text)

st.caption(f"最后扫描时间（北京时间）: {now_bj()}")

if auto_run:
    time.sleep(refresh_sec)
    st.rerun()
