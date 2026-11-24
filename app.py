import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timezone, timedelta

st.set_page_config(page_title="单币种短线策略面板", layout="wide")
st.title("⚡ 单币种短线策略与持仓跟进面板")

# 初始化会话状态
if "active_coin" not in st.session_state:
    st.session_state.active_coin = "BTC"
if "recent_coins" not in st.session_state:
    st.session_state.recent_coins = ["BTC", "ETH", "SOL", "ZEC"]
if "test_push_msg" not in st.session_state:
    st.session_state.test_push_msg = ""
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


def build_coin_ids(symbol_input):
    clean = symbol_input.strip().upper().replace("-USDT-SWAP", "").replace("_USDT", "").replace("USDT", "")
    if not clean:
        clean = "BTC"
    return {
        "name": clean,
        "okx": f"{clean}-USDT-SWAP",
        "gate": f"{clean}_USDT",
    }


def push_wechat(webhook, text):
    if not webhook or not webhook.startswith("http"):
        return False, "未填写有效 Webhook（仅本地记录）"
    try:
        resp = requests.post(
            webhook,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=5
        )
        if resp.status_code == 200:
            return True, f"推送成功 ({now_bj()})"
        return False, f"推送返回状态码 {resp.status_code} ({now_bj()})"
    except Exception as e:
        return False, f"推送异常: {e} ({now_bj()})"


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


def calc_trend_label(df):
    c = df.iloc[-1]
    return "多头" if float(c["close"]) > float(c["ema50"]) else "空头"


def add_tracked_position(coin_name, side, entry, sl, tp1, source_tag):
    for p in st.session_state.positions:
        if p["coin"] == coin_name and p["side"] == side and p["active"]:
            return
    st.session_state.positions.insert(0, {
        "id": f"{coin_name}_{side}_{int(time.time())}",
        "coin": coin_name,
        "side": side,
        "entry": float(entry),
        "sl": float(sl),
        "tp1": float(tp1),
        "be_done": False,
        "tp1_done": False,
        "active": True,
        "tag": source_tag,
        "opened_at": now_bj(),
    })


def record_alert(webhook, cooldown_min, auto_track, coin_name, tag, side, trend_1h, title, detail, entry, sl, tp1):
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
        "coin": coin_name,
        "side": side,
        "title": title,
        "detail": full_detail,
        "has_conflict": bool(conflict_text),
    })

    if auto_track:
        add_tracked_position(coin_name, side, entry, sl, tp1, title)

    push_wechat(webhook, f"{title}\n时间: {t_str}\n{full_detail}")


def evaluate_positions(webhook, cooldown_min, latest_market):
    updates = []
    for pos in st.session_state.positions:
        if not pos["active"]:
            continue
        info = latest_market.get(pos["coin"])
        if not info:
            continue

        coin = pos["coin"]
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

        if (not pos["be_done"]) and pnl_pts >= risk:
            pos["be_done"] = True
            pos["sl"] = entry
            msg = f"🛡️ [推保本提醒] {coin} {'做多' if pos['side']=='LONG' else '做空'} @{entry} 当前浮盈约 {rr_now}R，建议止损移至入场价 {entry}。"
            updates.append(msg)

        if (not pos["tp1_done"]) and hit_tp1:
            pos["tp1_done"] = True
            msg = f"🎯 [TP1已到] {coin} {'做多' if pos['side']=='LONG' else '做空'} @{entry} 触及目标 {pos['tp1']}（现价 {price}），建议分批减仓/锁盈。"
            updates.append(msg)

        if bad_struct:
            notice_key = f"{pos['id']}_BAD_STRUCT"
            if time.time() - st.session_state.pos_notices.get(notice_key, 0) > cooldown_min * 60:
                st.session_state.pos_notices[notice_key] = time.time()
                msg = f"⚠️ [结构走弱] {coin} {'做多' if pos['side']=='LONG' else '做空'} @{entry} 现价 {price} 已转弱于 15m EMA21({round(ema21_15m, 4)})，注意评估是否提前离场。"
                updates.append(msg)

    for m in updates:
        push_wechat(webhook, f"{m}\n时间: {now_bj()}")
    return updates


# ================= 左侧栏：币种搜索/切换 + 参数 =================
with st.sidebar:
    st.header("🔍 币种搜索与切换")
    search_val = st.text_input("输入币种代码（如 BTC / ETH / SOL / ZEC / SUI）", value=st.session_state.active_coin)
    if st.button("切换监控此币种", use_container_width=True):
        parsed = build_coin_ids(search_val)
        st.session_state.active_coin = parsed["name"]
        if parsed["name"] not in st.session_state.recent_coins:
            st.session_state.recent_coins.insert(0, parsed["name"])
        st.rerun()

    picked_quick = st.selectbox(
        "常用/最近搜索币种",
        options=st.session_state.recent_coins,
        index=st.session_state.recent_coins.index(st.session_state.active_coin)
        if st.session_state.active_coin in st.session_state.recent_coins else 0
    )
    if picked_quick != st.session_state.active_coin:
        st.session_state.active_coin = picked_quick
        st.rerun()

    st.divider()
    st.header("⚙️ 策略与推送设置")
    webhook_url = st.text_input(
        "企业微信 Webhook（可选）",
        placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
    )
    if st.button("发送测试推送", use_container_width=True):
        ok, info_msg = push_wechat(webhook_url, f"[测试推送] 当前监控币种: {st.session_state.active_coin}，面板连接正常。")
        st.session_state.test_push_msg = f"{'✅' if ok else 'ℹ️'} {info_msg}"

    if st.session_state.test_push_msg:
        st.caption(st.session_state.test_push_msg)

    min_rr = st.number_input("最小盈亏比 (RR)", min_value=1.0, max_value=10.0, value=1.5, step=0.1)
    cooldown_min = st.number_input("同源信号冷却（分钟）", min_value=5, max_value=240, value=30, step=5)
    auto_track_signals = st.toggle("触发策略自动加入持仓跟进", value=True)
    refresh_sec = st.number_input("自动刷新间隔（秒）", min_value=10, max_value=300, value=20, step=5)
    auto_run = st.toggle("开启自动刷新监控", value=True)

    if st.button("清空已触发策略记录", use_container_width=True):
        st.session_state.alerts = []
        st.rerun()


# ================= 主面板：仅展示当前选中的单个币种 =================
coin = build_coin_ids(st.session_state.active_coin)
df5, _ = fetch_klines(coin, "5m", 60)
df15, src15 = fetch_klines(coin, "15m", 80)
df1h, _ = fetch_klines(coin, "1h", 60)

latest_market = {}

if df5 is None or df15 is None or df1h is None:
    st.error(f"未能获取到 **{coin['name']}** 的完整 K 线数据（请检查币种拼写是否支持永续合约，或稍后重试）。")
else:
    df5 = add_indicators(df5)
    df15 = add_indicators(df15)
    df1h = add_indicators(df1h)

    trend_5m = calc_trend_label(df5)
    trend_15m = calc_trend_label(df15)
    trend_1h = calc_trend_label(df1h)

    curr = df15.iloc[-1]
    prev20 = df15.iloc[-21:-1]
    sweep30 = df15.iloc[-31:-1]

    price = float(curr["close"])
    atr = float(curr["atr"])
    ema21 = float(curr["ema21"])

    if not np.isnan(atr) and atr > 0:
        latest_market[coin["name"]] = {
            "price": price,
            "atr_15m": atr,
            "ema21_15m": ema21,
            "trend_1h": trend_1h,
        }

        m1, m2, m3, m4 = st.columns(4)
        m1.metric(f"{coin['name']} 现价 ({src15})", f"{price}", f"ATR(14): {atr:.4f}")
        m2.metric("5m 方向", trend_5m, delta="多头" if trend_5m == "多头" else "空头",
                  delta_color="normal" if trend_5m == "多头" else "inverse")
        m3.metric("15m 方向", trend_15m, delta="多头" if trend_15m == "多头" else "空头",
                  delta_color="normal" if trend_15m == "多头" else "inverse")
        m4.metric("1H 方向", trend_1h, delta="多头" if trend_1h == "多头" else "空头",
                  delta_color="normal" if trend_1h == "多头" else "inverse")

        high20, low20 = float(prev20["high"].max()), float(prev20["low"].min())
        high30, low30 = float(sweep30["high"].max()), float(sweep30["low"].min())
        vol_ma20 = float(prev20["vol"].mean())

        # 分支A1：15m 放量突破做多
        if trend_1h == "多头" and price > high20 and float(curr["vol"]) > vol_ma20 * 1.1:
            sl = round(price - 1.5 * atr, 4)
            tp1 = round(price + 2.0 * atr, 4)
            tp2 = round(price + 3.0 * atr, 4)
            if price > sl:
                rr = round((tp1 - price) / (price - sl), 2)
                if rr >= min_rr:
                    record_alert(
                        webhook_url, cooldown_min, auto_track_signals,
                        coin["name"], "A_LONG_BREAK", "LONG", trend_1h,
                        f"[分支A·突破做多] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 1H多头，15m放量突破前20高点({round(high20, 4)})\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

        # 分支A2：15m 放量跌破做空
        elif trend_1h == "空头" and price < low20 and float(curr["vol"]) > vol_ma20 * 1.1:
            sl = round(price + 1.5 * atr, 4)
            tp1 = round(price - 2.0 * atr, 4)
            tp2 = round(price - 3.0 * atr, 4)
            if sl > price:
                rr = round((price - tp1) / (sl - price), 2)
                if rr >= min_rr:
                    record_alert(
                        webhook_url, cooldown_min, auto_track_signals,
                        coin["name"], "A_SHORT_BREAK", "SHORT", trend_1h,
                        f"[分支A·跌破做空] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 1H空头，15m放量跌破前20低点({round(low20, 4)})\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

        # 分支B1：假突破扫损反转做空
        upper_shadow = float(curr["high"]) - max(float(curr["open"]), price)
        if float(curr["high"]) > high30 and price < high30 and upper_shadow > 0.6 * atr:
            sl = round(float(curr["high"]) + 0.3 * atr, 4)
            tp1 = round(price - 1.8 * atr, 4)
            tp2 = round(price - 2.8 * atr, 4)
            if sl > price:
                rr = round((price - tp1) / (sl - price), 2)
                if rr >= min_rr:
                    record_alert(
                        webhook_url, cooldown_min, auto_track_signals,
                        coin["name"], "B_SHORT_SWEEP", "SHORT", trend_1h,
                        f"[分支B·扫损反转空] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 15m刺破前30高点({round(high30, 4)})至{round(float(curr['high']), 4)}后收回\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

        # 分支B2：假跌破扫损反转做多
        lower_shadow = min(float(curr["open"]), price) - float(curr["low"])
        if float(curr["low"]) < low30 and price > low30 and lower_shadow > 0.6 * atr:
            sl = round(float(curr["low"]) - 0.3 * atr, 4)
            tp1 = round(price + 1.8 * atr, 4)
            tp2 = round(price + 2.8 * atr, 4)
            if price > sl:
                rr = round((tp1 - price) / (price - sl), 2)
                if rr >= min_rr:
                    record_alert(
                        webhook_url, cooldown_min, auto_track_signals,
                        coin["name"], "B_LONG_SWEEP", "LONG", trend_1h,
                        f"[分支B·扫损反转多] {coin['name']} @{price} (TP1 RR {rr}:1)",
                        f"形态: 15m刺破前30低点({round(low30, 4)})至{round(float(curr['low']), 4)}后收回\n止损: {sl} | TP1: {tp1} | TP2: {tp2}",
                        price, sl, tp1
                    )

pos_updates = evaluate_positions(webhook_url, cooldown_min, latest_market)

st.divider()
st.subheader(f"🚨 {coin['name']} 策略预警记录")
coin_alerts = [a for a in st.session_state.alerts if a["coin"] == coin["name"]]
if not coin_alerts:
    st.info(f"当前 {coin['name']} 暂无符合 RR 门槛且未在冷却期内的策略预警。")
else:
    for item in coin_alerts[:20]:
        body_text = f"**{item['title']}** （{item['time']}）\n\n{item['detail']}"
        if item["has_conflict"]:
            st.warning(body_text)
        elif item["side"] == "LONG":
            st.success(body_text)
        else:
            st.error(body_text)

st.divider()
st.subheader("📌 持仓跟进（推保本 / TP1减仓 / 结构提示）")
for m in pos_updates:
    st.warning(m)

with st.expander("➕ 手动添加一笔跟进单"):
    c1, c2, c3, c4, c5 = st.columns(5)
    m_coin = c1.text_input("品种", value=coin["name"])
    m_side = c2.selectbox("方向", ["LONG", "SHORT"])
    m_entry = c3.number_input("入场价", min_value=0.0, value=0.0, format="%.4f")
    m_sl = c4.number_input("止损价", min_value=0.0, value=0.0, format="%.4f")
    m_tp1 = c5.number_input("TP1目标价", min_value=0.0, value=0.0, format="%.4f")
    if st.button("加入跟进"):
        clean_m_coin = build_coin_ids(m_coin)["name"]
        if m_entry > 0 and m_sl > 0 and m_tp1 > 0:
            add_tracked_position(clean_m_coin, m_side, m_entry, m_sl, m_tp1, "手动跟进")
            st.rerun()
        else:
            st.error("请填写大于 0 的入场价、止损价和 TP1 目标价")

active_pos = [p for p in st.session_state.positions if p["active"]]
if not active_pos:
    st.caption("当前没有跟进中的单子。")
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

st.caption(f"最后扫描时间（北京时间）: {now_bj()}")

if auto_run:
    time.sleep(refresh_sec)
    st.rerun()
