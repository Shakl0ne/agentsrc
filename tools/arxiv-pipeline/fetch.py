#!/usr/bin/env python3
"""arXiv AI/LLM/Agent 论文抓取器。

通过 arXiv 官方 API（Atom XML）抓取最近 N 天、覆盖 cs.AI / cs.LG /
cs.SE / cs.CL 分类下 AI Agent、LLM、模型等方向的论文，输出结构化
JSON 供下游选题/拆解消费。

要点：
- 默认单次请求即可覆盖（最近 N 天 + 关键词命中量可控）。
- 遇到 429 / 5xx 自动退避重试，遵循 Retry-After。
- 选题判断由 Agent 层基于输出 JSON 完成，脚本不做选题，只抓取与排序。

用法:
    python3 fetch.py [--days 7] [--max 200] [-o arxiv_latest.json]
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests，请先安装: pip install requests")

API = "https://export.arxiv.org/api/query"
USER_AGENT = "agentsrc-arxiv-pipeline/0.1 (tech-blog; Agent Source reading)"
NAMESPACES = {"atom": "http://www.w3.org/2005/Atom"}

# 命中标题或摘要即算相关：覆盖 Agent / LLM / 模型方向（综合收窄）
# 注意：这些同时也是「过滤复核」的依据，命中任一才留下
KEYWORDS = [
    # --- Agent 方向 ---
    "agent", "agentic", "agent system", "multi-agent", "autonomous",
    "tool use", "tool-use", "computer use", "model context protocol",
    "mcp", "agent skill", "skill library",
    # --- LLM 方向 ---
    "llm", "llms", "large language model", "language model",
    "instruction tuning", "instruction-tuned", "prompt", "prompting",
    "few-shot", "in-context learning", "chain-of-thought", "reasoning",
    # --- 模型 / 训练方向 ---
    "model training", "finetuning", "fine-tuning", "reinforcement",
    "rl", "rlhf", "model optimization", "scaling", "world model",
    "foundation model", "neural", "transformer", "diffusion model",
    "generative model", "representation", "embedding",
]

# 相关度权重：Agent 系最高（论坛取向），LLM/模型系居中，通用词垫底
KEYWORD_WEIGHT = {
    # --- Agent（高）---
    "agentic": 2.0, "agent system": 2.0, "agent skill": 2.0,
    "llm agent": 1.8, "ai agent": 1.8, "multi-agent": 1.8,
    "coding agent": 1.6, "code agent": 1.6, "computer use": 1.6,
    "tool use": 1.5, "agent": 1.2, "autonomous": 1.2,
    "model context protocol": 1.5, "mcp": 1.5, "skill library": 1.4,
    # --- LLM（中） ---
    "llm": 1.0, "llms": 1.0, "large language model": 1.0,
    "language model": 0.9, "instruction tuning": 0.9, "prompt": 0.8,
    "few-shot": 0.8, "in-context learning": 0.9, "chain-of-thought": 0.9,
    "reasoning": 0.8,
    # --- 模型 / 训练（中低） ---
    "reinforcement": 0.8, "rl": 0.7, "rlhf": 0.8, "finetuning": 0.8,
    "fine-tuning": 0.8, "model training": 0.6, "scaling": 0.8,
    "world model": 0.9, "foundation model": 0.9, "neural": 0.4,
    "transformer": 0.5, "diffusion model": 0.6, "generative model": 0.6,
    "representation": 0.5, "embedding": 0.6,
} 


def _is_recent(published: str, days: int) -> bool:
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


def _relevance(title: str, abstract: str) -> float:
    """粗分：关键词命中度，仅供排序。Agent 层用它做初筛，不做最终判断。"""
    text = f"{title} {abstract}".lower()
    return sum(w for kw, w in KEYWORD_WEIGHT.items() if kw in text)


def _match_signals(text: str) -> list:
    """返回命中的关键词列表，供 meta 与过滤使用。"""
    return [kw for kw in KEYWORDS if kw in text]


def _parse(xml_text: str) -> list:
    root = ET.fromstring(xml_text)
    entries = []
    for entry in root.findall("atom:entry", NAMESPACES):
        id_url = entry.findtext("atom:id", default="", namespaces=NAMESPACES)
        m = re.search(r"abs/(\d{4}\.\d{4,5}(?:v\d+)?)", id_url)
        arxiv_id = m.group(1) if m else ""
        title = re.sub(
            r"\s+", " ",
            entry.findtext("atom:title", default="", namespaces=NAMESPACES),
        ).strip()
        summary = re.sub(
            r"\s+", " ",
            entry.findtext("atom:summary", default="", namespaces=NAMESPACES),
        ).strip()
        authors = [
            a.findtext("atom:name", default="", namespaces=NAMESPACES)
            for a in entry.findall("atom:author", NAMESPACES)
        ]
        categories = [c.get("term") for c in entry.findall("atom:category", NAMESPACES)]
        published = entry.findtext("atom:published", default="", namespaces=NAMESPACES)
        entries.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else id_url,
                "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
                "abstract": summary,
                "authors": authors,
                "categories": categories,
                "published": published,
                "keywords": _match_signals(f"{title} {summary}"),
                "relevance": _relevance(title, summary),
            }
        )
    return entries


def _request(params: dict, retries: int = 4, base_wait: float = 3.0) -> str:
    """带退避重试地 GET arXiv API，返回响应文本。"""
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(retries + 1):
        try:
            resp = requests.get(API, params=params, headers=headers, timeout=30)
        except Exception as e:
            if attempt == retries:
                raise
            wait = base_wait * (2 ** attempt)
            print(f"[retry] 网络错误({e})，{wait:.0f}s 后重试", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code in (200, 301, 302):
            return resp.text
        if resp.status_code in (429, 500, 502, 503, 504):
            # 尊重 Retry-After
            wait = float(resp.headers.get("Retry-After", base_wait * (2 ** attempt)))
            print(
                f"[rate-limited] HTTP {resp.status_code}，{wait:.0f}s 后重试",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"重试 {retries} 次后仍失败")


def main():
    parser = argparse.ArgumentParser(
        description="抓取 arXiv 最近的 AI / LLM / Agent 论文"
    )
    parser.add_argument("--days", type=int, default=7, help="取最近 N 天（默认 7）")
    parser.add_argument("--max", type=int, default=200, help="API 最大返回条数（默认 200）")
    parser.add_argument("-o", "--output", default="arxiv_latest.json", help="输出 JSON 路径")
    args = parser.parse_args()

    query = urllib.parse.urlencode(
        {
            "search_query": "(cat:cs.AI OR cat:cs.LG OR cat:cs.SE OR cat:cs.CL) "
            "AND (all:agent OR all:agentic OR all:llm OR all:large+language+model "
            "OR all:reinforcement OR all:world+model OR all:foundation+model)",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": args.max,
        }
    )
    xml_text = _request(query)
    papers = _parse(xml_text)

    filtered = [p for p in papers if _is_recent(p["published"], args.days)]
    # 去掉未命中关键词的（避免误收无关论文）
    filtered = [p for p in filtered if p["keywords"]]
    # 去掉过短 abstract
    filtered = [p for p in filtered if len(p.get("abstract", "")) >= 60]

    # 同一主题可能多版本，按 arxiv_id 去重取最新
    seen, uniq = set(), []
    for p in filtered:
        base = p["arxiv_id"].split("v")[0] if p["arxiv_id"] else p["arxiv_id"]
        if base in seen:
            continue
        seen.add(base)
        uniq.append(p)
    filtered = uniq

    # 按 relevance 降序（给 Agent 一个倾向，真正的选题判断交给 Agent）
    filtered.sort(key=lambda p: p["relevance"], reverse=True)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "count": len(filtered),
        "papers": filtered,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"抓取完成: {len(filtered)} 篇命中（最近 {args.days} 天），已写入 {args.output}")
    for p in filtered:
        print(f"  - [{p['published'][:10]}] {p['arxiv_id']} {p['title']}")


if __name__ == "__main__":
    main()