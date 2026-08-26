import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'
import { giscusPlugin } from 'vitepress-plugin-giscus'

export default withMermaid(defineConfig({
  srcExclude: ['agent/**', 'archive/**', 'jds/**', 'tmp/**', 'docs/**', 'node_modules/**', '**/verify-*.md', 'AGENTS.md', 'README.md', 'TODO.md'],
  base: '/agentsrc/',
  lang: 'zh-CN',
  title: 'Agent Src',
  description: 'AI Agent 源码精读 — 逐行拆解 Claude Code、OpenCode、Codex 与 DeepSeek Harness 的架构设计、实现原理与工程哲学',

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
    ['link', { rel: 'icon', type: 'image/svg+xml', href: '/agentsrc/logo.svg' }],
    ['link', { rel: 'stylesheet', href: '/agentsrc/custom.css' }],
    ['meta', { property: 'og:site_name', content: 'Agent Src' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:locale', content: 'zh-CN' }],
    ['meta', { property: 'og:url', content: 'https://shakl0ne.github.io/agentsrc/' }],
    ['meta', { property: 'og:title', content: 'Agent Src — AI Agent 源码精读' }],
    ['meta', { property: 'og:description', content: 'AI Agent 源码精读 — 逐行拆解 Claude Code、OpenCode、Codex 与 DeepSeek Harness 的架构设计、实现原理与工程哲学' }],
    ['meta', { property: 'og:image', content: 'https://shakl0ne.github.io/agentsrc/og-banner.png' }],
    ['meta', { property: 'og:image:width', content: '1200' }],
    ['meta', { property: 'og:image:height', content: '630' }],
    ['meta', { property: 'og:image:alt', content: 'Agent Src — AI Agent 源码精读' }],
    ['meta', { name: 'twitter:card', content: 'summary_large_image' }],
    ['meta', { name: 'twitter:site', content: '@shakl0ne' }],
    ['meta', { name: 'twitter:title', content: 'Agent Src — AI Agent 源码精读' }],
    ['meta', { name: 'twitter:description', content: '逐行拆解 Claude Code、OpenCode、Codex 与 DeepSeek Harness 的架构设计、源码实现与工程哲学' }],
    ['meta', { name: 'twitter:image', content: 'https://shakl0ne.github.io/agentsrc/og-banner.png' }],
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
      /* Zoom-in cursor on all mermaid diagrams */
      .mermaid svg { cursor: zoom-in; }
    `],
    ['script', {}, `
(function(){
  function init() {
    document.addEventListener('click', function(e){
      const svg = e.target.closest('.mermaid svg');
      if (!svg) return;
      if (document.querySelector('.mermaid-lightbox')) return;

      const overlay = document.createElement('div');
      overlay.className = 'mermaid-lightbox';

      const inner = document.createElement('div');
      inner.className = 'mermaid-lightbox__inner';

      const clone = svg.cloneNode(true);
      clone.removeAttribute('style');

      const closeBtn = document.createElement('button');
      closeBtn.className = 'mermaid-lightbox__close';
      closeBtn.setAttribute('aria-label', 'Close');
      closeBtn.innerHTML = '\\u00D7';

      function remove() { overlay.remove(); }

      overlay.addEventListener('click', remove);
      closeBtn.addEventListener('click', function(evt){ evt.stopPropagation(); remove(); });
      document.addEventListener('keydown', function esc(k){ if (k.key === 'Escape') { remove(); document.removeEventListener('keydown', esc); } });

      inner.appendChild(clone);
      inner.appendChild(closeBtn);
      overlay.appendChild(inner);
      document.body.appendChild(overlay);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
    `],
  ],

  themeConfig: {
    logo: '/logo.svg',

    nav: [
      { text: '首页', link: '/' },
      { text: 'OpenCode', link: '/opencode/01-overview' },
      { text: 'Codex', link: '/codex/01-overview' },
      { text: 'Claude Code', link: '/claudecode/01-overview' },
      { text: 'DeepSeek Harness', link: '/deepseek/01-overview' },
      { text: 'Reading', link: '/reading/01-skill-paper' },
    ],

    sidebar: {
      '/opencode/': [
        {
          text: 'OpenCode 源码精读',
          items: [
            { text: '1. 整体架构', link: '/opencode/01-overview' },
            { text: '2. 主循环 runLoop', link: '/opencode/02-runloop' },
            { text: '3. 工具系统', link: '/opencode/03-tools' },
            { text: '4. 会话压缩', link: '/opencode/04-compact' },
            { text: '5. Agent 系统', link: '/opencode/05-agents' },
            { text: '6. 上下文装配', link: '/opencode/06-context' },
            { text: '7. plan-execute-verify 编排', link: '/opencode/07-plan-execute-verify' },
          ],
        },
      ],
      '/codex/': [
        {
          text: 'Codex 源码精读',
          items: [
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
      '/claudecode/': [
        {
          text: 'Claude Code 源码精读',
          items: [
            { text: '1. 整体架构与启动流程', link: '/claudecode/01-overview' },
            { text: '2. 主循环：QueryEngine', link: '/claudecode/02-mainloop' },
            { text: '3. 工具系统：50+ 内置工具', link: '/claudecode/03-tools' },
            { text: '4. 对话压缩：5 级机制', link: '/claudecode/04-compact' },
            { text: '5. Agent 系统', link: '/claudecode/05-agents' },
            { text: '6. 命令系统：50+ 斜杠命令', link: '/claudecode/06-commands' },
            { text: '7. 权限系统：7 种权限模式', link: '/claudecode/07-permissions' },
            { text: '8. MCP 集成架构', link: '/claudecode/08-mcp' },
            { text: '9. Bridge 桥接与远程模式', link: '/claudecode/09-bridge' },
            { text: '10. 记忆系统与上下文注入', link: '/claudecode/10-memory' },
          ],
        },
      ],
      '/deepseek/': [
        {
          text: 'DeepSeek Harness 源码精读',
          items: [
            { text: '1. 全景：一切皆插件', link: '/deepseek/01-overview' },
            { text: '2. Cordis 组合框架', link: '/deepseek/02-cordis' },
            { text: '3. Agent 接口与默认 loop', link: '/deepseek/03-agent-loop' },
            { text: '4. 会话日志与上下文投影', link: '/deepseek/04-session-log' },
            { text: '5. 工具系统与执行管线', link: '/deepseek/05-tools-pipeline' },
            { text: '6. Capability 缝体系', link: '/deepseek/06-capability-seams' },
            { text: '7. 压缩 / 上下文注入 / 子代理', link: '/deepseek/07-compaction-context-subagent' },
            { text: '8. 自改 / hooks 桥 / 生态', link: '/deepseek/08-self-modification-hooks' },
          ],
        },
      ],
      '/reading/': [
{
          text: 'Reading · 论文解读与随笔',
          items: [
            { text: '1. Skill 到底为什么有效，又在哪失效', link: '/reading/01-skill-paper' },
            { text: '2. LLM 验证的「自动驾驶等级」', link: '/reading/02-verification-autonomy' },
          ],
        },
      ],
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/Shakl0ne/agentsrc' },
    ],

    footer: {
      message: 'Agent Src — AI Agent 源码精读',
      copyright: 'Copyright © 2026 Shakl0ne',
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
    sequence: {
      useMaxWidth: false,
    },
  },
}))
