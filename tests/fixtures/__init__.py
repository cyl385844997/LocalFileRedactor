"""测试夹具包。

存在意义：让 ``from fixtures.make_samples import ...`` 在 pytest 的
rootdir 导入模式下稳定工作（不依赖 PEP 420 命名空间包的隐式行为）。
"""