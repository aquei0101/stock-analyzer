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
import urllib.parse

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


def round_or_none(x, digits=2):
    return round(x, digits) if x is not None else None


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

    # 指標を計算
    sma_short = sma(closes, 25)   # 短期線(25日)
    sma_long = sma(closes, 75)    # 長期線(75日)
    macd_line, signal_line, hist_macd = macd(closes)

    prev_close = closes[-1]  # 最新の終値(=前日終値として扱う。手入力の現在値と比較)

    # 通貨判定
    currency = "JPY" if ticker.endswith(".T") else "USD"

    return {
        "ticker": ticker,
        "currency": currency,
        "prev_close": round_or_none(prev_close),
        "dates": dates,
        "closes": [round_or_none(c) for c in closes],
        "volumes": volumes,
        "sma_short": [round_or_none(x) for x in sma_short],
        "sma_long": [round_or_none(x) for x in sma_long],
        "macd": [round_or_none(x, 3) for x in macd_line],
        "macd_signal": [round_or_none(x, 3) for x in signal_line],
        "macd_hist": [round_or_none(x, 3) for x in hist_macd],
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
    """主要指数の前日比を取得。市場全体が上げか下げかを判断する材料。"""
    out = {}
    for symbol, name in MARKET_INDICES.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d")
            if len(hist) < 2:
                continue
            last = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            chg = (last - prev) / prev * 100
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
        "stocks": {},
    }

    # 市場指数(市場全体の地合い)
    print("市場指数を取得中...")
    result["market"] = fetch_market_indices()

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

    with open("market_data.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    print(f"完了: {len(result['stocks'])}銘柄 + 指数{len(result['market'])}件を market_data.json に保存")


if __name__ == "__main__":
    main()
