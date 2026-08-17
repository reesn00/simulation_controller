# Phase 5 Camoufox Setup

Camoufox 是可选 Provider，锁定 `cloverlabs-camoufox==0.6.0`，直接使用 `AsyncCamoufox`，不使用实验性远端/MCP 封装。

```powershell
uv sync --extra browser-camoufox
python -m camoufox fetch
python -m simulate_serve --check-tools
```

程序启动不会执行 fetch。默认 headless、固定 locale/OS、关闭 humanize，不使用 persistent profile、geoip 或自动定位。

Playwright 为主 Provider；只有 `bot_blocked/renderer_crash/browser_incompatible` 允许回退。404、付费墙、策略拒绝、验证码或业务验证失败不触发回退，也不提供 CAPTCHA 绕过。
