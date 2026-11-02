#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trend-atlas 机器信号抓取 + 行业到期复查。

只写 data/metrics.json 与 data/due.md，永远不碰 data/industries.json
（分数是人给的，信号是机器给的）。

用法:
    python scripts/fetch_metrics.py              # 联网抓取（CI 用）
    python scripts/fetch_metrics.py --offline    # 不发任何网络请求，用内置 fixture 自测
    python scripts/fetch_metrics.py --offline --out /tmp/metrics.json

可靠性约定:
    * 每个 probe 独立 try/except，失败只把该 probe 标成 status="error"，不影响其他 probe
    * 任一 probe 失败时退出码仍为 0（仅当输出文件不可写时才返回 1）
    * 单次请求超时 <= 15s，全部 probe 总耗时 <= 60s（超预算的请求直接跳过并记为 error）
    * 风口雷达 HTML 解析失败只记 warning，绝不拖垮脚本
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

TIMEOUT = 15            # 单次网络请求超时（秒）
TOTAL_BUDGET = 60       # 全部 probe 总耗时上限（秒）
REQUEST_BUDGET = 45     # 留给请求本身的预算，剩余时间用作写文件等收尾
MAX_HIGHLIGHTS = 20     # SCHEMA 规定每 probe 最多 20 条
MAX_TEXT = 160          # 单条 text 截断长度
UA = "trend-atlas-metrics/1.0 (+https://github.com/trend-atlas; stdlib urllib)"
DUE_TZ = timezone(timedelta(hours=8))   # 到期判定按北京时间

_STARTED = datetime.now(timezone.utc)

# 定位仓库根目录，不引入 os.path：__file__ 可能是相对路径
_SCRIPT = str(__file__).replace("\\", "/")
_DIR = _SCRIPT.rsplit("/", 1)[0] if "/" in _SCRIPT else ""
if _DIR == "scripts":
    _ROOT = "."
elif _DIR.endswith("/scripts"):
    _ROOT = _DIR.rsplit("/", 1)[0]
elif _DIR:
    _ROOT = _DIR
else:
    _ROOT = "."
DEFAULT_OUT = _ROOT + "/data/metrics.json"
# 页面文件在不同仓库里可能叫 index.html（GitHub Pages 根）或中文原名，按顺序取第一个能读到的
HTML_CANDIDATES = ("index.html", "风口雷达-合并版.html")


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elapsed():
    return (datetime.now(timezone.utc) - _STARTED).total_seconds()


def _quote_query(text):
    """最小化的 URL query 编码，省掉 urllib.parse 依赖。"""
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_.~":
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out)


def _fetch_json(url, accept="application/json"):
    left = REQUEST_BUDGET - _elapsed()
    if left < 3:
        raise TimeoutError("总耗时预算已用尽，跳过该请求")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=min(TIMEOUT, left)) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _normalize(items):
    """把各源的原始条目规整成 SCHEMA 的 {text, value, url}。"""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            value = int(item.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        out.append({
            "text": text[:MAX_TEXT],
            "value": value,
            "url": str(item.get("url") or ""),
        })
        if len(out) >= MAX_HIGHLIGHTS:
            break
    return out


# --------------------------------------------------------------------------- #
# 探测器 1: Hacker News 官方 Firebase API（无需鉴权）
# --------------------------------------------------------------------------- #
HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/%d.json"


def probe_hackernews():
    ids = _fetch_json(HN_TOP)
    if not isinstance(ids, list):
        raise ValueError("topstories 返回结构异常")
    highlights = []
    for story_id in ids[:10]:
        if REQUEST_BUDGET - _elapsed() < 5:
            break  # 预算不足就交回已有部分，不整体失败
        try:
            item = _fetch_json(HN_ITEM % int(story_id))
        except Exception:
            continue  # 单条取不到不影响同源其他条目
        if not isinstance(item, dict) or not item.get("title"):
            continue
        highlights.append({
            "text": item["title"],
            "value": int(item.get("score") or 0),
            "url": item.get("url") or "https://news.ycombinator.com/item?id=%d" % int(story_id),
        })
    return highlights


# --------------------------------------------------------------------------- #
# 探测器 2: GitHub Search API（无需 token，未鉴权限 10 次/分钟）
# --------------------------------------------------------------------------- #
def probe_github():
    since = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    query = "created:>" + since + " stars:>=100"
    url = ("https://api.github.com/search/repositories?q=" + _quote_query(query)
           + "&sort=stars&order=desc&per_page=10")
    payload = _fetch_json(url, accept="application/vnd.github+json")
    items = payload.get("items") if isinstance(payload, dict) else None
    if not items:
        raise ValueError("search/repositories 未返回 items")
    highlights = []
    for repo in items[:10]:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        highlights.append({
            "text": "%s: %s" % (full_name, (repo.get("description") or "").strip()),
            "value": int(repo.get("stargazers_count") or 0),
            "url": repo.get("html_url") or ("https://github.com/" + full_name),
        })
    return highlights


# --------------------------------------------------------------------------- #
# 探测器 3: Wikipedia 英文站每日浏览量榜（Wikimedia REST API，无需鉴权）
# --------------------------------------------------------------------------- #
WIKI_TOP = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/%d/%02d/%02d"
WIKI_SKIP = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "Help:", "File:")


def probe_wikipedia():
    # 该榜单数据滞后约一天，先试昨天，失败再退一天
    for back in (1, 2):
        day = datetime.now(timezone.utc) - timedelta(days=back)
        url = WIKI_TOP % (day.year, day.month, day.day)
        try:
            payload = _fetch_json(url)
        except Exception:
            continue
        articles = None
        if isinstance(payload, dict) and payload.get("items"):
            articles = payload["items"][0].get("articles")
        if not articles:
            continue
        highlights = []
        for entry in articles:
            name = str(entry.get("article") or "")
            if not name or name.startswith(WIKI_SKIP):
                continue
            highlights.append({
                "text": name.replace("_", " "),
                "value": int(entry.get("views") or 0),
                "url": "https://en.wikipedia.org/wiki/" + name,
            })
            if len(highlights) >= 10:
                break
        if highlights:
            return highlights
    raise RuntimeError("Wikipedia 日榜连续两日不可用")


# --------------------------------------------------------------------------- #
# probe 注册表（恰好 3 个，SCHEMA 契约不变）
# --------------------------------------------------------------------------- #
PROBES = [
    ("hackernews", "Hacker News 首页热榜", probe_hackernews),
    ("github", "GitHub 近 45 天新建高星仓库", probe_github),
    ("wikipedia", "Wikipedia 英文站每日浏览量榜", probe_wikipedia),
]

OFFLINE_FIXTURES = {
    "hackernews": [
        ("Show HN: I built an AI video editor in a weekend", 412,
         "https://news.ycombinator.com/item?id=40000001"),
        ("Ask HN: 独立开发者现在靠什么渠道获客？", 287,
         "https://news.ycombinator.com/item?id=40000002"),
        ("The rising cost of AI inference is reshaping SaaS margins", 198,
         "https://news.ycombinator.com/item?id=40000003"),
    ],
    "github": [
        ("acme/ai-video-cut: 一条命令把长视频切成短视频", 1843, "https://github.com/acme/ai-video-cut"),
        ("acme/agent-runner: 轻量 Agent 运行时", 921, "https://github.com/acme/agent-runner"),
        ("acme/edge-tts-clone: 离线语音克隆", 640, "https://github.com/acme/edge-tts-clone"),
    ],
    "wikipedia": [
        ("Artificial intelligence", 182344, "https://en.wikipedia.org/wiki/Artificial_intelligence"),
        ("Large language model", 91233, "https://en.wikipedia.org/wiki/Large_language_model"),
        ("Electric vehicle", 61007, "https://en.wikipedia.org/wiki/Electric_vehicle"),
    ],
}


def _run_probe(probe_id, label, fn):
    """单个 probe 独立执行：任何异常都被吃掉并记录，绝不向上抛。"""
    fetched = datetime.now(timezone.utc)
    try:
        highlights = _normalize(fn())
        if not highlights:
            raise ValueError("未解析到任何条目")
        status, error = "ok", None
    except Exception as exc:
        highlights = []
        status = "error"
        error = (type(exc).__name__ + ": " + str(exc)).replace("\n", " ")[:300]
    return {
        "id": probe_id,
        "label": label,
        "status": status,
        "fetchedAt": _iso(fetched),
        "error": error,
        "highlights": highlights,
    }


def _offline_probe(probe_id, label):
    """--offline：用内置 fixture 走完全相同的规整与输出路径，零网络。"""
    now = _iso(datetime.now(timezone.utc))
    rows = OFFLINE_FIXTURES.get(probe_id, [])
    highlights = _normalize([
        {"text": text, "value": value, "url": url} for text, value, url in rows
    ])
    return {
        "id": probe_id,
        "label": label,
        "status": "ok" if highlights else "error",
        "fetchedAt": now,
        "error": None if highlights else "offline fixture 为空",
        "highlights": highlights,
    }


# --------------------------------------------------------------------------- #
# 行业到期复查：解析 风口雷达-合并版.html 的 DATA 数组 -> data/due.md
# --------------------------------------------------------------------------- #
_ENTRY_START = re.compile(r'\{n:"')
_NAME = re.compile(r'^([^"\\]*)')
_NEXT = re.compile(r'next:"(\d{4}-\d{2}-\d{2})"')
_TITLE = re.compile(r'\bt:"((?:[^"\\]|\\.)*)"')


def _unescape(text):
    return text.replace('\\"', '"').replace("\\\\", "\\")


def parse_industries(html):
    """抽出 DATA 里每条的 {name, t, next}；任何一条解析不了就跳过，不抛异常。"""
    out = []
    for chunk in _ENTRY_START.split(html or "")[1:]:
        match_name = _NAME.match(chunk)
        match_next = _NEXT.search(chunk)
        if not match_name or not match_next:
            continue
        name = match_name.group(1).strip()
        if not name:
            continue
        match_t = _TITLE.search(chunk)
        out.append({
            "name": name,
            "next": match_next.group(1),
            "t": _unescape(match_t.group(1).strip()) if match_t else "",
        })
    return out


def collect_due(entries, today=None):
    """next <= 今天 的条目；日期不合法的条目忽略。"""
    today = today or datetime.now(DUE_TZ).date()
    due = []
    for entry in entries or []:
        try:
            day = datetime.strptime(entry["next"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if day <= today:
            due.append(entry)
    return due


def render_due(due):
    """Markdown 列表，每条一行；无到期条目时返回空字符串。"""
    lines = []
    for entry in due or []:
        summary = entry.get("t") or "（无摘要）"
        lines.append("- **%s** — 复查日 %s — %s" % (entry["name"], entry["next"], summary))
    return "".join(line + "\n" for line in lines)


def resolve_html(root, explicit=None):
    """按 --page / index.html / 中文原名 的顺序找第一个能读到的页面文件；找不到返回可读失败的回退路径。"""
    names = []
    if explicit:
        names.append(explicit)
    names.extend(HTML_CANDIDATES)
    for name in names:
        path = name if name.startswith("/") or name[1:3] == ":/" else root + "/" + name
        try:
            with open(path, "rb") as fh:
                fh.read(1)
            return path, name.rsplit("/", 1)[-1]
        except OSError:
            continue
    return root + "/" + HTML_CANDIDATES[0], HTML_CANDIDATES[0]


def update_due(html_path, html_name, due_path, today=None):
    """读 HTML 写 due.md；任何失败都只 warning（写空文件），绝不让脚本失败。"""
    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as fh:
            html = fh.read()
    except OSError as exc:
        sys.stderr.write("warning: 读不到 %s（%s），due.md 按无到期处理\n" % (html_name, exc))
        html = ""
    try:
        due = collect_due(parse_industries(html), today)
    except Exception as exc:
        sys.stderr.write("warning: 解析 %s 失败（%s），due.md 按无到期处理\n" % (html_name, exc))
        due = []
    try:
        with open(due_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_due(due))
    except OSError as exc:
        sys.stderr.write("warning: 无法写 %s（%s）\n" % (due_path, exc))
        return []
    return due


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="生成 trend-atlas 的 data/metrics.json 与 data/due.md（机器信号，不含人工判断）")
    parser.add_argument("--offline", action="store_true",
                        help="不发任何网络请求，用内置 fixture 生成一份合法的 metrics.json")
    parser.add_argument("--page", default=None,
                        help="页面文件名或绝对路径，默认依次尝试 index.html 与 风口雷达-合并版.html")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help="输出路径，默认 <repo>/data/metrics.json")
    args = parser.parse_args(argv)

    if args.offline:
        probes = [_offline_probe(pid, label) for pid, label, _ in PROBES]
    else:
        probes = [_run_probe(pid, label, fn) for pid, label, fn in PROBES]

    document = {
        "schemaVersion": 1,
        "generatedAt": _iso(datetime.now(timezone.utc)),
        "probes": probes,
    }
    try:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        sys.stderr.write("无法写入 %s: %s\n" % (args.out, exc))
        return 1

    due_path = _ROOT + "/data/due.md"
    html_path, html_name = resolve_html(_ROOT, args.page)
    due = update_due(html_path, html_name, due_path)
    ok = sum(1 for p in probes if p["status"] == "ok")
    print("probes=%d ok=%d error=%d elapsed=%.1fs%s -> %s"
          % (len(probes), ok, len(probes) - ok, _elapsed(),
             " [offline]" if args.offline else "", args.out))
    print("due=%d -> %s%s" % (len(due), due_path, "" if due else "（无到期条目，文件为空）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
