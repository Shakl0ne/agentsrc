import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'
import { giscusPlugin } from 'vitepress-plugin-giscus'

export default withMermaid(defineConfig({
  srcExclude: ['agent/**', 'archive/**', 'jds/**', 'tmp/**', 'node_modules/**', '**/verify-*.md'],
  lang: 'zh-CN',
  title: 'Agent Src',
  description: 'AI Agent 源码精读 — 逐行拆解 OpenCode 与 Codex 的架构设计、实现原理与工程哲学',

  vite: {
    plugins: [
      giscusPlugin({
        repo: 'Shakl0ne/agentsrc',
        repoId: 'R_kgDOTfEJCA',
        category: 'Announcements',
        categoryId: 'DIC_kwDOTfEJCM4DBpmq',
        mapping: 'pathname',
        inputPosition: 'top',
        reactionsEnabled: true,
        lang: 'zh-CN',
        loading: 'lazy',
        showCommentBtn: true,
      }),
    ],
  },

  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: '/logo.svg' }],
    ['link', { rel: 'stylesheet', href: '/custom.css' }],
    ['meta', { property: 'og:site_name', content: 'Agent Src' }],
    ['meta', { property: 'og:type', content: 'article' }],
    ['meta', { property: 'og:locale', content: 'zh-CN' }],
    ['style', {}, `
      /* Fix mermaid foreignObject bottom text clipping */
      .mermaid .label foreignObject div {
        line-height: normal !important;
      }
      .mermaid svg {
        overflow: visible !important;
      }
      .mermaid .node foreignObject {
        overflow: visible !important;
      }
      /* Fix mermaid double border: remove inner label rect stroke */
      svg[id^="mermaid"] g.node g.label rect {
        stroke: none !important;
      }
    `],
  ],

  themeConfig: {
    logo: '/logo.svg',

    nav: [
      { text: '首页', link: '/' },
      { text: 'OpenCode', link: '/opencode/' },
      { text: 'Codex', link: '/codex/' },
    ],

    sidebar: {
      '/opencode/': [
        {
          text: 'OpenCode 源码精读',
          items: [
            { text: '专栏介绍', link: '/opencode/' },
            { text: '1. 整体架构源码精读', link: '/opencode/01-overview' },
            { text: '2. 主循环 runLoop 源码精读', link: '/opencode/02-runloop' },
            { text: '3. 工具系统源码精读', link: '/opencode/03-tools' },
            { text: '4. 上下文压缩源码精读', link: '/opencode/04-compact' },
            { text: '5. Agent 系统源码精读', link: '/opencode/05-agents' },
            { text: '6. 上下文架构源码精读', link: '/opencode/06-context' },
          ],
        },
      ],
      '/codex/': [
        {
          text: 'Codex 源码精读',
          items: [
            { text: '专栏介绍', link: '/codex/' },
            { text: '1. 全景：架构与定位', link: '/codex/01-overview' },
            { text: '2. 主循环：Submission 驱动', link: '/codex/02-mainloop' },
            { text: '3. 上下文组合与增量注入', link: '/codex/03-context' },
            { text: '4. Compact 3 种压缩机制', link: '/codex/04-compact' },
            { text: '5. 多 Agent 编排架构', link: '/codex/05-multi-agents' },
            { text: '6. 工具系统与安全沙箱', link: '/codex/06-tools-sandbox' },
            { text: '7. 模型管理与 Provider 抽象', link: '/codex/07-models' },
            { text: '8. Codex vs CC 设计哲学对比', link: '/codex/08-philosophy' },
          ],
        },
      ],
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/Shakl0ne' },
    ],

    footer: {
      message: 'Agent Src — AI Agent 源码精读',
      copyright: 'Copyright © 2026 Shaco',
    },

    editLink: false,
    lastUpdated: true,
  },

  mermaid: {
    theme: 'base',
    darkMode: true,
    themeVariables: {
      primaryColor: '#0d1b2a',
      primaryTextColor: '#e0e0e0',
      primaryBorderColor: '#00D4FF',
      secondaryColor: '#1a2a47',
      tertiaryColor: '#0d1b2a',
      background: '#0a1628',
      lineColor: '#00D4FF',
      nodeBkg: '#0f1a2e',
      nodeBorder: '#00D4FF',
      nodeTextColor: '#e0e0e0',
      edgeLabelBackground: '#0a1628',
      edgeLabelText: '#e0e0e0',
      clusterBkg: '#1a2a47',
      clusterBorder: '#00D4FF',
      titleColor: '#e0e0e0',
      defaultLinkColor: '#00D4FF',
      fontFamily: 'Inter, system-ui, sans-serif',
    },
    flowchart: {
      useMaxWidth: true,
      curve: 'basis',
    },
  },
}))
