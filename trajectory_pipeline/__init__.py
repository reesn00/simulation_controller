"""Agent 轨迹合成管线 v2（代码管事实层，LLM 只管语言层）。

三条不可破的纪律——违反其一即视为架构回退：

1. **导入纪律**：本包内一律 ``trajectory_pipeline.<模块>`` 绝对导入。
   禁止顶层裸导入（``from schema import X``）。
   理由：存量 gdr 因顶层风格与 ``gdr.`` 前缀风格并存，同一份源码被加载成两套
   类对象，跨边界 ``isinstance`` 静默判 False，``usage_prune`` 从未真正执行。
   本包**刻意不进** ``[tool.uv.workspace]``——那正是该事故的成因。

2. **存量隔离**：禁止 import ``simulate_serve`` / ``gdr`` / ``etl`` /
   ``orchestration`` / ``label_studio`` / ``tool_runtime``。
   存量仅供功能参考（读代码、抄思路），不建依赖边。
   外部能力走**进程边界**：MCP 协议 / HTTP，与 Python 导入无关。

3. **依赖方向**：单向，禁止回指。依赖图见 docs/设计方案/00-总体方案.md §2。

模块地图（编号对应总体方案 §3）：

    taskgen      模块 1  任务生成 + persona 画像库
    executor     模块 2  代码执行循环（控制流在这里，不在任何 LLM 里）
    perception   模块 3  语义感知（可替换插件，见 01 号文档）
    rationale    模块 4  rationale 边写边生成 + 一致性闸门三查
    assembler    模块 5  组装与切分
    evaluation   模块 6  评估体系（黄金集 + 三层瀑布 + LS 双向）

    common       跨模块地基（无业务逻辑）
    llm          LLM 客户端（全树唯一出口）
    storage      P1/P2/P3 契约读写
"""
