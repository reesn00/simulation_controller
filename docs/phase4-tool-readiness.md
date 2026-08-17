# Phase 4 工具可用性

默认配置结果：

```text
Tool readiness
  playwright     DISABLED
  camoufox       DISABLED
```

启用后检查包括依赖、构造、MCP connect、工具发现、Schema 和本地 `about:blank` probe。缺包、缺浏览器或 probe 失败都会打印原因，普通启动不会自动下载。

```powershell
python -m simulate_serve --check-tools
```
