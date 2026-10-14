"""pytest 全局夹具：把 tests/fixtures 加入导入路径并提供样例生成钩子。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 tests.fixtures.make_samples 可被直接导入
FIXTURES_DIR = Path(__file__).parent / "fixtures"
if str(FIXTURES_DIR.parent) not in sys.path:
    sys.path.insert(0, str(FIXTURES_DIR.parent))

from fixtures import make_samples  # noqa: E402


@pytest.fixture(scope="session")
def samples_module():
    """暴露样例生成模块，避免各测试重复 import。"""
    return make_samples


@pytest.fixture()
def fresh_samples(tmp_path, samples_module):
    """把样例生成到 pytest 的临时目录，保证测试互不干扰、仓库保持干净。

    做法：monkeypatch 模块级 SAMPLES_DIR，指向 tmp_path。
    """
    import fixtures.make_samples as module

    target = tmp_path / "samples"
    target.mkdir(parents=True, exist_ok=True)
    original = module.SAMPLES_DIR
    module.SAMPLES_DIR = target
    try:
        yield target
    finally:
        module.SAMPLES_DIR = original