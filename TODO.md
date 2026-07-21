# 待办

## 已完成
- [x] 网站从 career 根目录独立到 `agentsrc/` 子目录
- [x] 品牌文案：「AI Agent 源码精读」定位确认
- [x] 首页重写：双栏导读目录 + 推荐阅读路线 + 三栏对比表 + 关于作者
- [x] 全站 14 篇文章内部链接统一（OpenCode 6 篇 + Codex 8 篇）
- [x] 53 张 AI 概念图生成并嵌入
- [x] mermaid 双 border 修复 + 暗色主题统一
- [x] Hero 居中对齐（`public/custom.css`）
- [x] SVG Logo + favicon
- [x] AGENTS.md：通用图像生成规范与部署说明
- [x] 导航栏精简（首页 | OpenCode | Codex）
- [x] GitHub 链接统一指向个人主页

## 待做
- [ ] **Giscus 评论系统激活**（代码已集成，待用户在 GitHub 侧激活）
  - 代码侧已完成：`.vitepress/config.mts` 引入 `giscusPlugin`，所有文章页底部自动注入评论区 + 右下角悬浮按钮
  - 当前状态：评论组件已渲染，因目标 repo `Shakl0ne/agentsrc-comments` 未创建而显示错误
  - 用户激活步骤（5 步）：
    1. 在 GitHub 创建公开仓库 `Shakl0ne/agentsrc-comments`
    2. Settings → General → Features 勾选 Discussions
    3. 安装 [Giscus App](https://github.com/apps/giscus) 到该仓库
    4. 去 [giscus.app](https://giscus.app/zh-CN) 获取 `repo-id` 和 `category-id`
    5. 替换 `.vitepress/config.mts` 中 `REPO_ID_PLACEHOLDER` 和 `CATEGORY_ID_PLACEHOLDER`
  - 完成后 `npm run dev` 即可看到评论区生效

## 长期可选
- [ ] 搜索功能（VitePress localSearch 或 Algolia）
- [ ] PWA 离线阅读
- [ ] 文章最后更新时间显示
- [ ] RSS 订阅
