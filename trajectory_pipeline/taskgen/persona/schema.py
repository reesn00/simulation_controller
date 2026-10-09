"""PersonaProfile——用户画像的值对象与**取值域定义**。

六个维度每个都对应一个**可观察的语言特征**，不是凭空设的标签
（对照见 :mod:`trajectory_pipeline.taskgen.persona.lexicon`）：

============  ==========================  ==================================
维度          影响的语言现象              对下游的作用
============  ==========================  ==================================
genre         领域词汇（"科幻"/"纪录片"）  搜索词构造、站点类型预期
popularity    用不用站点名/别名/译名      头部站直达 vs 长尾多站遍历
urgency       催促语、"今晚就看"          首轮长度、单站点停留预算
verbal_style  句长、语气词、网络用语      表述生成长度与噪声容差
persona_presence  身份线索句             首轮附带信息量
task_specificity   指名 vs 指代          搜索查询构造难度
============  ==========================  ==================================

⚠️ **两个字段与方案 §3.1.2 的偏离，都是为了让「来源纪律」真正成立**：

**① ``content_tier`` 不在画像上，在组合层推导。**
方案把 ``content_tier``（A/B/C，评估切片轴）列为 PersonaProfile 的字段，
同一节却要求它「**不得由 LLM 自评，必须来自任务骨架的客观属性**」。
这两条不能同时成立于「画像自带一个 tier 字段」——只要它躺在画像上，
它就既可能被 LLM 自评，也可能被人工随手填，两种情况下切片轴都不可信。
本实现的取舍是服从纪律：档位在 :meth:`PersonaProfile.content_tier_for`
里由 ``popularity`` 与骨架的客观属性推出，画像 yaml 里**没有**这个键。

**② ``task_specificity`` 是「用户偏好」，不是「本次表述的事实」。**
用户可以习惯指代；但当骨架**根本没有具体片名**时（存量 98 个 task 里有 19 个
是"找最近上映的科幻电影在线观看"这类泛指），任何偏好都渲染不出片名——
那是任务的事实，不是用户的意愿。所以 persona 上存的是**偏好**，
实际渲染成什么由 :mod:`renderer` 依据骨架的 ``title_available`` 决定，
两个值分别落进 provenance（``requested`` / ``actual``）。
**人为设定不得覆盖事实**，这条比"画像该有几个字段"重要。
"""

from __future__ import annotations

from typing import Literal, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── 六个维度的取值域 ──────────────────────────────────────────────────
# 刻意收窄成 Literal 而不是自由字符串：取值域写在这里，
# 采样器才能按维度做均匀覆盖，而「自由字符串的 persona 库」
# 等于没有 persona 库——只能靠人去翻 yaml 看有什么。

Genre: Final = Literal[
    "动作", "悬疑", "科幻", "喜剧", "纪录片", "动画", "综艺", "恐怖", "爱情", "战争",
]
Popularity: Final = Literal["头部", "腰部", "长尾", "冷门"]
Urgency: Final = Literal["立即", "本周", "闲时"]
VerbalStyle: Final = Literal["书面", "口语", "强口语"]
PersonaPresence: Final = Literal["无", "轻", "重"]
Specificity: Final = Literal["指名", "半指代", "纯描述"]
Tier: Final = Literal["A", "B", "C"]

#: 每个维度的完整取值域，供 :mod:`library` 做覆盖度检查。
#: 单独列出来是因为 pydantic 的 ``get_args`` 在 ``Final`` 注解上取不到，
#: 而「库里这个维度到底覆盖了几档」是采样前必须知道的事。
DIMENSION_DOMAIN: Final[dict[str, tuple[str, ...]]] = {
    "genre": ("动作", "悬疑", "科幻", "喜剧", "纪录片",
              "动画", "综艺", "恐怖", "爱情", "战争"),
    "popularity": ("头部", "腰部", "长尾", "冷门"),
    "urgency": ("立即", "本周", "闲时"),
    "verbal_style": ("书面", "口语", "强口语"),
    "persona_presence": ("无", "轻", "重"),
    "task_specificity": ("指名", "半指代", "纯描述"),
}


class PersonaProfile(BaseModel):
    """一个用户画像。

    **不可变**（``frozen=True``）：画像一旦入库就不能改，否则同一条轨迹
    重放时会算出不同的表述，存档与重跑对不上（违反感知层 I3 幂等的同源要求）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_id: str = Field(min_length=1)
    genre: Genre
    popularity: Popularity
    urgency: Urgency
    verbal_style: VerbalStyle
    persona_presence: PersonaPresence
    task_specificity: Specificity = "指名"

    #: 70% True / 30% False。False = 用户手里没有可对照的标准，
    #: 于是不会"等确认了才行动"——这批样本教的是**在信息不全时先动**。
    #: 采样器 (:mod:`trajectory_pipeline.taskgen.sampler`) 强制这个配比，
    #: 库里的初值只作为默认。
    has_standard: bool = True

    # ── 以下不是画像字段 ────────────────────────────────────────────
    # content_tier **故意缺席**：见模块 docstring ①。

    @field_validator("persona_id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("persona_id 不能为空串")
        return v

    # ── 组合层派生 ───────────────────────────────────────────────────

    def content_tier_for(self, *, skeleton_popularity: str | None = None) -> Tier:
        """由画像与骨架的客观属性推导评估切片档位（A/B/C）。

        ``skeleton_popularity`` 是**骨架侧**的客观属性（如目标作品是否头部站点），
        可为 ``None``（存量 task 没有这个字段）。它的作用是**否决**：
        画像说"头部"但骨架指向一部冷门作品时，档位按骨架走——
        切片轴错配会让"头部 × 长尾"这类组合的分母失真，而分母失真
        是最容易被读成"模型在头部题材上退化"的一类假信号。

        派生规则（刻意简单，且**可复算**）：

        ==========  ======================================
        条件        档位
        ==========  ======================================
        冷门        C
        头部且骨架不否决  A
        其余        B
        ==========  ======================================
        """
        if self.popularity == "冷门":
            return "C"
        if skeleton_popularity == "冷门":
            return "C"
        if self.popularity == "头部" and skeleton_popularity in (None, "头部"):
            return "A"
        return "B"

    def profile_dict(self) -> dict[str, str | bool]:
        """落 provenance 用的扁平字典。

        刻意**不含** ``persona_id`` 之外的派生字段，也**不含** content_tier
        （它属于 persona × skeleton 的组合，落 TaskInstance 而非画像）。
        """
        return {
            "persona_id": self.persona_id,
            "genre": self.genre,
            "popularity": self.popularity,
            "urgency": self.urgency,
            "verbal_style": self.verbal_style,
            "persona_presence": self.persona_presence,
            "task_specificity": self.task_specificity,
            "has_standard": self.has_standard,
        }


def load_profile(data: object, *, source: str = "") -> PersonaProfile:
    """把一条已解析的映射校验成 :class:`PersonaProfile`。

    单独提供而不是只靠 pydantic 的 ``model_validate``，是为了让**报错信息
    指明是哪个 persona 哪一维出错**。persona 库是人手写的 yaml，
    一次笔误（写成"强口语 "带尾空格、或 ``"口语化"``）会让整批采样静默少一档，
    而错误信息里没有 persona_id 的话根本查不出是哪一条。
    """
    if not isinstance(data, dict):
        raise ValueError(f"persona 记录不是映射: {type(data).__name__}"
                         f"{f'（{source}）' if source else ''}")
    pid = str(data.get("persona_id", "")).strip() or f"<{source or '未标注来源'}>"
    try:
        return PersonaProfile.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 细节长，逐行透传
        raise ValueError(f"persona {pid} 校验失败: {exc}") from exc