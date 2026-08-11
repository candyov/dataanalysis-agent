"""Eval fixtures — 评估测试共享配置"""
import json
import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_DATA_PATH = Path(__file__).parent.parent.parent / "storage" / "uploads" / "full_e2e_test_2025.csv"


@pytest.fixture(scope="session")
def golden_cases():
    """加载 golden test cases"""
    with open(FIXTURES_DIR / "golden_cases.json", "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def test_csv_path():
    """测试数据文件路径"""
    path = str(TEST_DATA_PATH)
    if not Path(path).exists():
        pytest.skip(f"测试数据文件不存在: {path}")
    return path
