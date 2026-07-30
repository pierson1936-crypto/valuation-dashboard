# -*- coding: utf-8 -*-
"""
个股 / ETF / 指数  估值·技术·基本面  一体化分析工具
数据源：东方财富（估值/基本面/代码解析）+ 腾讯证券（K线量能，前复权）—— 全部免费公开接口
用法：python app.py  → 浏览器自动打开，输入代码即可（支持 600519 / 000858 / 510300 / 159915 / 000300 等）
免责声明：本工具仅供学习研究，所有结论均由公开数据按固定规则计算，不构成任何投资建议。
"""
import json
import io
import gzip
import math
import os
import re
import ssl
import time
import socket
import threading
import http.client
import urllib.request
import urllib.parse
import urllib.error
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = 8688
YEARS = 5          # 分位/技术分析区间：近5年
KLINE_N = 1300     # 拉取的K线根数（约5.3年，之后按日期裁到5年）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122 Safari/537.36")
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def _referer(url):
    """按域名给合适的 Referer——跨域 Referer 会被某些接口拒绝（返回空）。"""
    if "eastmoney.com" in url:
        return "https://data.eastmoney.com/"
    if "gtimg" in url or "qq.com" in url:
        return "https://gu.qq.com/"
    if "sina" in url:
        return "https://finance.sina.com.cn/"
    return None


def http_text(url, timeout=25, retries=2):
    """健壮抓取：UA + 按域名Referer + gzip解压 + 空响应重试。失败抛异常。"""
    last = ""
    for _ in range(max(1, retries)):
        try:
            headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
            ref = _referer(url)
            if ref:
                headers["Referer"] = ref
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or raw[:2] == b"\x1f\x8b":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            text = None
            for cs in ("utf-8", "gbk"):
                try:
                    text = raw.decode(cs)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = raw.decode("utf-8", "ignore")
            if text.strip():
                return text
            last = "空响应"
        except Exception as e:
            last = str(e)
    raise ValueError(last or "无响应")


def fetch_json(url, timeout=25, retries=2):
    return json.loads(http_text(url, timeout=timeout, retries=retries))


# 瞬时连接错误（对端断开、连接重置、超时等），值得重试
_TRANSIENT = (http.client.RemoteDisconnected, http.client.IncompleteRead,
              ConnectionError, socket.timeout, TimeoutError)


def api_post(url, headers, body, timeout=120, retries=3):
    """给大模型接口用的 POST：HTTP 错误(401/400/402)立即抛出；瞬时网络错误自动重试。"""
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise                      # 鉴权/参数/余额类错误不重试，直接上报
        except urllib.error.URLError as e:
            last = e
            if not isinstance(getattr(e, "reason", None), _TRANSIENT):
                # 其它 URLError（如 DNS）也重试一次，但记录
                pass
            time.sleep(1.2 * (attempt + 1))
        except _TRANSIENT as e:
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise last or ConnectionError("连接失败")


# ----------------------------------------------------------------------------
# 数据抓取
# ----------------------------------------------------------------------------
def _guess(code):
    """suggest 接口失败时的兜底：按代码前缀猜市场/类型（覆盖绝大多数常见代码）。"""
    c = code
    _COMMON = {
        "600519": "贵州茅台", "000858": "五粮液", "300750": "宁德时代", "601318": "中国平安",
        "000651": "格力电器", "002415": "海康威视", "600036": "招商银行", "600276": "恒瑞医药",
        "601012": "隆基绿能", "600030": "中信证券", "300059": "东方财富", "601166": "兴业银行",
        "000333": "美的集团", "002594": "比亚迪", "601899": "紫金矿业", "600900": "长江电力",
        "600809": "山西汾酒", "000568": "泸州老窖", "603259": "药明康德", "601888": "中国中免",
        "002475": "立讯精密", "300274": "阳光电源", "600585": "海螺水泥", "601919": "中远海控",
        "510300": "沪深300ETF", "510050": "上证50ETF", "159915": "创业板ETF", "512880": "证券ETF",
        "159919": "沪深300ETF深", "512690": "白酒ETF", "512480": "半导体ETF", "512010": "医药ETF",
        "000300": "沪深300", "000001": "上证指数", "399001": "深证成指", "399006": "创业板指",
        "000905": "中证500", "000688": "科创50",
        "512000": "券商ETF", "515030": "新能源车ETF", "512800": "银行ETF", "512660": "军工ETF",
        "159928": "消费ETF", "515790": "光伏ETF", "515000": "科技ETF", "512200": "地产ETF",
        "515220": "煤炭ETF", "512170": "医疗ETF", "159865": "养殖ETF", "512760": "芯片ETF",
        "516160": "新能源ETF",
    }
    _INDEX = {
        "000001": ("上证指数", "sh"), "000300": ("沪深300", "sh"),
        "000905": ("中证500", "sh"), "000688": ("科创50", "sh"),
        "399001": ("深证成指", "sz"), "399006": ("创业板指", "sz"),
    }
    if c in _INDEX:
        name, p = _INDEX[c]
        stock, cls = False, "Index"
    elif c[0] == "6":
        name = _COMMON.get(c, "")
        p, stock, cls = "sh", True, "AStock"
    elif c[0] == "5":
        name = _COMMON.get(c, "")
        p, stock, cls = "sh", False, "Fund"     # 沪市ETF/LOF
    elif c[0] in "89":
        name = _COMMON.get(c, "")
        p, stock, cls = "bj", True, "AStock"     # 北交所
    elif c[0] == "1":
        name = _COMMON.get(c, "")
        p, stock, cls = "sz", False, "Fund"      # 深市ETF/LOF
    elif c[0] in "03":
        name = _COMMON.get(c, "")
        p, stock, cls = "sz", True, "AStock"     # 深市股票(000/002/003/300)
    else:
        name = _COMMON.get(c, "")
        p, stock, cls = "sh", False, "Index"
    return {
        "code": c, "name": name, "secid": "", "prefix": p, "classify": cls,
        "type_name": {"AStock": "股票", "Fund": "基金", "Index": "指数"}.get(cls, ""),
        "is_stock": stock,
        "secucode": "%s.%s" % (c, "SH" if p == "sh" else "SZ"),
    }

def resolve(code):
    """代码 -> 市场/名称/类型。suggest 优先，失败用前缀兜底（永不返回 None）。"""
    try:
        url = "https://searchapi.eastmoney.com/api/suggest/get?input=%s&type=14&count=6" % code
        data = fetch_json(url)
        rows = (data.get("QuotationCodeTable") or {}).get("Data") or []
        hit = next((r for r in rows if r.get("Code") == code), rows[0] if rows else None)
        if hit:
            mkt = hit.get("MktNum")            # "1"=沪 "0"=深 "2"=北?
            prefix = {"1": "sh", "0": "sz"}.get(mkt)
            if prefix:
                classify = hit.get("Classify", "")
                return {
                    "code": hit.get("Code", code), "name": hit.get("Name", ""),
                    "secid": hit.get("QuoteID", ""), "prefix": prefix, "classify": classify,
                    "type_name": hit.get("SecurityTypeName", ""),
                    "is_stock": classify == "AStock",
                    "secucode": "%s.%s" % (code, "SH" if mkt == "1" else "SZ"),
                }
    except Exception:
        pass
    return _guess(code)


def _parse_qt(q):
    """从腾讯 qt 数组解析实时价与涨跌幅。返回 {price,chg,time} 或 None。"""
    try:
        price = float(q[3])
        ti, idx = "", -1
        for i, v in enumerate(q):
            if isinstance(v, str) and len(v) == 14 and v.isdigit():
                ti, idx = v, i
                break
        chg = None
        if idx > 0 and idx + 2 < len(q):
            try:
                chg = float(q[idx + 2])
            except ValueError:
                chg = None
        return {"price": price, "chg": chg, "time": ti}
    except (IndexError, ValueError, TypeError):
        return None


def _parse_tencent(data, sym):
    node = ((data.get("data") or {}).get(sym)) or {}
    arr = node.get("qfqday") or node.get("day") or []
    name, live = "", None
    qt = node.get("qt") or {}
    if isinstance(qt, dict) and isinstance(qt.get(sym), list):
        q = qt[sym]
        if len(q) > 1:
            name = q[1]
        live = _parse_qt(q)
    out = []
    for it in arr:
        try:
            out.append({"date": it[0], "open": float(it[1]), "close": float(it[2]),
                        "high": float(it[3]), "low": float(it[4]), "vol": float(it[5])})
        except (IndexError, ValueError):
            continue
    return out, name, live


def _fetch_sina(prefix, code, n=1100):
    """新浪日K兜底：返回 [ {date,open,close,low,high,vol} ]。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s%s&scale=240&datalen=%d" % (prefix, code, n))
    arr = fetch_json(url)
    out = []
    for it in arr:
        try:
            out.append({"date": it["day"], "open": float(it["open"]), "close": float(it["close"]),
                        "high": float(it["high"]), "low": float(it["low"]), "vol": float(it["volume"])})
        except (KeyError, ValueError):
            continue
    return out


def fetch_kline(prefix, code, n=KLINE_N):
    """前复权日K，腾讯优先、新浪兜底。返回 (按日期升序的行, 名称, 实时报价)。"""
    if not prefix:
        return [], "", None
    out, name, live = [], "", None
    sym = prefix + code
    try:
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (sym, n)
        out, name, live = _parse_tencent(fetch_json(url), sym)
    except Exception:
        out = []
    if len(out) < 30:                      # 腾讯失败或数据太少 -> 新浪
        try:
            out = _fetch_sina(prefix, code)
        except Exception:
            pass
    cutoff = (date.today() - timedelta(days=365 * YEARS + 5)).strftime("%Y-%m-%d")
    out = [r for r in out if r["date"] >= cutoff]
    out.sort(key=lambda r: r["date"])
    return out, name, live


def fetch_quote(prefix, code, name=""):
    """轻量实时报价（大盘/板块/自选用）：返回价格、涨幅和涨跌额。"""
    try:
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s%s,day,,,3,qfq" % (prefix, code)
        # 首页行情优先保证响应速度：失败时直接降级为暂无数据，不拖慢首屏。
        out, nm, live = _parse_tencent(fetch_json(url, timeout=5, retries=1), prefix + code)
        name = name or nm
        if live and live.get("price") is not None:
            chg = live.get("chg")
            prev = out[-2]["close"] if len(out) >= 2 and out[-2]["close"] else None
            if chg is None and len(out) >= 2 and out[-2]["close"]:
                chg = round((live["price"] / prev - 1) * 100, 2)
            change = live["price"] - prev if prev else None
            return {"code": code, "name": name, "chg": chg, "price": round(live["price"], 3),
                    "change": _r(change, 3)}
        if len(out) >= 2 and out[-2]["close"]:
            chg = round((out[-1]["close"] / out[-2]["close"] - 1) * 100, 2)
            return {"code": code, "name": name, "chg": chg, "price": round(out[-1]["close"], 3),
                    "change": round(out[-1]["close"] - out[-2]["close"], 3)}
    except Exception:
        pass
    return {"code": code, "name": name, "chg": None, "price": None, "change": None}


def fetch_watch_quotes(codes):
    """批量刷新自选行情；只走轻量行情接口，避免列表逐行串行等待。"""
    clean = [str(c).strip() for c in codes if re.fullmatch(r"\d{6}", str(c).strip())][:30]
    if not clean:
        return []

    def one(code):
        meta = _guess(code)
        quote = fetch_quote(meta["prefix"], code)
        if not quote.get("name"):
            try:
                url = "https://searchapi.eastmoney.com/api/suggest/get?input=%s&type=14&count=8" % code
                rows = ((fetch_json(url, timeout=4, retries=1).get("QuotationCodeTable") or {}).get("Data") or [])
                hit = next((r for r in rows if r.get("Code") == code), None)
                quote["name"] = (hit or {}).get("Name", "")
            except Exception:
                pass
        quote["name"] = quote.get("name") or meta.get("name", "")
        return quote

    with ThreadPoolExecutor(max_workers=min(12, len(clean))) as ex:
        return list(ex.map(one, clean))


# 大盘指数 与 板块ETF（前缀写死，避免代码前缀歧义）
MARKET_INDICES = [
    ("sh", "000001", "上证指数"), ("sz", "399001", "深证成指"), ("sz", "399006", "创业板指"),
    ("sh", "000300", "沪深300"), ("sh", "000905", "中证500"), ("sh", "000688", "科创50"),
]
MARKET_SECTORS = [
    ("sh", "512690", "白酒"), ("sh", "512480", "半导体"), ("sh", "512010", "医药"),
    ("sh", "512000", "券商"), ("sh", "515030", "新能源车"), ("sh", "512800", "银行"),
    ("sh", "512660", "军工"), ("sz", "159928", "消费"), ("sh", "515790", "光伏"),
    ("sh", "515000", "科技"), ("sh", "512200", "地产"), ("sh", "515220", "煤炭"),
    ("sh", "512170", "医疗"), ("sz", "159865", "养殖"), ("sh", "512760", "芯片"),
    ("sh", "516160", "新能源"),
]
_MKT = [0.0, None]
MKT_TTL = 120
_mkt_lock = threading.Lock()


def market_overview():
    """大盘指数 + 板块今日涨跌（用于首页与板块轮动）。带2分钟缓存。"""
    now = time.time()
    if _MKT[1] and now - _MKT[0] < MKT_TTL:
        return _MKT[1]
    # 预热线程和页面请求可能同时进来；只让第一个请求真正发起 22 个外部调用。
    with _mkt_lock:
        now = time.time()
        if _MKT[1] and now - _MKT[0] < MKT_TTL:
            return _MKT[1]
        items = MARKET_INDICES + MARKET_SECTORS
        # 22 个独立快请求，12 个 worker 可将最慢加载时间压缩到约两批请求。
        with ThreadPoolExecutor(max_workers=min(12, len(items))) as ex:
            quotes = list(ex.map(lambda t: fetch_quote(*t), items))
        n = len(MARKET_INDICES)
        indices = quotes[:n]
        sectors = [s for s in quotes[n:] if s["chg"] is not None]
        sectors.sort(key=lambda s: s["chg"], reverse=True)
        data = {"indices": indices, "sectors": sectors,
                "time": time.strftime("%Y-%m-%d %H:%M")}
        _MKT[0], _MKT[1] = time.time(), data
        return data


def generate_market_ai_report(api_key, market_data=None):
    """用 DeepSeek 对大盘/板块数据生成一句简洁的 AI 解析报告。"""
    market_data = market_data or market_overview()
    prompt = (
        "你是一名中国A股市场分析师。请根据下面的大盘和板块数据，输出一段简洁、实用的中文市场分析报告。"
        "要求：1. 先给结论；2. 结合指数涨跌、板块轮动和资金情绪做判断；3. 适当指出风险点；"
        "4. 结尾给出一句简短的操作建议；5. 不要编造信息。\n\n数据如下：\n"
        + json.dumps(market_data, ensure_ascii=False, indent=2)
    )
    body = {
        "model": AGENT_MODEL,
        "messages": [
            {"role": "system", "content": "你是专业的A股市场分析师，擅长用公开市场数据做简洁判断。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1000,
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    resp = api_post(AGENT_BASE + "/chat/completions", headers, body, timeout=120, retries=3)
    if "choices" not in resp:
        raise ValueError(resp.get("error", {}).get("message") or "模型返回异常")
    return {"report": resp["choices"][0]["message"]["content"].strip(), "time": market_data.get("time", "")}


def fetch_valuation(code):
    """个股每日 PE-TTM / PB（东财），按日期升序。"""
    cutoff = (date.today() - timedelta(days=365 * YEARS + 5)).strftime("%Y-%m-%d")
    base = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    rows, page = [], 1
    while True:
        params = {
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": "TRADE_DATE,CLOSE_PRICE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP,SECURITY_NAME_ABBR,BOARD_CODE,BOARD_NAME",
            "filter": '(SECURITY_CODE="%s")' % code,
            "pageNumber": page, "pageSize": 500,
            "sortTypes": -1, "sortColumns": "TRADE_DATE",
            "source": "WEB", "client": "WEB",
        }
        data = fetch_json(base + "?" + urllib.parse.urlencode(params))
        res = data.get("result") or {}
        batch = res.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        if batch[-1]["TRADE_DATE"][:10] < cutoff or page >= res.get("pages", 1):
            break
        page += 1
    rows = [r for r in rows if r["TRADE_DATE"][:10] >= cutoff]
    rows.sort(key=lambda r: r["TRADE_DATE"])
    return [{
        "date": r["TRADE_DATE"][:10],
        "close": r.get("CLOSE_PRICE"),
        "pe": r.get("PE_TTM"),
        "pb": r.get("PB_MRQ"),
        "cap": r.get("TOTAL_MARKET_CAP"),
        "name": r.get("SECURITY_NAME_ABBR"),
        "board_code": r.get("BOARD_CODE"),
        "board_name": r.get("BOARD_NAME"),
    } for r in rows]


def median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def fetch_industry(board_code, board_name, trade_date, target_code):
    """同行业个股 PE/PB（当日），算行业中位数并给出目标股相对位置。"""
    if not board_code:
        return None
    base = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    params = {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP",
        "filter": '(BOARD_CODE="%s")(TRADE_DATE=\'%s\')' % (board_code, trade_date),
        "pageNumber": 1, "pageSize": 300,
        "sortTypes": 1, "sortColumns": "PE_TTM",
        "source": "WEB", "client": "WEB",
    }
    try:
        data = fetch_json(base + "?" + urllib.parse.urlencode(params))
    except Exception:
        return None
    rows = (data.get("result") or {}).get("data") or []
    if len(rows) < 2:
        return None
    peers = [{"code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"),
              "pe": r.get("PE_TTM"), "pb": r.get("PB_MRQ"),
              "cap": r.get("TOTAL_MARKET_CAP")} for r in rows]
    pos_pe = [p["pe"] for p in peers if p["pe"] is not None and p["pe"] > 0]
    pos_pb = [p["pb"] for p in peers if p["pb"] is not None and p["pb"] > 0]
    tgt = next((p for p in peers if p["code"] == target_code), None)
    med_pe, med_pb = median(pos_pe), median(pos_pb)
    cheaper = None
    if tgt and tgt["pe"] is not None and tgt["pe"] > 0 and pos_pe:
        cheaper = sum(1 for x in pos_pe if x < tgt["pe"])
    # 取最便宜的若干 + 最贵的一只 作为展示样本
    sample = [p for p in peers if p["pe"] is not None and p["pe"] > 0][:6]
    return {
        "name": board_name, "count": len(peers),
        "median_pe": _r(med_pe), "median_pb": _r(med_pb),
        "target_pe": _r(tgt["pe"]) if tgt else None,
        "target_pb": _r(tgt["pb"]) if tgt else None,
        "cheaper_than_target": cheaper,          # 行业内比目标更便宜(PE更低)的家数
        "pos_pe_count": len(pos_pe),
        "peers": [{"name": p["name"], "pe": _r(p["pe"]), "pb": _r(p["pb"])} for p in sample],
    }


def fetch_fundamentals(secucode):
    """个股主要财务指标，最近若干报告期。"""
    base = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    params = {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": "REPORT_DATE,EPSJB,ROEJQ,XSMLL,ZCFZL,YYZSRGDHBZC",
        "filter": '(SECUCODE="%s")' % secucode,
        "pageSize": 6, "sortTypes": -1, "sortColumns": "REPORT_DATE",
        "source": "WEB", "client": "WEB",
    }
    try:
        data = fetch_json(base + "?" + urllib.parse.urlencode(params))
    except Exception:
        return []
    rows = (data.get("result") or {}).get("data") or []
    return [{
        "date": r["REPORT_DATE"][:10],
        "eps": r.get("EPSJB"),
        "roe": r.get("ROEJQ"),
        "gross": r.get("XSMLL"),
        "debt": r.get("ZCFZL"),
        "rev_yoy": r.get("YYZSRGDHBZC"),
    } for r in rows]


def fetch_moneyflow(secid, days=5):
    """近N日资金流向（东财 push2his）。返回按日期升序 [{date,main,super,large,mid,small}]（单位:亿元）。
    接口临时不可用时返回 []（不影响其余分析）。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           "?lmt=0&klt=101&secid=%s&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55" % secid)
    try:
        data = fetch_json(url)
    except Exception:
        return []
    klines = ((data.get("data") or {}).get("klines")) or []
    out = []
    for k in klines[-days:]:
        p = k.split(",")
        try:
            out.append({"date": p[0],
                        "main": float(p[1]) / 1e8,    # 主力净额(超大+大)
                        "small": float(p[2]) / 1e8,   # 小单(散户)
                        "mid": float(p[3]) / 1e8,      # 中单
                        "large": float(p[4]) / 1e8,    # 大单
                        "super": float(p[5]) / 1e8})   # 超大单
        except (IndexError, ValueError):
            continue
    return out


# ----------------------------------------------------------------------------
# 指标计算（纯 Python，无需 numpy）
# ----------------------------------------------------------------------------
def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out = [None] * len(vals)
    k = 2.0 / (n + 1)
    e = None
    for i, v in enumerate(vals):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [(a - b) if (a is not None and b is not None) else None for a, b in zip(e12, e26)]
    difc = [d if d is not None else 0.0 for d in dif]
    dea = ema(difc, 9)
    hist = [2 * (d - s) if (d is not None and s is not None) else None for d, s in zip(dif, dea)]
    return dif, dea, hist


def boll(closes, n=20, k=2):
    mid = sma(closes, n)
    up = [None] * len(closes)
    low = [None] * len(closes)
    for i in range(len(closes)):
        if i >= n - 1:
            window = closes[i - n + 1:i + 1]
            m = mid[i]
            var = sum((x - m) ** 2 for x in window) / n
            sd = math.sqrt(var)
            up[i] = m + k * sd
            low[i] = m - k * sd
    return up, mid, low


def percentile_rank(values, cur):
    vals = [v for v in values if v is not None]
    if not vals or cur is None:
        return None
    less = sum(1 for v in vals if v < cur)
    eq = sum(1 for v in vals if v == cur)
    return round((less + 0.5 * eq) / len(vals) * 100, 1)


def stat_block(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {}
    n = len(vals)
    mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"min": round(vals[0], 2), "max": round(vals[-1], 2),
            "median": round(mid, 2), "mean": round(sum(vals) / n, 2)}


def max_drawdown(closes):
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = max(mdd, (peak - c) / peak)
    return round(mdd * 100, 1)


def annual_vol(closes):
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return round(sd * math.sqrt(244) * 100, 1)


# ----------------------------------------------------------------------------
# 综合分析
# ----------------------------------------------------------------------------
def analyze(code):
    code = code.strip()
    meta = resolve(code)
    if not meta or not meta["prefix"]:
        return {"error": "未找到代码 %s。请确认是6位A股/ETF/指数代码（如 600519、510300、000300）。" % code}

    # —— 并行抓取（K线/资金流/估值/财务同时发起，大幅缩短总耗时）——
    secid = meta.get("secid") or (("1." if meta["prefix"] == "sh" else "0.") + code)
    is_stock = meta["is_stock"]
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_kline = ex.submit(fetch_kline, meta["prefix"], code)
        f_mf = ex.submit(fetch_moneyflow, secid)
        f_val = ex.submit(fetch_valuation, code) if is_stock else None
        f_fund = ex.submit(fetch_fundamentals, meta["secucode"]) if is_stock else None
        kl, kl_name, kl_live = f_kline.result()
        mf = f_mf.result()
        val = f_val.result() if f_val else []
        fund = f_fund.result() if f_fund else []

    if not meta["name"] and kl_name:      # suggest 兜底时用K线里的名称补全
        meta["name"] = kl_name
    meta["name"] = meta["name"] or code
    if len(kl) < 30:
        return {"error": "未取到 %s 的行情数据。请检查网络，或确认代码正确（如 600519、510300、000300）。" % code}

    dates = [r["date"] for r in kl]
    o = [r["open"] for r in kl]
    c = [r["close"] for r in kl]
    h = [r["high"] for r in kl]
    low = [r["low"] for r in kl]
    vol = [r["vol"] for r in kl]

    ma5, ma10, ma20, ma60 = sma(c, 5), sma(c, 10), sma(c, 20), sma(c, 60)
    vma5 = sma(vol, 5)
    bu, bm, bl = boll(c)
    rsi14 = rsi(c)
    dif, dea, hist = macd(c)

    price = c[-1]
    prev = c[-2] if len(c) > 1 else price
    chg = round((price / prev - 1) * 100, 2) if prev else 0

    # 买卖信号：MA5 上穿/下穿 MA20（金叉/死叉）
    buys, sells = [], []
    for i in range(1, len(c)):
        if ma5[i] is None or ma20[i] is None or ma5[i - 1] is None or ma20[i - 1] is None:
            continue
        if ma5[i - 1] <= ma20[i - 1] and ma5[i] > ma20[i]:
            buys.append([dates[i], round(low[i], 3)])
        elif ma5[i - 1] >= ma20[i - 1] and ma5[i] < ma20[i]:
            sells.append([dates[i], round(h[i], 3)])

    price_pct = percentile_rank(c, price)
    hi, lo = max(c), min(c)

    res = {
        "code": code, "name": meta["name"], "type_name": meta["type_name"],
        "is_stock": meta["is_stock"], "classify": meta["classify"],
        "date": dates[-1], "price": round(price, 3), "chg": chg,
        "start": dates[0], "count": len(dates), "years": YEARS,
        "price_pct": price_pct,
        "price_hi": round(hi, 3), "price_lo": round(lo, 3),
        "from_hi": round((price / hi - 1) * 100, 1),
        "from_lo": round((price / lo - 1) * 100, 1),
        "tech": {
            "ma5": _r(ma5[-1]), "ma20": _r(ma20[-1]), "ma60": _r(ma60[-1]),
            "rsi": _r(rsi14[-1], 1),
            "macd_dif": _r(dif[-1], 3), "macd_dea": _r(dea[-1], 3), "macd_hist": _r(hist[-1], 3),
            "boll_up": _r(bu[-1]), "boll_mid": _r(bm[-1]), "boll_low": _r(bl[-1]),
            "vol_ratio": _r(vol[-1] / vma5[-1], 2) if vma5[-1] else None,
            "vol": vol[-1],
            "mdd": max_drawdown(c), "vola": annual_vol(c),
        },
        "chart": {
            "dates": dates,
            "candle": [[_r(o[i]), _r(c[i]), _r(low[i]), _r(h[i])] for i in range(len(c))],
            "vol": [_r(v, 0) for v in vol],
            "vup": [1 if c[i] >= o[i] else 0 for i in range(len(c))],
            "ma5": _rl(ma5), "ma10": _rl(ma10), "ma20": _rl(ma20), "ma60": _rl(ma60),
            "boll_up": _rl(bu), "boll_low": _rl(bl),
            "dif": _rl(dif, 3), "dea": _rl(dea, 3), "hist": _rl(hist, 3),
            "buys": buys, "sells": sells,
        },
    }

    # 个股：估值 + 基本面（val/fund 已在上面并行抓好）
    if is_stock:
        if val:
            pe_list = [v["pe"] for v in val]
            pb_list = [v["pb"] for v in val]
            cur_pe = val[-1]["pe"]
            cur_pb = val[-1]["pb"]
            res["pe"] = _r(cur_pe)
            res["pb"] = _r(cur_pb)
            res["pe_pct"] = percentile_rank(pe_list, cur_pe)
            res["pb_pct"] = percentile_rank(pb_list, cur_pb)
            res["pe_stats"] = stat_block(pe_list)
            res["pb_stats"] = stat_block(pb_list)
            res["cap"] = val[-1].get("cap")
            res["val_series"] = {
                "dates": [v["date"] for v in val],
                "pe": [_r(v["pe"]) for v in val],
                "pb": [_r(v["pb"]) for v in val],
            }
            # 行业平均对比（需要板块码，故在估值之后）
            bc = val[-1].get("board_code")
            bn = val[-1].get("board_name")
            if bc:
                res["industry"] = fetch_industry(bc, bn, val[-1]["date"], code)
        res["fund"] = fund

    # 近5日资金流向（mf 已在上面并行抓好）
    if mf:
        res["moneyflow"] = _moneyflow_summary(mf)

    # 实时报价（腾讯 qt 块，~1-3分钟延迟）：覆盖显示用的现价/涨跌
    if kl_live and kl_live.get("price") is not None:
        res["price"] = round(kl_live["price"], 3)
        if kl_live.get("chg") is not None:
            res["chg"] = round(kl_live["chg"], 2)
        rt = kl_live.get("time") or ""
        if len(rt) == 14:
            res["rt_time"] = "%s-%s-%s %s:%s" % (rt[:4], rt[4:6], rt[6:8], rt[8:10], rt[10:12])
        res["realtime"] = True

    res.update(build_report(res))
    res["alerts"] = build_alerts(res)
    return _clean(res)


def _moneyflow_summary(days):
    """把5日资金流整理成前端要的结构 + 主力连续同向天数。"""
    main = [d["main"] for d in days]
    sign = lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
    s = sign(main[-1]); streak = 0
    for v in reversed(main):
        if s != 0 and sign(v) == s:
            streak += 1
        else:
            break
    return {
        "days": [{"date": d["date"][5:], "main": _r(d["main"]), "super": _r(d["super"]),
                  "large": _r(d["large"]), "mid": _r(d["mid"]), "small": _r(d["small"])} for d in days],
        "main_today": _r(main[-1]),
        "main_sum5": _r(sum(main)),
        "streak": streak, "streak_dir": s,     # s: 1流入 / -1流出
    }


def build_alerts(res):
    """异动提醒：结合资金流、量能、价格。返回 [{lv,t}]，lv: in/out/warn/info。"""
    alerts = []
    mf = res.get("moneyflow")
    if mf:
        tdy, s5 = mf["main_today"], mf["main_sum5"]
        if mf["streak"] >= 3 and mf["streak_dir"] > 0:
            alerts.append({"lv": "in", "t": "主力连续 %d 日净流入" % mf["streak"]})
        elif mf["streak"] >= 3 and mf["streak_dir"] < 0:
            alerts.append({"lv": "out", "t": "主力连续 %d 日净流出" % mf["streak"]})
        if tdy is not None and abs(tdy) >= 1:
            alerts.append({"lv": "in" if tdy > 0 else "out",
                           "t": "今日主力%s %.2f 亿" % ("净流入" if tdy > 0 else "净流出", abs(tdy))})
        if s5 is not None and abs(s5) >= 2:
            alerts.append({"lv": "in" if s5 > 0 else "out",
                           "t": "近5日主力累计%s %.2f 亿" % ("净流入" if s5 > 0 else "净流出", abs(s5))})
    t = res.get("tech", {})
    vr, chg = t.get("vol_ratio"), res.get("chg")
    if vr is not None and vr >= 2:
        alerts.append({"lv": "warn", "t": "今日显著放量（%.1f× 5日均量）" % vr})
    if chg is not None and abs(chg) >= 5:
        alerts.append({"lv": "warn" if chg < 0 else "in", "t": "股价异动 %+.1f%%" % chg})
    if vr is not None and vr >= 1.8 and chg is not None:
        if chg < -2:
            alerts.append({"lv": "out", "t": "放量下跌，注意抛压"})
        elif chg > 2:
            alerts.append({"lv": "in", "t": "放量上涨，资金活跃"})
    if not alerts:
        alerts.append({"lv": "info", "t": "近日无明显资金或量价异动"})
    return alerts


def _clean(x):
    """把 NaN/Infinity 换成 None，保证前端 JSON 可解析。"""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


# ---- 结果缓存（相当于内存数据库）：网页与 AI 助手共用，避免重复抓取 ----
ANALYZE_TTL = 180            # 秒；缓存有效期内直接返回，不再抓取
_ACACHE = {}
_acache_lock = threading.Lock()


def analyze_cached(code):
    """带缓存的分析：TTL 内命中直接返回。AI 助手/网页/投研团共用同一份缓存。"""
    code = code.strip()
    now = time.time()
    with _acache_lock:
        hit = _ACACHE.get(code)
        if hit and now - hit[0] < ANALYZE_TTL:
            return hit[1]
    res = analyze(code)
    if "error" not in res:
        with _acache_lock:
            _ACACHE[code] = (now, res)
    return res


def prewarm():
    """启动后台预热：先抓大盘，再抓几只常用股，之后打开即秒开、AI 助手也走缓存。"""
    try:
        market_overview()
    except Exception:
        pass
    for c in ("600519", "300750", "000858", "510300"):
        try:
            analyze_cached(c)
        except Exception:
            pass


def warm_market_cache():
    """仅预热首屏大盘数据，避免启动时额外抢占个股分析所需的网络连接。"""
    try:
        market_overview()
    except Exception:
        pass


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def _rl(lst, nd=2):
    return [None if x is None else round(x, nd) for x in lst]


# ----------------------------------------------------------------------------
# 报告 + 风险评分（规则化，可解释）
# ----------------------------------------------------------------------------
def _lvl(p):
    if p is None:
        return ("中性", "mid")
    if p < 30:
        return ("低估区", "low")
    if p <= 70:
        return ("合理区", "mid")
    return ("高估区", "high")


def build_report(res):
    t = res["tech"]
    risk = 0
    reasons = []
    ops = []

    # —— 估值维度 ——
    val_txt = []
    key_pct = res.get("pe_pct")
    if res.get("is_stock") and res.get("pe") is not None:
        if res["pe"] < 0:
            val_txt.append("公司当前 PE 为负（处于亏损或扣非亏损），市盈率失去参考意义，估值以 PB 为主。")
            risk += 15
            reasons.append("当前亏损（PE为负）")
            key_pct = res.get("pb_pct")
        else:
            lv, _ = _lvl(res["pe_pct"])
            val_txt.append("PE-TTM %.2f，处于近%d年 %.1f%% 分位（%s）。" % (res["pe"], YEARS, res["pe_pct"], lv))
        if res.get("pb_pct") is not None:
            lv, _ = _lvl(res["pb_pct"])
            val_txt.append("PB %.2f，处于近%d年 %.1f%% 分位（%s）。" % (res["pb"], YEARS, res["pb_pct"], lv))
    else:
        key_pct = res.get("price_pct")
        val_txt.append("该品种为%s，无个股 PE/PB，估值以股价所处历史区间衡量。" % (res.get("type_name") or "基金/指数"))

    if res.get("price_pct") is not None:
        lv, _ = _lvl(res["price_pct"])
        val_txt.append("股价处于近%d年 %.1f%% 分位（%s），区间 %.3f ~ %.3f，距最高 %+.1f%%、距最低 %+.1f%%。" % (
            YEARS, res["price_pct"], lv, res["price_lo"], res["price_hi"], res["from_hi"], res["from_lo"]))

    if key_pct is not None:
        if key_pct > 80:
            risk += 25; reasons.append("估值分位偏高（>80%）")
        elif key_pct > 60:
            risk += 12; reasons.append("估值分位中等偏高")
        elif key_pct < 20:
            ops.append("估值处于历史低位（<20%分位），若基本面无恶化，属相对有利的布局区间。")

    # —— 行业对比 ——
    ind = res.get("industry")
    if ind and ind.get("median_pe") is not None and ind.get("target_pe") is not None:
        tpe, mpe = ind["target_pe"], ind["median_pe"]
        rel = "高于" if tpe > mpe else "低于"
        val_txt.append("所属行业「%s」共%d只，行业中位 PE %.1f、PB %.1f；本股 PE %.1f，%s行业中位。" % (
            ind["name"], ind["count"], mpe, ind.get("median_pb") or 0, tpe, rel))
        if ind.get("cheaper_than_target") is not None and ind.get("pos_pe_count"):
            val_txt.append("在行业%d只盈利个股中，有%d只比它更便宜（PE更低）。" % (
                ind["pos_pe_count"], ind["cheaper_than_target"]))
        if tpe > mpe * 1.5:
            risk += 8; reasons.append("估值显著高于行业中位")

    # —— 技术维度 ——
    tech_txt = []
    ma5, ma20, ma60 = t["ma5"], t["ma20"], t["ma60"]
    if None not in (ma5, ma20, ma60):
        if ma5 > ma20 > ma60:
            tech_txt.append("均线多头排列（MA5>MA20>MA60），中短期趋势向上。")
        elif ma5 < ma20 < ma60:
            tech_txt.append("均线空头排列（MA5<MA20<MA60），中短期趋势向下。")
            risk += 6; reasons.append("均线空头排列")
        else:
            tech_txt.append("均线交织，趋势不明朗，处于震荡或转折阶段。")
    pos = "上方" if (ma20 and res["price"] > ma20) else "下方"
    tech_txt.append("现价位于 MA20 %s。" % pos)

    if t["rsi"] is not None:
        if t["rsi"] >= 75:
            tech_txt.append("RSI %.1f，进入超买区，短线过热。" % t["rsi"]); risk += 10; reasons.append("RSI超买")
        elif t["rsi"] <= 25:
            tech_txt.append("RSI %.1f，进入超卖区，短线或有反弹。" % t["rsi"]); ops.append("RSI超卖，关注短线反弹机会。")
        else:
            tech_txt.append("RSI %.1f，处于中性区间。" % t["rsi"])

    if t["macd_dif"] is not None and t["macd_hist"] is not None:
        st = "红柱（多头动能）" if t["macd_hist"] >= 0 else "绿柱（空头动能）"
        tech_txt.append("MACD DIF %.3f / DEA %.3f，%s。" % (t["macd_dif"], t["macd_dea"], st))

    if t["boll_up"] is not None:
        if res["price"] >= t["boll_up"]:
            tech_txt.append("股价触及布林上轨（+2σ），偏离均值较远。")
        elif res["price"] <= t["boll_low"]:
            tech_txt.append("股价触及布林下轨（-2σ），偏离均值较远。")

    # —— 量能维度 ——
    vol_txt = []
    if t["vol_ratio"] is not None:
        if t["vol_ratio"] >= 2:
            vol_txt.append("最新成交量为5日均量的 %.2f 倍，显著放量，资金活跃。" % t["vol_ratio"])
        elif t["vol_ratio"] >= 1.5:
            vol_txt.append("成交量温和放大（%.2f 倍5日均量）。" % t["vol_ratio"])
        elif t["vol_ratio"] <= 0.6:
            vol_txt.append("成交量萎缩（%.2f 倍5日均量），交投清淡。" % t["vol_ratio"])
        else:
            vol_txt.append("成交量与近期均值相当（%.2f 倍）。" % t["vol_ratio"])

    # —— 波动/回撤 ——
    if t["vola"] is not None:
        if t["vola"] >= 45:
            risk += 12; reasons.append("年化波动率高（%.0f%%）" % t["vola"])
        elif t["vola"] >= 30:
            risk += 6
        tech_txt.append("近%d年年化波动率约 %.0f%%，最大回撤 %.0f%%。" % (YEARS, t["vola"], t["mdd"]))

    if res["price_pct"] is not None and res["price_pct"] > 90:
        risk += 10; reasons.append("股价接近5年高点")

    # —— 基本面（个股）——
    fund_txt = []
    fund = res.get("fund") or []
    if fund:
        f = fund[0]
        parts = []
        if f["roe"] is not None:
            parts.append("加权ROE %.1f%%" % f["roe"])
            if f["roe"] < 5:
                risk += 8; reasons.append("ROE偏低")
        if f["rev_yoy"] is not None:
            parts.append("营收同比 %+.1f%%" % f["rev_yoy"])
            if f["rev_yoy"] < 0:
                risk += 8; reasons.append("营收负增长")
        if f["gross"] is not None:
            parts.append("毛利率 %.1f%%" % f["gross"])
        if f["debt"] is not None:
            parts.append("资产负债率 %.1f%%" % f["debt"])
            if f["debt"] > 70:
                risk += 10; reasons.append("负债率偏高（>70%）")
        if f["eps"] is not None:
            parts.append("每股收益 %.2f元" % f["eps"])
        fund_txt.append("最新报告期（%s）：%s。" % (f["date"], "，".join(parts)))
    elif res.get("is_stock"):
        fund_txt.append("暂未取得财务指标数据。")

    risk = min(risk, 100)
    if risk < 20:
        rlvl, rcls = "低", "low"
    elif risk < 45:
        rlvl, rcls = "中", "mid"
    elif risk < 70:
        rlvl, rcls = "偏高", "high"
    else:
        rlvl, rcls = "高", "high"
    if not reasons:
        reasons.append("各维度未见突出风险点")

    signal_note = ("信号说明：图中买卖标记为 MA5 上穿/下穿 MA20 的金叉/死叉点，是趋势跟随类指标，"
                   "在震荡市中会频繁误报，仅作观察参考，不等于买卖建议。")

    return {
        "report": {
            "valuation": val_txt, "technical": tech_txt,
            "volume": vol_txt, "fundamental": fund_txt,
            "opportunities": ops,
        },
        "risk": {"score": risk, "level": rlvl, "cls": rcls, "reasons": reasons},
        "signal_note": signal_note,
    }


# ----------------------------------------------------------------------------
# Excel 导出
# ----------------------------------------------------------------------------
def build_excel(code):
    try:
        from openpyxl import Workbook
        from openpyxl.chart import LineChart, Reference
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return None, "缺少 openpyxl，请先运行：pip install openpyxl"
    res = analyze(code)
    if "error" in res:
        return None, res["error"]

    wb = Workbook()
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="1F3A5F")
    white = Font(bold=True, color="FFFFFF")
    wrap = Alignment(wrap_text=True, vertical="top")

    ws = wb.active
    ws.title = "分析概览"
    rows = [
        ("品种", "%s (%s) · %s" % (res["name"], res["code"], res.get("type_name", ""))),
        ("数据日期", res["date"]),
        ("现价", "%s  (%+.2f%%)" % (res["price"], res["chg"])),
    ]
    if res.get("is_stock") and res.get("pe") is not None:
        rows += [
            ("PE-TTM / 近%d年分位" % YEARS, "%s  /  %s%%" % (res["pe"], res.get("pe_pct"))),
            ("PB / 近%d年分位" % YEARS, "%s  /  %s%%" % (res["pb"], res.get("pb_pct"))),
        ]
    rows += [
        ("股价近%d年分位" % YEARS, "%s%%   （区间 %s ~ %s）" % (res["price_pct"], res["price_lo"], res["price_hi"])),
        ("均线", "MA5 %s / MA20 %s / MA60 %s" % (res["tech"]["ma5"], res["tech"]["ma20"], res["tech"]["ma60"])),
        ("RSI(14)", res["tech"]["rsi"]),
        ("MACD", "DIF %s / DEA %s / 柱 %s" % (res["tech"]["macd_dif"], res["tech"]["macd_dea"], res["tech"]["macd_hist"])),
        ("布林带(±2σ)", "上 %s / 中 %s / 下 %s" % (res["tech"]["boll_up"], res["tech"]["boll_mid"], res["tech"]["boll_low"])),
        ("量比(对5日)", res["tech"]["vol_ratio"]),
        ("年化波动率 / 最大回撤", "%s%% / %s%%" % (res["tech"]["vola"], res["tech"]["mdd"])),
        ("风险评级", "%s（%d/100）" % (res["risk"]["level"], res["risk"]["score"])),
        ("主要风险点", "；".join(res["risk"]["reasons"])),
    ]
    for i, (k, v) in enumerate(rows, 1):
        a = ws.cell(i, 1, k); a.font = white; a.fill = fill
        ws.cell(i, 2, v)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 60

    # 文字报告
    wr = wb.create_sheet("分析报告")
    r = 1
    for title, key in [("估值", "valuation"), ("技术面", "technical"),
                       ("量能", "volume"), ("基本面", "fundamental"), ("关注点", "opportunities")]:
        lines = res["report"].get(key) or []
        if not lines:
            continue
        cc = wr.cell(r, 1, title); cc.font = white; cc.fill = fill; r += 1
        for ln in lines:
            wr.cell(r, 1, "· " + ln).alignment = wrap; r += 1
        r += 1
    wr.cell(r, 1, res["signal_note"]).alignment = wrap; r += 2
    wr.cell(r, 1, "免责声明：本表由公开数据按固定规则自动生成，仅供研究，不构成投资建议。").alignment = wrap
    wr.column_dimensions["A"].width = 95

    # K线数据
    wk = wb.create_sheet("K线数据")
    wk.append(["日期", "开", "高", "低", "收", "成交量", "MA5", "MA20", "MA60", "RSI", "布林上", "布林下"])
    for c in wk[1]:
        c.font = white; c.fill = fill
    ch = res["chart"]
    for i in range(len(ch["dates"])):
        cd = ch["candle"][i]
        wk.append([ch["dates"][i], cd[0], cd[3], cd[2], cd[1], ch["vol"][i],
                   ch["ma5"][i], ch["ma20"][i], ch["ma60"][i], None,
                   ch["boll_up"][i], ch["boll_low"][i]])
    wk.freeze_panes = "A2"
    n = len(ch["dates"])
    lc = LineChart(); lc.title = "%s 收盘价与MA20（近%d年）" % (res["name"], YEARS)
    lc.add_data(Reference(wk, min_col=5, min_row=1, max_row=n + 1), titles_from_data=True)
    lc.add_data(Reference(wk, min_col=8, min_row=1, max_row=n + 1), titles_from_data=True)
    lc.set_categories(Reference(wk, min_col=1, min_row=2, max_row=n + 1))
    lc.height, lc.width = 9, 24
    ws.add_chart(lc, "D2")

    # 估值数据（个股）
    if res.get("val_series"):
        wv = wb.create_sheet("估值数据")
        wv.append(["日期", "PE-TTM", "PB"])
        for c in wv[1]:
            c.font = white; c.fill = fill
        vs = res["val_series"]
        for i in range(len(vs["dates"])):
            wv.append([vs["dates"][i], vs["pe"][i], vs["pb"][i]])
        wv.freeze_panes = "A2"
        m = len(vs["dates"])
        pc = LineChart(); pc.title = "PE-TTM 走势"
        pc.add_data(Reference(wv, min_col=2, min_row=1, max_row=m + 1), titles_from_data=True)
        pc.set_categories(Reference(wv, min_col=1, min_row=2, max_row=m + 1))
        pc.height, pc.width = 8, 24
        ws.add_chart(pc, "D20")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), None


# ============================================================================
# AI 助手（网页版 Agent）——DeepSeek / OpenAI 兼容
# ============================================================================
AGENT_BASE = os.environ.get("AGENT_BASE", "https://api.deepseek.com")   # 换服务商改这里
AGENT_MODEL = os.environ.get("AGENT_MODEL", "deepseek-v4-pro")          # DeepSeek V4 Pro（换模型改这里）
AGENT_KEY_ENV = os.environ.get("DEEPSEEK_API_KEY", "")                  # 服务器端默认Key（可选）
MAX_TOOL_ROUNDS = 8

AGENT_SYSTEM = (
    "你是一名中国A股估值与技术分析助手，服务对象是普通投资者。分析对象是股票、ETF、指数。\n"
    "铁律：所有数字（价格、PE/PB分位、技术指标、行业对比、财务、风险）必须通过工具获取，"
    "严禁凭记忆编造或估算。需要数据就调用工具；用户给名称就先用 find_code 找代码。\n"
    "遇到含糊或需要你替用户做判断的地方（如未指明看哪只、口径不清），先简短反问用户确认，不要擅自假设。\n"
    "回答：先结论后依据，务必点明分位数、行业对比位置、风险等级；多标的先各自分析再横向对比；简洁口语。\n"
    "每次给出倾向性判断后，都要一句话提示这是基于历史公开数据的规则化分析，不构成投资建议。"
)

AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "find_code",
        "description": "按名称/关键词搜索A股/ETF/指数的6位代码。用户说名称而非代码时先用它。",
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string", "description": "如 茅台、宁德时代、沪深300"}},
                       "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "analyze_stock",
        "description": ("对一个6位代码做完整分析，返回现价涨跌、PE/PB/股价近5年分位、行业中位对比、"
                        "均线趋势、RSI、MACD、量比、波动率、最大回撤、基本面(ROE/营收/毛利/负债/EPS)、风险评分。"),
        "parameters": {"type": "object",
                       "properties": {"code": {"type": "string", "description": "6位代码，如 600519"}},
                       "required": ["code"]}}},
]


def _agent_summarize(code):
    r = analyze_cached(code)      # 走缓存：AI 助手不再每次重新抓取
    if "error" in r:
        return r
    t = r["tech"]
    out = {"code": r["code"], "name": r["name"], "type": r.get("type_name"),
           "price": r["price"], "change_pct": r["chg"], "data_date": r["date"],
           "valuation": {"price_percentile_5y": r.get("price_pct"),
                         "from_high_pct": r.get("from_hi"), "from_low_pct": r.get("from_lo")},
           "technical": {"ma_trend": ("多头排列" if (t["ma5"] and t["ma20"] and t["ma60"]
                          and t["ma5"] > t["ma20"] > t["ma60"]) else
                          ("空头排列" if (t["ma5"] and t["ma20"] and t["ma60"]
                           and t["ma5"] < t["ma20"] < t["ma60"]) else "均线交织")),
                         "rsi14": t["rsi"], "macd_hist": t["macd_hist"],
                         "vol_ratio_vs_5d": t["vol_ratio"],
                         "annual_volatility_pct": t["vola"], "max_drawdown_pct": t["mdd"]},
           "risk": r["risk"]}
    if r.get("pe") is not None:
        out["valuation"].update({"pe_ttm": r["pe"], "pe_percentile_5y": r.get("pe_pct"),
                                 "pb": r["pb"], "pb_percentile_5y": r.get("pb_pct")})
    if r.get("industry"):
        i = r["industry"]
        out["industry"] = {"name": i["name"], "median_pe": i["median_pe"], "median_pb": i["median_pb"],
                           "this_pe": i["target_pe"], "cheaper_peers": i.get("cheaper_than_target"),
                           "profitable_peers": i.get("pos_pe_count")}
    if r.get("fund"):
        f = r["fund"][0]
        out["fundamental"] = {"report_date": f["date"], "roe_weighted": f["roe"],
                              "revenue_yoy_pct": f["rev_yoy"], "gross_margin_pct": f["gross"],
                              "debt_ratio_pct": f["debt"], "eps": f["eps"]}
    if r.get("moneyflow"):
        mf = r["moneyflow"]
        out["money_flow_5d"] = {"main_today_yi": mf["main_today"], "main_sum5_yi": mf["main_sum5"],
                                "streak_days": mf["streak"], "streak_dir": "in" if mf["streak_dir"] > 0 else "out"}
    if r.get("alerts"):
        out["alerts"] = [a["t"] for a in r["alerts"]]
    return out


def _agent_find_code(keyword):
    url = "https://searchapi.eastmoney.com/api/suggest/get?input=%s&type=14&count=8" % \
          urllib.parse.quote(keyword)
    try:
        data = fetch_json(url)
        rows = (data.get("QuotationCodeTable") or {}).get("Data") or []
        out = [{"code": r.get("Code"), "name": r.get("Name"), "type": r.get("SecurityTypeName")}
               for r in rows[:8]]
        return {"matches": out} if out else {"matches": [], "note": "未找到匹配"}
    except Exception as e:
        return {"error": str(e)}


AGENT_DISPATCH = {
    "find_code": lambda a: _agent_find_code(a.get("keyword", "")),
    "analyze_stock": lambda a: _agent_summarize(a.get("code", "")),
}


def agent_llm(messages, api_key):
    body = {"model": AGENT_MODEL, "messages": messages, "tools": AGENT_TOOLS,
            "tool_choice": "auto", "temperature": 0.3}
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    resp = api_post(AGENT_BASE + "/chat/completions", headers, body, timeout=120, retries=3)
    if "choices" not in resp:
        raise ValueError(resp.get("error", {}).get("message") or "模型返回异常")
    return resp["choices"][0]["message"]


def agent_run(history, api_key):
    """处理一轮对话：返回 (最终回复, 工具轨迹, 新history)。history 不含 system。"""
    trace = []
    messages = [{"role": "system", "content": AGENT_SYSTEM}] + history
    for _ in range(MAX_TOOL_ROUNDS):
        msg = agent_llm(messages, api_key)
        messages.append(msg)
        calls = msg.get("tool_calls")
        if not calls:
            return (msg.get("content") or "(无输出)", trace, messages[1:])
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            trace.append({"tool": name, "args": args})
            try:
                result = AGENT_DISPATCH[name](args) if name in AGENT_DISPATCH else {"error": "未知工具"}
            except Exception as e:
                result = {"error": str(e)}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
    return ("（工具调用次数过多已中止，请把问题拆细些再问。）", trace, messages[1:])


# ============================================================================
# AI 投研团：多个「分析员」子 Agent 并行看不同股票 →「首席」汇总
# 全部使用 DeepSeek V4 Pro；分析员用不同「角色视角」分工（价值/技术/风控），
# 想混入其它厂商模型，只需给某个角色改 base/model（见下方注释）。
# ============================================================================
DS_BASE = "https://api.deepseek.com"
DS_MODEL = "deepseek-v4-pro"

ANALYST_ROLES = [
    {"role": "价值派", "base": DS_BASE, "model": DS_MODEL, "model_label": "DeepSeek V4 Pro",
     "persona": "你是价值投资分析员，重点评估：估值分位、行业中位对比、基本面(ROE/营收增速/毛利/负债)与安全边际。"},
    {"role": "技术派", "base": DS_BASE, "model": DS_MODEL, "model_label": "DeepSeek V4 Pro",
     "persona": "你是技术交易分析员，重点评估：均线多空排列、MACD、RSI、量比、布林位置与短期买卖时机。"},
    {"role": "风控派", "base": DS_BASE, "model": DS_MODEL, "model_label": "DeepSeek V4 Pro",
     "persona": "你是风险控制分析员，专挑风险点：估值偏高、波动大、回撤深、负债高、亏损或增长下滑。"},
    # 想让某个角色换成通义/Kimi：改这一条的 base/model/model_label，并在前端多收一个该厂商的 Key 即可。
    # 例：{"role":"另一视角","base":"https://dashscope.aliyuncs.com/compatible-mode/v1","model":"qwen-plus","model_label":"通义千问", ...}
]
# 首席汇总（默认 DeepSeek V4 Pro）
CHIEF_MODEL = {"base": DS_BASE, "model": DS_MODEL, "label": "DeepSeek V4 Pro · 首席"}

# 可选：用 GPT 做首席汇总。每次投研团只调用 1 次，成本≈几分钱，不会经费爆炸。
# 分析员仍用便宜的 DeepSeek（高频），只有最后这一步换成 GPT。
OAI_BASE = "https://api.openai.com/v1"
OAI_CHIEF_MODEL = "gpt-4.1-mini"   # ← 最省($0.4/$1.6)。想更强改这里：gpt-5.6-luna / gpt-5.6-terra（按你账号可用模型名填）
OAI_CHIEF_LABEL = "GPT · 首席"

ANALYST_SYS_BASE = ("你是一名严谨的卖方分析员。基于给定某一只股票的量化数据(JSON)，用4-6句话给出你的判断。"
                    "只依据数据、不得编造数字。最后一句给风险提示。你的分析视角是：")
CHIEF_SYS = ("你是投研团队首席分析师。下面是多位不同视角的分析员对不同股票各自的独立点评。请综合："
             "1) 按用户目标给出横向对比和明确排序；2) 用关键数字说明理由；3) 指出分析员之间的分歧点（若有）；"
             "4) 一句总体提示。简洁专业。结尾注明：基于历史公开数据的规则化分析，不构成投资建议。")


def openai_complete(base, model, api_key, system, user, max_tokens=900):
    """OpenAI 兼容的纯文本补全（分析员用，无需工具）。带瞬时错误重试。"""
    body = {"model": model, "temperature": 0.4, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    resp = api_post(base + "/chat/completions", headers, body, timeout=120, retries=3)
    if "choices" not in resp:
        raise ValueError(resp.get("error", {}).get("message") or "模型返回异常")
    return resp["choices"][0]["message"]["content"]


def claude_complete(model, api_key, system, user, max_tokens=1600):
    """Anthropic 原生 Messages 接口（首席汇总用）。"""
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    resp = api_post("https://api.anthropic.com/v1/messages", headers, body, timeout=150, retries=3)
    parts = resp.get("content") or []
    txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return txt or "(汇总为空)"


def gpt_complete(model, api_key, system, user, max_tokens=1600):
    """OpenAI 接口（首席汇总用）。兼容新老模型：先试 max_completion_tokens，失败再退回 max_tokens。"""
    base = {"model": model, "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]}
    attempts = [dict(base, max_completion_tokens=max_tokens),
                dict(base, max_tokens=max_tokens, temperature=0.4)]
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    last = None
    for body in attempts:
        try:
            resp = api_post(OAI_BASE + "/chat/completions", headers, body, timeout=150, retries=3)
            if "choices" in resp:
                return resp["choices"][0]["message"]["content"]
            last = ValueError(resp.get("error", {}).get("message") or "GPT 返回异常")
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 400:      # 400 多为参数问题，值得换 body 重试；其它错误直接抛
                raise
        except Exception as e:
            last = e
    raise last


def _one_analyst(i, code, ds_key):
    """单个分析员：抓数据 + 以分配到的角色视角点评（模型 DeepSeek V4 Pro）。"""
    r = ANALYST_ROLES[i % len(ANALYST_ROLES)]
    label = "%s · %s" % (r["role"], r["model_label"])
    summ = _agent_summarize(code)
    if "error" in summ:
        return {"code": code, "name": code, "model": label, "role": r["role"],
                "text": "数据获取失败：" + summ["error"], "err": True}
    system = ANALYST_SYS_BASE + r["persona"]
    user = "请分析这只股票（数据JSON如下）：\n" + json.dumps(summ, ensure_ascii=False)
    try:
        txt = openai_complete(r["base"], r["model"], ds_key, system, user)
    except urllib.error.HTTPError as e:
        txt = "分析员调用失败(%s)：可能是 DeepSeek Key 或余额问题" % e.code
    except Exception as e:
        txt = "分析员调用失败：%s" % e
    return {"code": code, "name": summ.get("name", code), "model": label, "role": r["role"], "text": txt}


def analyze_multidim(code, ds_key):
    """对单只股票做三视角多维分析：价值/技术/风控。"""
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("请输入6位数字代码")
    reports = []
    for idx in range(3):
        reports.append(_one_analyst(idx, code, ds_key))
    return {"code": code, "name": reports[0].get("name", code), "reports": reports}


def panel_analyze(codes, ds_key, goal="", chief="deepseek", oai_key="", oai_model=""):
    """多角色分析员并行（DeepSeek）+ 首席汇总（chief: 'deepseek' 或 'gpt'）。"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(codes), 6)) as pool:
        analysts = list(pool.map(lambda t: _one_analyst(*t, ds_key), enumerate(codes)))
    briefs = ["【%s（%s）· %s】\n%s" % (a["name"], a["code"], a["model"], a["text"])
              for a in analysts if not a.get("err")]
    chief_user = "用户目标：%s\n\n以下是各分析员的独立点评：\n\n%s" % (goal or "综合比较这些标的", "\n\n".join(briefs))
    use_gpt = (chief == "gpt" and oai_key)
    gpt_model = (oai_model or OAI_CHIEF_MODEL).strip()
    chief_label = "%s（%s）" % (OAI_CHIEF_LABEL, gpt_model) if use_gpt else CHIEF_MODEL["label"]
    try:
        if use_gpt:
            chief_txt = gpt_complete(gpt_model, oai_key, CHIEF_SYS, chief_user, max_tokens=1600)
        else:
            chief_txt = openai_complete(CHIEF_MODEL["base"], CHIEF_MODEL["model"], ds_key, CHIEF_SYS, chief_user, max_tokens=1600)
    except urllib.error.HTTPError as e:
        who = "GPT" if use_gpt else "DeepSeek"
        code_msg = {401: "%s Key 无效" % who, 402: "余额不足", 429: "限流",
                    404: "模型名不对（改 app.py 顶部 OAI_CHIEF_MODEL）", 400: "请求被拒（模型名或参数）"}
        chief_txt = "首席汇总失败(%s)：%s" % (e.code, code_msg.get(e.code, "接口错误"))
    except Exception as e:
        chief_txt = "首席汇总失败：%s" % e
    return {"analysts": analysts, "chief": {"model": chief_label, "text": chief_txt}, "goal": goal}


# ----------------------------------------------------------------------------
# 前端页面
# ----------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>估值·技术·基本面 分析台</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/lucide@0.468.0/dist/umd/lucide.min.js"></script>
<style>
*{box-sizing:border-box} body{margin:0;font-family:"Microsoft YaHei","Segoe UI",sans-serif;
 background:#0b0f17;color:#c9d4e5}
.wrap{max-width:1180px;margin:0 auto;padding:22px 18px 60px}
h1{font-size:20px;font-weight:600;margin:0 0 2px;color:#eaf1fb;letter-spacing:1px}
.searchbar{display:flex;gap:10px;align-items:center;margin:16px 0 8px;flex-wrap:wrap}
input{background:#141b28;border:1px solid #2b3a52;color:#eaf1fb;
 font-size:17px;padding:11px 14px;border-radius:10px;width:210px;outline:none}
input:focus{border-color:#3b82f6}
button{border:0;border-radius:10px;padding:11px 20px;font-size:15px;cursor:pointer;color:#fff;background:#2563eb}
button:hover{background:#1d4ed8} button.g{background:#059669} button.g:hover{background:#047857}
#status{color:#64748b;font-size:13px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.chip{background:#141b28;border:1px solid #22304a;color:#93a4bf;font-size:12px;padding:5px 10px;border-radius:20px;cursor:pointer}
.chip:hover{border-color:#3b82f6;color:#dbe6f7}
.card{background:#111826;border:1px solid #1e293b;border-radius:14px;padding:18px 20px;margin:14px 0;
 box-shadow:0 2px 10px rgba(0,0,0,.25)}
.head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.head .nm{font-size:22px;font-weight:700;color:#f1f6ff} .head .cd{color:#64748b;font-size:14px}
.badge{font-size:12px;padding:2px 9px;border-radius:6px;background:#1e293b;color:#93a4bf}
.px{font-size:26px;font-weight:700} .up{color:#f2495c} .down{color:#2ec26e}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:14px}
.metric{background:#0e1521;border:1px solid #1c2740;border-radius:12px;padding:14px 16px}
.metric h3{margin:0 0 8px;font-size:13px;color:#7b8aa6;font-weight:500}
.metric .v{font-size:22px;font-weight:700;color:#eaf1fb}
.pct{display:inline-block;font-size:13px;font-weight:700;padding:2px 9px;border-radius:6px;color:#fff;margin-left:6px}
.low{background:#059669}.mid{background:#d97706}.high{background:#dc2626}
.bar{height:8px;border-radius:5px;margin:10px 0 5px;position:relative;
 background:linear-gradient(90deg,#059669,#d97706,#dc2626)}
.mk{position:absolute;top:-4px;width:3px;height:16px;background:#fff;border-radius:2px;box-shadow:0 0 4px #000}
.sub{color:#64748b;font-size:12px} .row{display:flex;justify-content:space-between;font-size:13px;color:#8ea0bd;margin-top:4px}
.sec-title{font-size:14px;color:#93a4bf;margin:2px 0 12px;font-weight:600;border-left:3px solid #3b82f6;padding-left:9px}
.report p{margin:7px 0;line-height:1.7;font-size:14px;color:#b6c3d9}
.report .grp{margin-bottom:14px} .report .lbl{color:#7b8aa6;font-size:13px;font-weight:600;margin-bottom:3px}
.riskbox{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.rscore{font-size:40px;font-weight:800} .reasons{font-size:13px;color:#9fb0cb;line-height:1.7}
.note{font-size:12px;color:#64748b;line-height:1.6;margin-top:8px;border-top:1px solid #1c2740;padding-top:8px}
.ops{color:#34d399} .warn{color:#fca5a5}
.disc{text-align:center;color:#475569;font-size:12px;margin-top:24px}
.hint{color:#64748b;font-size:13px;text-align:center;margin-top:40px;line-height:1.9}
.keybar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;background:#111826;border:1px solid #22304a;
 border-radius:10px;padding:10px 14px;margin:14px 0 4px}
.keybar .kb-label{color:#cbd5e1;font-size:13px;font-weight:600}
.keybar input{font-size:14px;padding:8px 11px;width:330px;flex:1;min-width:200px}
.keybar button{font-size:14px;padding:8px 16px}
.keybar .klink{font-size:12px}
/* AI 助手 */
#fab{position:fixed;right:22px;bottom:22px;z-index:50;background:#2563eb;color:#fff;border-radius:30px;
 padding:12px 20px;font-size:15px;cursor:pointer;box-shadow:0 4px 16px rgba(37,99,235,.5);border:0}
#fab:hover{background:#1d4ed8}
#chat{position:fixed;right:22px;bottom:22px;z-index:51;width:390px;max-width:calc(100vw - 30px);height:76vh;
 display:none;flex-direction:column;background:#0e1521;border:1px solid #263349;border-radius:16px;overflow:hidden;
 box-shadow:0 12px 40px rgba(0,0,0,.55)}
#chat .ch-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;
 background:#111a2b;border-bottom:1px solid #22304a}
#chat .ch-head b{color:#eaf1fb;font-size:15px} #chat .ch-head .x{cursor:pointer;color:#7b8aa6;font-size:20px}
.keyrow{display:flex;gap:6px;padding:9px 12px;border-bottom:1px solid #1c2740;align-items:center;flex-wrap:wrap}
.keyrow input{font-size:13px;padding:7px 10px;width:200px;flex:1}
.keyrow button{font-size:13px;padding:7px 12px}
.klink{color:#60a5fa;font-size:11px;text-decoration:none}
.msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:86%;padding:9px 13px;border-radius:12px;font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.msg.u{align-self:flex-end;background:#2563eb;color:#fff;border-bottom-right-radius:3px}
.msg.a{align-self:flex-start;background:#1a2436;color:#d7e2f2;border-bottom-left-radius:3px}
.msg.e{align-self:flex-start;background:#3b1d22;color:#fca5a5;border:1px solid #7f1d1d}
.traceln{align-self:flex-start;font-size:12px;color:#5b6b86;padding:1px 6px}
.inrow{display:flex;gap:8px;padding:10px 12px;border-top:1px solid #22304a}
.inrow textarea{flex:1;resize:none;height:42px;background:#141b28;border:1px solid #2b3a52;color:#eaf1fb;
 border-radius:10px;padding:9px 11px;font-size:14px;font-family:inherit;outline:none}
.inrow button{padding:0 18px}
.ex-q{font-size:12px;color:#64748b;padding:0 14px 8px;line-height:1.7}
.ex-q span{color:#60a5fa;cursor:pointer}
/* 投研团 */
.an-card{background:#0e1521;border:1px solid #1c2740;border-radius:12px;padding:13px 16px;margin:10px 0}
.an-head{display:flex;align-items:center;gap:9px;margin-bottom:6px;flex-wrap:wrap}
.an-name{font-weight:700;color:#eaf1fb;font-size:15px}
.mbadge{font-size:11px;padding:2px 9px;border-radius:20px;font-weight:600}
.m-ds{background:#0e2a4a;color:#7dd3fc;border:1px solid #1d4ed8}
.m-cl{background:#3b1d5a;color:#d8b4fe;border:1px solid #a855f7}
.an-text{font-size:13.5px;color:#b6c3d9;line-height:1.75;white-space:pre-wrap}
.chief-card{background:linear-gradient(160deg,#1a1030,#0e1521);border:1px solid #7c3aed;border-radius:12px;padding:15px 18px;margin:12px 0}
.watch-list{display:flex;flex-direction:column;gap:8px;margin-top:6px}
.watch-addbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 8px}.watch-addbar input{height:38px}.watch-addbar button{height:38px;display:inline-flex;align-items:center;gap:6px}.watch-addbar button svg{width:16px;height:16px}.watch-add-status{min-height:18px;color:#86efac;font-size:12px}
#wadd{width:180px}#wgroup{width:190px}
#watchList{gap:0;margin-top:12px;border:1px solid #1c2740;border-radius:8px;overflow:hidden;background:#0e1521}
.watch-head,.watch-row{display:grid;grid-template-columns:minmax(175px,1.8fr) minmax(82px,.8fr) minmax(82px,.8fr) minmax(82px,.8fr) 112px;align-items:center;column-gap:12px}
.watch-head{min-height:40px;padding:0 14px;color:#7b8aa6;font-size:12px;background:#111c2c;border-bottom:1px solid #22304a}
.watch-head .watch-col:not(:first-child),.watch-row .watch-num{text-align:right}
.watch-group-section+.watch-group-section{border-top:1px solid #30415d}.watch-group-summary{display:grid;grid-template-columns:minmax(150px,1.4fr) repeat(3,minmax(76px,.65fr)) 106px 116px;align-items:center;gap:10px;min-height:58px;padding:8px 14px;background:#101a29;border-bottom:1px solid #22304a}.watch-group-identity{min-width:0}.watch-group-name{display:flex;align-items:center;gap:7px;min-width:0;color:#eaf1fb;font-size:14px;font-weight:700}.watch-group-name>svg{width:16px;height:16px;color:#7dd3fc;flex:0 0 auto}.watch-group-name span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.watch-group-edit{width:26px;height:26px;padding:0;border:1px solid transparent;border-radius:6px;background:transparent;color:#8ea0bd;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto}.watch-group-edit:hover{color:#dbeafe;background:#17243a;border-color:#2b3a52}.watch-group-edit svg{width:13px;height:13px;stroke-width:1.8}.watch-group-count{display:block;margin:4px 0 0 23px;color:#7183a0;font-size:10px}.watch-group-metric{min-width:0}.watch-group-metric label{display:block;color:#7183a0;font-size:10px;margin-bottom:3px}.watch-group-metric strong{display:block;color:#dce7f7;font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}.watch-track{display:flex;align-items:center;justify-content:center;min-width:0;height:34px}.watch-track svg{display:block;width:96px;height:30px}.watch-track-zero{stroke:#31415a;stroke-width:1;stroke-dasharray:2 3}.watch-track-line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.watch-track-empty{color:#64748b;font-size:10px;white-space:nowrap}.watch-signal{justify-self:end;min-width:96px;padding:5px 7px;border:1px solid #334155;border-radius:6px;color:#cbd5e1;background:#172033;font-size:11px;font-weight:700;text-align:center;white-space:nowrap}.watch-signal.warming{color:#fecdd3;border-color:#7f3040;background:#2b1720}.watch-signal.rising{color:#fde68a;border-color:#71571b;background:#292313}.watch-signal.strong{color:#fda4af;border-color:#7f3040;background:#301821}.watch-signal.pressured{color:#86efac;border-color:#256240;background:#13281e}
.watch-row{min-height:76px;padding:8px 14px;border-bottom:1px solid #1c2740;cursor:pointer;transition:background .16s ease}
.watch-group-section .watch-row:last-child{border-bottom:0}.watch-row:hover{background:#152238}
.watch-security{min-width:0}.watch-name{font-size:16px;color:#eaf1fb;font-weight:600;line-height:1.3;overflow-wrap:anywhere}
.watch-code{font-size:12px;color:#7183a0;margin-top:5px;letter-spacing:.3px}.watch-num{font-variant-numeric:tabular-nums;font-size:15px;font-weight:700;white-space:nowrap}
.watch-num .unit{font-size:11px;color:#64748b;font-weight:400;margin-left:2px}.watch-up{color:#f2495c}.watch-down{color:#2ec26e}.watch-flat{color:#c9d4e5}
.watch-tools{display:flex;justify-content:flex-end;gap:5px}.watch-icon-btn{width:30px;height:30px;padding:0;border:1px solid #2b3a52;border-radius:6px;background:#17243a;color:#c8ddff;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}.watch-icon-btn svg{width:15px;height:15px;stroke-width:1.8}.watch-icon-btn:hover{background:#223b5c}.watch-icon-btn.remove{background:#24171d;color:#f7b6bf;border-color:#5d2633}.watch-icon-btn.remove:hover{background:#3a1c25}
.watch-group-dialog{width:min(420px,calc(100vw - 32px));padding:0;border:1px solid #334155;border-radius:8px;background:#0e1521;color:#eaf1fb;box-shadow:0 22px 60px rgba(0,0,0,.5)}.watch-group-dialog::backdrop{background:rgba(3,8,17,.72)}.watch-group-dialog-head{display:flex;align-items:center;justify-content:space-between;padding:13px 15px;border-bottom:1px solid #22304a}.watch-group-dialog-head b{font-size:15px}.watch-group-dialog-head button{width:30px;height:30px;padding:0;display:inline-flex;align-items:center;justify-content:center;background:transparent;border:1px solid transparent}.watch-group-dialog-head button:hover{background:#17243a;border-color:#2b3a52}.watch-group-dialog-head svg{width:16px;height:16px}.watch-group-dialog-body{padding:15px}.watch-group-dialog-security{color:#8ea0bd;font-size:12px;margin-bottom:12px}.watch-group-dialog-body label{display:block;color:#8ea0bd;font-size:12px;margin-bottom:6px}.watch-group-dialog-body input{width:100%;height:40px}.watch-group-dialog-actions{display:flex;justify-content:flex-end;gap:8px;padding:0 15px 15px}.watch-group-dialog-actions button{display:inline-flex;align-items:center;gap:6px;padding:8px 12px}.watch-group-dialog-actions svg{width:15px;height:15px}
@media(max-width:760px){.watch-group-summary{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.watch-group-identity{grid-column:1/-1}.watch-track{justify-content:flex-start}.watch-signal{justify-self:stretch;min-width:0}}
@media(max-width:620px){.watch-addbar{display:grid;grid-template-columns:minmax(0,1fr) auto}.watch-addbar input{width:100%!important}.watch-addbar #wgroup{grid-column:1/-1}.watch-add-status{grid-column:1/-1}.watch-head,.watch-row{grid-template-columns:minmax(120px,1.5fr) minmax(74px,1fr) minmax(74px,1fr);column-gap:8px}.watch-head .watch-col.delta,.watch-head .watch-col.tools,.watch-row .watch-delta{display:none}.watch-group-summary{grid-template-columns:repeat(2,minmax(0,1fr));padding:9px 10px}.watch-group-identity{grid-column:1/-1}.watch-track{justify-content:flex-start}.watch-signal{justify-self:stretch}.watch-row{min-height:90px;padding:9px 10px}.watch-name{font-size:14px}.watch-num{font-size:14px}.watch-tools{grid-column:1/-1;justify-content:flex-start;margin-top:3px}}
/* 盯盘 */
.monitor-runtime{display:flex;align-items:center;gap:12px;min-height:58px;padding:10px 14px;margin:14px 0;border-top:1px solid #22304a;border-bottom:1px solid #22304a}
.monitor-state{display:flex;align-items:center;gap:9px;min-width:210px}.monitor-dot{width:9px;height:9px;border-radius:50%;background:#64748b;flex:0 0 auto}.monitor-dot.running{background:#34d399;box-shadow:0 0 8px rgba(52,211,153,.55)}.monitor-state b{color:#eaf1fb;font-size:14px}.monitor-runtime-meta{color:#7183a0;font-size:12px;flex:1;line-height:1.5}
.monitor-actions{display:flex;gap:7px;align-items:center}.monitor-actions button,.monitor-save{display:inline-flex;align-items:center;justify-content:center;gap:7px}.monitor-actions button{padding:8px 12px;font-size:13px;border:1px solid #2b3a52;background:#17243a}.monitor-actions button:hover{background:#223b5c}.monitor-actions button.stop{color:#f7b6bf;background:#24171d;border-color:#5d2633}.monitor-actions svg,.monitor-save svg,.monitor-icon-btn svg{width:16px;height:16px;stroke-width:1.8}
.monitor-columns{display:grid;grid-template-columns:minmax(300px,.72fr) minmax(440px,1.28fr);gap:14px;min-width:0;max-width:100%}.monitor-columns>.card{min-width:0;max-width:100%;margin:0}.monitor-form{display:grid;grid-template-columns:1fr 1fr;gap:11px;min-width:0}.monitor-field{min-width:0}.monitor-field.full{grid-column:1/-1}.monitor-field label{display:block;color:#8ea0bd;font-size:12px;margin:0 0 5px}.monitor-field input{width:100%;font-size:14px;padding:9px 10px;border-radius:7px}.monitor-form-foot{grid-column:1/-1;display:flex;align-items:center;gap:10px;margin-top:2px}.monitor-save{padding:9px 14px;font-size:13px;border-radius:7px}.monitor-form-status{font-size:12px;color:#7183a0;line-height:1.5}
.monitor-field textarea{width:100%;min-height:76px;resize:vertical;font-size:13px;line-height:1.55;padding:9px 10px;border-radius:7px}.monitor-logic-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px;min-width:0;max-width:100%}.monitor-logic-form{display:grid;grid-template-columns:150px minmax(0,1fr);gap:11px;min-width:0}.monitor-logic-form .monitor-field.wide{grid-column:1/-1}.monitor-logic-actions{grid-column:1/-1;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}.monitor-logic-actions button{display:inline-flex;align-items:center;gap:7px;max-width:100%;padding:8px 12px;font-size:13px;white-space:normal}.monitor-draft{min-width:0;max-width:100%;min-height:100%;border-left:1px solid #22304a;padding-left:18px}.monitor-draft-empty{color:#7183a0;font-size:12px;padding:22px 0}.monitor-draft-summary{color:#dce7f7;font-size:13px;line-height:1.65;overflow-wrap:anywhere}.monitor-draft-meta{color:#7183a0;font-size:11px;margin-top:6px}.monitor-draft-outcomes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:12px 0;border-top:1px solid #22304a;border-bottom:1px solid #22304a}.monitor-draft-outcome{min-width:0;padding:9px 8px}.monitor-draft-outcome+.monitor-draft-outcome{border-left:1px solid #22304a}.monitor-draft-outcome span{display:block;color:#7183a0;font-size:10px;margin-bottom:3px}.monitor-draft-outcome strong{display:block;color:#eaf1fb;font-size:13px;overflow-wrap:anywhere}.monitor-draft-outcome strong.ok{color:#86efac}.monitor-draft-list{margin-top:10px;border-top:1px solid #22304a}.monitor-draft-rule{display:grid;grid-template-columns:76px minmax(0,1fr) auto;gap:9px;align-items:center;padding:9px 0;border-bottom:1px solid #1c2740;font-size:12px}.monitor-draft-rule b{min-width:0;color:#eaf1fb;overflow-wrap:anywhere}.monitor-draft-rule span{color:#93a4bf}.monitor-draft-review{margin-top:11px;padding-top:9px;border-top:1px solid #22304a}.monitor-draft-review b{display:block;color:#8ea0bd;font-size:11px;margin-bottom:5px}.monitor-draft-review div{color:#aebbd0;font-size:12px;line-height:1.6;overflow-wrap:anywhere}.monitor-draft-confirm{margin-top:12px;padding:10px 11px;border-left:3px solid #f59e0b;background:#1c1e25}.monitor-draft-confirm b{display:block;color:#fcd34d;font-size:11px;margin-bottom:5px}.monitor-draft-confirm p{margin:0;color:#dce7f7;font-size:12px;line-height:1.6;overflow-wrap:anywhere}.monitor-draft-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0;margin-top:10px}.monitor-draft-actions button,.monitor-draft-apply{display:inline-flex;align-items:center;gap:7px;max-width:100%;padding:8px 12px;font-size:13px;white-space:normal}.monitor-draft-actions button:disabled{opacity:.55;cursor:wait}
.monitor-summary{display:flex;gap:8px;flex-wrap:wrap;margin:-2px 0 12px}.monitor-kpi{font-size:12px;color:#93a4bf;border-right:1px solid #2b3a52;padding-right:9px}.monitor-kpi:last-child{border:0}.monitor-kpi strong{color:#eaf1fb;margin-left:4px}.monitor-list{border-top:1px solid #22304a}.monitor-row{display:grid;grid-template-columns:minmax(130px,1.4fr) repeat(3,minmax(66px,.7fr)) minmax(86px,.8fr) 106px;gap:9px;align-items:center;min-height:65px;border-bottom:1px solid #1c2740;padding:8px 0}.monitor-row:last-child{border-bottom:0}.monitor-security{min-width:0}.monitor-security b{display:block;color:#eaf1fb;font-size:14px;overflow-wrap:anywhere}.monitor-security span{display:block;color:#7183a0;font-size:11px;margin-top:3px}.monitor-price label{display:block;color:#64748b;font-size:10px;margin-bottom:3px}.monitor-price strong{color:#dce7f7;font-size:13px;font-variant-numeric:tabular-nums}.monitor-move{font-size:12px;color:#7dd3fc}.monitor-row-actions{display:flex;justify-content:flex-end;gap:5px}.monitor-icon-btn{width:30px;height:30px;padding:0;border-radius:6px;background:#17243a;border:1px solid #2b3a52;color:#c8ddff;display:inline-flex;align-items:center;justify-content:center}.monitor-icon-btn:hover{background:#223b5c}.monitor-icon-btn.remove{color:#f7b6bf;background:#24171d;border-color:#5d2633}
.monitor-empty{color:#7183a0;font-size:12px;padding:26px 4px;text-align:center}.monitor-preview{display:none;margin:4px 0 12px;padding:11px 12px;border-left:3px solid #f59e0b;background:#1c1e25}.monitor-preview.visible{display:grid;grid-template-columns:minmax(140px,.65fr) minmax(260px,1.7fr) 78px;gap:12px;align-items:start}.monitor-preview-title{color:#fcd34d;font-size:13px}.monitor-preview-title span{display:block;color:#8b98ad;font-size:11px;margin-top:4px}.monitor-preview-message{font-size:12px;color:#c6d0df;line-height:1.55;white-space:pre-line}.monitor-explanation{display:none;margin:4px 0 12px;padding:12px;border-left:3px solid #38bdf8;background:#101d2a}.monitor-explanation.visible{display:block}.monitor-explanation-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap;color:#dce7f7;font-size:13px}.monitor-explanation-meta{color:#7183a0;font-size:11px}.monitor-explanation-body{color:#b9c6d8;font-size:12px;line-height:1.65;margin-top:9px}.monitor-explanation-cols{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}.monitor-explanation-col b{display:block;color:#8ea0bd;font-size:11px;margin-bottom:4px}.monitor-explanation-col div{color:#b9c6d8;font-size:12px;line-height:1.55}.monitor-events{border-top:1px solid #22304a}.monitor-event{display:grid;grid-template-columns:145px minmax(140px,.7fr) minmax(260px,1.5fr) 90px;gap:12px;align-items:start;padding:11px 0;border-bottom:1px solid #1c2740}.monitor-event:last-child{border-bottom:0}.monitor-event-time{color:#7183a0;font-size:11px;font-variant-numeric:tabular-nums}.monitor-event-security{font-size:13px;color:#eaf1fb}.monitor-event-security span{display:block;color:#7183a0;font-size:11px;margin-top:3px}.monitor-event-message{font-size:12px;color:#aebbd0;line-height:1.55;white-space:pre-line}.monitor-event-actions{display:flex;align-items:center;justify-content:flex-end;gap:6px;flex-wrap:wrap}.monitor-event-actions .monitor-icon-btn{width:26px;height:26px}.monitor-tag{display:inline-block;font-size:11px;padding:3px 7px;border-radius:5px;background:#17243a;color:#93a4bf}.monitor-tag.buy{color:#fca5a5;background:#3a1620}.monitor-tag.sell{color:#86efac;background:#0e2a20}.monitor-tag.alert{color:#fcd34d;background:#3a2a10}.monitor-tag.simulated{color:#fcd34d;background:#3a2a10}
@media(max-width:980px){.monitor-columns{grid-template-columns:minmax(0,1fr)}.monitor-runtime{align-items:flex-start;flex-wrap:wrap}.monitor-runtime-meta{order:3;flex-basis:100%}.monitor-actions{margin-left:auto}.monitor-logic-grid{grid-template-columns:minmax(0,1fr)}.monitor-draft{border-left:0;border-top:1px solid #22304a;padding:14px 0 0}}
@media(max-width:620px){.monitor-runtime{padding:10px 2px}.monitor-state{min-width:0;flex:1}.monitor-actions{width:100%;margin:0}.monitor-actions button{flex:1;padding:8px 7px}.monitor-form,.monitor-logic-form{grid-template-columns:1fr}.monitor-field.full,.monitor-form-foot,.monitor-logic-form .monitor-field.wide,.monitor-logic-actions{grid-column:1}.monitor-form-foot{align-items:flex-start;flex-direction:column}.monitor-row{grid-template-columns:minmax(120px,1.4fr) repeat(2,minmax(62px,.7fr));gap:8px}.monitor-price.target,.monitor-move{display:none}.monitor-row-actions{grid-column:1/-1;justify-content:flex-start}.monitor-draft-outcomes{grid-template-columns:1fr}.monitor-draft-outcome+.monitor-draft-outcome{border-left:0;border-top:1px solid #22304a}.monitor-draft-rule{grid-template-columns:68px minmax(0,1fr)}.monitor-draft-rule>span:last-child{grid-column:2}.monitor-draft-actions{align-items:stretch;flex-direction:column}.monitor-draft-actions button{justify-content:center;width:100%;white-space:normal}.monitor-preview.visible{grid-template-columns:1fr 78px}.monitor-preview-title{grid-column:1}.monitor-preview-message{grid-column:1/-1}.monitor-preview .monitor-tag{grid-column:2;grid-row:1;justify-self:end}.monitor-explanation-cols{grid-template-columns:1fr}.monitor-event{grid-template-columns:1fr 100px}.monitor-event-security{grid-column:1}.monitor-event-time{grid-column:1}.monitor-event-message{grid-column:1/-1}.monitor-event-actions{grid-column:2;grid-row:1/3;justify-self:end}}
/* 标签导航 */
.tabnav{display:flex;gap:6px;margin:16px 0 4px;border-bottom:1px solid #22304a;flex-wrap:wrap}
.tabbtn{background:transparent;border:0;border-bottom:2px solid transparent;color:#7b8aa6;font-size:15px;
 padding:10px 16px;cursor:pointer;border-radius:0;font-weight:600}
.tabbtn:hover{color:#dbe6f7}
.tabbtn.active{color:#eaf1fb;border-bottom-color:#3b82f6}
.tabpage{animation:fade .25s ease}
@keyframes fade{from{opacity:.3}to{opacity:1}}
/* 大盘 */
.idx{background:#0e1521;border:1px solid #1c2740;border-radius:12px;padding:12px 15px}
.idx .nm{font-size:13px;color:#93a4bf}.idx .pv{font-size:22px;font-weight:700}.idx .cg{font-size:13px;font-weight:600}
.market-report{background:linear-gradient(135deg,#111826,#0e1521);border:1px solid #22304a;border-radius:12px;padding:14px 16px;margin-top:12px;line-height:1.8;color:#dce7f7}
.market-report .mr-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.market-report .mr-body{font-size:14px;white-space:pre-wrap}
.secbar{display:flex;align-items:center;gap:10px;margin:5px 0}
.secbar .lab{width:66px;font-size:13px;color:#c9d4e5;text-align:right;flex-shrink:0}
.secbar .track{flex:1;background:#0b111c;border-radius:6px;height:22px;position:relative;overflow:hidden}
.secbar .fill{height:100%;border-radius:6px;transition:width 1s cubic-bezier(.2,.8,.2,1);width:0}
.secbar .pct{width:56px;font-size:13px;font-weight:700;flex-shrink:0}
.mkt-flow-wrap{height:300px;position:relative;overflow:hidden;border:1px solid #1c2740;border-radius:12px;
 background:radial-gradient(circle at 50% 50%,#15233a 0,#0b111c 56%,#080d16 100%)}
#mktFlowSvg{width:100%;height:100%;display:block;shape-rendering:geometricPrecision;text-rendering:geometricPrecision}
.mkt-flow-meta{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:10px;color:#8ea0bd;font-size:12px}
.mkt-flow-meta strong{color:#eaf1fb;font-weight:600}
@media(max-width:560px){.mkt-flow-wrap{height:340px}.mkt-flow-meta{line-height:1.6}}
/* 资金流向 / 异动 */
.alerts{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.alert{font-size:13px;font-weight:600;padding:5px 12px;border-radius:8px;display:flex;align-items:center;gap:6px}
.alert::before{content:"";width:7px;height:7px;border-radius:50%;display:inline-block}
.a-in{background:#3a1620;color:#ff9aa6;border:1px solid #b91c3a}.a-in::before{background:#f2495c;box-shadow:0 0 6px #f2495c}
.a-out{background:#0e2a20;color:#8ff0bf;border:1px solid #15803d}.a-out::before{background:#2ec26e;box-shadow:0 0 6px #2ec26e}
.a-warn{background:#3a2a10;color:#fcd34d;border:1px solid #b45309}.a-warn::before{background:#f59e0b;box-shadow:0 0 6px #f59e0b}
.a-info{background:#141b28;color:#93a4bf;border:1px solid #2b3a52}.a-info::before{background:#64748b}
.flowwrap{display:flex;gap:16px;flex-wrap:wrap;align-items:stretch;margin-top:8px}
.flowbox{background:#0b111c;border:1px solid #1c2740;border-radius:12px;padding:8px}
.mf-nums{display:flex;gap:20px;flex-wrap:wrap;margin-top:10px}
.mf-nums .k{color:#7b8aa6;font-size:12px}.mf-nums .val{font-size:20px;font-weight:700}
</style></head><body><div class="wrap">
<h1>估值 · 技术 · 基本面 分析台</h1>
<div class="sub">股票 / ETF / 指数通用 · 数据源：东方财富 + 腾讯证券 · AI 全部由 DeepSeek V4 Pro 驱动</div>
<div class="keybar" id="dsKeyBar">
 <span class="kb-label">🔑 DeepSeek Key</span>
 <input id="gkey" type="password" placeholder="粘贴 Key（sk-...），本地保存一次即可 · AI 助手/分析员/首席默认都用它">
 <button onclick="saveGKey()">保存</button>
 <span id="gkstat" class="sub"></span>
 <a class="klink" href="https://platform.deepseek.com" target="_blank">申请</a>
</div>
<div class="keybar" id="openAiKeyBar">
 <span class="kb-label">🔑 OpenAI Key <span style="font-weight:400;color:#64748b">可选</span></span>
 <input id="okey" type="password" placeholder="sk-... 仅当「投研团首席」选 GPT 时才需要（每次只调1次，成本≈几分钱）">
 <button onclick="saveOKey()">保存</button>
 <span id="okstat" class="sub"></span>
 <a class="klink" href="https://platform.openai.com/api-keys" target="_blank">申请</a>
</div>
<div class="tabnav">
 <button class="tabbtn active" data-tab="market" onclick="showTab('market')">📊 大盘</button>
 <button class="tabbtn" data-tab="analyze" onclick="showTab('analyze')">🔍 个股分析</button>
 <button class="tabbtn" data-tab="watch" onclick="showTab('watch')">⭐ 自选</button>
 <button class="tabbtn" data-tab="monitor" onclick="showTab('monitor')">⏱ 盯盘</button>
 <button class="tabbtn" data-tab="panel" onclick="showTab('panel')">🧑‍💼 多股对比</button>
</div>

<div id="tab-market" class="tabpage">
 <div class="card"><div class="sec-title">大盘指数 <span class="sub" id="mktTime" style="font-weight:400"></span>
   <span onclick="loadMarket(true)" style="float:right;color:#60a5fa;cursor:pointer;font-size:12px">↻ 刷新</span></div>
   <div id="mktIndices" class="grid">加载中…</div></div>
 <div class="card"><div class="sec-title">大盘强弱传导 · 粒子动效 <span class="sub" style="font-weight:400">（基于指数与板块涨跌幅估算，并非实时成交资金）</span></div>
   <div class="mkt-flow-wrap"><svg id="mktFlowSvg" role="img" aria-label="大盘强弱板块传导动效"></svg></div>
   <div class="mkt-flow-meta" id="mktFlowMeta"><span>正在整理市场强弱结构…</span></div></div>
 <div class="card"><div class="sec-title">板块轮动 · 今日资金往哪流 <span class="sub" style="font-weight:400">（按板块ETF今日涨跌排序）</span></div>
   <div id="sectorRotation"><div class="sub">加载中…</div></div></div>
 <div class="card"><div class="sec-title">AI 大盘解析报告</div>
   <div class="sub" style="margin-bottom:8px">使用你在顶部保存的 DeepSeek Key，自动生成一段基于今日大盘与板块数据的中文判断。</div>
   <button onclick="loadMarketAIReport()" id="mktAiBtn">生成 AI 报告</button>
   <div id="marketAiReport" class="market-report" style="display:none"></div></div>
</div>

<div id="tab-analyze" class="tabpage" style="display:none">
<div class="searchbar">
 <input id="code" placeholder="输入代码，如 600519" maxlength="6" onkeydown="if(event.key==='Enter')q()">
 <button onclick="q()">分析</button>
 <button class="g" id="xls" style="display:none" onclick="dl()">导出 Excel</button>
 <span id="status"></span>
</div>
<div class="chips">
 <span class="chip" onclick="ex('600519')">600519 贵州茅台</span>
 <span class="chip" onclick="ex('300750')">300750 宁德时代</span>
 <span class="chip" onclick="ex('510300')">510300 沪深300ETF</span>
 <span class="chip" onclick="ex('159915')">159915 创业板ETF</span>
 <span class="chip" onclick="ex('000858')">000858 五粮液</span>
</div>
<div id="result" style="display:none"></div>
<div id="multiDimCard" class="card" style="display:none;margin-top:12px">
 <div class="sec-title">多维分析 · 价值 / 技术 / 风控三视角</div>
 <div class="sub" style="margin-bottom:8px">分别由价值派、技术派、风控派对当前标的做独立调研，并给出各自报告。</div>
 <button onclick="runMultiDimAnalysis()" id="multidimBtn">启动多维分析</button>
 <div id="multidimResult" class="watch-list" style="margin-top:10px"></div>
</div>
<div id="hint" class="hint">输入一个代码开始分析。<br>支持沪深股票、ETF、指数；ETF/指数无 PE/PB，将以股价历史分位 + 技术面呈现。<br><span style="color:#60a5fa">右下角「AI 助手」可用大白话提问、多股对比与筛选。</span></div>
</div>

<div id="tab-watch" class="tabpage" style="display:none">
 <div class="card" id="watchCard">
  <div class="sec-title">⭐ 自选股 <span class="sub" style="font-weight:400">· 分组追踪 · 60 秒刷新</span><span onclick="refreshWatchQuotes(true)" style="float:right;color:#60a5fa;cursor:pointer;font-size:12px;font-weight:400">↻ 刷新行情</span></div>
 <div class="watch-addbar">
   <input id="wadd" maxlength="6" placeholder="加自选：6位代码" onkeydown="if(event.key==='Enter')addWatch()">
   <input id="wgroup" maxlength="20" list="watchGroupOptions" placeholder="分组，如 科技龙头" onkeydown="if(event.key==='Enter')addWatch()">
   <datalist id="watchGroupOptions"></datalist>
   <button onclick="addWatch()"><i data-lucide="plus"></i><span>添加</span></button>
   <span id="watchAddStatus" class="watch-add-status"></span>
 </div>
 <div id="watchList" class="watch-list"></div>
 </div>
 <dialog id="watchGroupDialog" class="watch-group-dialog">
  <div class="watch-group-dialog-head"><b id="watchGroupDialogTitle">调整分组</b><button type="button" onclick="closeWatchGroupDialog()" title="关闭" aria-label="关闭"><i data-lucide="x"></i></button></div>
  <div class="watch-group-dialog-body">
   <div id="watchGroupEditSecurity" class="watch-group-dialog-security"></div>
   <label for="watchGroupEdit">分组名称</label>
   <input id="watchGroupEdit" maxlength="20" list="watchGroupOptions" placeholder="留空则归入未分组" onkeydown="if(event.key==='Enter')saveWatchGroupChange()">
  </div>
  <div class="watch-group-dialog-actions"><button type="button" onclick="closeWatchGroupDialog()">取消</button><button type="button" onclick="saveWatchGroupChange()"><i data-lucide="folder-input"></i><span id="watchGroupSaveLabel">保存分组</span></button></div>
 </dialog>
</div>

<div id="tab-monitor" class="tabpage" style="display:none">
 <div class="monitor-runtime">
  <div class="monitor-state"><span id="monitorDot" class="monitor-dot"></span><b id="monitorStateText">读取运行状态…</b></div>
  <div id="monitorRuntimeMeta" class="monitor-runtime-meta"></div>
  <div class="monitor-actions">
   <button id="monitorStartBtn" onclick="monitorRuntime('start')" title="启动后台盯盘"><i data-lucide="play"></i><span>启动</span></button>
   <button id="monitorStopBtn" class="stop" onclick="monitorRuntime('stop')" title="暂停后台盯盘"><i data-lucide="pause"></i><span>暂停</span></button>
   <button id="monitorRunBtn" onclick="monitorRuntime('run_once')" title="按当前交易时段立即检查"><i data-lucide="refresh-cw"></i><span>检查</span></button>
  </div>
 </div>
 <div class="monitor-columns">
  <div class="card">
   <div class="sec-title">三线设置</div>
   <div class="monitor-form">
    <div class="monitor-field"><label for="monitorCode">代码</label><input id="monitorCode" maxlength="6" inputmode="numeric" placeholder="600519" onblur="hydrateMonitorName()"></div>
    <div class="monitor-field"><label for="monitorName">名称</label><input id="monitorName" maxlength="80" placeholder="自动识别或手工填写"></div>
    <div class="monitor-field"><label for="monitorWatchPrice">关注价</label><input id="monitorWatchPrice" type="number" min="0" step="0.01" placeholder="价格回落关注"></div>
    <div class="monitor-field"><label for="monitorRiskPrice">风险价</label><input id="monitorRiskPrice" type="number" min="0" step="0.01" placeholder="低于关注价"></div>
    <div class="monitor-field full"><label for="monitorTargetPrice">目标价（可选）</label><input id="monitorTargetPrice" type="number" min="0" step="0.01" placeholder="高于关注价"></div>
    <div class="monitor-form-foot">
     <button class="monitor-save" id="monitorSaveBtn" onclick="saveMonitorSetup()"><i data-lucide="save"></i><span>保存三线</span></button>
     <span id="monitorFormStatus" class="monitor-form-status">自动异动 ±3%</span>
    </div>
   </div>
  </div>
  <div class="card">
   <div class="sec-title">盯盘标的 <span class="sub" id="monitorChannels" style="font-weight:400"></span></div>
   <div id="monitorSummary" class="monitor-summary"></div>
   <div id="monitorWatchList" class="monitor-list"><div class="monitor-empty">正在读取…</div></div>
 </div>
 </div>
 <div class="card">
  <div class="sec-title">持仓逻辑转化 <span class="sub" id="monitorLogicStatus" style="font-weight:400"></span></div>
  <div class="monitor-logic-grid">
   <div class="monitor-logic-form">
    <div class="monitor-field"><label for="monitorLogicCode">代码</label><input id="monitorLogicCode" maxlength="6" inputmode="numeric" placeholder="600519"></div>
    <div class="monitor-field"><label for="monitorLogicName">名称</label><input id="monitorLogicName" maxlength="80" placeholder="可选"></div>
    <div class="monitor-field wide"><label for="monitorThesis">买入 / 持有逻辑</label><textarea id="monitorThesis" maxlength="2000" placeholder="只写你已经明确采用的判断和价格"></textarea></div>
    <div class="monitor-field wide"><label for="monitorInvalidation">失效条件</label><textarea id="monitorInvalidation" maxlength="1500" placeholder="哪些情况出现后，原逻辑不再成立"></textarea></div>
    <div class="monitor-field wide"><label for="monitorReviewItems">复核事项</label><textarea id="monitorReviewItems" maxlength="1500" placeholder="提醒触发后需要核实什么"></textarea></div>
    <div class="monitor-logic-actions">
     <button onclick="saveMonitorLogic()"><i data-lucide="save"></i><span>保存逻辑</span></button>
     <button id="monitorDraftBtn" onclick="draftMonitorRules()"><i data-lucide="sparkles"></i><span>AI 整理规则</span></button>
    </div>
   </div>
   <div id="monitorDraft" class="monitor-draft"><div class="monitor-draft-empty">先保存持仓逻辑，再按需整理自动规则</div></div>
  </div>
 </div>
 <div class="card">
  <div class="sec-title">最近提醒 <span class="sub" style="font-weight:400">· 本地事件记录</span></div>
  <div id="monitorPreview" class="monitor-preview"></div>
  <div id="monitorExplanation" class="monitor-explanation"></div>
  <div id="monitorEvents" class="monitor-events"><div class="monitor-empty">正在读取…</div></div>
 </div>
</div>

<div id="tab-panel" class="tabpage" style="display:none">
<div class="card" id="panelCard">
 <div class="sec-title">AI 多股对比 · 多分析员并行 + DeepSeek 首席汇总</div>
 <div class="sub" style="margin-bottom:10px">输入 2-6 个代码（空格或逗号分隔），每只股票由一位分析员独立点评，最后由首席汇总给出横向对比结论。</div>
 <div class="searchbar" style="margin:0 0 6px">
   <input id="pcodes" style="width:320px" placeholder="如：600519 000858 300750">
   <input id="pgoal" style="width:240px" placeholder="目标(可选)：如 挑估值最低的">
   <button onclick="runPanel()" id="pbtn">召集投研团</button>
 </div>
 <div style="margin:0 0 8px;font-size:13px;color:#93a4bf;display:flex;align-items:center;gap:8px;flex-wrap:wrap">首席汇总用：
   <select id="chiefSel" onchange="g('gptModelWrap').style.display=this.value==='gpt'?'inline':'none'" style="background:#141b28;color:#eaf1fb;border:1px solid #2b3a52;border-radius:8px;padding:6px 10px;font-size:13px">
     <option value="deepseek">DeepSeek V4 Pro（默认 · 最省）</option>
     <option value="gpt">GPT（更强 · 每次仅多 1 次调用 ≈ 几分钱）</option>
   </select>
   <span id="gptModelWrap" style="display:none">GPT 模型：<input id="gptModel" value="gpt-4.1-mini" style="width:170px;font-size:13px;padding:5px 9px" title="填你 OpenAI 账号里可用的确切模型名">
     <span class="sub" style="font-size:11px">省:gpt-4.1-mini｜强:gpt-5.6-terra/sol</span></span>
 </div>
 <div class="sub" style="font-size:12px;margin-bottom:4px">分析员固定「价值派 / 技术派 / 风控派」三视角、用 DeepSeek；首席可切 GPT（需在顶部填 OpenAI Key）。</div>
 <div id="panelOut"></div>
</div>
</div>
<div class="disc">本工具所有结论均由公开数据按固定规则自动计算，仅供学习研究，不构成任何投资建议。据此操作风险自负。</div>
</div>
<button id="fab" onclick="toggleChat(true)">🤖 AI 助手</button>
<div id="chat">
 <div class="ch-head"><b>🤖 AI 助手 · DeepSeek</b><span class="x" onclick="toggleChat(false)">×</span></div>
 <div class="keyrow" style="font-size:12px;color:#7b8aa6">
   <span id="chatkeystat">使用页面顶部保存的 DeepSeek Key</span>
 </div>
 <div class="ex-q">试试：<span onclick="ask('茅台现在估值贵不贵？')">茅台贵不贵</span> ·
   <span onclick="ask('600519和000858哪个更便宜')">茅台vs五粮液</span> ·
   <span onclick="ask('从600519 300750 000858里挑风险最低的')">三选一挑风险最低</span></div>
 <div class="msgs" id="msgs"></div>
 <div class="inrow">
   <textarea id="cin" placeholder="输入问题，回车发送…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"></textarea>
   <button onclick="send()" id="sendBtn">发送</button>
 </div>
</div>
<script>
let cur="";
const g=id=>document.getElementById(id);
function ex(c){showTab('analyze');g('code').value=c;q();window.scrollTo({top:0,behavior:'smooth'});}
/* ===================== 自选股 ===================== */
const watchQuotes=new Map();let watchQuoteLoading=false,watchGroupEditingCode='',watchGroupEditingName='';
const WATCH_UNGROUPED='未分组',WATCH_GROUP_HISTORY_KEY='watch_group_history_v1',WATCH_GROUP_HISTORY_LIMIT=72;
function escHtml(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function normalizeWatchGroup(v){const name=String(v??'').trim().replace(/\s+/g,' ').slice(0,20);return name||WATCH_UNGROUPED;}
function loadWatch(){try{const raw=JSON.parse(localStorage.getItem('watchlist')||'[]');return Array.isArray(raw)?raw.map(x=>({code:String(x.code||'').trim(),name:String(x.name||'').trim(),group:normalizeWatchGroup(x.group)})).filter(x=>/^\d{6}$/.test(x.code)):[];}catch(e){return[];}}
function groupWatchItems(w){const groups=new Map();w.forEach(x=>{const name=normalizeWatchGroup(x.group);if(!groups.has(name))groups.set(name,[]);groups.get(name).push(x);});return groups;}
function renderWatchGroupOptions(w){const el=g('watchGroupOptions');if(!el)return;el.innerHTML=[...groupWatchItems(w).keys()].filter(x=>x!==WATCH_UNGROUPED).map(x=>`<option value="${escHtml(x)}"></option>`).join('');}
function loadWatchGroupHistory(){try{const raw=JSON.parse(localStorage.getItem(WATCH_GROUP_HISTORY_KEY)||'{}');return raw&&raw.version===1&&raw.groups&&typeof raw.groups==='object'?raw:{version:1,groups:{}};}catch(e){return{version:1,groups:{}};}}
function saveWatchGroupHistory(history){try{localStorage.setItem(WATCH_GROUP_HISTORY_KEY,JSON.stringify(history));}catch(e){}}
function renameWatchGroupHistory(previousGroup,nextGroup){if(previousGroup===nextGroup)return;const history=loadWatchGroupHistory(),previousKey=watchGroupKey(previousGroup),nextKey=watchGroupKey(nextGroup),entry=history.groups[previousKey];if(!entry||history.groups[nextKey])return;history.groups[nextKey]={...entry,name:nextGroup};delete history.groups[previousKey];saveWatchGroupHistory(history);}
function watchLocalDate(ts=Date.now()){const d=new Date(ts),m=String(d.getMonth()+1).padStart(2,'0'),day=String(d.getDate()).padStart(2,'0');return `${d.getFullYear()}-${m}-${day}`;}
function watchGroupKey(name){return 'g:'+encodeURIComponent(name);}
function watchGroupStats(items){const values=items.map(x=>Number((watchQuotes.get(x.code)||{}).chg)).filter(Number.isFinite),sorted=[...values].sort((a,b)=>a-b),n=values.length,mid=Math.floor(n/2);return{total:items.length,valid:n,avg:n?values.reduce((a,b)=>a+b,0)/n:null,median:n?(n%2?sorted[mid]:(sorted[mid-1]+sorted[mid])/2):null,up:values.filter(v=>v>0).length,upRatio:n?values.filter(v=>v>0).length/n:null};}
function watchGroupEntry(history,name,items){const key=watchGroupKey(name),members=items.map(x=>x.code).sort().join(','),entry=history.groups[key];return entry&&entry.date===watchLocalDate()&&entry.members===members?entry:null;}
function recordWatchGroupSnapshots(w,force){const history=loadWatchGroupHistory(),groups=groupWatchItems(w),now=Date.now(),active=new Set();groups.forEach((items,name)=>{const key=watchGroupKey(name),members=items.map(x=>x.code).sort().join(','),stats=watchGroupStats(items);active.add(key);if(!stats.valid)return;let entry=watchGroupEntry(history,name,items)||{name,date:watchLocalDate(now),members,samples:[]};const sample={ts:now,avg:Number(stats.avg.toFixed(4)),median:Number(stats.median.toFixed(4)),upRatio:Number(stats.upRatio.toFixed(4)),valid:stats.valid,total:stats.total},last=entry.samples[entry.samples.length-1];if(force||!last||now-last.ts>=60000)entry.samples.push(sample);else entry.samples[entry.samples.length-1]=sample;entry.samples=entry.samples.slice(-WATCH_GROUP_HISTORY_LIMIT);history.groups[key]=entry;});Object.keys(history.groups).forEach(key=>{if(!active.has(key)||history.groups[key].date!==watchLocalDate(now))delete history.groups[key];});saveWatchGroupHistory(history);}
function saveWatch(w){const clean=w.map(x=>({code:String(x.code||'').trim(),name:String(x.name||'').trim(),group:normalizeWatchGroup(x.group)})).filter(x=>/^\d{6}$/.test(x.code));localStorage.setItem('watchlist',JSON.stringify(clean));recordWatchGroupSnapshots(clean,false);renderWatch();}
function watchTone(q){const chg=Number(q&&q.chg);return !Number.isFinite(chg)?'watch-flat':(chg>0?'watch-up':(chg<0?'watch-down':'watch-flat'));}
function signedNumber(v,suffix=''){const n=Number(v);if(!Number.isFinite(n))return '—';return (n>0?'+':'')+String(v)+suffix;}
function watchPct(v){const n=Number(v);return Number.isFinite(n)?signedNumber(n.toFixed(2),'%'):'—';}
function watchPoint(v){const n=Number(v);return Number.isFinite(n)?signedNumber(n.toFixed(2),'点'):'建立基线';}
function watchGroupTrend(name,items,stats){const entry=watchGroupEntry(loadWatchGroupHistory(),name,items),samples=entry&&Array.isArray(entry.samples)?entry.samples:[],base=samples[0];return{samples,delta:base&&Number.isFinite(stats.avg)?stats.avg-base.avg:null,breadthDelta:base&&Number.isFinite(stats.upRatio)?stats.upRatio-base.upRatio:null};}
function watchGroupSignal(stats,trend){if(stats.valid<2)return{label:'样本不足',kind:'',title:'至少需要 2 只有效行情才能判断组内同步性'};if(trend.samples.length>=2&&trend.delta>=.3&&trend.breadthDelta>=.2)return{label:'同步回暖',kind:'warming',title:'组均涨幅较今日首次记录提升至少 0.3 个百分点，且上涨占比提升至少 20 个百分点'};if(trend.samples.length>=2&&(trend.delta>=.3||trend.breadthDelta>=.2))return{label:'回升观察',kind:'rising',title:'组均涨幅或上涨占比较今日首次记录明显回升，但尚未同时满足'};if(stats.avg>=.5&&stats.upRatio>=.6)return{label:'整体偏强',kind:'strong',title:'当前组均涨幅至少 0.5%，且上涨标的占比至少 60%'};if(stats.avg<=-.5&&stats.upRatio<=.4)return{label:'整体承压',kind:'pressured',title:'当前组均涨幅不高于 -0.5%，且上涨标的占比不高于 40%'};return{label:'分化 / 平稳',kind:'',title:'当前组内强弱不一，或变化尚未达到观察阈值'};}
function watchGroupSparkline(samples){if(samples.length<2)return'<span class="watch-track-empty">基线待更新</span>';const values=samples.map(x=>Number(x.avg)).filter(Number.isFinite);if(values.length<2)return'<span class="watch-track-empty">基线待更新</span>';const width=96,height=30,pad=3,min=Math.min(...values),max=Math.max(...values),range=Math.max(max-min,.01),points=values.map((v,i)=>`${(pad+i*(width-pad*2)/(values.length-1)).toFixed(1)},${(height-pad-(v-min)*(height-pad*2)/range).toFixed(1)}`).join(' '),tone=values[values.length-1]>=values[0]?'#f2495c':'#2ec26e',zero=min<=0&&max>=0?(height-pad-(0-min)*(height-pad*2)/range).toFixed(1):null;return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="组均涨幅今日轨迹">${zero?`<line class="watch-track-zero" x1="${pad}" x2="${width-pad}" y1="${zero}" y2="${zero}"></line>`:''}<polyline class="watch-track-line" points="${points}" stroke="${tone}"></polyline></svg>`;}
function renderWatch(){const w=loadWatch(),el=g('watchList');if(!el)return;
 renderWatchGroupOptions(w);
 if(!w.length){el.innerHTML='<div class="sub" style="font-size:12px;padding:12px">还没有自选股。上方输入代码添加，或分析某只后点「★ 加自选」。</div>';return;}
 const head='<div class="watch-head"><div class="watch-col">名称 / 代码</div><div class="watch-col">最新</div><div class="watch-col">涨幅</div><div class="watch-col delta">涨跌</div><div class="watch-col tools">操作</div></div>';
 el.innerHTML=head+[...groupWatchItems(w)].map(([group,items])=>{const stats=watchGroupStats(items),trend=watchGroupTrend(group,items,stats),signal=watchGroupSignal(stats,trend),tone=!Number.isFinite(stats.avg)?'watch-flat':(stats.avg>0?'watch-up':(stats.avg<0?'watch-down':'watch-flat')),delta=trend.samples.length>=2?watchPoint(trend.delta):'建立基线';return `<section class="watch-group-section">
  <div class="watch-group-summary">
   <div class="watch-group-identity"><div class="watch-group-name"><i data-lucide="folder-kanban"></i><span>${escHtml(group)}</span><button class="watch-group-edit" data-group="${escHtml(group)}" onclick="event.stopPropagation();openRenameWatchGroupDialog(this.dataset.group)" title="编辑分组名称" aria-label="编辑分组名称：${escHtml(group)}"><i data-lucide="pencil"></i></button></div><span class="watch-group-count">${items.length} 只 · 今日 ${trend.samples.length} 次记录</span></div>
   <div class="watch-group-metric"><label>组均涨幅</label><strong class="${tone}">${escHtml(watchPct(stats.avg))}</strong></div>
   <div class="watch-group-metric"><label>中位涨幅</label><strong class="${!Number.isFinite(stats.median)?'watch-flat':(stats.median>0?'watch-up':(stats.median<0?'watch-down':'watch-flat'))}">${escHtml(watchPct(stats.median))}</strong></div>
   <div class="watch-group-metric"><label>上涨家数</label><strong>${stats.valid?`${stats.up}/${stats.valid}`:'—'}</strong></div>
   <div class="watch-group-metric watch-track" title="组均涨幅今日轨迹">${watchGroupSparkline(trend.samples)}</div>
   <div class="watch-signal ${signal.kind}" title="${escHtml(signal.title)}">${escHtml(signal.label)} · ${escHtml(delta)}</div>
  </div>
  ${items.map(x=>{const q=watchQuotes.get(x.code),rowTone=watchTone(q),name=x.name||(q&&q.name)||'正在获取名称…';return `<div class="watch-row" onclick="ex('${x.code}')">
   <div class="watch-security"><div class="watch-name">${escHtml(name)}</div><div class="watch-code">${escHtml(x.code)}</div></div>
   <div class="watch-num ${rowTone}">${q&&q.price!=null?escHtml(q.price):'—'}</div>
   <div class="watch-num ${rowTone}">${q&&q.chg!=null?escHtml(signedNumber(q.chg,'%')):'—'}</div>
   <div class="watch-num watch-delta ${rowTone}">${q&&q.change!=null?escHtml(signedNumber(q.change)):'—'}</div>
   <div class="watch-tools"><button class="watch-icon-btn" onclick="event.stopPropagation();ex('${x.code}')" title="分析" aria-label="分析 ${escHtml(name)}"><i data-lucide="chart-no-axes-combined"></i></button><button class="watch-icon-btn" onclick="event.stopPropagation();openWatchGroupDialog('${x.code}')" title="调整分组" aria-label="调整 ${escHtml(name)} 分组"><i data-lucide="folder-input"></i></button><button class="watch-icon-btn remove" onclick="event.stopPropagation();rmWatch('${x.code}')" title="移除" aria-label="移除 ${escHtml(name)}"><i data-lucide="trash-2"></i></button></div>
  </div>`;}).join('')}</section>`;}).join('');
 refreshLucide();
}
function setWatchAddStatus(text){const el=g('watchAddStatus');if(el)el.textContent=text||'';}
async function refreshWatchQuotes(force){
 const w=loadWatch();if(!w.length||watchQuoteLoading)return;
 watchQuoteLoading=true;renderWatch();
 try{
  const codes=w.map(x=>x.code).join(','),d=await fetch('/api/watch_quotes?codes='+encodeURIComponent(codes),{cache:'no-store'}).then(r=>r.json());
  let namesChanged=false;(d.quotes||[]).forEach(q=>{if(!q||!/^\d{6}$/.test(String(q.code||'')))return;watchQuotes.set(q.code,q);const item=w.find(x=>x.code===q.code),name=String(q.name||'').trim();if(item&&name&&item.name!==name){item.name=name;namesChanged=true;}});
  if(namesChanged)localStorage.setItem('watchlist',JSON.stringify(w));
  recordWatchGroupSnapshots(w,!!force);
 }catch(e){}finally{watchQuoteLoading=false;renderWatch();}
}
function hydrateWatchNames(){refreshWatchQuotes();}
async function addWatch(code){const fromForm=!code,rawGroup=fromForm&&g('wgroup')?g('wgroup').value.trim():'';code=(code||g('wadd').value).trim();
 if(!/^\d{6}$/.test(code)){alert('请输入6位代码');return;}
 let w=loadWatch(),existing=w.find(x=>x.code===code);if(existing){if(rawGroup&&existing.group!==normalizeWatchGroup(rawGroup)){existing.group=normalizeWatchGroup(rawGroup);saveWatch(w);setWatchAddStatus(`已移入 ${existing.group}`);}else setWatchAddStatus(`已在 ${existing.group}`);if(g('wadd'))g('wadd').value='';return;}
 const group=normalizeWatchGroup(rawGroup);w.push({code,name:'',group});saveWatch(w);setWatchAddStatus(`已加入 ${group}`);refreshWatchQuotes();if(g('wadd'))g('wadd').value='';}
function rmWatch(code){watchQuotes.delete(code);saveWatch(loadWatch().filter(x=>x.code!==code));}
function showWatchGroupDialog(){const dialog=g('watchGroupDialog');if(!dialog)return;if(typeof dialog.showModal==='function')dialog.showModal();else dialog.setAttribute('open','');g('watchGroupEdit').focus();refreshLucide();}
function openWatchGroupDialog(code){const item=loadWatch().find(x=>x.code===code);if(!item)return;watchGroupEditingCode=code;watchGroupEditingName='';g('watchGroupDialogTitle').textContent='调整分组';g('watchGroupSaveLabel').textContent='保存分组';g('watchGroupEditSecurity').textContent=`${item.name||code} · ${code}`;g('watchGroupEdit').placeholder='留空则归入未分组';g('watchGroupEdit').value=item.group===WATCH_UNGROUPED?'':item.group;showWatchGroupDialog();}
function openRenameWatchGroupDialog(name){const group=normalizeWatchGroup(name),items=groupWatchItems(loadWatch()).get(group)||[];if(!items.length)return;watchGroupEditingCode='';watchGroupEditingName=group;g('watchGroupDialogTitle').textContent='编辑分组名称';g('watchGroupSaveLabel').textContent='保存名称';g('watchGroupEditSecurity').textContent=`${group} · ${items.length} 只自选`;g('watchGroupEdit').placeholder='输入分组名称';g('watchGroupEdit').value=group;showWatchGroupDialog();}
function closeWatchGroupDialog(){const dialog=g('watchGroupDialog');watchGroupEditingCode='';watchGroupEditingName='';if(!dialog)return;if(typeof dialog.close==='function'&&dialog.open)dialog.close();else dialog.removeAttribute('open');}
function saveWatchGroupChange(){const w=loadWatch(),nextGroup=normalizeWatchGroup(g('watchGroupEdit').value);if(watchGroupEditingName){const previousGroup=watchGroupEditingName,merging=previousGroup!==nextGroup&&w.some(x=>normalizeWatchGroup(x.group)===nextGroup);if(!merging)renameWatchGroupHistory(previousGroup,nextGroup);w.forEach(x=>{if(normalizeWatchGroup(x.group)===previousGroup)x.group=nextGroup;});saveWatch(w);setWatchAddStatus(previousGroup===nextGroup?`${previousGroup} 名称未变`:(merging?`${previousGroup} 已合并到 ${nextGroup}`:`${previousGroup} 已重命名为 ${nextGroup}`));closeWatchGroupDialog();return;}const item=w.find(x=>x.code===watchGroupEditingCode);if(!item){closeWatchGroupDialog();return;}item.group=nextGroup;saveWatch(w);setWatchAddStatus(`${item.name||item.code} 已移入 ${item.group}`);closeWatchGroupDialog();}
function inWatch(code){return loadWatch().some(x=>x.code===code);}

/* ===================== 盯盘 ===================== */
let monitorData=null,monitorBusy=false,monitorLoaded=false,monitorPreview=null,monitorDraft=null,monitorExplanation=null,monitorLogicSaved=false,monitorDraftPending=false;
function monitorFmt(v){if(v===null||v===undefined||v==='')return '未设置';const n=Number(v);return Number.isFinite(n)?String(v):'未设置';}
function monitorMove(v){return v===null||v===undefined||v===''?'异动 未设置':`异动 ±${escHtml(monitorFmt(v))}%`;}
function monitorTime(v){if(!v)return '—';const d=new Date(v);return Number.isNaN(d.getTime())?escHtml(v):d.toLocaleString('zh-CN',{hour12:false});}
function refreshLucide(){if(window.lucide)window.lucide.createIcons();}
async function monitorRequest(path,options){
 const r=await fetch(path,{cache:'no-store',...(options||{})}),d=await r.json();
 if(d.error)throw new Error(d.error);return d;
}
function renderMonitor(){
 if(!monitorData)return;
 const runtime=monitorData.runtime||{},settings=monitorData.settings||{},counts=monitorData.counts||{},summary=runtime.last_summary||{};
 g('monitorDot').classList.toggle('running',!!runtime.running);
 g('monitorStateText').textContent=runtime.running?'盯盘运行中':'盯盘未运行';
 const runText=summary.skipped_reason?summary.skipped_reason:(summary.observed_at?`最近检查 ${monitorTime(summary.observed_at)} · 触发 ${summary.triggered_count||0}`:'尚未执行检查');
 g('monitorRuntimeMeta').textContent=`${runText} · 每 ${settings.poll_seconds||60} 秒 · 单轮 ${settings.token_usage_per_poll||0} Token${runtime.last_error?' · '+runtime.last_error:''}`;
 g('monitorStartBtn').disabled=!!runtime.running||monitorBusy;g('monitorStopBtn').disabled=!runtime.running||monitorBusy;g('monitorRunBtn').disabled=monitorBusy;
 g('monitorChannels').textContent='· '+(settings.channels||[]).join(' · ');
 g('monitorSummary').innerHTML=`<span class="monitor-kpi">标的<strong>${counts.watches||0}</strong></span><span class="monitor-kpi">规则<strong>${counts.rules||0}</strong></span><span class="monitor-kpi">事件<strong>${counts.events||0}</strong></span><span class="monitor-kpi">分钟数据<strong>${settings.minute_retention_days||7} 天</strong></span>`;
 const watches=monitorData.watches||[],list=g('monitorWatchList');
 if(!watches.length)list.innerHTML='<div class="monitor-empty">暂无盯盘标的</div>';
 else list.innerHTML=watches.map(w=>`<div class="monitor-row">
  <div class="monitor-security"><b>${escHtml(w.name||'未命名')}</b><span>${escHtml(w.code)}</span></div>
  <div class="monitor-price"><label>关注</label><strong>${escHtml(monitorFmt(w.watch_price))}</strong></div>
  <div class="monitor-price"><label>风险</label><strong>${escHtml(monitorFmt(w.risk_price))}</strong></div>
  <div class="monitor-price target"><label>目标</label><strong>${escHtml(monitorFmt(w.target_price))}</strong></div>
  <div class="monitor-move">${monitorMove(w.move_percent)}${w.logic&&w.logic.configured?'<br><span class="sub">逻辑已保存</span>':''}${w.advanced_rule_count?`<br><span class="sub">高级 ${w.advanced_rule_count}</span>`:''}</div>
  <div class="monitor-row-actions">${w.configured?`<button class="monitor-icon-btn" onclick="simulateMonitorRisk('${w.code}')" title="预览风险提醒" aria-label="预览风险提醒"><i data-lucide="bell-ring"></i></button>`:''}<button class="monitor-icon-btn" onclick="editMonitorWatch('${w.code}')" title="编辑三线" aria-label="编辑三线"><i data-lucide="pencil"></i></button><button class="monitor-icon-btn remove" onclick="removeMonitorWatch('${w.code}')" title="移除盯盘" aria-label="移除盯盘"><i data-lucide="trash-2"></i></button></div>
 </div>`).join('');
 const previewEl=g('monitorPreview');
 if(!monitorPreview){previewEl.className='monitor-preview';previewEl.innerHTML='';}
 else{previewEl.className='monitor-preview visible';previewEl.innerHTML=`<div class="monitor-preview-title">${escHtml(monitorPreview.name||monitorPreview.code)}<span>${escHtml(monitorPreview.code)} · 未入库 · 未发送 · 0 Token</span></div><div class="monitor-preview-message">${escHtml(monitorPreview.message||'')}</div><span class="monitor-tag simulated">模拟</span>`;}
 renderMonitorDraft();
 const explanationEl=g('monitorExplanation');
 if(!monitorExplanation){explanationEl.className='monitor-explanation';explanationEl.innerHTML='';}
 else{
  const relation={supports:'与原逻辑相符',contradicts:'与原逻辑冲突',neutral:'信息不足'}[monitorExplanation.relation]||'待复核';
  const explainItems=(items,empty)=>items&&items.length?items.map(x=>'· '+escHtml(x)).join('<br>'):empty;
  explanationEl.className='monitor-explanation visible';
  explanationEl.innerHTML=`<div class="monitor-explanation-head"><b>${escHtml(relation)}</b><span class="monitor-tag simulated">${monitorExplanation.cached?'缓存':'AI 复核'}</span><span class="monitor-explanation-meta">本次 ${monitorExplanation.token_usage||0} Token · 今日 ${monitorExplanation.daily_usage&&monitorExplanation.daily_usage.calls||0} 次</span></div><div class="monitor-explanation-body">${escHtml(monitorExplanation.summary||'')}</div><div class="monitor-explanation-cols"><div class="monitor-explanation-col"><b>与原逻辑的关系</b><div>${explainItems(monitorExplanation.logic_matches,'未发现明确对应项')}</div></div><div class="monitor-explanation-col"><b>失效条件复核</b><div>${explainItems(monitorExplanation.invalidation_checks,'没有已保存的明确检查项')}</div></div><div class="monitor-explanation-col"><b>需要核实</b><div>${explainItems(monitorExplanation.review_questions,'暂无')}</div></div><div class="monitor-explanation-col"><b>信息边界</b><div>${explainItems(monitorExplanation.limitations,'暂无')}</div></div></div>`;
 }
 const events=monitorData.events||[],eventsEl=g('monitorEvents');
 if(!events.length)eventsEl.innerHTML='<div class="monitor-empty">暂无触发提醒</div>';
 else eventsEl.innerHTML=events.map(e=>`<div class="monitor-event">
  <div class="monitor-event-time">${monitorTime(e.occurred_at)}</div>
  <div class="monitor-event-security">${escHtml(e.name||e.code)}<span>${escHtml(e.code)} · ${escHtml(e.notification_status)}</span></div>
  <div class="monitor-event-message">${escHtml(String(e.message||'').split('\n').slice(1,4).join('\n'))}</div>
  <div class="monitor-event-actions"><span class="monitor-tag ${escHtml(e.direction)}">${e.direction==='buy'?'关注':(e.direction==='sell'?'风险/目标':'异动')}</span><button class="monitor-icon-btn" onclick="explainMonitorEvent(${Number(e.id)})" title="AI 复核提醒" aria-label="AI 复核提醒"><i data-lucide="sparkles"></i></button></div>
 </div>`).join('');
 refreshLucide();
}
async function loadMonitor(force){
 if(monitorBusy&&!force)return;monitorBusy=true;
 try{monitorData=await monitorRequest('/api/monitor/overview');monitorLoaded=true;renderMonitor();}
 catch(e){g('monitorStateText').textContent='盯盘状态读取失败';g('monitorRuntimeMeta').textContent=e.message;}
 finally{monitorBusy=false;if(monitorData)renderMonitor();}
}
async function hydrateMonitorName(){
 const code=g('monitorCode').value.trim();if(!/^\d{6}$/.test(code)||g('monitorName').value.trim())return;
 try{const d=await monitorRequest('/api/name?code='+encodeURIComponent(code));if(d.name)g('monitorName').value=d.name;}catch(e){}
}
function editMonitorWatch(code){
 const w=(monitorData&&monitorData.watches||[]).find(x=>x.code===code);if(!w)return;
 g('monitorCode').value=w.code;g('monitorName').value=w.name||'';g('monitorWatchPrice').value=w.watch_price??'';g('monitorRiskPrice').value=w.risk_price??'';g('monitorTargetPrice').value=w.target_price??'';g('monitorCode').focus();
 g('monitorLogicCode').value=w.code;g('monitorLogicName').value=w.name||'';g('monitorThesis').value=w.logic&&w.logic.thesis||'';g('monitorInvalidation').value=w.logic&&w.logic.invalidation||'';g('monitorReviewItems').value=w.logic&&w.logic.review_items||'';
 monitorLogicSaved=!!(w.logic&&w.logic.configured);monitorDraft=null;renderMonitorDraft();
}
async function saveMonitorSetup(){
 const payload={code:g('monitorCode').value.trim(),name:g('monitorName').value.trim(),watch_price:g('monitorWatchPrice').value,risk_price:g('monitorRiskPrice').value,target_price:g('monitorTargetPrice').value};
 const btn=g('monitorSaveBtn'),status=g('monitorFormStatus');btn.disabled=true;status.textContent='保存中…';
 try{await monitorRequest('/api/monitor/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});status.textContent='已保存 · 自动异动 ±3%';await loadMonitor(true);}
 catch(e){status.textContent=e.message;status.style.color='#fca5a5';}
 finally{btn.disabled=false;setTimeout(()=>{status.style.color='';},2400);}
}
async function removeMonitorWatch(code){
 if(!confirm('移除 '+code+' 的盯盘设置和规则？'))return;
 try{await monitorRequest('/api/monitor/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});await loadMonitor(true);}
 catch(e){g('monitorRuntimeMeta').textContent=e.message;}
}
async function simulateMonitorRisk(code){
 try{monitorPreview=await monitorRequest('/api/monitor/simulate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,kind:'risk'})});renderMonitor();g('monitorPreview').scrollIntoView({behavior:'smooth',block:'nearest'});}
 catch(e){g('monitorRuntimeMeta').textContent=e.message;}
}
function monitorLogicPayload(){
 const code=g('monitorLogicCode').value.trim(),name=g('monitorLogicName').value.trim(),thesis=g('monitorThesis').value.trim(),invalidation=g('monitorInvalidation').value.trim(),review_items=g('monitorReviewItems').value.trim();
 return {code,name,thesis,invalidation,review_items};
}
async function saveMonitorLogic(silent){
 const payload=monitorLogicPayload(),status=g('monitorLogicStatus');
 if(!/^\d{6}$/.test(payload.code)){status.textContent='· 请输入 6 位代码';return null;}
 try{const d=await monitorRequest('/api/monitor/logic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),logic=d.logic||d;monitorLogicSaved=logic.logic_saved!==false;if(!silent){status.textContent='· 逻辑卡已保存';monitorDraft={...logic,logic_saved:true,rules:[],auto_rule_count:0};}await loadMonitor(true);return logic;}
 catch(e){status.textContent='· '+e.message;return null;}
}
function renderMonitorDraft(){
 const box=g('monitorDraft');if(!box)return;
 if(!monitorDraft){box.innerHTML=`<div class="monitor-draft-empty">${monitorLogicSaved?'逻辑卡已保存，可继续让 AI 整理价格规则':'先保存持仓逻辑，再按需整理自动规则'}</div>`;return;}
 const operators={lte:'≤',lt:'<',gte:'≥',gt:'>',crosses_above:'上穿',crosses_below:'下穿'};
 const allRules=Array.isArray(monitorDraft.rules)?monitorDraft.rules:[],basicMetrics=['price','change_pct','change_amount'],rules=allRules.filter(r=>r&&basicMetrics.includes(r.metric));
 const hiddenRules=allRules.filter(r=>!r||!basicMetrics.includes(r.metric)),legacyConfirm=Array.isArray(monitorDraft.needs_confirmation)?monitorDraft.needs_confirmation:[];
 const next=monitorDraft.next_question||(legacyConfirm[0]?{id:'legacy',text:legacyConfirm[0],confirmation:null}:null);
 const technicalDetail=/\b(?:rsi|macd|confirm_count|cooldown_seconds|hysteresis)\b/i;
 const asItems=value=>Array.isArray(value)?value:(value?[value]:[]);
 const manual=[...asItems(monitorDraft.manual_review_items),...asItems(monitorDraft.manual_review),...asItems(monitorDraft.unsupported_conditions),...asItems(monitorDraft.unsupported),...hiddenRules.map(()=>'高级指标条件已归入人工复核，基础盯盘不会自动执行')].map(x=>technicalDetail.test(String(x))?'高级指标条件已归入人工复核，基础盯盘不会自动执行':String(x)).filter((x,i,a)=>x&&a.indexOf(x)===i);
 const periodic=monitorDraft.periodic_review_items||[];
 const logicSaved=monitorDraft.logic_saved!==false&&(monitorLogicSaved||monitorDraft.logic_saved===true);
 const autoCount=Number.isFinite(Number(monitorDraft.auto_rule_count))?Number(monitorDraft.auto_rule_count):rules.length;
 const metricLabels={price:'价格',change_pct:'当日涨跌幅',change_amount:'当日涨跌额'};
 const ruleLabel=r=>r.metric!=='price'?'异动':(r.direction==='buy'?'关注价':(r.direction==='sell'&&['gte','gt','crosses_above'].includes(r.operator)?'目标价':'风险价'));
 const list=(items,empty)=>items.length?items.map(x=>'· '+escHtml(x)).join('<br>'):empty;
 const summary=!rules.length?'持仓逻辑已保存；当前没有自动启用任何规则。':'已整理出待你核对的价格规则草案。';
 const draftMeta=monitorDraft.model==='deterministic-history'?'本地历史价格识别 · 0 Token':(monitorDraft.cached?'7 天缓存复用 · 0 Token':'本次 AI 整理');
 box.innerHTML=`<div class="monitor-draft-summary">${escHtml(summary)}</div><div class="monitor-draft-meta">${draftMeta} · 草案不会自动启用</div><div class="monitor-draft-outcomes"><div class="monitor-draft-outcome"><span>逻辑卡</span><strong class="${logicSaved?'ok':''}">${logicSaved?'已保存':'未保存'}</strong></div><div class="monitor-draft-outcome"><span>自动规则草案</span><strong>${autoCount} 条</strong></div><div class="monitor-draft-outcome"><span>待确认</span><strong>${next?'1 个关键问题':'0 个'}</strong></div></div>${rules.length?`<div class="monitor-draft-list">${rules.map(r=>`<div class="monitor-draft-rule"><span class="monitor-tag ${escHtml(r.direction)}">${escHtml(ruleLabel(r))}</span><b>${escHtml(r.name||ruleLabel(r))}</b><span>${escHtml(metricLabels[r.metric]||r.metric)} ${escHtml(operators[r.operator]||'到达')} ${escHtml(r.threshold)}</span></div>`).join('')}</div>`:'<div class="monitor-draft-empty">逻辑已保留，暂时没有可自动执行的轻量行情规则</div>'}${manual.length?`<div class="monitor-draft-review"><b>人工复核事项</b><div>${list(manual,'暂无')}</div></div>`:''}${periodic.length?`<div class="monitor-draft-review"><b>定期复核事项</b><div>${list(periodic,'暂无')}</div></div>`:''}${next?`<div class="monitor-draft-confirm"><b>还差一个关键确认</b><p>${escHtml(next.text||'请确认是否继续生成待确认风险规则。')}</p></div>`:''}<div class="monitor-draft-actions">${next&&next.confirmation?`<button id="monitorConfirmDraftBtn" onclick="continueMonitorDraft()" ${monitorDraftPending?'disabled':''}><i data-lucide="scan-search"></i><span>${monitorDraftPending?'正在生成…':'生成待确认风险规则'}</span></button>`:''}${rules.some(r=>r.metric==='price')?'<button class="monitor-draft-apply" onclick="applyMonitorDraft()"><i data-lucide="arrow-up-to-line"></i><span>填入三线</span></button>':''}</div>`;
 refreshLucide();
}
function monitorLogicText(payload){
 const parts=[];if(payload.thesis)parts.push('买入或持有逻辑：'+payload.thesis);if(payload.invalidation)parts.push('失效条件：'+payload.invalidation);if(payload.review_items)parts.push('复核事项：'+payload.review_items);return parts.join('\n');
}
async function draftMonitorRules(confirmation){
 const payload=monitorLogicPayload(),status=g('monitorLogicStatus'),btn=g('monitorDraftBtn');
 if(!/^\d{6}$/.test(payload.code)){status.textContent='· 请输入 6 位代码';return;}
 const logicText=monitorLogicText(payload);if(!logicText){status.textContent='· 请先填写逻辑';return;}
 const key=(localStorage.getItem('ds_key')||'').trim();btn.disabled=true;monitorDraftPending=!!confirmation;status.textContent=confirmation?'· 正在生成待确认风险规则…':'· 正在整理…';renderMonitorDraft();
 try{const saved=await saveMonitorLogic(true);if(!saved)return;monitorDraft=await monitorRequest('/api/monitor/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:payload.code,logic_text:logicText,advanced_mode:false,key,...(confirmation?{confirmation}: {})})});monitorLogicSaved=monitorDraft.logic_saved!==false;status.textContent=`· 逻辑已保存 · 自动规则 ${Number(monitorDraft.auto_rule_count)||0} 条`;renderMonitor();}
 catch(e){status.textContent='· 逻辑已保存 · '+e.message;monitorDraft={logic_saved:true,auto_rule_count:0,rules:[],summary:'持仓逻辑已保存，自动规则整理尚未完成。',manual_review_items:[],periodic_review_items:[],needs_confirmation:[]};}
 finally{btn.disabled=false;monitorDraftPending=false;renderMonitorDraft();}
}
async function continueMonitorDraft(){
 const next=monitorDraft&&monitorDraft.next_question;if(!next||!next.confirmation||monitorDraftPending)return;
 await draftMonitorRules(next.confirmation);
}
function applyMonitorDraft(){
 if(!monitorDraft)return;const rules=monitorDraft.rules||[],code=g('monitorLogicCode').value.trim();g('monitorCode').value=code;g('monitorName').value=g('monitorLogicName').value.trim();
 const watch=rules.find(r=>r.metric==='price'&&r.direction==='buy'&&['lte','lt'].includes(r.operator));
 const risk=rules.find(r=>r.metric==='price'&&r.direction==='sell'&&['lte','lt'].includes(r.operator));
 const target=rules.find(r=>r.metric==='price'&&r.direction==='sell'&&['gte','gt'].includes(r.operator));
 if(watch)g('monitorWatchPrice').value=watch.threshold;if(risk)g('monitorRiskPrice').value=risk.threshold;if(target)g('monitorTargetPrice').value=target.threshold;g('monitorFormStatus').textContent='已填入草案 · 请核对后保存';g('monitorCode').scrollIntoView({behavior:'smooth',block:'center'});
}
async function explainMonitorEvent(eventId){
 const key=(localStorage.getItem('ds_key')||'').trim();if(!key){g('monitorRuntimeMeta').textContent='请先在页面顶部保存 DeepSeek Key';return;}
 g('monitorRuntimeMeta').textContent='正在按需复核提醒…';
 try{monitorExplanation=await monitorRequest('/api/monitor/explain',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:eventId,key})});renderMonitor();g('monitorExplanation').scrollIntoView({behavior:'smooth',block:'nearest'});}
 catch(e){g('monitorRuntimeMeta').textContent=e.message;}
}
async function monitorRuntime(action){
 if(monitorBusy)return;monitorBusy=true;renderMonitor();
 try{await monitorRequest('/api/monitor/runtime',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});}
 catch(e){g('monitorRuntimeMeta').textContent=e.message;}
 finally{monitorBusy=false;await loadMonitor(true);}
}
function lvlClass(p){return p==null?'mid':(p<30?'low':(p<=70?'mid':'high'));}
function lvlText(p){return p==null?'—':(p<30?'低估':(p<=70?'合理':'高估'));}
function fmtCap(x){return x?(x/1e8).toFixed(0)+'亿':'—';}

async function q(){
 showTab('analyze');
 const code=g('code').value.trim();
 if(!/^\d{6}$/.test(code)){alert('请输入6位数字代码');return;}
 g('status').textContent='抓取与计算中，约5-10秒…';g('xls').style.display='none';
 try{
  const r=await(await fetch('/api/analyze?code='+code)).json();
  g('status').textContent='';
  if(r.error){alert(r.error);return;}
  cur=code;render(r);
  currentMultiCode=code;
  const card=g('multiDimCard'); if(card)card.style.display='block';
  const box=g('multidimResult'); if(box)box.innerHTML='<div class="sub">已切换到当前标的，可直接点击下方按钮启动多维分析。</div>';
  g('xls').style.display='inline-block';g('hint').style.display='none';
 }catch(e){g('status').textContent='';alert('失败：'+e);}
}
function dl(){if(cur)location.href='/api/excel?code='+cur;}

function metricPct(label,val,pct,st,extra){
 let h=`<div class="metric"><h3>${label}</h3><span class="v">${val==null?'—':val}</span>`;
 if(pct!=null){h+=`<span class="pct ${lvlClass(pct)}">${pct}% ${lvlText(pct)}</span>
   <div class="bar"><div class="mk" style="left:calc(${Math.max(0,Math.min(100,pct))}% - 1.5px)"></div></div>`;}
 if(st)h+=`<div class="row"><span>低 ${st.min}</span><span>中 ${st.median}</span><span>高 ${st.max}</span></div>`;
 if(extra)h+=`<div class="sub" style="margin-top:6px">${extra}</div>`;
 return h+'</div>';
}

function render(r){
 const t=r.tech, up=r.chg>=0;
 let h=`<div class="card"><div class="head">
   <span class="nm">${r.name}</span><span class="cd">${r.code}</span>
   <span class="badge">${r.type_name||r.classify}</span>
   <span class="px ${up?'up':'down'}">${r.price} <span style="font-size:15px">${up?'+':''}${r.chg}%</span></span>
   <div style="display:inline-flex;gap:6px;flex-wrap:wrap">
     <button onclick="addWatch('${r.code}');this.textContent='★ 已在自选'" style="background:#1e293b;border:1px solid #f59e0b;color:#fcd34d;font-size:13px;padding:6px 12px">★ 加自选</button>
     <button onclick="openMultiDim('${r.code}')" style="background:#17324d;border:1px solid #2563eb;color:#dbeafe;font-size:13px;padding:6px 12px">🔍 多维分析</button>
   </div>
   <span class="sub">${r.realtime&&r.rt_time?('现价实时·'+r.rt_time+' ｜ '):''}市值 ${fmtCap(r.cap)} · 估值/资金截至收盘 ${r.date} · 样本 ${r.count} 日</span></div></div>`;

 // 估值/分位卡片
 h+='<div class="card"><div class="sec-title">估值分位</div><div class="grid">';
 if(r.is_stock && r.pe!=null){
   h+=metricPct('PE-TTM（近5年分位）',r.pe,r.pe_pct,r.pe_stats,r.pe<0?'当前为负值，参考PB':'');
   h+=metricPct('PB（近5年分位）',r.pb,r.pb_pct,r.pb_stats,'');
 }
 h+=metricPct('股价（近5年分位）',r.price,r.price_pct,null,
     `区间 ${r.price_lo} ~ ${r.price_hi}　距高 ${r.from_hi}%　距低 +${r.from_lo}%`);
 h+='</div></div>';

 // 行业平均对比
 if(r.industry && r.industry.median_pe!=null){
   const ind=r.industry, cheap=ind.target_pe!=null&&ind.target_pe<ind.median_pe;
   h+=`<div class="card"><div class="sec-title">行业平均对比 · ${ind.name}（${ind.count}只）</div><div class="grid">`;
   h+=`<div class="metric"><h3>行业中位 PE</h3><span class="v">${ind.median_pe}</span></div>`;
   h+=`<div class="metric"><h3>行业中位 PB</h3><span class="v">${ind.median_pb??'—'}</span></div>`;
   if(ind.target_pe!=null)
     h+=`<div class="metric"><h3>本股 PE 相对行业</h3><span class="v ${cheap?'down':'up'}">${cheap?'低于':'高于'}中位</span>
         <div class="sub" style="margin-top:6px">本股 ${ind.target_pe} vs 中位 ${ind.median_pe}</div></div>`;
   if(ind.cheaper_than_target!=null)
     h+=`<div class="metric"><h3>行业内更便宜的</h3><span class="v">${ind.cheaper_than_target}<span style="font-size:14px;color:#7b8aa6"> / ${ind.pos_pe_count} 只盈利股</span></span></div>`;
   h+='</div>';
   if(ind.peers&&ind.peers.length){
     h+='<div class="sub" style="margin-top:12px">同业估值最低几只：</div><div class="row" style="flex-wrap:wrap;gap:10px;margin-top:6px">';
     ind.peers.forEach(p=>{h+=`<span class="badge" style="font-size:12px">${p.name} PE ${p.pe==null?'亏':p.pe}</span>`;});
     h+='</div>';
   }
   h+='</div>';
 }

 // 技术面卡片
 h+='<div class="card"><div class="sec-title">技术面 / 量能</div><div class="grid">';
 const trend = (t.ma5>t.ma20&&t.ma20>t.ma60)?'多头排列':((t.ma5<t.ma20&&t.ma20<t.ma60)?'空头排列':'均线交织');
 h+=`<div class="metric"><h3>均线趋势</h3><span class="v" style="font-size:19px">${trend}</span>
     <div class="row"><span>MA5 ${t.ma5}</span><span>MA20 ${t.ma20}</span><span>MA60 ${t.ma60}</span></div></div>`;
 const rc=t.rsi==null?'':(t.rsi>=75?'high':(t.rsi<=25?'low':'mid'));
 h+=`<div class="metric"><h3>RSI(14)</h3><span class="v">${t.rsi??'—'}</span>
     ${t.rsi!=null?`<span class="pct ${rc}">${t.rsi>=75?'超买':(t.rsi<=25?'超卖':'中性')}</span>`:''}</div>`;
 const mc=t.macd_hist>=0?'up':'down';
 h+=`<div class="metric"><h3>MACD</h3><span class="v ${mc}" style="font-size:19px">${t.macd_hist>=0?'红柱·多头':'绿柱·空头'}</span>
     <div class="row"><span>DIF ${t.macd_dif}</span><span>DEA ${t.macd_dea}</span></div></div>`;
 const vr=t.vol_ratio;
 const vt=vr==null?'—':(vr>=2?'显著放量':(vr>=1.5?'温和放量':(vr<=0.6?'缩量':'正常')));
 h+=`<div class="metric"><h3>量比（对5日均量）</h3><span class="v">${vr??'—'}</span>
     <span class="pct ${vr>=1.5?'high':(vr<=0.6?'low':'mid')}">${vt}</span></div>`;
 h+=`<div class="metric"><h3>年化波动率</h3><span class="v">${t.vola??'—'}%</span>
     <div class="sub" style="margin-top:6px">近5年最大回撤 ${t.mdd}%</div></div>`;
 h+=`<div class="metric"><h3>布林带(±2σ)</h3><span class="v" style="font-size:17px">${t.boll_low} ~ ${t.boll_up}</span>
     <div class="sub" style="margin-top:6px">中轨 ${t.boll_mid}</div></div>`;
 h+='</div></div>';

 // 主图
 h+=`<div class="card"><div class="sec-title">K线 · 均线 · 布林±2σ · 买卖信号 · 量能 · MACD</div>
     <div id="chart" style="height:560px"></div></div>`;

 // 资金流向 + 异动提醒
 h+='<div class="card"><div class="sec-title">资金流向（近5日）· 异动提醒</div>';
 if(r.alerts&&r.alerts.length){
   h+='<div class="alerts">';
   r.alerts.forEach(a=>{const cls={in:'a-in',out:'a-out',warn:'a-warn',info:'a-info'}[a.lv]||'a-info';
     h+=`<span class="alert ${cls}">${a.t}</span>`;});
   h+='</div>';
 }
 if(r.moneyflow){
   const mf=r.moneyflow, ti=mf.main_today>=0;
   h+=`<div class="flowwrap">
     <div class="flowbox"><canvas id="flowCanvas" width="320" height="230"></canvas></div>
     <div class="flowbox" style="flex:1;min-width:300px"><div id="mfbars" style="height:230px"></div></div>
   </div>
   <div class="mf-nums">
     <div><div class="k">今日主力净额</div><div class="val ${ti?'up':'down'}">${ti?'+':''}${mf.main_today} 亿</div></div>
     <div><div class="k">近5日主力累计</div><div class="val ${mf.main_sum5>=0?'up':'down'}">${mf.main_sum5>=0?'+':''}${mf.main_sum5} 亿</div></div>
     <div><div class="k">主力动向</div><div class="val" style="font-size:16px">${mf.streak>=2?('连续'+mf.streak+'日'+(mf.streak_dir>0?'净流入':'净流出')):'—'}</div></div>
   </div>
   <div class="sub" style="margin-top:8px">红=流入 · 绿=流出（A股习惯）；柱状为每日超大/大/中/小单净额（亿元）。</div>`;
 }else{
   h+='<div class="sub">资金流向数据暂不可用（接口临时波动或该品种不支持），其余分析不受影响。</div>';
 }
 h+='</div>';

 // 基本面
 if(r.fund && r.fund.length){
   const f=r.fund[0];
   h+=`<div class="card"><div class="sec-title">基本面（最新报告期 ${f.date}）</div><div class="grid">`;
   const fm=[['加权ROE',f.roe,'%'],['营收同比',f.rev_yoy,'%'],['毛利率',f.gross,'%'],
             ['资产负债率',f.debt,'%'],['每股收益',f.eps,'元']];
   fm.forEach(([k,v,u])=>{if(v!=null)h+=`<div class="metric"><h3>${k}</h3><span class="v">${(+v).toFixed(2)}${u}</span></div>`;});
   h+='</div></div>';
 }

 // 报告 + 风险
 const rp=r.report;
 h+='<div class="card"><div class="sec-title">分析报告</div><div class="report">';
 const grp=(lbl,arr,cls)=>{if(!arr||!arr.length)return '';
   return `<div class="grp"><div class="lbl">${lbl}</div>`+arr.map(x=>`<p class="${cls||''}">${x}</p>`).join('')+'</div>';};
 h+=grp('估值',rp.valuation);
 h+=grp('技术面',rp.technical);
 h+=grp('量能',rp.volume);
 h+=grp('基本面',rp.fundamental);
 if(rp.opportunities&&rp.opportunities.length)h+=grp('关注点',rp.opportunities,'ops');
 h+=`<div class="note">${r.signal_note}</div></div></div>`;

 const rk=r.risk;
 h+=`<div class="card"><div class="sec-title">风险评估</div><div class="riskbox">
     <div style="text-align:center"><div class="rscore ${rk.cls==='low'?'down':(rk.cls==='mid'?'':'up')}"
      style="${rk.cls==='mid'?'color:#f59e0b':''}">${rk.level}</div>
      <div class="sub">评分 ${rk.score}/100</div></div>
     <div class="reasons"><b style="color:#93a4bf">主要风险点：</b><br>${rk.reasons.map(x=>'· '+x).join('<br>')}</div>
     </div></div>`;

 g('result').innerHTML=h;
 g('result').style.display='block';
 drawChart(r);
 if(r.moneyflow) drawMoneyflow(r);
}

/* ============ 资金流向：动效 + 柱状 ============ */
let flowRAF=0, mfChart=null;
function drawMoneyflow(r){
 const mf=r.moneyflow;
 // 5日堆叠柱（超大/大/中/小单净额，亿元）
 const el=document.getElementById('mfbars');
 if(el&&window.echarts){
   if(mfChart)mfChart.dispose();
   mfChart=echarts.init(el,'dark');
   const dates=mf.days.map(d=>d.date);
   const mk=(name,key,color)=>({name:name,type:'bar',stack:'x',data:mf.days.map(d=>d[key]),
     itemStyle:{color:color},emphasis:{focus:'series'}});
   mfChart.setOption({backgroundColor:'transparent',animationDuration:900,
     legend:{top:0,textStyle:{color:'#8ea0bd'},data:['超大单','大单','中单','小单']},
     tooltip:{trigger:'axis',valueFormatter:v=>(v>=0?'+':'')+v+'亿'},
     grid:{left:44,right:12,top:30,bottom:24},
     xAxis:{type:'category',data:dates,axisLabel:{color:'#64748b'},axisLine:{lineStyle:{color:'#33415c'}}},
     yAxis:{type:'value',name:'亿元',axisLabel:{color:'#64748b'},splitLine:{lineStyle:{color:'#1a2440'}}},
     series:[mk('超大单','super','#f2495c'),mk('大单','large','#ff8a95'),
             mk('中单','mid','#5b8def'),mk('小单','small','#2ec26e')]});
 }
 // Canvas 粒子流动效
 drawFlow(mf);
}
function drawFlow(mf){
 const cv=document.getElementById('flowCanvas'); if(!cv)return;
 const ctx=cv.getContext('2d'), W=cv.width, H=cv.height;
 const d=mf.days[mf.days.length-1];
 const node={x:230,y:H/2,r:38};
 const src=[{name:'主力',x:64,y:72,v:d.main},{name:'散户',x:64,y:162,v:(d.small||0)+(d.mid||0)}];
 src.forEach(s=>{s.in=s.v>=0; s.mag=Math.min(Math.abs(s.v),26);
   s.n=Math.max(18,Math.round(s.mag*2.2+16)); s.parts=[];
   for(let i=0;i<s.n;i++)s.parts.push({t:Math.random(),sp:0.003+Math.random()*0.007+s.mag*0.0003,phase:Math.random()*Math.PI*2,size:1.4+Math.random()*2.4});});
 cancelAnimationFrame(flowRAF);
 function frame(t){
   ctx.clearRect(0,0,W,H);
   const grd=ctx.createRadialGradient(node.x,node.y,8,node.x,node.y,110);
   grd.addColorStop(0,'rgba(59,130,246,0.28)');grd.addColorStop(1,'rgba(7,11,22,0)');
   ctx.fillStyle=grd;ctx.fillRect(0,0,W,H);
   ctx.strokeStyle='rgba(148,163,184,0.16)';ctx.lineWidth=1;
   for(let i=0;i<5;i++){ctx.beginPath();ctx.moveTo(20,40+i*26);ctx.quadraticCurveTo(160,70+i*12,280,90+i*8);ctx.stroke();}
   src.forEach(s=>{
     const col=s.in?'#f2495c':'#2ec26e';
     ctx.strokeStyle=col+'33';ctx.lineWidth=2;
     ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.quadraticCurveTo((s.x+node.x)/2, (s.y+node.y)/2-30, node.x,node.y);ctx.stroke();
     s.parts.forEach(p=>{p.t+=s.in?p.sp:-p.sp; if(p.t>1)p.t=0; if(p.t<0)p.t=1; const x=s.x+(node.x-s.x)*p.t; const y=s.y+(node.y-s.y)*p.t; const dx=node.x-x, dy=node.y-y; const curve=1-Math.abs(p.t-0.5)*0.4; ctx.beginPath();ctx.arc(x+dx*0.04, y+dy*0.04+Math.sin(t/800+p.phase)*8*curve, p.size,0,Math.PI*2);ctx.fillStyle=col;ctx.fill();});
   });
   ctx.save();
   const halo=ctx.createRadialGradient(node.x,node.y,6,node.x,node.y,44);
   halo.addColorStop(0,'rgba(96,165,250,0.95)');halo.addColorStop(1,'rgba(14,21,33,0.1)');
   ctx.fillStyle=halo;ctx.beginPath();ctx.arc(node.x,node.y,node.r+8,0,Math.PI*2);ctx.fill();
   const core=ctx.createRadialGradient(node.x,node.y,3,node.x,node.y,node.r);
   core.addColorStop(0,'#eaf1fb');core.addColorStop(1,'#1d4ed8');ctx.fillStyle=core;ctx.beginPath();ctx.arc(node.x,node.y,node.r,0,Math.PI*2);ctx.fill();
   ctx.restore();
   ctx.fillStyle='#eaf1fb';ctx.font='13px "Microsoft YaHei"';ctx.textAlign='center';ctx.fillText('资金流向',node.x,node.y+4);
   src.forEach(s=>{ctx.fillStyle='#94a3b8';ctx.font='12px "Microsoft YaHei"';ctx.textAlign='center';ctx.fillText(s.name,s.x,s.y-16); ctx.fillStyle=s.in?'#f2495c':'#2ec26e';ctx.font='bold 12px "Microsoft YaHei"';ctx.fillText((s.v>=0?'+':'')+s.v.toFixed(2)+'亿',s.x,s.y+20);});
   flowRAF=requestAnimationFrame(frame);
 }
 frame(0);
}

function drawChart(r){
 const c=r.chart, ch=echarts.init(document.getElementById('chart'),'dark');
 const volColors=c.vup.map(u=>u?'#f2495c':'#2ec26e');
 const opt={
  backgroundColor:'transparent',
  animation:false,
  legend:{top:0,textStyle:{color:'#8ea0bd'},
    data:['K线','MA5','MA20','MA60','布林上','布林下']},
  tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
  axisPointer:{link:[{xAxisIndex:'all'}]},
  grid:[{left:52,right:22,top:34,height:'52%'},
        {left:52,right:22,top:'64%',height:'12%'},
        {left:52,right:22,top:'80%',height:'13%'}],
  xAxis:[
   {type:'category',data:c.dates,scale:true,boundaryGap:false,axisLine:{lineStyle:{color:'#33415c'}},
    splitLine:{show:false},axisLabel:{color:'#64748b'}},
   {type:'category',gridIndex:1,data:c.dates,axisLabel:{show:false},axisLine:{show:false},axisTick:{show:false}},
   {type:'category',gridIndex:2,data:c.dates,axisLabel:{show:false},axisLine:{show:false},axisTick:{show:false}}
  ],
  yAxis:[
   {scale:true,axisLine:{lineStyle:{color:'#33415c'}},splitLine:{lineStyle:{color:'#1a2440'}},axisLabel:{color:'#64748b'}},
   {gridIndex:1,splitNumber:2,axisLabel:{show:false},axisLine:{show:false},splitLine:{show:false}},
   {gridIndex:2,axisLabel:{show:false},axisLine:{show:false},splitLine:{show:false}}
  ],
  dataZoom:[{type:'inside',xAxisIndex:[0,1,2],start:Math.max(0,100-250/c.dates.length*100),end:100},
            {type:'slider',xAxisIndex:[0,1,2],bottom:2,height:16,start:70,end:100,
             textStyle:{color:'#64748b'},borderColor:'#22304a'}],
  series:[
   {name:'K线',type:'candlestick',data:c.candle,
     itemStyle:{color:'#f2495c',color0:'#2ec26e',borderColor:'#f2495c',borderColor0:'#2ec26e'},
     markPoint:{symbol:'pin',symbolSize:0,data:[]}},
   {name:'MA5',type:'line',data:c.ma5,smooth:true,showSymbol:false,lineStyle:{width:1,color:'#e6b422'}},
   {name:'MA20',type:'line',data:c.ma20,smooth:true,showSymbol:false,lineStyle:{width:1,color:'#42a5f5'}},
   {name:'MA60',type:'line',data:c.ma60,smooth:true,showSymbol:false,lineStyle:{width:1,color:'#ab47bc'}},
   {name:'布林上',type:'line',data:c.boll_up,smooth:true,showSymbol:false,lineStyle:{width:1,type:'dashed',color:'#64748b'}},
   {name:'布林下',type:'line',data:c.boll_low,smooth:true,showSymbol:false,lineStyle:{width:1,type:'dashed',color:'#64748b'}},
   {name:'买入',type:'scatter',data:c.buys,symbol:'triangle',symbolSize:11,
     itemStyle:{color:'#f2495c'},tooltip:{formatter:o=>'金叉买点 '+o.data[0]}},
   {name:'卖出',type:'scatter',data:c.sells,symbol:'triangle',symbolRotate:180,symbolSize:11,
     itemStyle:{color:'#2ec26e'},tooltip:{formatter:o=>'死叉卖点 '+o.data[0]}},
   {name:'量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:c.vol.map((v,i)=>({value:v,itemStyle:{color:volColors[i]}}))},
   {name:'MACD',type:'bar',xAxisIndex:2,yAxisIndex:2,
     data:c.hist.map(v=>({value:v,itemStyle:{color:v>=0?'#f2495c':'#2ec26e'}}))},
   {name:'DIF',type:'line',xAxisIndex:2,yAxisIndex:2,data:c.dif,showSymbol:false,lineStyle:{width:1,color:'#e6b422'}},
   {name:'DEA',type:'line',xAxisIndex:2,yAxisIndex:2,data:c.dea,showSymbol:false,lineStyle:{width:1,color:'#42a5f5'}}
  ]
 };
 ch.setOption(opt);
 window.addEventListener('resize',()=>ch.resize());
}

/* ===================== 全局 Key ===================== */
function loadGKey(){const k=localStorage.getItem('ds_key')||'';g('gkey').value=k;
 g('gkstat').textContent=k?'✓ 已保存（本地）':'未设置';g('gkstat').style.color=k?'#34d399':'#f59e0b';}
function saveGKey(){const k=g('gkey').value.trim();localStorage.setItem('ds_key',k);loadGKey();}
function loadOKey(){const k=localStorage.getItem('oai_key')||'';if(g('okey'))g('okey').value=k;
 if(g('okstat')){g('okstat').textContent=k?'✓ 已保存（本地）':'未设置';g('okstat').style.color=k?'#34d399':'#64748b';}}
function saveOKey(){const k=g('okey').value.trim();localStorage.setItem('oai_key',k);loadOKey();}
document.addEventListener('DOMContentLoaded',function(){loadGKey();loadOKey();renderWatch();hydrateWatchNames();loadMarket();refreshLucide();setInterval(()=>{if(g('tab-monitor').style.display!=='none')loadMonitor();},15000);setInterval(()=>{if(g('tab-watch').style.display!=='none')refreshWatchQuotes();},60000);});

/* ===================== 标签导航 ===================== */
function showTab(name){
 document.querySelectorAll('.tabpage').forEach(p=>p.style.display='none');
 document.querySelectorAll('.tabbtn').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
 const el=g('tab-'+name); if(el)el.style.display='block';
 const monitorMode=name==='monitor',workMode=monitorMode||name==='watch';
 if(g('dsKeyBar'))g('dsKeyBar').style.display='flex';
 if(g('openAiKeyBar'))g('openAiKeyBar').style.display=monitorMode?'none':'flex';
 if(workMode){g('chat').style.display='none';g('fab').style.display='none';}
 else if(g('chat').style.display==='none')g('fab').style.display='block';
 if(name==='market'){loadMarket();resumeMarketFlow();}else stopMarketFlow();
 if(name==='watch')refreshWatchQuotes();
 if(name==='monitor')loadMonitor(true);
}

let currentMultiCode='';
function openMultiDim(code){
  currentMultiCode=(code||g('code').value.trim());
  if(!/^\d{6}$/.test(currentMultiCode)){alert('请输入6位代码');return;}
  showTab('analyze');
  const card=g('multiDimCard');
  if(card){card.style.display='block';}
  const box=g('multidimResult');
  if(box){box.innerHTML='<div class="sub">点击下方按钮开始三维分析。</div>';}
  if(card){card.scrollIntoView({behavior:'smooth',block:'start'});}
}

async function runMultiDimAnalysis(){
  const code=currentMultiCode||g('code').value.trim();
  const ds=(localStorage.getItem('ds_key')||'').trim();
  const box=g('multidimResult');
  const btn=g('multidimBtn');
  if(!/^\d{6}$/.test(code)){alert('请输入6位代码');return;}
  if(!ds){alert('请先在页面顶部保存 DeepSeek Key。');return;}
  if(box){box.innerHTML='<div class="sub">正在调用价值派 / 技术派 / 风控派进行多维分析…</div>';}
  if(btn){btn.disabled=true;btn.textContent='分析中…';}
  try{
    const r=await fetch('/api/multidim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,key:ds})}).then(x=>x.json());
    if(r.error){if(box)box.innerHTML='<div class="msg e" style="max-width:100%">⚠ '+r.error+'</div>';return;}
    const reports=(r.reports||[]).filter(Boolean);
    let h='';
    reports.forEach((a,index)=>{
      const roleName=['价值派','技术派','风控派'][index]||('分析员'+(index+1));
      h+=`<div class="an-card"><div class="an-head"><span class="an-name">${roleName}</span><span class="cd">${a.code}</span><span class="mbadge m-ds">${a.model||'DeepSeek'}</span></div><div class="an-text">${(a.text||'').replace(/\n/g,'<br>')}</div></div>`;
    });
    if(box)box.innerHTML=h || '<div class="sub">暂无报告。</div>';
  }catch(e){if(box)box.innerHTML='<div class="msg e" style="max-width:100%">⚠ 请求失败：'+e+'</div>';}
  finally{if(btn){btn.disabled=false;btn.textContent='启动多维分析';}}
}
/* ===================== 大盘资金流向粒子动效 ===================== */
let mktFlowRAF=0,mktFlowSource=null,mktFlowResizeTimer=0,mktFlowResizeObserver=null;
const reduceMktMotion=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const SVG_NS='http://www.w3.org/2000/svg';
function stopMarketFlow(){cancelAnimationFrame(mktFlowRAF);mktFlowRAF=0;}
function shortLabel(v,n=6){const s=String(v||'');return s.length>n?s.slice(0,n-1)+'…':s;}
function flowColor(chg){return chg>=0?'#f2495c':'#2ec26e';}
function svgEl(tag,attrs={},text=''){const el=document.createElementNS(SVG_NS,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));if(text)el.textContent=text;return el;}
function flowPoint(a,b,t){const cx=(a.x+b.x)/2,cy=(a.y+b.y)/2-22,u=1-t;return {x:u*u*a.x+2*u*t*cx+t*t*b.x,y:u*u*a.y+2*u*t*cy+t*t*b.y};}
function flowPath(a,b){return `M ${a.x} ${a.y} Q ${(a.x+b.x)/2} ${(a.y+b.y)/2-22} ${b.x} ${b.y}`;}
function makeMarketFlowState(indices,sectors){
 const svg=g('mktFlowSvg');if(!svg)return null;const rect=svg.getBoundingClientRect(),w=Math.max(300,Math.round(rect.width||740)),h=Math.max(260,Math.round(rect.height||300));
 svg.setAttribute('viewBox',`0 0 ${w} ${h}`);svg.replaceChildren();
 const validIndices=(indices||[]).filter(x=>Number.isFinite(Number(x.chg))).map(x=>({...x,chg:Number(x.chg)}));
 const validSectors=(sectors||[]).filter(x=>Number.isFinite(Number(x.chg))).map(x=>({...x,chg:Number(x.chg)}));
 const positive=validSectors.filter(x=>x.chg>=0).sort((a,b)=>b.chg-a.chg).slice(0,3),negative=validSectors.filter(x=>x.chg<0).sort((a,b)=>a.chg-b.chg).slice(0,3);
 const narrow=w<560,core={x:w/2,y:h/2+18,r:narrow?31:38},makeNodes=(items,side)=>items.map((x,i)=>({...x,side,x:side==='in'?(narrow?68:112):w-(narrow?68:112),y:items.length===1?h/2+22:112+i*((h-154)/(items.length-1)),r:narrow?15:18}));
 const nodes=[...makeNodes(negative,'in'),...makeNodes(positive,'out')],particles=[];
 nodes.forEach(node=>{const count=Math.min(9,Math.max(4,Math.round(Math.abs(node.chg)*3+4)));for(let i=0;i<count;i++)particles.push({from:node.side==='out'?core:node,to:node.side==='out'?node:core,color:flowColor(node.chg),phase:Math.random(),speed:.00012+Math.random()*.0001,size:1.25+Math.random()*1.4});});
 const avg=validIndices.length?validIndices.reduce((sum,x)=>sum+x.chg,0)/validIndices.length:null;
 return {svg,w,h,core,nodes,particles,indices:validIndices.slice(0,narrow?2:4),positive,negative,avg,dots:[],aura:null};
}
function drawIndexRibbon(svg,state){const items=state.indices;if(!items.length)return;const gap=7,w=Math.min(136,(state.w-26-gap*(items.length-1))/items.length),x0=(state.w-(w*items.length+gap*(items.length-1)))/2;items.forEach((item,i)=>{const x=x0+i*(w+gap),col=flowColor(item.chg);svg.append(svgEl('rect',{x,y:14,width:w,height:28,rx:7,fill:'#0f192a',stroke:col,'stroke-opacity':.55}));svg.append(svgEl('text',{x:x+8,y:31,fill:'#c9d4e5','font-size':11,'font-family':'Microsoft YaHei,Segoe UI,sans-serif'},shortLabel(item.name,5)));svg.append(svgEl('text',{x:x+w-8,y:31,fill:col,'font-size':11,'text-anchor':'end','font-family':'Microsoft YaHei,Segoe UI,sans-serif'},(item.chg>=0?'+':'')+item.chg.toFixed(2)+'%'));});}
function drawMarketNode(svg,node){const col=flowColor(node.chg);svg.append(svgEl('circle',{cx:node.x,cy:node.y,r:node.r,fill:'#111c2d',stroke:col,'stroke-width':1.5}));svg.append(svgEl('circle',{cx:node.x,cy:node.y,r:3,fill:col}));svg.append(svgEl('text',{x:node.x,y:node.y+4,fill:'#eaf1fb','font-size':12,'text-anchor':'middle','font-family':'Microsoft YaHei,Segoe UI,sans-serif'},shortLabel(node.name,5)));svg.append(svgEl('text',{x:node.x,y:node.y+node.r+16,fill:col,'font-size':11,'text-anchor':'middle','font-family':'Microsoft YaHei,Segoe UI,sans-serif'},(node.chg>=0?'+':'')+node.chg.toFixed(2)+'%'));}
function buildMarketFlowSvg(state){
 const {svg,w,h,core,nodes,particles}=state;svg.append(svgEl('title',{},'大盘强弱板块传导动效'));drawIndexRibbon(svg,state);
 svg.append(svgEl('text',{x:18,y:70,fill:'#8ea0bd','font-size':12,'font-family':'Microsoft YaHei,Segoe UI,sans-serif'},'承压板块'));
 svg.append(svgEl('text',{x:w-18,y:70,fill:'#8ea0bd','font-size':12,'text-anchor':'end','font-family':'Microsoft YaHei,Segoe UI,sans-serif'},'强势板块'));
 nodes.forEach(node=>svg.append(svgEl('path',{d:flowPath(core,node),fill:'none',stroke:flowColor(node.chg),'stroke-opacity':.32,'stroke-width':1.2})));
 const dots=svgEl('g');particles.forEach(p=>{const dot=svgEl('circle',{r:p.size,fill:p.color,'fill-opacity':.95});dots.append(dot);state.dots.push({dot,...p});});svg.append(dots);
 state.aura=svgEl('circle',{cx:core.x,cy:core.y,r:core.r+13,fill:'none',stroke:'#60a5fa','stroke-opacity':.32,'stroke-width':1});svg.append(state.aura);
 svg.append(svgEl('circle',{cx:core.x,cy:core.y,r:core.r,fill:'#172b48',stroke:'#60a5fa','stroke-width':1.5}));
 svg.append(svgEl('text',{x:core.x,y:core.y-7,fill:'#eaf1fb','font-size':13,'text-anchor':'middle','font-family':'Microsoft YaHei,Segoe UI,sans-serif'},'市场动向'));
 svg.append(svgEl('text',{x:core.x,y:core.y+14,fill:state.avg==null?'#8ea0bd':flowColor(state.avg),'font-size':12,'text-anchor':'middle','font-family':'Microsoft YaHei,Segoe UI,sans-serif'},state.avg==null?'指数数据暂缺':(state.avg>=0?'+':'')+state.avg.toFixed(2)+'%'));
 nodes.forEach(node=>drawMarketNode(svg,node));if(!nodes.length)svg.append(svgEl('text',{x:w/2,y:h/2+68,fill:'#8ea0bd','font-size':14,'text-anchor':'middle','font-family':'Microsoft YaHei,Segoe UI,sans-serif'},'暂无可用板块数据'));
}
function drawMarketFlowFrame(state,time){state.dots.forEach(p=>{const pt=flowPoint(p.from,p.to,(p.phase+time*p.speed)%1);p.dot.setAttribute('cx',pt.x);p.dot.setAttribute('cy',pt.y+Math.sin(time/520+p.phase*6)*1.5);});if(state.aura)state.aura.setAttribute('r',state.core.r+13+(reduceMktMotion?0:Math.sin(time/900)*2));if(!reduceMktMotion&&document.visibilityState==='visible')mktFlowRAF=requestAnimationFrame(t=>drawMarketFlowFrame(state,t));}
function updateMarketFlowMeta(state){const meta=g('mktFlowMeta');if(!meta)return;const avg=state.avg==null?'—':(state.avg>=0?'+':'')+state.avg.toFixed(2)+'%',strong=state.positive.map(x=>x.name).join('、')||'暂无',weak=state.negative.map(x=>x.name).join('、')||'暂无';meta.textContent=`指数平均 ${avg} · 强势：${strong} · 承压：${weak}`;}
function watchMarketFlowSize(svg){if(mktFlowResizeObserver||!window.ResizeObserver)return;mktFlowResizeObserver=new ResizeObserver(()=>{clearTimeout(mktFlowResizeTimer);mktFlowResizeTimer=setTimeout(()=>{if(mktFlowSource&&g('tab-market').style.display!=='none')drawMarketFlow(mktFlowSource.indices,mktFlowSource.sectors);},100);});mktFlowResizeObserver.observe(svg.parentElement);}
function drawMarketFlow(indices,sectors){mktFlowSource={indices:indices||[],sectors:sectors||[]};const state=makeMarketFlowState(mktFlowSource.indices,mktFlowSource.sectors);if(!state)return;stopMarketFlow();buildMarketFlowSvg(state);updateMarketFlowMeta(state);watchMarketFlowSize(state.svg);drawMarketFlowFrame(state,performance.now());}
function resumeMarketFlow(){if(mktFlowSource&&g('tab-market').style.display!=='none')drawMarketFlow(mktFlowSource.indices,mktFlowSource.sectors);}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')stopMarketFlow();else resumeMarketFlow();});
/* ===================== 大盘 + 板块轮动 ===================== */
let mktLoaded=false,mktLoading=false;
async function loadMarket(force){
 if(mktLoaded&&!force){resumeMarketFlow();return;}
 if(mktLoading)return;
 mktLoading=true;
 try{
  const d=await(await fetch('/api/market'+(force?'?t='+Date.now():''))).json();
  mktLoaded=true;
  g('mktTime').textContent='· 更新于 '+(d.time||'');
  // 指数卡片
  g('mktIndices').innerHTML=d.indices.map(x=>{
    const up=x.chg>=0,col=up?'#f2495c':'#2ec26e';
    return `<div class="idx"><div class="nm">${x.name}</div>
      <div class="pv" style="color:${col}">${x.price??'—'}</div>
      <div class="cg" style="color:${col}">${x.chg==null?'—':(up?'+':'')+x.chg+'%'}</div></div>`;}).join('');
  // 板块轮动条
  const secs=d.sectors||[];
  const mx=Math.max(1,...secs.map(s=>Math.abs(s.chg)));
  g('sectorRotation').innerHTML=secs.map(s=>{
    const up=s.chg>=0,col=up?'#f2495c':'#2ec26e',w=Math.abs(s.chg)/mx*100;
    return `<div class="secbar"><div class="lab">${s.name}</div>
      <div class="track"><div class="fill" data-w="${w}" style="background:${col}"></div></div>
      <div class="pct" style="color:${col}">${up?'+':''}${s.chg}%</div></div>`;}).join('')
    || '<div class="sub">板块数据暂不可用。</div>';
  // 触发填充动画
  setTimeout(()=>document.querySelectorAll('#sectorRotation .fill').forEach(f=>{f.style.width=f.dataset.w+'%';}),60);
  // 触发资金流向粒子动效
  drawMarketFlow(d.indices||[], d.sectors||[]);
 }catch(e){g('sectorRotation').innerHTML='<div class="sub">大盘数据加载失败：'+e+'</div>';g('mktFlowMeta').textContent='大盘数据暂不可用，请稍后刷新。';}
 finally{mktLoading=false;}
}

async function loadMarketAIReport(){
  const key=(localStorage.getItem('ds_key')||'').trim();
  const box=g('marketAiReport');
  if(!key){box.style.display='block';box.innerHTML='<div class="mr-body">请先在页面顶部保存 DeepSeek Key。</div>';return;}
  box.style.display='block';
  box.innerHTML='<div class="mr-head"><b>正在生成中…</b></div><div class="mr-body">正在调用 DeepSeek 接口，请稍候。</div>';
  try{
    const d=await fetch('/api/market_report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})}).then(r=>r.json());
    if(d.error){box.innerHTML='<div class="mr-body">⚠ '+d.error+'</div>';return;}
    box.innerHTML=`<div class="mr-head"><b>AI 大盘解析</b><span class="sub">${d.time||''}</span></div><div class="mr-body">${(d.report||'').replace(/\n/g,'<br>')}</div>`;
  }catch(e){box.innerHTML='<div class="mr-body">⚠ 请求失败：'+e+'</div>';}
}

/* ===================== AI 助手 ===================== */
let chatHistory=[], busy=false;
function toggleChat(open){g('chat').style.display=open?'flex':'none';g('fab').style.display=open?'none':'block';
 if(open){const k=(localStorage.getItem('ds_key')||'').trim();
   g('chatkeystat').textContent=k?'✓ 已用页面顶部的 DeepSeek Key':'⚠ 请先在页面顶部粘贴并保存 Key';
   g('cin').focus();}}
function addMsg(cls,text){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;
 g('msgs').appendChild(d);g('msgs').scrollTop=g('msgs').scrollHeight;return d;}
function addTrace(text){const d=document.createElement('div');d.className='traceln';d.textContent='🔧 '+text;
 g('msgs').appendChild(d);g('msgs').scrollTop=g('msgs').scrollHeight;}
function ask(q){toggleChat(true);g('cin').value=q;send();}
async function send(){
 if(busy)return;
 const text=g('cin').value.trim(); if(!text)return;
 const key=(localStorage.getItem('ds_key')||'').trim();
 if(!key){addMsg('e','请先在上方粘贴 DeepSeek Key 并点保存。没有的话点右侧「去申请」。');return;}
 g('cin').value='';addMsg('u',text);chatHistory.push({role:'user',content:text});
 busy=true;g('sendBtn').textContent='…';
 const wait=addMsg('a','思考中…');
 try{
  const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({history:chatHistory,key:key})});
  const d=await r.json();
  wait.remove();
  if(d.error){addMsg('e','⚠ '+d.error);busy=false;g('sendBtn').textContent='发送';return;}
  (d.trace||[]).forEach(t=>addTrace(t.tool+'('+Object.values(t.args).join(', ')+')'));
  addMsg('a',d.reply);
  chatHistory=d.history||chatHistory;   // 保留完整上下文（含工具调用）
 }catch(e){wait.remove();addMsg('e','⚠ 请求失败：'+e);}
 busy=false;g('sendBtn').textContent='发送';
}

/* ===================== AI 投研团 ===================== */
async function runPanel(){
 const codes=(g('pcodes').value.trim().split(/[\s,，、]+/)).filter(x=>/^\d{6}$/.test(x));
 if(codes.length<2){alert('请至少输入2个6位代码（空格或逗号分隔）');return;}
 const ds=(localStorage.getItem('ds_key')||'').trim();
 if(!ds){alert('缺少 DeepSeek Key：请在页面顶部粘贴并保存。');return;}
 const chief=g('chiefSel').value, oai=(localStorage.getItem('oai_key')||'').trim();
 if(chief==='gpt'&&!oai){alert('首席选了 GPT，但还没填 OpenAI Key：请在页面顶部「OpenAI Key」处粘贴保存。');return;}
 g('pbtn').disabled=true;g('pbtn').textContent='分析中…约30-60秒';
 g('panelOut').innerHTML='<div class="sub" style="padding:10px">'+codes.length+' 位分析员并行开工，'+(chief==='gpt'?'GPT':'DeepSeek')+' 首席稍后汇总…</div>';
 try{
  const r=await fetch('/api/panel',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({codes:codes,goal:g('pgoal').value.trim(),ds_key:ds,chief:chief,oai_key:oai,oai_model:(g('gptModel')?g('gptModel').value.trim():'')})});
  const d=await r.json();
  if(d.error){g('panelOut').innerHTML='<div class="msg e" style="max-width:100%">⚠ '+d.error+'</div>';}
  else{
   let h='';
   d.analysts.forEach(a=>{h+=`<div class="an-card"><div class="an-head">
     <span class="an-name">${a.name}</span><span class="cd">${a.code}</span>
     <span class="mbadge m-ds">${a.model}</span></div>
     <div class="an-text">${a.text}</div></div>`;});
   h+=`<div class="chief-card"><div class="an-head"><span class="an-name">🏛 首席汇总</span>
     <span class="mbadge m-cl">${d.chief.model}</span></div>
     <div class="an-text">${d.chief.text}</div></div>`;
   g('panelOut').innerHTML=h;
  }
 }catch(e){g('panelOut').innerHTML='<div class="msg e" style="max-width:100%">⚠ 请求失败：'+e+'</div>';}
 g('pbtn').disabled=false;g('pbtn').textContent='召集投研团';
}
</script></body></html>"""


_MONITOR_WEB_CONTROLLER = None
_MONITOR_WEB_LOCK = threading.Lock()


def monitor_web_controller():
    global _MONITOR_WEB_CONTROLLER
    if _MONITOR_WEB_CONTROLLER is None:
        with _MONITOR_WEB_LOCK:
            if _MONITOR_WEB_CONTROLLER is None:
                from monitoring.web import MonitorWebController

                _MONITOR_WEB_CONTROLLER = MonitorWebController()
    return _MONITOR_WEB_CONTROLLER


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        print(f"DEBUG: GET {self.path}")
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        code = (qs.get("code", [""])[0]).strip()
        try:
            if u.path == "/":
                print(f"DEBUG: Sending HTML, size={len(HTML)}")
                self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")
                print(f"DEBUG: HTML sent")
            elif u.path == "/api/analyze":
                if not re.fullmatch(r"\d{6}", code):
                    self._send(json.dumps({"error": "请输入6位数字代码"}, ensure_ascii=False).encode("utf-8"))
                else:
                    self._send(json.dumps(analyze_cached(code), ensure_ascii=False).encode("utf-8"))
            elif u.path == "/api/market":
                self._send(json.dumps(market_overview(), ensure_ascii=False).encode("utf-8"))
            elif u.path == "/api/name":
                nm = ""
                if re.fullmatch(r"\d{6}", code):
                    rows = fetch_watch_quotes([code])
                    nm = rows[0].get("name", "") if rows else ""
                self._send(json.dumps({"code": code, "name": nm}, ensure_ascii=False).encode("utf-8"))
            elif u.path == "/api/watch_quotes":
                raw_codes = (qs.get("codes", [""])[0]).split(",")
                self._send(json.dumps({"quotes": fetch_watch_quotes(raw_codes)}, ensure_ascii=False).encode("utf-8"))
            elif u.path == "/api/monitor/overview":
                self._send(
                    json.dumps(
                        monitor_web_controller().overview(), ensure_ascii=False
                    ).encode("utf-8")
                )
            elif u.path == "/api/excel":
                data, err = build_excel(code)
                if err:
                    self._send(json.dumps({"error": err}, ensure_ascii=False).encode("utf-8"))
                else:
                    self._send(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               {"Content-Disposition": "attachment; filename=%s_analysis.xlsx" % code})
            else:
                self.send_error(404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(json.dumps({"error": "出错了：%s" % e}, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            payload = {}
        if u.path == "/api/monitor/setup":
            try:
                result = monitor_web_controller().save_setup(payload)
                self._send(
                    json.dumps({"ok": True, "watch": result}, ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
            except (ValueError, OSError) as e:
                self._send(
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                )
        elif u.path == "/api/monitor/remove":
            try:
                removed = monitor_web_controller().remove_watch(payload.get("code"))
                self._send(
                    json.dumps({"ok": True, "removed": removed}, ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
            except ValueError as e:
                self._send(
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                )
        elif u.path == "/api/monitor/simulate":
            try:
                preview = monitor_web_controller().simulate_watch(
                    payload.get("code"),
                    str(payload.get("kind") or "risk").strip(),
                )
                self._send(json.dumps(preview, ensure_ascii=False).encode("utf-8"))
            except ValueError as e:
                self._send(
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                )
        elif u.path == "/api/monitor/logic":
            try:
                logic = monitor_web_controller().save_logic(payload)
                self._send(
                    json.dumps(
                        {"ok": True, "logic": logic}, ensure_ascii=False
                    ).encode("utf-8")
                )
            except (ValueError, OSError) as e:
                self._send(
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                )
        elif u.path == "/api/monitor/draft":
            try:
                api_key = (payload.get("key") or AGENT_KEY_ENV or "").strip()
                draft = monitor_web_controller().draft_rules(payload, api_key)
                self._send(json.dumps(draft, ensure_ascii=False).encode("utf-8"))
            except (ValueError, OSError) as e:
                self._send(
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as e:
                self._send(
                    json.dumps(
                        {"error": "规则整理失败：%s" % type(e).__name__},
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
        elif u.path == "/api/monitor/explain":
            try:
                api_key = (payload.get("key") or AGENT_KEY_ENV or "").strip()
                explanation = monitor_web_controller().explain_event(
                    payload.get("event_id"),
                    api_key,
                    bool(payload.get("force")),
                )
                self._send(
                    json.dumps(explanation, ensure_ascii=False).encode("utf-8")
                )
            except (ValueError, OSError) as e:
                self._send(
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as e:
                self._send(
                    json.dumps(
                        {"error": "事件复核失败：%s" % type(e).__name__},
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
        elif u.path == "/api/monitor/runtime":
            try:
                action = str(payload.get("action") or "").strip()
                controller = monitor_web_controller()
                if action == "start":
                    runtime = controller.start()
                elif action == "stop":
                    runtime = controller.stop()
                elif action == "run_once":
                    controller.run_once()
                    runtime = controller.runtime_status()
                else:
                    raise ValueError("不支持的盯盘操作")
                self._send(
                    json.dumps({"ok": True, "runtime": runtime}, ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
            except (ValueError, OSError) as e:
                self._send(
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                )
            except Exception as e:
                self._send(
                    json.dumps(
                        {"error": "盯盘运行失败：%s" % type(e).__name__},
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
        elif u.path == "/api/chat":
            try:
                history = payload.get("history") or []
                api_key = (payload.get("key") or AGENT_KEY_ENV or "").strip()
                if not api_key:
                    self._send(json.dumps({"error": "未设置 API Key。请在上方输入框粘贴你的 DeepSeek Key。"},
                                          ensure_ascii=False).encode("utf-8"))
                    return
                reply, trace, new_hist = agent_run(history, api_key)
                self._send(json.dumps({"reply": reply, "trace": trace, "history": new_hist},
                                      ensure_ascii=False).encode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = "认证失败(401)：Key 不正确或无权限" if e.code == 401 else \
                         ("余额不足(402)" if e.code == 402 else "模型接口错误 %s" % e.code)
                self._send(json.dumps({"error": detail}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._send(json.dumps({"error": "对话失败：%s（检查网络/Key/余额）" % e},
                                      ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/market_report":
            try:
                api_key = (payload.get("key") or AGENT_KEY_ENV or "").strip()
                if not api_key:
                    self._send(json.dumps({"error": "缺少 DeepSeek Key，请先在页面顶部保存。"}, ensure_ascii=False).encode("utf-8"))
                    return
                self._send(json.dumps(generate_market_ai_report(api_key, market_overview()), ensure_ascii=False).encode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = "认证失败(401)：Key 不正确或无权限" if e.code == 401 else \
                         ("余额不足(402)" if e.code == 402 else "模型接口错误 %s" % e.code)
                self._send(json.dumps({"error": detail}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._send(json.dumps({"error": "生成报告失败：%s" % e}, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/multidim":
            try:
                code = str(payload.get("code") or "").strip()
                api_key = (payload.get("key") or AGENT_KEY_ENV or "").strip()
                if not api_key:
                    self._send(json.dumps({"error": "缺少 DeepSeek Key，请先在页面顶部保存。"}, ensure_ascii=False).encode("utf-8"))
                    return
                if not re.fullmatch(r"\d{6}", code):
                    self._send(json.dumps({"error": "请输入6位数字代码"}, ensure_ascii=False).encode("utf-8"))
                    return
                self._send(json.dumps(analyze_multidim(code, api_key), ensure_ascii=False).encode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = "认证失败(401)：Key 不正确或无权限" if e.code == 401 else \
                         ("余额不足(402)" if e.code == 402 else "模型接口错误 %s" % e.code)
                self._send(json.dumps({"error": detail}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._send(json.dumps({"error": "多维分析失败：%s" % e}, ensure_ascii=False).encode("utf-8"))
        elif u.path == "/api/panel":
            try:
                codes = [c for c in (payload.get("codes") or []) if re.fullmatch(r"\d{6}", str(c))][:6]
                ds_key = (payload.get("ds_key") or AGENT_KEY_ENV or "").strip()
                goal = (payload.get("goal") or "").strip()
                chief = (payload.get("chief") or "deepseek").strip()
                oai_key = (payload.get("oai_key") or os.environ.get("OPENAI_API_KEY", "") or "").strip()
                oai_model = (payload.get("oai_model") or "").strip()
                if len(codes) < 2:
                    self._send(json.dumps({"error": "请至少提供2个6位代码（最多6个）"}, ensure_ascii=False).encode("utf-8"))
                    return
                if not ds_key:
                    self._send(json.dumps({"error": "缺少 DeepSeek Key（分析员使用）。请在页面顶部粘贴保存。"},
                                          ensure_ascii=False).encode("utf-8"))
                    return
                if chief == "gpt" and not oai_key:
                    self._send(json.dumps({"error": "选了 GPT 首席但缺少 OpenAI Key。请在页面顶部粘贴 GPT Key。"},
                                          ensure_ascii=False).encode("utf-8"))
                    return
                self._send(json.dumps(panel_analyze(codes, ds_key, goal, chief, oai_key, oai_model),
                                      ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._send(json.dumps({"error": "投研团失败：%s" % e}, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_error(404)


if __name__ == "__main__":
    print("=" * 52)
    print("  Valuation / Tech / Fundamental Analysis Server started")
    print("  Open: http://localhost:%d" % PORT)
    print("  Close this window to stop.")
    print("  Warming market data in background...")
    print("=" * 52)
    threading.Thread(target=warm_market_cache, name="market-warmup", daemon=True).start()
    if os.environ.get("APP_NO_BROWSER", "").strip() != "1":
        threading.Timer(
            0.5, lambda: webbrowser.open("http://localhost:%d" % PORT)
        ).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
