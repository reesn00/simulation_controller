"""生成 ``persona/library.yaml`` 的一次性构建脚本。

**不入库**（用完即弃）。产出物 ``library.yaml`` 是普通数据文件，人照常手改。

为什么用生成而不是手写
---------------------
覆盖度是这个库的核心约束：六维度里任何一档缺了，切片表就会多出一个
**只有单格**的行，而那种行的分母小到任何"强口语 × 长尾表现退化"的结论
都能被单条样本翻转——报告会以"发现显著问题"的样子呈现一个统计噪声。
手写 40 条必然漏档，且漏了不报错。

所以这里用**人群模板 × 品类**交叉生成：模板固定五个维度，
只让 ``genre`` 轮转，于是每个维度的每一档都被成比例覆盖，
而每一条画像又对应一个**真实存在的人群**（"追剧学生""带娃的家长"），
不是笛卡尔积里凭空造的无意义组合。

``has_standard`` 的裸任务按 :data:`BARE_TASK_RATIO` 就地分布到具体画像上，
而不是单独造一批——虚构画像会稀释每个维度的自然比例。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).with_name("library.yaml")
BARE_RATIO = 0.3

#: 人群模板。五个维度固定，``genres`` 决定这个人群看哪些品类。
#:
#: 模板数与每模板的 genre 数是**按覆盖度反推**的，不是凭感觉堆的。
#: 初版 8 模板 24 条，体检立刻抓到两处会出**单格行**的切片轴：
#: ``genre=恐怖`` 只 1 条、``task_specificity=半指代`` 只 3 条。
#: 分母 1 意味着"恐怖题材上是否退化"能被一条样本决定方向，
#: 而报告会以"发现显著问题"的样子呈现它。现在补到 11 模板 44 条。
TEMPLATES: tuple[dict[str, object], ...] = (
    {
        "key": "binge",
        "name": "追剧学生",
        "popularity": "长尾", "urgency": "立即", "verbal_style": "强口语",
        "persona_presence": "轻", "task_specificity": "指名",
        "genres": ("悬疑", "科幻", "动作", "恐怖"),
    },
    {
        "key": "office",
        "name": "加班后的上班族",
        "popularity": "腰部", "urgency": "本周", "verbal_style": "口语",
        "persona_presence": "重", "task_specificity": "指名",
        "genres": ("喜剧", "爱情", "动作", "战争"),
    },
    {
        "key": "cinephile",
        "name": "影视资料收藏者",
        "popularity": "头部", "urgency": "闲时", "verbal_style": "书面",
        "persona_presence": "无", "task_specificity": "半指代",
        "genres": ("战争", "悬疑", "喜剧", "纪录片"),
    },
    {
        "key": "student",
        "name": "穷学生党",
        "popularity": "冷门", "urgency": "立即", "verbal_style": "强口语",
        "persona_presence": "轻", "task_specificity": "纯描述",
        "genres": ("动画", "科幻", "喜剧", "爱情"),
    },
    {
        "key": "parent",
        "name": "带娃的家长",
        "popularity": "腰部", "urgency": "本周", "verbal_style": "口语",
        "persona_presence": "重", "task_specificity": "指名",
        "genres": ("动画", "综艺", "纪录片", "喜剧"),
    },
    {
        "key": "commuter",
        "name": "地铁通勤族",
        "popularity": "头部", "urgency": "本周", "verbal_style": "口语",
        "persona_presence": "重", "task_specificity": "纯描述",
        "genres": ("喜剧", "综艺", "爱情", "战争"),
    },
    {
        "key": "doc",
        "name": "纪录片观众",
        "popularity": "冷门", "urgency": "闲时", "verbal_style": "书面",
        "persona_presence": "无", "task_specificity": "指名",
        "genres": ("纪录片", "战争", "悬疑", "科幻"),
    },
    {
        "key": "variety",
        "name": "综艺党",
        "popularity": "头部", "urgency": "立即", "verbal_style": "强口语",
        "persona_presence": "轻", "task_specificity": "指名",
        "genres": ("综艺", "喜剧", "科幻", "爱情"),
    },
    # ↓↓ 下面三个专补初版的两处单格行：恐怖 / 综艺 / 动画 各只有 1–2 条
    {
        "key": "half",
        "name": "说得出类型说不出片名的人",
        "popularity": "长尾", "urgency": "本周", "verbal_style": "书面",
        "persona_presence": "无", "task_specificity": "半指代",
        "genres": ("科幻", "恐怖", "悬疑", "爱情"),
    },
    {
        "key": "horror",
        "name": "恐怖片爱好者",
        "popularity": "冷门", "urgency": "闲时", "verbal_style": "书面",
        "persona_presence": "无", "task_specificity": "半指代",
        "genres": ("恐怖", "动作", "悬疑", "科幻"),
    },
    {
        "key": "kids",
        "name": "亲子动画党",
        "popularity": "腰部", "urgency": "本周", "verbal_style": "口语",
        "persona_presence": "重", "task_specificity": "指名",
        "genres": ("动画", "综艺", "纪录片", "喜剧"),
    },
)


def build() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for tpl in TEMPLATES:
        for genre in tpl["genres"]:                      # type: ignore[union-attr]
            rows.append({
                "persona_id": f"{tpl['key']}-{genre}",
                "genre": genre,
                "popularity": tpl["popularity"],
                "urgency": tpl["urgency"],
                "verbal_style": tpl["verbal_style"],
                "persona_presence": tpl["persona_presence"],
                "task_specificity": tpl["task_specificity"],
                # 下面两项目标分布；实际值在 main() 里均匀撒开。
                "has_standard": True,
            })

    # 裸任务均匀撒开：每 floor(3/n) 条里有一条，末尾补齐到精确比例。
    want_bare = round(len(rows) * BARE_RATIO)
    step = len(rows) / want_bare if want_bare else 0
    targets = {min(len(rows) - 1, int(i * step)) for i in range(want_bare)}
    for i in targets:
        rows[i]["has_standard"] = False      # type: ignore[index]
    return rows


def dump(rows: list[dict[str, object]]) -> None:
    lines = [
        "# 用户画像库——persona × scenario 交叉采样的 persona 侧输入。",
        "#",
        "# ⚠️ 改这个文件前先跑 `python -m trajectory_pipeline.executor.cli check-persona`：",
        "#   覆盖度体检（每档是否有画像）与退化维度（只落在 ≤2 档的维度）。",
        "#   一个维度只落在两档时，切片表里那一行只有单格，分母小到任何结论",
        "#   都能被单条样本翻转。",
        "#",
        "# 本文件由 `taskgen/persona/_gen_library.py` 生成过一次，覆盖度由此保证；",
        "# 之后手改完全有效，增删画像不破坏任何结构约束。",
        "#",
        "# 字段说明见 taskgen/persona/schema.py；语言特征见 lexicon.py。",
        "# 注意：**没有** content_tier 字段——它由 persona × 骨架的组合推导，",
        "# 画像自带它就等于可以被随手填，切片轴随之失信。",
        "",
        "personas:",
    ]
    for row in rows:
        lines.append(f"- persona_id: {row['persona_id']}")
        lines.append(f"  genre: {row['genre']}")
        lines.append(f"  popularity: {row['popularity']}")
        lines.append(f"  urgency: {row['urgency']}")
        lines.append(f"  verbal_style: {row['verbal_style']}")
        lines.append(f"  persona_presence: {row['persona_presence']}")
        lines.append(f"  task_specificity: {row['task_specificity']}")
        lines.append(f"  has_standard: {str(row['has_standard']).lower()}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {OUT}（{len(rows)} 条，裸任务 "
          f"{sum(1 for r in rows if not r['has_standard'])} 条）")


if __name__ == "__main__":
    dump(build())