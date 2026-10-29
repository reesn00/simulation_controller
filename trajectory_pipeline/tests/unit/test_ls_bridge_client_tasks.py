"""``ls_bridge.client`` 的**读 task / annotation** 那部分（**不联网**）。

为什么单独一个文件
------------------
R1 实测打翻过初稿的写法（2026-10-29，LS 1.23.0）：

* ``GET /api/projects/{id}/export?exportType=JSON`` 返回的是 **list**，
  而且**只含已标注的 task**（一条标注都没有时是 ``[]``）——
  既不是初稿假设的 ``{"tasks": [...], "annotations": [...]}``，
  也**满足不了**「没复核的行也要出一行」这条要求；
* ``GET /api/projects/{id}/tasks?page_size=N`` **不分页**（只是截断）⇒
  一版 294 条只能拿到前 N 条，剩下的**静默少掉**；
* ``GET /api/tasks?project={id}&page_size=N&page=P`` 才是**能翻页取全**的那个。

⇒ ``export_project_json`` 改成从 ``/api/tasks`` 取。这几条用假 client 钉住。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.evaluation.ls_bridge.client import LabelStudioClient
from trajectory_pipeline.evaluation.ls_bridge.errors import LabelStudioError


class _Fake:
    """按 URL 形态回话的假 client（**只截 `_request`**）。"""

    def __init__(self, pages: dict[str, object]) -> None:
        # ⚠️ **两个端点有默认值**：
        # * ``/api/projects/``（批量 export）——``export_project_json`` 现在
        #   **一定**会打它（两边对账），不给默认值 ⇒ 每个测试都得记得配一条，
        #   漏配就是「未预期的请求」——测试会因实现细节碎掉，而真正要测的
        #   东西反而没测；
        # * ``/api/tasks/``（逐条标注）——默认「这条没有标注」。
        self.pages = {"/api/projects/": [], "/api/tasks/": [], **pages}
        self.calls: list[str] = []

    def _request(self, method: str, path: str, **kwargs: object) -> object:
        self.calls.append(path)
        # ⚠️ **最长前缀优先**：``"/api/tasks"`` 会把
        # ``"/api/tasks/3/annotations"`` 也吃掉（前者是后者的前缀）⇒
        # 不按长度挑，逐条端点就永远拿不到自己那份数据。
        best: str | None = None
        for prefix in self.pages:
            if path.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            raise AssertionError(f"未预期的请求: {path}")
        payload = self.pages[best]
        if callable(payload):
            return payload(path)  # type: ignore[operator]
        return payload


def _client(fake: _Fake) -> LabelStudioClient:
    client = LabelStudioClient("http://ls.invalid", "k" * 40)
    client._request = fake._request  # type: ignore[method-assign]
    return client


class TestListProjectTasks:
    def test_翻页取全(self) -> None:
        all_ids = [3, 4, 5, 6, 7]

        def page(path: str) -> dict:
            p = int(path.rsplit("page=", 1)[1])
            start = (p - 1) * 2
            ids = all_ids[start : start + 2]
            return {"tasks": [{"id": i, "data": {}} for i in ids], "total": len(all_ids)}

        fake = _Fake({"/api/tasks": page})
        tasks = _client(fake).list_project_tasks(78, page_size=2)
        assert [t["id"] for t in tasks] == all_ids
        assert len(fake.calls) == 3

    def test_这页没满就停(self) -> None:
        """末页少于 ``page_size`` ⇒ 收工（不空转一整页）。"""
        fake = _Fake({"/api/tasks": {"tasks": [{"id": 3}], "total": 1}})
        tasks = _client(fake).list_project_tasks(78, page_size=100)
        assert [t["id"] for t in tasks] == [3]
        assert len(fake.calls) == 1

    def test_空项目返回空表(self) -> None:
        fake = _Fake({"/api/tasks": {"tasks": [], "total": 0}})
        assert _client(fake).list_project_tasks(78) == []

    def test_形态异常退错(self) -> None:
        """返回不是 dict ⇒ **报错**，不静默当成「没有 task」。"""
        fake = _Fake({"/api/tasks": []})
        with pytest.raises(LabelStudioError):
            _client(fake).list_project_tasks(78)


class TestExportProjectJson:
    def test_汇出task与annotation(self) -> None:
        fake = _Fake(
            {
                "/api/tasks": {
                    "tasks": [
                        {
                            "id": 3,
                            "data": {"golden_id": "g1"},
                            "annotations": [{"id": 11, "task": 3, "result": []}],
                            "predictions": [],
                        },
                        {"id": 4, "data": {"golden_id": "g2"}, "annotations": []},
                    ],
                    "total": 2,
                }
            }
        )
        payload = _client(fake).export_project_json(78)
        assert [t["id"] for t in payload["tasks"]] == [3, 4]
        assert [a["id"] for a in payload["annotations"]] == [11]

    def test_标注没有task字段时补上所属task(self) -> None:
        """⚠️ 下游按 ``annotation["task"]`` 配对 ⇒ 缺了这个字段，
        **一条都配不上**，而那读起来跟「一条都没复核」一模一样（静默的错）。"""
        fake = _Fake(
            {
                "/api/tasks": {
                    "tasks": [
                        {"id": 3, "data": {}, "annotations": [{"id": 11, "result": []}]},
                    ],
                    "total": 1,
                }
            }
        )
        payload = _client(fake).export_project_json(78)
        assert payload["annotations"][0]["task"] == 3

    def test_task来源是分页端点不是export端点(self) -> None:
        """⚠️ **LS 实测**：export 端点**只给已标注的 task** ⇒
        「没复核的行也要出一行」根本满足不了 ⇒ **task 来源必须是分页端点**。

        这条钉的是**「export 端点空着，task 照样取得到」**——
        若实现改成「拿 export 当 task 来源」，这里会变成 0 条。
        """
        fake = _Fake(
            {
                "/api/tasks": {
                    "tasks": [{"id": 3, "data": {}, "total_annotations": 0}],
                    "total": 1,
                },
                "/api/projects/78/export": [],
            }
        )
        payload = _client(fake).export_project_json(78)
        assert [t["id"] for t in payload["tasks"]] == [3]

    def test_身份字段从逐条端点补上(self) -> None:
        """⚠️⚠️ **实测坑**：批量 export 端点给的标注**没有**
        ``created_username``（只有数字 ``completed_by``）⇒ 只读批量端点
        ⇒ **署名栏读出来是「1」**，而不是账号。

        ⇒ 每条有标注的 task 都要去逐条端点取一份，两边合并。
        """
        fake = _Fake(
            {
                "/api/tasks": {
                    "tasks": [
                        {"id": 3, "data": {}, "total_annotations": 1},
                    ],
                    "total": 1,
                },
                "/api/projects/78/export": [
                    {
                        "id": 3,
                        "annotations": [
                            {
                                "id": 11,
                                "task": 3,
                                "result": [],
                                "completed_by": 1,
                            }
                        ],
                    }
                ],
                "/api/tasks/3/annotations": [
                    {
                        "id": 11,
                        "task": 3,
                        "result": [],
                        "created_username": " reesn@qq.com, 1",
                        "completed_by": 1,
                    }
                ],
            }
        )
        payload = _client(fake).export_project_json(78)
        assert len(payload["annotations"]) == 1
        assert payload["annotations"][0]["created_username"] == " reesn@qq.com, 1"

    def test_条数对不上记进mismatches(self) -> None:
        """⚠️ **两个端点对不上账 ⇒ 记下来，不静默**。

        少报比多报更难发现：它读起来像「没复核」⇒ 覆盖率会虚高。
        """
        fake = _Fake(
            {
                "/api/tasks": {
                    "tasks": [{"id": 3, "data": {}, "total_annotations": 2}],
                    "total": 1,
                },
                "/api/projects/78/export": [
                    {"id": 3, "annotations": [{"id": 11, "task": 3, "result": []}]}
                ],
                "/api/tasks/3/annotations": [
                    {"id": 11, "task": 3, "result": []}
                ],
            }
        )
        payload = _client(fake).export_project_json(78)
        assert payload["mismatches"], "条数对不上必须记进 mismatches"
        assert payload["mismatches"][0]["expected"] == 2
        assert payload["mismatches"][0]["got"] == 1

    def test_未标注的task也在表里(self) -> None:
        """「没复核」和「没导出来」是两件事——漏掉会让覆盖率读成 100%。"""
        fake = _Fake(
            {
                "/api/tasks": {
                    "tasks": [{"id": 3, "data": {}, "annotations": []}],
                    "total": 1,
                }
            }
        )
        payload = _client(fake).export_project_json(78)
        assert len(payload["tasks"]) == 1
        assert payload["annotations"] == []
