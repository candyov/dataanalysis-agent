"""glossary_cache 持久化缓存测试: 跨会话复用 Curator 探查结果 (方案 A)"""

import time

import pytest

from dia.infrastructure.persistence import glossary_cache as gc


@pytest.fixture(autouse=True)
def _clean_db():
    gc.clear_glossary_cache()
    gc.clear_history()
    yield
    gc.clear_glossary_cache()
    gc.clear_history()


class TestGlossaryCache:
    def test_save_load_roundtrip(self):
        gc.save_glossary_cache(
            "src1",
            {"region": {"name": "region", "role": "dimension"}},
            ["total_sales"],
            {"confirm": {"caliber": "sum(sales)"}, "source_id": "src1"},
        )
        cache = gc.load_glossary_cache("src1")
        assert cache is not None
        assert cache["glossary"]["region"]["role"] == "dimension"
        assert cache["kpis"] == ["total_sales"]
        assert cache["curator_report"]["confirm"]["caliber"] == "sum(sales)"

    def test_overwrite_same_source(self):
        gc.save_glossary_cache("src1", {"a": 1}, [], {})
        gc.save_glossary_cache("src1", {"b": 2}, [], {})
        cache = gc.load_glossary_cache("src1")
        assert cache["glossary"] == {"b": 2}

    def test_missing_source_returns_none(self):
        assert gc.load_glossary_cache("no_such_src") is None

    def test_empty_source_id_not_saved(self):
        gc.save_glossary_cache("", {"a": 1}, [], {})
        assert gc.load_glossary_cache("") is None


class TestFreshness:
    def test_fresh_cache(self):
        gc.save_glossary_cache("src1", {}, [], {})
        cache = gc.load_glossary_cache("src1")
        assert gc.is_fresh(cache)

    def test_stale_cache(self):
        gc.save_glossary_cache("src1", {}, [], {})
        cache = gc.load_glossary_cache("src1")
        # 模拟超过 TTL: updated_at 回拨
        cache["updated_at"] = time.time() - gc.GLOSSARY_CACHE_TTL - 10
        assert not gc.is_fresh(cache)

    def test_none_cache_not_fresh(self):
        assert not gc.is_fresh(None)


class TestHistory:
    def test_append_and_load_newest_first(self):
        gc.append_history("src1", "结论一", "问题一")
        gc.append_history("src1", "结论二", "问题二")
        hist = gc.load_history("src1")
        assert [h["question"] for h in hist] == ["问题二", "问题一"]

    def test_history_capped_at_limit(self):
        for i in range(6):
            gc.append_history("src1", f"结论{i}", f"问题{i}")
        hist = gc.load_history("src1")
        assert len(hist) == gc.HISTORY_LIMIT == 3
        # 保留最新的
        assert [h["question"] for h in hist] == ["问题5", "问题4", "问题3"]

    def test_history_isolated_by_source(self):
        gc.append_history("src1", "甲", "q1")
        gc.append_history("src2", "乙", "q2")
        assert len(gc.load_history("src1")) == 1
        assert len(gc.load_history("src2")) == 1
        assert gc.load_history("src1")[0]["conclusion"] == "甲"

    def test_append_empty_ignored(self):
        gc.append_history("src1", "", "q")
        assert gc.load_history("src1") == []

    def test_history_survives_glossary_overwrite(self):
        """history 独立表: glossary 缓存覆盖 (INSERT OR REPLACE) 不丢历史"""
        gc.append_history("src1", "旧结论", "q")
        gc.save_glossary_cache("src1", {"a": 1}, [], {})  # 覆盖 glossary 行
        hist = gc.load_history("src1")
        assert len(hist) == 1
        assert hist[0]["conclusion"] == "旧结论"
