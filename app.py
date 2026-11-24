import time
import requests
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime, timezone, timedelta

# 页面基本配置
st.set_page_config(page_title="短线预警监控面板", layout="wide")
st.title("⚡ 短线预警监控面板（BTC / ETH / SOL / ZEC）")

# 侧边栏参数设置
with st.sidebar:
    st.header("⚙️ 监控参数")
    webhook_url = st.text_input("企业微信 Webhook（可选）", placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...")
    min_rr = st.number_input("最小盈亏比 (RR)", min_value=1.0, max_value=10.0, value=1.5, step=0.1)
    refresh_sec = st.number_input("自动刷新间隔（秒）", min_value=10, max_value=300, value=20, step=5)
    auto_run = st.toggle("开启自动刷新监控", value=True)
    if st.button("🔔 发送测试预警"):
        st.toast("测试预警已生成！")
        if webhook_url.startswith("http"):
            try:
                requests.post(webhook_url, json={"msgtype": "text", "text": {"content": "[测试预警] Streamlit 短线监控推送正常"}}, timeout=5)
                st.success("企业微信推送成功")
            except Exception as e:
                st.error(f"推送失败: {e}")

# 监控标的（优先 OKX/Gate 公开接口，海外服务器极稳且支持 ZEC）
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

def now_bj():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%H:%M:%S")

def fetch_klines(coin, bar="15m", limit=80):
    """优先从 OKX 获取永续合约 K 线，失败则自动切 Gate"""
    # 1. 尝试 OKX
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        r = requests.get(url, params={"instId": coin["okx"], "bar": bar, "limit": limit}, timeout=6).json()
        if r.get("code") == "0" and len(r.get("data", [])) >= 50:
            rows = r["data"]
            df = pd.DataFrame(rows).iloc[:, :6]
            df.columns = ["ts", "open", "high", "low", "close", "vol"]
            for c in ["ts", "open", "high", "low", "close", "vol"]:
                df[c] = df[c].astype(float)
            df = df.sort_values("ts").reset_index(drop=True)
            return df, "OKX"
    except Exception:
        pass

    # 2. 备用 Gate 合约接口
    try:
        url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        r = requests.get(url, params={"contract": coin["gate"], "interval": bar, "limit": limit}, timeout=6).json()
        if isinstance(r, list) and len(r) >= 50:
            df = pd.DataFrame(r)
            df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "vol"})
            for c in ["ts", "open", "high", "low", "close", "vol"]:
                df[c] = df[c].astype(float)
            df = df.sort_values("ts").reset_index(drop=True)
            return df, "Gate"
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

def record_alert(coin_name, tag, side, title, detail):
    key = f"{coin_name}_{tag}"
    now_ts = time.time()
    if now_ts - st.session_state.cooldown.get(key, 0) < 30 * 60:
        return
    st.session_state.cooldown[key] = now_ts
    time_str = now_bj()
    st.session_state.alerts.insert(0, {
        "time": time_str,
        "side": side,
        "title": title,
        "detail": detail
    })
    if webhook_url.startswith("http"):
        try:
            text = f"{title}\n时间: {time_str}\n{detail}"
            requests.post(webhook_url, json={"msgtype": "text", "text": {"content": text}}, timeout=5)
        except Exception:
            pass

# 扫描并展示实时卡片
cols = st.columns(4)
for idx, coin in enumerate(COINS):
    df15, src15 = fetch_klines(coin, "15m", 80)
    df1h, _ = fetch_klines(coin, "1h", 60)

    with cols[idx]:
        if df15 is None or df1h is None:
            st.error(f"**{coin['name']}** 行情拉取失败")
            continue

        df15 = add_indicators(df15)
        df1h = add_indicators(df1h)

        c1h = df1h.iloc[-1]
        trend_1h = "多头" if c1h["close"] > c1h["ema50"] else "空头"

        curr = df15.iloc[-1]
        prev20 = df15.iloc[-21:-1]
        sweep30 = df15.iloc[-31:-1]

        price = curr["close"]
        atr = curr["atr"]
        if np.isnan(atr) or atr <= 0:
            continue

        # 卡片 UI
        st.metric(
            label=f"{coin['name']} ({src15})",
            value=f"{price}",
            delta=f"1H {trend_1h} | ATR {atr:.4f}",
            delta_color="normal" if trend_1h == "多头" else "inverse"
        )

        high20, low20 = prev20["high"].max(), prev20["low"].min()
        high30, low30 = sweep30["high"].max(), sweep30["low"].min()

        # 分支A：15m 顺势突破/跌破
        if trend_1h == "多头" and price > high20 and curr["vol"] > curr["vol_ma20"] * 1.1:
            sl = round(price - 1.5 * atr, 4)
            tp = round(price + 2.5 * atr, 4)
            rr = round((tp - price) / (price - sl), 2)
            if rr >= min_rr:
                record_alert(
                    coin["name"], "LONG_BREAK", "LONG",
                    f"[突破预警] {coin['name']} 做多 @{price} (RR {rr}:1)",
                    f"分支: 15m顺势突破单 | 1H多头，放量突破前20高点({high20})\n止损: {sl} | 止盈: {tp}"
                )
        elif trend_1h == "空头" and price < low20 and curr["vol"] > curr["vol_ma20"] * 1.1:
            sl = round(price + 1.5 * atr, 4)
            tp = round(price - 2.5 * atr, 4)
            rr = round((price - tp) / (sl - price), 2)
            if rr >= min_rr:
                record_alert(
                    coin["name"], "SHORT_BREAK", "SHORT",
                    f"[跌破预警] {coin['name']} 做空 @{price} (RR {rr}:1)",
                    f"分支: 15m顺势跌破单 | 1H空头，放量跌破前20低点({low20})\n止损: {sl} | 止盈: {tp}"
                )

        # 分支B：15m 假突破扫损反转
        upper_shadow = curr["high"] - max(curr["open"], curr["close"])
        if curr["high"] > high30 and price < high30 and upper_shadow > 0.6 * atr:
            sl = round(curr["high"] + 0.3 * atr, 4)
            tp = round(price - 2.2 * atr, 4)
            if sl > price:
                rr = round((price - tp) / (sl - price), 2)
                if rr >= min_rr:
                    record_alert(
                        coin["name"], "SHORT_SWEEP", "SHORT",
                        f"[假突破反转] {coin['name']} 做空 @{price} (RR {rr}:1)",
                        f"分支: 扫损反转单 | 刺破前30高点({high30})至{curr['high']}后收回\n止损: {sl} | 止盈: {tp}"
                    )

        lower_shadow = min(curr["open"], curr["close"]) - curr["low"]
        if curr["low"] < low30 and price > low30 and lower_shadow > 0.6 * atr:
            sl = round(curr["low"] - 0.3 * atr, 4)
            tp = round(price + 2.2 * atr, 4)
            if price > sl:
                rr = round((tp - price) / (price - sl), 2)
                if rr >= min_rr:
                    record_alert(
                        coin["name"], "LONG_SWEEP", "LONG",
                        f"[假跌破反转] {coin['name']} 做多 @{price} (RR {rr}:1)",
                        f"分支: 扫损反转单 | 刺破前30低点({low30})至{curr['low']}后收回\n止损: {sl} | 止盈: {tp}"
                    )

st.divider()
st.subheader("🚨 触发的预警记录")
if not st.session_state.alerts:
    st.info("暂无触发预警，正在持续扫描盘面...")
else:
    for item in st.session_state.alerts[:30]:
        if item["side"] == "LONG":
            st.success(f"**{item['title']}** （{item['time']}）\n\n{item['detail']}")
        else:
            st.error(f"**{item['title']}** （{item['time']}）\n\n{item['detail']}")

st.caption(f"最后扫描时间（北京时间）: {now_bj()}")

if auto_run:
    time.sleep(refresh_sec)
    st.rerun()
