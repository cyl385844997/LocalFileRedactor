"""本地文件脱敏工具（Local File Redactor）。

离线、不可逆、绝不覆盖原文件。
本包不含任何网络请求代码——这是产品的核心承诺，不要在依赖里引入 requests 之类。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]