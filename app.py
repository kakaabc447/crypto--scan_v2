import streamlit as st
import requests
import time
import math
from datetime import datetime

# -------------------------- 配置区 --------------------------
COINS = [
    {"name": "BTC", "weex": "cmt_btcusdt", "binance": "BTCUSDT"},
    {"name": "ETH", "weex": "cmt_ethusdt", "binance": "ETHUSDT"},
    {"name": "SOL", "weex": "cmt_solusdt", "binance": "SOLUSDT"},
    {"name": "ZEC", "weex": "cmt_zecusdt", "binance": "ZECUSDT"},
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
# 冷却字典
if "cooldown" not in st.session_state:
    st.session_state.cooldown = {}
if "alert_list" not in st.session_state:
    st.session_state.alert_list = []
if "running" not in st.session_state:
    st.session_state.running = False
if "log" not in st.session_state:
    st.session_state.log = []

def add_log(msg):
    t = datetime.now().strftime("%H:%M:%S")
    st.session_state.log.append(f"[{t}] {msg}")
    if len(st.session_state.log) > 200:
        st.session_state.log.pop(0)

def add_alert(title, detail, side):
    t = datetime.now().strftime("%H:%M:%S")
    st.session_state.alert_list.insert(0, {
        "time": t,
        "title": title,
        "detail": detail,
        "side": side
    })
    if len(st.session_state.alert_list) > 50:
        st.session_state.alert_list.pop()

def test_alert():
    add_alert("[测试预警] BTC 做多 @90000 (RR 2.0:1)",
              "分支: 本地界面测试\n说明: 预警卡片显示正常，后续行情触发会自动出现在这里。",
              "long")
    add_log("已生成一条本地测试预警")

def fetch_kline(coin, interval, limit=80):
    # 优先WEEX，失败切币安
    weex_url = f"https://api-contract.weex.com/capi/v2/market/candles?symbol={coin['weex']}&granularity={interval}&limit={limit}"
    bn_url = f"https://api.binance.com/api/v3/klines?symbol={coin['binance']}&interval={interval}&limit={limit}"
    try:
        resp = requests.get(weex_url, headers=HEADERS, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            raw = data if isinstance(data, list) else data.get("data", [])
            rows = []
            for r in raw:
                rows.append({
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "vol": float(r[5])
                })
            return rows, "WEEX"
    except Exception as e:
        add_log(f"{coin['name']} WEEX拉取失败: {str(e)}")

    try:
        resp = requests.get(bn_url, headers=HEADERS, timeout=6)
        if resp.status_code == 200:
            raw = resp.json()
            rows = []
            for r in raw:
                rows.append({
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "vol": float(r[5])
                })
            return rows, "Binance"
    except Exception as e:
        add_log(f"{coin['name']} Binance拉取失败: {str(e)}")
    raise Exception("WEEX、Binance接口全部请求失败")

def ema(series, period):
    if len(series) < period:
        return None
    k = 2 / (period + 1)
    val = series[0]
    for p in series[1:]:
        val = p * k + val * (1 - k)
    return val

def atr(klines, period=14):
    if len(klines) < period + 2:
        return None
    trs = []
    for i in range(1, len(klines)):
        h = klines[i]["high"]
        l = klines[i]["low"]
        pc = klines[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period

def can_trigger(coin_name, tag):
    key = f"{coin_name}_{tag}"
    now = time.time()
    last = st.session_state.cooldown.get(key, 0)
    if now - last > 30 * 60:
        st.session_state.cooldown[key] = now
        return True
    return False

def scan_coin(coin, min_rr):
    k15, src15 = fetch_kline(coin, "15m", 80)
    k1h, src1h = fetch_kline(coin, "1h", 60)
    close_1h = [x["close"] for x in k1h]
    ema50_1h = ema(close_1h, 50)
    trend = "多头" if k1h[-1]["close"] > ema50_1h else "空头"
    curr = k15[-1]
    price = curr["close"]
    atr_val = atr(k15, 14)
    if atr_val is None:
        return None
    prev20 = k15[-21:-1]
    sweep30 = k15[-31:-1]
    vol_list = [x["vol"] for x in prev20]
    vol_ma20 = sum(vol_list)/len(vol_list)
    high20 = max(x["high"] for x in prev20)
    low20 = min(x["low"] for x in prev20)
    high30 = max(x["high"] for x in sweep30)
    low30 = min(x["low"] for x in sweep30)

    signal = None
    # 顺势突破做多
    if trend == "多头" and price > high20 and curr["vol"] > vol_ma20 * 1.1:
        sl = round(price - 1.5 * atr_val, 4)
        tp = round(price + 2.5 * atr_val, 4)
        rr = round((tp - price)/(price - sl), 2)
        if rr >= min_rr and can_trigger(coin["name"], "LONG_BREAK"):
            add_alert(f"[突破预警] {coin['name']} 做多 @{price} (RR {rr}:1)",
                      f"分支: 15m顺势突破单\n方向: 1H多头，放量突破前20高点 ({high20})\n止损: {sl} | 止盈: {tp}",
                      "long")
    # 顺势跌破做空
    elif trend == "空头" and price < low20 and curr["vol"] > vol_ma20 * 1.1:
        sl = round(price + 1.5 * atr_val, 4)
        tp = round(price - 2.5 * atr_val, 4)
        rr = round((price - tp)/(sl - price), 2)
        if rr >= min_rr and can_trigger(coin["name"], "SHORT_BREAK"):
            add_alert(f"[跌破预警] {coin['name']} 做空 @{price} (RR {rr}:1)",
                      f"分支: 15m顺势跌破单\n方向: 1H空头，放量跌破前20低点 ({low20})\n止损: {sl} | 止盈: {tp}",
                      "short")
    # 假突破反转做空（上影刺破高点收回）
    upper_shadow = curr["high"] - max(curr["open"], curr["close"])
    if curr["high"] > high30 and price < high30 and upper_shadow > 0.6 * atr_val:
        sl = round(curr["high"] + 0.3 * atr_val, 4)
        tp = round(price - 2.2 * atr_val, 4)
        risk = sl - price
        if risk > 0:
            rr = round((price - tp)/risk, 2)
            if rr >= min_rr and can_trigger(coin["name"], "SHORT_SWEEP"):
                add_alert(f"[假突破反转] {coin['name']} 做空 @{price} (RR {rr}:1)",
                          f"分支: 扫损反转单\n形态: 刺破前30高点({high30})至{curr['high']}后收回\n止损: {sl} | 止盈: {tp}",
                          "short")
    # 假跌破反转做多（下影刺破低点收回）
    lower_shadow = min(curr["open"], curr["close"]) - curr["low"]
    if curr["low"] < low30 and price > low30 and lower_shadow > 0.6 * atr_val:
        sl = round(curr["low"] - 0.3 * atr_val, 4)
        tp = round(price + 2.2 * atr_val, 4)
        risk = price - sl
        if risk > 0:
            rr = round((tp - price)/risk, 2)
            if rr >= min_rr and can_trigger(coin["name"], "LONG_SWEEP"):
                add_alert(f"[假跌破反转] {coin['name']} 做多 @{price} (RR {rr}:1)",
                          f"分支: 扫损反转单\n形态: 刺破前30低点({low30})至{curr['low']}后收回\n止损: {sl} | 止盈: {tp}",
                          "long")
    return {
        "name": coin["name"],
        "price": price,
        "trend": trend,
        "atr": atr_val,
        "source": src15
    }

# -------------------------- UI页面 --------------------------
st.set_page_config(page_title="短线预警监控", layout="wide")
st.title("短线预警监控面板（BTC / ETH / SOL / ZEC）")

col1, col2, col3, col4, col5 = st.columns([3,1,1,1,1])
with col1:
    webhook = st.text_input("企业微信 Webhook（暂不填也可运行）", "")
with col2:
    interval_sec = st.number_input("间隔(秒)", min_value=5, max_value=300, value=15)
with col3:
    min_rr = st.number_input("最小RR", min_value=1.0, max_value=10.0, value=1.5, step=0.1)
with col4:
    start_btn = st.button("开始监控", type="primary")
with col5:
    stop_btn = st.button("停止监控", type="secondary")

test_btn = st.button("测试本地预警")
if test_btn:
    test_alert()

status_text = st.empty()
card_container = st.container()
alert_container = st.container()
log_container = st.container()

if start_btn:
    st.session_state.running = True
    add_log("✅ 开始监控")
if stop_btn:
    st.session_state.running = False
    add_log("🛑 停止监控")

# 循环主逻辑
if st.session_state.running:
    status_text.info(f"✅ 运行中，扫描间隔：{interval_sec}秒")
else:
    status_text.info("⏸️ 状态：已停止")

# 渲染预警记录
with alert_container:
    st.subheader("触发的预警记录")
    if len(st.session_state.alert_list) == 0:
        st.info("暂无触发预警")
    else:
        for a in st.session_state.alert_list:
            if a["side"] == "long":
                st.success(f"**{a['title']}**\n{a['detail']}")
            else:
                st.error(f"**{a['title']}**\n{a['detail']}")

# 渲染日志
with log_container:
    st.subheader("运行日志")
    log_str = "\n".join(st.session_state.log[-80:])
    st.code(log_str, language="text")

# 后台循环（streamlit 注意：每次点击按钮会刷新页面，所以用sleep循环适合短时间盯盘）
if st.session_state.running:
    coin_data_list = []
    for coin in COINS:
        try:
            res = scan_coin(coin, min_rr)
            if res:
                coin_data_list.append(res)
        except Exception as e:
            add_log(f"{coin['name']} 扫描异常: {str(e)}")
    # 渲染币种卡片
    with card_container:
        st.subheader("币种状态")
        cols = st.columns(4)
        for idx, item in enumerate(coin_data_list):
            c = cols[idx]
            trend_color = "🟢" if item["trend"] == "多头" else "🔴"
            c.metric(label=f"{item['name']} {trend_color}{item['trend']}", value=f"{item['price']:.2f}",
                    help=f"ATR:{item['atr']:.4f} | 数据源:{item['source']}")
    add_log("本轮扫描完成")
    time.sleep(interval_sec)
    st.rerun()
