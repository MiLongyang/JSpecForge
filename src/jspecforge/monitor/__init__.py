"""控制台监控面板。

公开:

- :class:`Dashboard`:rich 监控面板主类(后台线程)
- :class:`DataCollector`:采集层(可单独使用)
- :class:`MonitorData`:不可变快照
- :func:`render_dashboard`:把 MonitorData 渲染成 rich Panel
"""

from jspecforge.monitor.dashboard import (
    Dashboard,
    DataCollector,
    MonitorData,
    render_dashboard,
)

__all__ = [
    "Dashboard",
    "DataCollector",
    "MonitorData",
    "render_dashboard",
]
