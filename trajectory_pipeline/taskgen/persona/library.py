"""画像库的加载、覆盖度体检与分层采样。

三个动作，顺序不能换：**先体检，再采样**。

覆盖度体检
----------
:func:`coverage` 报的是「每个维度实际覆盖了几档」。这件事必须在采样**之前**
看一眼，原因不是形式主义：六维度里只要有一维只落在两档，
最终的切片表就会出现一个只有单格的行，而那一行的分母小到
任何「强口语 × 长尾表现退化」的结论都能被单条样本翻转——
**报告会以"发现了一个显著问题"的样子呈现一个统计噪声。**
覆盖度是切片轴可信度的前提。

分层采样
--------
不做独立随机，而是**按维度分层轮转**。理由很实际：独立随机的期望分布是对的，
但小样本下偏差极大——抽 10 条画像可能 8 条都是「头部 + 立即 + 口语」。
切片表里那一格有 8 条、其余格 0 条，于是"长尾题材上是否退化"这个问题
在采样结束前就已经无法回答了。轮转让每个桶都拿到样本，
小样本下覆盖面是**保证**的而不是**期望**。

确定性：同 ``seed`` 同库 → 同结果（与感知层 I3 幂等同源要求）。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from trajectory_pipeline.taskgen.persona.schema import (
    DIMENSION_DOMAIN,
    PersonaProfile,
    load_profile,
)

#: 画像库落点。放在包内而非 ``output/``——它是**输入**（人手写的源数据），
#: 该跟代码一起进版本控制，否则换个机器就没法复现批次。
DEFAULT_LIBRARY_PATH: Final = Path(__file__).with_name("library.yaml")

#: 裸任务（``has_standard=False``）的目标配比。
#:
#: 30% 不是随便取的：没有这批样本，训练出来的模型会**默认等确认才行动**——
#: 用户问得含糊时它反问一句，而反问在真实对话里通常就是失败。
#: 但也不能更高，否则任务本身变得可自校验，评估集与真实分布脱节。
BARE_TASK_RATIO: Final = 0.3


class LibraryError(ValueError):
    """画像库不可用。显式抛，不静默降级。"""


@dataclass(frozen=True, slots=True)
class PersonaLibrary:
    """一个画像库。不可变——库改了就是换了实验条件，不该原地改。"""

    profiles: tuple[PersonaProfile, ...]
    source: str = ""

    def __len__(self) -> int:
        return len(self.profiles)

    def __iter__(self) -> Iterable[PersonaProfile]:
        return iter(self.profiles)

    def by_id(self, persona_id: str) -> PersonaProfile:
        for p in self.profiles:
            if p.persona_id == persona_id:
                return p
        raise KeyError(f"库里没有 persona {persona_id!r}（共 {len(self.profiles)} 条）")

    # ── 覆盖度体检 ────────────────────────────────────────────────

    def coverage(self) -> dict[str, dict[str, int]]:
        """每个维度的取值覆盖。**缺档会显式列出来**，不是只报已覆盖的。"""
        out: dict[str, dict[str, int]] = {}
        for dim, domain in DIMENSION_DOMAIN.items():
            counts = {value: 0 for value in domain}
            for p in self.profiles:
                counts[str(getattr(p, dim))] += 1
            out[dim] = counts
        return out

    def missing_dims(self) -> dict[str, tuple[str, ...]]:
        """**从未被任何画像覆盖**的取值。空 dict 才算健康。"""
        out: dict[str, tuple[str, ...]] = {}
        for dim, counts in self.coverage().items():
            gaps = tuple(v for v, n in counts.items() if n == 0)
            if gaps:
                out[dim] = gaps
        return out

    def degenerate_dims(self) -> dict[str, int]:
        """只落在 ≤2 档的维度——见模块 docstring：这类维度撑不起切片表。"""
        return {
            dim: sum(1 for n in counts.values() if n > 0)
            for dim, counts in self.coverage().items()
            if 0 < sum(1 for n in counts.values() if n > 0) <= 2
        }

    # ── 采样 ──────────────────────────────────────────────────────

    def stratified_sample(
        self,
        n: int,
        *,
        seed: int = 0,
        strata: Sequence[str] = ("genre", "verbal_style", "urgency"),
        bare_ratio: float = BARE_TASK_RATIO,
    ) -> tuple[PersonaProfile, ...]:
        """分层轮转采 ``n`` 条。

        ``strata`` 列出的维度会**依次轮转**：先按 ``genre`` 各桶取一轮，
        再按 ``verbal_style`` 各桶取一轮，……。所以传三个维度时，
        采样结果的维度组合是均匀散开的，而不是"genre 均匀、其余随机"。

        ``bare_ratio`` 是 ``has_standard=False`` 的**精确目标比例**。
        库里裸任务不够时**报错而不是悄悄降配比**——配比降了不会有任何
        外部症状，只会让"30% 裸任务"这个设计在报告里消失。
        """
        if n <= 0:
            return ()
        if n > len(self.profiles):
            raise LibraryError(
                f"要采 {n} 条但库里只有 {len(self.profiles)} 条；"
                f"放大库或减少采样量，不要重复采样凑数"
            )

        unknown = [d for d in strata if d not in DIMENSION_DOMAIN]
        if unknown:
            raise LibraryError(f"未知分层维度 {unknown}；可用：{sorted(DIMENSION_DOMAIN)}")

        rng = random.Random(f"{seed}:{len(self.profiles)}")
        pool = list(self.profiles)

        with_std = [p for p in pool if p.has_standard]
        bare = [p for p in pool if not p.has_standard]
        want_bare = round(n * bare_ratio)
        if want_bare > len(bare):
            raise LibraryError(
                f"要 {want_bare} 条裸任务（{bare_ratio:.0%} × {n}）但库里只有 "
                f"{len(bare)} 条；配比是评估切片的基础，降配比不会报错、"
                f"只会让这批数据在报告里悄悄失去对照"
            )
        if want_bare > len(with_std) or n - want_bare > len(with_std):
            raise LibraryError(
                f"按 bare_ratio={bare_ratio} 切分后需要的带标���画像超过库存："
                f"带标准 {len(with_std)} 条 / 裸任务 {len(bare)} 条 / 要采 {n} 条"
            )

        chosen = _round_robin(with_std, n - want_bare, strata, rng)
        chosen += _round_robin(bare, want_bare, strata, rng)
        rng.shuffle(chosen)          # 打断轮转痕迹，避免下游误读顺序即分布
        return tuple(chosen)

    def digest(self) -> str:
        """库内容指纹。进每条样本的 provenance。

        有了它，"这两批数据能不能比"就有客观依据；没有的话，
        两批用了不同的画像库却混在一张表里比较，是评估里最常见的不可比来源。
        """
        blob = "\n".join(
            "|".join(str(v) for v in p.profile_dict().values()) for p in self.profiles
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _round_robin(
    pool: Sequence[PersonaProfile],
    n: int,
    strata: Sequence[str],
    rng: random.Random,
) -> list[PersonaProfile]:
    """按 ``strata`` 逐维轮转取样。同维度桶内按 rng 顺序取。"""
    taken: list[PersonaProfile] = []
    used: set[str] = set()

    for dim in strata:
        buckets: dict[str, list[PersonaProfile]] = {}
        for p in pool:
            if p.persona_id in used:
                continue
            buckets.setdefault(str(getattr(p, dim)), []).append(p)
        for value in sorted(buckets):
            if len(taken) >= n:
                return taken
            rng.shuffle(buckets[value])
            for p in buckets[value]:
                if len(taken) >= n:
                    return taken
                taken.append(p)
                used.add(p.persona_id)

    # 维度轮转取不满（池子被前面几轮吃掉大半）→ 剩余的按 id 顺序补。
    if len(taken) < n:
        for p in pool:
            if len(taken) >= n:
                break
            if p.persona_id not in used:
                taken.append(p)
                used.add(p.persona_id)
    return taken


def load_library(path: Path | str = DEFAULT_LIBRARY_PATH) -> PersonaLibrary:
    """从 yaml 读画像库。

    **库为空直接报错。** 空库的表现是"采样返回空 → 批次产出零样本"，
    而零样本的失败点在下游（跑了一小时才发现没数据），
    远不如在加载时就炸。
    """
    import yaml

    p = Path(path)
    if not p.exists():
        raise LibraryError(
            f"画像库 {p} 不存在。新树不 import 存量，画像库是新树自己的输入，"
            f"缺失就是缺失，不猜默认路径"
        )
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LibraryError(f"读 {p} 失败: {type(exc).__name__}: {exc}") from exc

    raw = (data or {}).get("personas")
    if not isinstance(raw, list) or not raw:
        raise LibraryError(f"{p} 里没有 personas 列表（或为空）")

    profiles = tuple(load_profile(item, source=f"{p.name}#{i}")
                     for i, item in enumerate(raw))

    ids = [x.persona_id for x in profiles]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise LibraryError(f"{p} 里 persona_id 重复：{sorted(dup)}；"
                           f"重复 id 会让 provenance 指向两条画像，切片轴失真")

    return PersonaLibrary(profiles=profiles, source=str(p))