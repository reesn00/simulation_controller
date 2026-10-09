"""Label Studio 双向通道（决策 D6：改造为双向评估节点）。

现状是单向终点（推送不回流），双向化须处理存量已踩过的坑：

    LS 1.23 无原生去重      → 本地台账 push_index__<project_id>.jsonl
    PATCH label_config 覆盖标注员改动 → 双向后禁全量 PATCH
    editable=true → annotation 是标注员修正稿 → 回流只限黄金集/评估集，
                                                   训练制品永不被覆盖
    validate/ 绿灯不算数     → 验收以标注页实视为准
    R11 凭据扫描 fail-closed → 保留，双向不豁免
"""
