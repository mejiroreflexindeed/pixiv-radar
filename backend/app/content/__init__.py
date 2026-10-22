"""正文轻量分析：只在追尾目标上按需运行，不参与广域发现。"""

from .analyzer import ANALYZER_VERSION, ContentFeatures, analyze_content

__all__ = ["ANALYZER_VERSION", "ContentFeatures", "analyze_content"]
