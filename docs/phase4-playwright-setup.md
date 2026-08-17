# Phase 4 Playwright MCP Setup

版本固定在 `tool_runtime/playwright/package.json` 和 `package-lock.json`：`@playwright/mcp 0.0.78`。禁止使用 `latest`。

显式安装：

```powershell
Set-Location tool_runtime/playwright
npm ci
npx playwright install chromium
Set-Location ../..
python -m simulate_serve --check-tools
```

应用启动和健康检查不会执行 `npm install` 或下载浏览器。默认配置为 headless、isolated、阻止 service worker、不共享 Profile、不开放任意文件访问。

URL allow/block 不是强安全沙箱；生产环境若需要访问敌意目标，应另配 egress proxy/sandbox。
