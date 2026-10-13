"""HTTP 层探测工具集。

2026-10-12 增量十八配套：阶段 4 L0 静态可达性探测。
2026-10-13 增量二十二配套：阶段 4 L1 媒体解析（ffprobe）。
2026-10-13 增量二十三配套：阶段 4 L2 浏览器媒体探针（media_probe）。

- L0 ``static_probe`` -- HTTP 层可达性初筛，产 ``broken`` / ``blocked``
- L1 ``ffprobe`` -- 媒体解析，产 ``broken`` / ``partial_free``
- L2 ``media_probe`` -- JS-aware 浏览器探，产 ``broken`` / ``partial_free``
"""

from trajectory_pipeline.executor.probe.ffprobe import (
    DEFAULT_TIMEOUT_S,
    FFPROBE_BIN,
    FFPROBE_TIMEOUT_S,
    FFProbeOutput,
    FFProbeRunner,
    MAX_IFRAME_HOPS,
    MAX_MEDIA_URLS,
    MEDIA_EXTENSIONS,
    PARTIAL_FREE_MAX_DURATION_S,
    MediaProbeResult,
    dump_result as dump_media_result,
    ffprobe_available,
    probe as probe_media,
    run_ffprobe_subprocess,
    to_branch as to_media_branch,
)
from trajectory_pipeline.executor.probe.media_probe import (
    BLACK_SCREEN_READY_STATE_MIN,
    DEFAULT_TIMEOUT_S as BROWSER_DEFAULT_TIMEOUT_S,
    IFRAME_SELECTOR,
    MAX_IFRAME_HOPS as BROWSER_MAX_IFRAME_HOPS,
    MEDIA_ERR_ABORTED,
    MEDIA_ERR_DECODE,
    MEDIA_ERR_NETWORK,
    MEDIA_ERR_OK,
    MEDIA_ERR_SRC_NOT_SUPPORTED,
    MEDIA_PROBE_JS,
    MEDIA_SELECTOR,
    PARTIAL_FREE_MAX_CURRENT_TIME_S,
    PROBE_WAIT_S,
    TIMEUPDATE_PROGRESS_THRESHOLD_S,
    BrowserProbeResult,
    MediaSignal,
    _combine_progress,
    _parse_media_payload,
    dump_result as dump_browser_result,
    probe as probe_browser_media,
    to_branch as to_browser_branch,
)
from trajectory_pipeline.executor.probe.static_probe import (
    StaticProbeResult,
    dump_result,
    probe,
    to_branch,
)

__all__ = [
    # L0
    "StaticProbeResult",
    "probe",
    "to_branch",
    "dump_result",
    # L1
    "FFProbeOutput",
    "MediaProbeResult",
    "FFProbeRunner",
    "MEDIA_EXTENSIONS",
    "MAX_IFRAME_HOPS",
    "MAX_MEDIA_URLS",
    "PARTIAL_FREE_MAX_DURATION_S",
    "DEFAULT_TIMEOUT_S",
    "FFPROBE_TIMEOUT_S",
    "FFPROBE_BIN",
    "ffprobe_available",
    "run_ffprobe_subprocess",
    "probe_media",
    "to_media_branch",
    "dump_media_result",
    # L2
    "BrowserProbeResult",
    "MediaSignal",
    "BLACK_SCREEN_READY_STATE_MIN",
    "BROWSER_DEFAULT_TIMEOUT_S",
    "BROWSER_MAX_IFRAME_HOPS",
    "IFRAME_SELECTOR",
    "MEDIA_ERR_ABORTED",
    "MEDIA_ERR_DECODE",
    "MEDIA_ERR_NETWORK",
    "MEDIA_ERR_OK",
    "MEDIA_ERR_SRC_NOT_SUPPORTED",
    "MEDIA_PROBE_JS",
    "MEDIA_SELECTOR",
    "PARTIAL_FREE_MAX_CURRENT_TIME_S",
    "PROBE_WAIT_S",
    "TIMEUPDATE_PROGRESS_THRESHOLD_S",
    "_combine_progress",
    "_parse_media_payload",
    "probe_browser_media",
    "to_browser_branch",
    "dump_browser_result",
]
