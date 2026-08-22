# arxiv-pipeline

抓取 arXiv 最近的 **AI / LLM / Agent** 论文，产出结构化 JSON，供 Agent 层挑选候选选题。

## 分工

- **脚本只负责「抓取 + 结构化」**：`fetch.py` 从 arXiv API 取回最近论文，覆盖 `cs.AI / cs.LG / cs.SE / cs.CL`，按 Agent / LLM / 模型方向关键词（`agent / agentic / llm / large language model / reinforcement / world model / foundation model` 等）检索，命中任一即收，输出 `arxiv_latest.json`。
  - 每篇带 `keywords`（命中词）与 `relevance`（Agent 系权重最高，LLM / 模型系居中），供 Agent 排序初筛。
- **选题是 Agent 的活**：拿到 JSON 后，由 Agent 结合本站 Reading 专栏调性（Agent 工程型、有取舍 / 设计启发、能翻译成工程博客）选 5 个发给你。脚本不做最终选题判断。

## 用法

```bash
cd agentsrc/tools/arxiv-pipeline
python3 fetch.py                      # 默认最近 7 天，max 200
python3 fetch.py --days 7 --max 100 -o arxiv_latest.json
```

参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--days` | `7` | 取最近 N 天 |
| `--max` | `200` | API 最大返回条数 |
| `-o/--output` | `arxiv_latest.json` | 输出 JSON 路径 |

## 输出结构

`arxiv_latest.json`：

```jsonc
{
  "fetched_at": "…",          // 抓取时间
  "days": 7,
  "count": 43,                // 命中论文数
  "papers": [
    {
      "arxiv_id": "2608.17528v1",
      "title": "…",
      "url": "https://arxiv.org/abs/2608.17528v1",
      "pdf_url": "https://arxiv.org/pdf/2608.17528v1",
      "abstract": "…",
      "authors": ["…"],
      "categories": ["cs.AI"],
      "published": "2026-08-18T…",
      "keywords": ["agentic", "coding agent"],  // 命中的 Agent 关键词
      "relevance": 5.0                          // 粗分排序（Agent 层初筛用）
    }
  ]
}
```

## 稳定抓取

- 用 **arXiv 官方 API**（Atom XML），非网页爬虫。
- 遇到 `429 / 5xx` 自动退避重试（遵循 `Retry-After`）。
- 单次请求即可覆盖最近 7 天命中量，避免高频触发限流。
- 已对同一论文的多版本去重（保留最新）。

## 将来 cron（可选）

```bash
# 每天 9 点抓取并落盘到 agentsrc（作为每日入口）
0 9 * * * cd /path/to/agentsrc && python3 tools/arxiv-pipeline/fetch.py
```

> cron 方式（严格由脚本提供文件的日常调度）通常不是必须；如果只是「今天想更新一批」，`fetch.py` 本身就以一天粒度抓取，够用。