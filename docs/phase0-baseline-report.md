# Phase 0 基线报告

日期：2026-08-15

## 结果

- CLI 入口已修复为 `simulate_serve.__main__:main`。
- `--help` 不初始化运行层、不创建日志文件、不连接网络。
- pytest、pytest-asyncio、pytest-cov、pytest-socket 和 build 已进入 dev 依赖。
- Windows asyncio 需要本机 socketpair，测试仅允许 `127.0.0.1/::1`，禁止公网。
- pytest 临时目录固定为工作区 `.pytest-tmp`，不使用当前无写权限的系统 Temp。
- Wheel 已验证包含三份正式 YAML，不包含重复配置文件。

## 基线命令

```powershell
python -m simulate_serve --help
python -m pytest -q
python -m build --wheel --no-isolation
```

当前远端 Agent 不可用，因此真实 HTTP 联调不是 Phase 0–6 的阻断门禁。
