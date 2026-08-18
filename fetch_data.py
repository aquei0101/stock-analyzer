#!/usr/bin/env python3
"""
保有銘柄の過去データをyfinanceで取得し、テクニカル指標を計算してJSONに保存する。
GitHub Actionsで1日数回実行し、生成されたmarket_data.jsonをアプリが読む。

- 取得: 日足の終値・出来高(約6ヶ月分)
- 計算: 短期/長期の移動平均線(SMA)、MACD、出来高
- 出力: market_data.json (アプリがfetchで読み込む)

注意: yfinanceは約20分遅延。リアルタイム値はアプリ側で手入力する設計。
"""
import json
import datetime
import sys
import os
import urllib.parse
import urllib.request

import yfinance as yf
import feedparser


# 監視する銘柄。日本株は "コード.T"。アプリのportfolioと対応させる。
# ここはリポジトリの watchlist.json から読む(なければデフォルト)。
def load_watchlist():
    try:
        with open("watchlist.json", encoding="utf-8") as f:
            return json.load(f)["tickers"]
    except Exception:
        # デフォルト(ユーザーが自分の保有に書き換える)
        return ["7203.T", "5803.T", "6758.T"]


# ------------------------------------------------------------------
# テクニカル指標の計算
# ------------------------------------------------------------------
def sma(values, period):
    """単純移動平均。各日について直近period日の平均を返す(足りない日はNone)。"""
    out = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            window = values[i + 1 - period:i + 1]
            out.append(sum(window) / period)
    return out


def ema(values, period):
    """指数移動平均。MACDの計算に使う。"""
    out = []
    k = 2 / (period + 1)
    prev = None
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        if prev is None:
            prev = v  # 最初はその値で初期化
            out.append(v)
        else:
            cur = v * k + prev * (1 - k)
            out.append(cur)
            prev = cur
    return out


def macd(values, fast=12, slow=26, signal=9):
    """MACD線 = EMA(fast) - EMA(slow)、シグナル線 = MACD線のEMA(signal)、
    ヒストグラム = MACD線 - シグナル線。3つのリストを返す。"""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)
    # シグナル線はMACD線(Noneでない部分)のEMA
    signal_line = ema([m if m is not None else 0 for m in macd_line], signal)
    hist = []
    for m, s in zip(macd_line, signal_line):
        if m is None or s is None:
            hist.append(None)
        else:
            hist.append(m - s)
    return macd_line, signal_line, hist


def stochastic(highs, lows, closes, k_period=14, d_period=3, smooth=3):
    """ストキャスティクス(%K, %D)を計算。
    Fast %K = (終値 - n日間安値) / (n日間高値 - n日間安値) * 100
    Slow %K = Fast %K の smooth日移動平均
    %D = Slow %K の d_period日移動平均
    一般的な「スロー・ストキャスティクス」を返す。"""
    n = len(closes)
    fast_k = [None] * n
    for i in range(n):
        if i + 1 < k_period:
            continue
        window_high = max(highs[i + 1 - k_period:i + 1])
        window_low = min(lows[i + 1 - k_period:i + 1])
        rng = window_high - window_low
        if rng == 0:
            fast_k[i] = 50.0  # レンジがゼロ(値動きなし)のときは中立50
        else:
            fast_k[i] = (closes[i] - window_low) / rng * 100
    # Slow %K = Fast %K の smooth日平均
    slow_k = sma([x if x is not None else 0 for x in fast_k], smooth)
    # None を維持(平滑化の元がNoneの区間)
    for i in range(n):
        if fast_k[i] is None:
            slow_k[i] = None
    # %D = Slow %K の d_period日平均
    pct_d = sma([x if x is not None else 0 for x in slow_k], d_period)
    for i in range(n):
        if slow_k[i] is None:
            pct_d[i] = None
    return slow_k, pct_d


def rsi(closes, period=14):
    """RSI(相対力指数)をWilderの平滑化で計算。0〜100。"""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    # 最初のperiod分の平均上昇/下落
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100 - (100 / (1 + rs))
    # 以降はWilder平滑化
    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff >= 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - (100 / (1 + rs))
    return out


def round_or_none(x, digits=2):
    import math
    if x is None:
        return None
    try:
        if math.isnan(x) or math.isinf(x):
            return None  # NaN/無限大はJSON非対応 → null にする
    except (TypeError, ValueError):
        return None
    return round(x, digits)


# ------------------------------------------------------------------
# 1銘柄分のデータを取得・整形
# ------------------------------------------------------------------
def fetch_one(ticker):
    t = yf.Ticker(ticker)
    hist = t.history(period="6mo")  # 約6ヶ月の日足
    if hist.empty or len(hist) < 2:
        print(f"  [warn] {ticker}: データ取得失敗またはデータ不足")
        return None

    dates = [d.strftime("%Y-%m-%d") for d in hist.index]
    closes = [float(c) for c in hist["Close"]]
    volumes = [int(v) for v in hist["Volume"]]
    highs = [float(h) for h in hist["High"]]
    lows = [float(l) for l in hist["Low"]]

    # 指標を計算
    sma_short = sma(closes, 25)   # 短期線(25日)
    sma_long = sma(closes, 75)    # 長期線(75日)
    macd_line, signal_line, hist_macd = macd(closes)
    stoch_k, stoch_d = stochastic(highs, lows, closes)  # ストキャスティクス(%K, %D)
    rsi_vals = rsi(closes)  # RSI(過熱判定用)

    # --- 前日終値と最新値を分けて取得 ---
    # prev_close: 本当の「前の営業日の終値」
    # latest_price: 今の時価(取引時間中なら形成中の値。約20分遅延)
    prev_close = None
    latest_price = None
    price_time = None
    try:
        fi = t.fast_info
        # fast_infoが持つ「前営業日終値」と「最新価格」
        prev_close = float(fi.get("previous_close")) if fi.get("previous_close") else None
        latest_price = float(fi.get("last_price")) if fi.get("last_price") else None
    except Exception as e:
        print(f"  [warn] {ticker} fast_info失敗: {e}")

    # フォールバック: fast_infoが取れないとき日足から推定
    # 日足の最終バーが「今日」なら、その1つ前が前日終値・最終バーが最新値
    if prev_close is None or latest_price is None:
        today = datetime.date.today().strftime("%Y-%m-%d")
        last_is_today = dates[-1] == today
        if last_is_today and len(closes) >= 2:
            if prev_close is None:
                prev_close = closes[-2]   # 前営業日の終値
            if latest_price is None:
                latest_price = closes[-1] # 今日の(形成中)値
        else:
            # 最終バーが前営業日 → それが前日終値。最新値も同じ(場が閉じている)
            if prev_close is None:
                prev_close = closes[-1]
            if latest_price is None:
                latest_price = closes[-1]

    # 最新値の時刻(このスクリプト実行時刻=データ取得時刻)
    jst = datetime.timezone(datetime.timedelta(hours=9))
    price_time = datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M")

    # 通貨判定
    currency = "JPY" if ticker.endswith(".T") else "USD"

    return {
        "ticker": ticker,
        "currency": currency,
        "prev_close": round_or_none(prev_close),
        "latest_price": round_or_none(latest_price),
        "price_time": price_time,
        "dates": dates,
        "closes": [round_or_none(c) for c in closes],
        "volumes": volumes,
        "sma_short": [round_or_none(x) for x in sma_short],
        "sma_long": [round_or_none(x) for x in sma_long],
        "macd": [round_or_none(x, 3) for x in macd_line],
        "macd_signal": [round_or_none(x, 3) for x in signal_line],
        "macd_hist": [round_or_none(x, 3) for x in hist_macd],
        "stoch_k": [round_or_none(x, 1) for x in stoch_k],
        "stoch_d": [round_or_none(x, 1) for x in stoch_d],
        "rsi": [round_or_none(x, 1) for x in rsi_vals],
    }


# ------------------------------------------------------------------
# 市場指数の取得(市場全体の地合いを見るため)
# ------------------------------------------------------------------
MARKET_INDICES = {
    "^N225": "日経平均",
    "^TPX": "TOPIX",
    "JPY=X": "ドル円",
    "^GSPC": "S&P500",
    "^SOX": "半導体指数(SOX)",
}


def fetch_market_indices():
    """主要指数の前日比を取得。市場全体が上げか下げかを判断する材料。
    米国指数は日本時間の日中だと当日分がNaNになることがあるため、
    NaNの行を除いて『有効な最新2つの終値』を使う。"""
    import math
    out = {}
    for symbol, name in MARKET_INDICES.items():
        try:
            hist = yf.Ticker(symbol).history(period="10d")
            if hist.empty:
                print(f"  [warn] 指数 {symbol}: データなし")
                continue
            # 終値のうちNaNでない有効な値だけを取り出す
            valid = [float(c) for c in hist["Close"] if not math.isnan(float(c))]
            if len(valid) < 2:
                print(f"  [warn] 指数 {symbol}: 有効な終値が不足({len(valid)}件)")
                continue
            last = valid[-1]
            prev = valid[-2]
            if prev == 0:
                continue
            chg = (last - prev) / prev * 100
            if math.isnan(chg) or math.isinf(chg):
                continue
            out[name] = {
                "value": round(last, 2),
                "change_pct": round(chg, 2),
            }
            print(f"  指数 {name}: {chg:+.2f}%")
        except Exception as e:
            print(f"  [warn] 指数 {symbol} 取得失敗: {e}")
    return out


# ------------------------------------------------------------------
# ニュースの取得(Googleニュース RSS)
# ------------------------------------------------------------------
def fetch_news_for(ticker, name, limit=5):
    """銘柄に関する最新ニュースの見出しを取得する。
    日本株は日本語、米国株は英語で検索。見出しに媒体名(Reuters/日経など)が含まれる。"""
    is_jp = ticker.endswith(".T")
    query = name if name else ticker
    if is_jp:
        query = f"{query} 株"
        params = "hl=ja&gl=JP&ceid=JP:ja"
    else:
        query = f"{query} stock"
        params = "hl=en-US&gl=US&ceid=US:en"
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&{params}"
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:limit]:
            # published日付があれば添える
            date = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import time
                date = time.strftime("%m/%d", entry.published_parsed)
            items.append({"title": entry.title, "date": date})
        return items
    except Exception as e:
        print(f"  [warn] {ticker} ニュース取得失敗: {e}")
        return []


# 主要銘柄コード→名前(ニュース検索の精度を上げる)
TICKER_NAMES = {
    "7203.T": "トヨタ自動車", "5803.T": "フジクラ", "6758.T": "ソニーグループ",
    "8306.T": "三菱UFJ", "9984.T": "ソフトバンクグループ", "6861.T": "キーエンス",
    "9433.T": "KDDI", "8035.T": "東京エレクトロン", "6098.T": "リクルート",
    "4063.T": "信越化学", "5016.T": "JX金属", "7974.T": "任天堂", "6501.T": "日立製作所",
    "NVDA": "NVIDIA", "AAPL": "Apple", "MSFT": "Microsoft", "TSLA": "Tesla",
}


def name_for(ticker):
    return TICKER_NAMES.get(ticker, ticker.replace(".T", ""))


# ------------------------------------------------------------------
# 米国経済指標(FRED)
# FRED = セントルイス連銀の公式経済データベース。無料・公式・正確。
# APIキーは環境変数 FRED_API_KEY から読む(GitHub Secretsで設定)。
# ------------------------------------------------------------------
# 監視する指標: (FREDシリーズID, 表示名, 単位, 発表サイクルの説明)
FRED_SERIES = [
    ("CPIAUCSL",   "CPI(消費者物価指数)",       "index", "毎月中旬"),
    ("CPILFESL",   "コアCPI(除く食品・エネルギー)", "index", "毎月中旬"),
    ("PPIACO",     "PPI(生産者物価指数)",       "index", "毎月中旬"),
    ("PCEPILFE",   "コアPCE(FRB重視の指標)",    "index", "毎月末〜翌月初"),
    ("UNRATE",     "失業率",                     "percent", "毎月第1金曜"),
    ("FEDFUNDS",   "FF金利(政策金利)",          "percent", "毎月"),
    ("DGS10",      "米10年国債利回り",           "percent", "毎営業日"),
]


def fetch_fred_series(series_id, api_key, n=13):
    """FREDから1つの指標の直近データを取得。前月比・前年比も計算。"""
    url = (f"https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={api_key}&file_type=json"
           f"&sort_order=desc&limit={n}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stock-monitor"})
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode())
        obs = data.get("observations", [])
        # 有効な値だけ(欠損は "." で来る)
        points = [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]
        if not points:
            return None
        latest_date, latest_val = points[0]
        result = {
            "date": latest_date,
            "value": round(latest_val, 2),
        }
        # 前月比(直近2点)
        if len(points) >= 2:
            prev_val = points[1][1]
            if prev_val:
                result["mom"] = round((latest_val - prev_val) / prev_val * 100, 2)
        # 前年比(12ヶ月前 = 13点目)
        if len(points) >= 13:
            yoy_val = points[12][1]
            if yoy_val:
                result["yoy"] = round((latest_val - yoy_val) / yoy_val * 100, 2)
        return result
    except Exception as e:
        print(f"  [warn] FRED {series_id} 取得失敗: {e}")
        return None


def estimate_next_release(latest_date_str, cycle_desc):
    """実績の最新日付から、次回発表時期をざっくり推定する。
    正確な発表日ではなく『おおよその目安』。"""
    try:
        d = datetime.datetime.strptime(latest_date_str, "%Y-%m-%d").date()
        # 月次指標は、データ基準月の翌月〜翌々月に発表されることが多い
        # 最新データの月+1ヶ月を「次回データ基準月」とみなし、その翌月中旬を目安に
        y, m = d.year, d.month
        # 2ヶ月後の中旬あたりを次回発表の目安とする
        m2 = m + 2
        y2 = y + (m2 - 1) // 12
        m2 = (m2 - 1) % 12 + 1
        return f"{y2}年{m2}月頃({cycle_desc})"
    except Exception:
        return f"次回未定({cycle_desc})"


def fetch_fred_indicators():
    """FREDの全指標を取得。APIキーが無ければスキップ。"""
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        print("  [info] FRED_API_KEY 未設定 → 経済指標はスキップ")
        return []
    out = []
    for series_id, name, unit, cycle in FRED_SERIES:
        data = fetch_fred_series(series_id, api_key)
        if data:
            data["name"] = name
            data["unit"] = unit
            data["cycle"] = cycle
            data["next_release"] = estimate_next_release(data["date"], cycle)
            out.append(data)
            print(f"  FRED {name}: {data['value']} ({data['date']})")
    return out


# ------------------------------------------------------------------
# 重要マーケットニュース(Googleニュース RSS)
# ------------------------------------------------------------------
MARKET_NEWS_QUERIES = [
    ("米国株 市場", "hl=ja&gl=JP&ceid=JP:ja"),
    ("FRB 金融政策", "hl=ja&gl=JP&ceid=JP:ja"),
    ("日経平均 相場", "hl=ja&gl=JP&ceid=JP:ja"),
    ("インフレ CPI", "hl=ja&gl=JP&ceid=JP:ja"),
]


def fetch_market_news(limit_per_query=4):
    """重要マーケットニュースの見出しを複数トピックから集める。"""
    seen = set()
    items = []
    for query, params in MARKET_NEWS_QUERIES:
        q = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={q}&{params}"
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:limit_per_query]:
                title = entry.title
                if title in seen:
                    continue
                seen.add(title)
                date = ""
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    import time
                    date = time.strftime("%m/%d", entry.published_parsed)
                items.append({"title": title, "date": date, "topic": query})
        except Exception as e:
            print(f"  [warn] マーケットニュース '{query}' 取得失敗: {e}")
    return items


# ------------------------------------------------------------------
# メイン
# ------------------------------------------------------------------
def main():
    tickers = load_watchlist()
    now = datetime.datetime.utcnow()
    # 日本時間に変換(UTC+9)
    jst = now + datetime.timedelta(hours=9)

    print(f"=== データ取得 {jst.strftime('%Y-%m-%d %H:%M')} JST ===")
    result = {
        "updated_at": jst.strftime("%Y-%m-%d %H:%M"),
        "updated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "yfinance日足データ(約20分遅延)。現在値はアプリで手入力。",
        "market": {},
        "economic": [],
        "market_news": [],
        "stocks": {},
    }

    # 市場指数(市場全体の地合い)
    print("市場指数を取得中...")
    result["market"] = fetch_market_indices()

    # 米国経済指標(FRED)
    print("経済指標(FRED)を取得中...")
    result["economic"] = fetch_fred_indicators()

    # 重要マーケットニュース
    print("マーケットニュースを取得中...")
    result["market_news"] = fetch_market_news()
    print(f"  マーケットニュース {len(result['market_news'])}件")

    # 各銘柄: 株価データ + ニュース
    for ticker in tickers:
        print(f"取得中: {ticker}")
        data = fetch_one(ticker)
        if data:
            name = name_for(ticker)
            print(f"  ニュース取得: {name}")
            data["news"] = fetch_news_for(ticker, name)
            result["stocks"][ticker] = data
            print(f"  OK ({len(data['dates'])}日分, 終値 {data['prev_close']}, ニュース{len(data['news'])}件)")

    # 最終安全網: データ全体を走査してNaN/無限大をnullに置換する。
    # (NaNはJSONとして無効で、アプリ側で読み込みエラーになるため)
    import math
    def sanitize(obj):
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [sanitize(v) for v in obj]
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
        return obj
    result = sanitize(result)

    with open("market_data.json", "w", encoding="utf-8") as f:
        # allow_nan=False: 万一NaNが残っていればエラーで気づけるようにする
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    print(f"完了: {len(result['stocks'])}銘柄 / 指数{len(result['market'])}件 / "
          f"経済指標{len(result['economic'])}件 / マーケットニュース{len(result['market_news'])}件 "
          f"を market_data.json に保存")


if __name__ == "__main__":
    main()
