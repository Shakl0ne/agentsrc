#!/usr/bin/env python3
"""arXiv 论文 GitHub 热度补查。

读取 fetch.py 产出的 `arxiv_latest.json`，为论文补上 GitHub star 数，
作为选题排序的参考维度之一（真正选题仍由 Agent 综合判断）。

做法：
- 优先从摘要提取显式 `github.com/owner/repo` 链接（精确命中，查仓库详情）。
- 否则按论文标题 + 主关键词去 GitHub 搜索仓库，取顶部最匹配项。
- 通过 `gh api`（复用已登录凭据，keyring）调用，规避未认证 60 次/小时限制。

注意：GitHub 搜索 API 认证后约 30 次/分钟，建议用 --limit 控制单批数量。

用法:
    python3 stars.py             # 处理 relevance 前 50 篇
    python3 stars.py --limit 20
    python3 stars.py --all       # 全部（慎用，易限流）
    python3 stars.py -o arxiv_stars.json
"""

import argparse
import json
import re
import subprocess
import sys
import time

PATH_RE = re.compile(r"github\.com[/:]([\w.-]+/[\w.-]+)", re.IGNORECASE)


def _gh(args: list, retries: int = 3):
    """执行 gh api，带退避重试。成功返回 stdout 文本，失败返回 None。"""
    base = ["gh", "api"]
    cmd = base + args
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            wait = min(60, 10 * (attempt + 1))
            print(f"[timeout] {' '.join(cmd)}，{wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
            continue
        if p.returncode == 0:
            return p.stdout
        if "rate limit" in p.stderr.lower():
            wait = 15 * (attempt + 1)
            print(f"[rate-limit] {wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
            continue
        # 404 等：仓库不存在，直接放弃
        return None
    return None


def _clean_path(p):
    return p.strip().rstrip("./ ,;")


def _explicit_repo(paper: dict):
    """从摘要提取 github.com/owner/repo，去掉尾部标点。"""
    m = PATH_RE.search(paper.get("abstract", "") or "")
    if m:
        return _clean_path(m.group(1))
    return None


def _repo_stars(full_name: str):
    """查显式仓库的星星。返回 (full_name, stars, html_url)。"""
    out = _gh(["repos/" + full_name])
    if not out:
        return None, 0, ""
    try:
        d = json.loads(out)
        return d.get("full_name"), d.get("stargazers_count", 0), d.get("html_url", "")
    except Exception:
        return None, 0, ""


def _search_stars(title: str):
    """按标题搜最匹配仓库。返回 (full_name, stars, html_url)。"""
    if not title:
        return None, 0, ""
    # 用标题前几个词作为搜索词，降低噪音
    query = title[:120]
    out = _gh(["-X", "GET", "search/repositories", "-f", f"q={query}", "--jq", ".items[0]"])
    if not out:
        return None, 0, ""
    try:
        first = json.loads(out)
        if not first:
            return None, 0, ""
        return (
            first.get("full_name"),
            first.get("stargazers_count", 0),
            first.get("html_url", ""),
        )
    except Exception:
        return None, 0, ""


def main():
    parser = argparse.ArgumentParser(description="补查论文 GitHub 星星")
    parser.add_argument("-i", "--input", default="arxiv_latest.json", help="fetch 输出")
    parser.add_argument("-o", "--output", default="arxiv_stars.json", help="输出 JSON")
    parser.add_argument("--limit", type=int, default=50, help="单批处理篇数（按 relevance）")
    parser.add_argument("--all", action="store_true", help="处理全部")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        papers = json.load(f).get("papers", [])

    papers = sorted(papers, key=lambda p: p.get("relevance", 0), reverse=True)
    if not args.all:
        papers = papers[: args.limit]

    results = []
    for i, p in enumerate(papers, 1):
        arxiv_id = p.get("arxiv_id", "")
        title = p.get("title", "")

        repo = _explicit_repo(p)
        method = "explicit"
        if repo:
            full, stars, url = _repo_stars(repo)
        else:
            full, stars, url = _search_stars(title)
            method = "search"

        results.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "repo": full or "",
                "repo_url": url or "",
                "stars": stars or 0,
                "method": method,
            }
        )
        # 搜索接口限流：小间隔
        time.sleep(1)
        if i % 20 == 0:
            print(f"  ... 已处理 {i}/{len(papers)}", file=sys.stderr)

    payload = {
        "count": len(results),
        "stars": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"处理 {len(results)} 篇，写入 {args.output}")


if __name__ == "__main__":
    main()