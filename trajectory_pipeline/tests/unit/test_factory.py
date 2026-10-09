"""感知层工厂的单元测试——**灰度只在构造期**。

两条要守住的不变式：

1. **W1 批次必须能在没有后端的机器上跑通。** 新插件上线不能让既有
   能力报废，所以 ``auto`` 模式未配置时退回规则版而**不报错**。
2. **显式 ``llm`` 而未配置则报错。** 静默退回规则版会让人以为跑的是
   LLM 版——存档里 ``perceptor`` 字段虽然诚实，但没人会去读那个字段。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.llm.client import LLMConfig, LLMUnavailable
from trajectory_pipeline.perception import factory
from trajectory_pipeline.perception.llm_perceptor import LLMPerceptor
from trajectory_pipeline.perception.rule_perceptor import RulePerceptor
from trajectory_pipeline.tests.fakes import FakeLLM

ENV_OK = {
    "TRAJECTORY_LLM_BASE_URL": "http://127.0.0.1:9/v1",
    "TRAJECTORY_LLM_MODEL": "test-model",
}


class TestModeSelection:
    def test_显式rule用规则版(self):
        assert factory.build_perceptor(mode="rule").name == "rule"

    def test_auto无配置退回规则版(self):
        """W1 批次不能因为没有后端就报废。"""
        p = factory.build_perceptor(mode="auto", env={})
        assert p.name == "rule"

    def test_auto有配置用LLM版(self):
        p = factory.build_perceptor(mode="auto", env=ENV_OK,
                                     client=FakeLLM())
        assert p.name == "llm"

    def test_显式llm未配置报错(self):
        """配置错误要暴露。静默退回会让「跑的是哪版」变得不可知。"""
        with pytest.raises(LLMUnavailable):
            factory.build_perceptor(mode="llm", env={})

    def test_默认模式是auto(self):
        assert factory.configured_mode({}) == "auto"

    def test_未知模式报错不猜(self):
        with pytest.raises(ValueError, match="未知"):
            factory.build_perceptor(mode="magic", env={})

    def test_环境变量读模式(self):
        assert factory.configured_mode(
            {"TRAJECTORY_PERCEPTOR": "rule"}) == "rule"

    def test_环境变量大小写与空格容错(self):
        assert factory.configured_mode(
            {"TRAJECTORY_PERCEPTOR": "  RULE "}) == "rule"


class TestTargetTitleInjection:
    def test_片名注入构造器(self):
        """判断点 ① 需要目标片名，而它不在观察里（是任务的属性）。"""
        p = factory.build_perceptor(mode="auto", env=ENV_OK,
                                    client=FakeLLM(), target_title="功夫")
        assert isinstance(p, LLMPerceptor)
        assert p._target_title == "功夫"

    def test_未注入时为空串(self):
        """契约测试裸构造，此时 ① 只能按「哪些像播放站」筛。"""
        p = factory.build_perceptor(mode="auto", env=ENV_OK, client=FakeLLM())
        assert p._target_title == ""


class TestDescribe:
    def test_规则版(self):
        assert "rule" in factory.describe(RulePerceptor())

    def test_llm版(self):
        p = factory.build_perceptor(mode="auto", env=ENV_OK, client=FakeLLM())
        assert "llm" in factory.describe(p)

    def test_health抛异常不打断检查(self):
        """自述失败不该让 CLI 的能力检查整个炸掉。"""

        class Broken:
            name = "broken"

            def health(self):
                raise RuntimeError("炸了")

        assert "broken" in factory.describe(Broken())

    def test_无health的实现(self):
        class Bare:
            name = "bare"

        assert "bare" in factory.describe(Bare())


class TestNoRuntimeFallback:
    """运行期切换被**明确禁止**——这里断言它不存在。"""

    def test_perceptor本身不持有备用实现(self):
        """一个 Perceptor 只能有一种来源。持有 fallback 意味着 LLM 挂掉时
        规则版顶上，而那不是 fail-closed——是拿不确定的输入驱动确定的输出。"""
        p = factory.build_perceptor(mode="auto", env=ENV_OK, client=FakeLLM())
        assert not hasattr(p, "_fallback")
        assert not hasattr(p, "_rule")

    def test_存档字段能区分来源(self):
        """``RunRecord.perceptor`` 记 ``name``，两批数据可分。"""
        assert RulePerceptor().name != LLMPerceptor(FakeLLM()).name


class TestConfigIsolation:
    def test_变量名不与存量v1撞名(self):
        """存量 v1 用 ``LLM_*``。共用变量名会让一次调参同时改动两边，
        而 v1 是冻结的——改了不该生效却生效了。"""
        for name in (factory.ENV_MODE, "TRAJECTORY_LLM_BASE_URL",
                     "TRAJECTORY_LLM_MODEL", "TRAJECTORY_LLM_API_KEY"):
            assert name.startswith("TRAJECTORY_")