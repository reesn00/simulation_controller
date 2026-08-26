"""Contract test: pipeline order is light-health → CU → fold → Router."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from pipeline.runner import process_one
from routing.health import light_health_score_for_session


def test_process_one_order(cfg, session_with_failed_retry):
    """确保 fold 在 Router.tag 之前调用，并且两者都接收 CU。"""
    session = session_with_failed_retry

    with patch("pipeline.runner.fold_failed_toolresults") as mock_fold_failed, \
         patch("pipeline.runner.fold_repeated_thinking") as mock_fold_thinking, \
         patch("pipeline.runner.Router") as mock_router_cls:

        mock_fold_failed.return_value = 0
        mock_fold_thinking.return_value = 0
        mock_router = MagicMock()
        mock_router.tag.return_value = ({}, [])
        mock_router_cls.return_value = mock_router

        with patch("pipeline.runner.build_context_for_session") as mock_build_cu:
            mock_cu = MagicMock()
            mock_build_cu.return_value = mock_cu
            process_one(session, cfg, ["browser"], set())

        # build CU 必须在 fold 之前
        assert mock_build_cu.called
        assert mock_fold_failed.called
        assert mock_fold_thinking.called
        assert mock_router.tag.called

        # fold 接收 cu
        _, kwargs = mock_fold_failed.call_args
        assert kwargs.get("cu") is mock_cu

        # Router.tag 接收 context_understanding
        _, kwargs = mock_router.tag.call_args
        assert kwargs.get("context_understanding") is mock_cu
