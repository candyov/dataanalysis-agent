"""Application config — Data Intelligence Agent"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


class Settings(BaseSettings):
    # ── LLM ──
    LLM_MODEL: str = "deepseek-v4-pro"
    LLM_API_KEY: str = ""
    LLM_API_BASE: str = "https://api.deepseek.com/v1"
    LLM_DEFAULT_TEMPERATURE: float = 0.2
    LLM_MAX_RETRIES: int = 2

    # ── Agent / Graph ──
    MAX_ITERATIONS: int = 3
    SESSION_TTL: int = 7 * 24 * 3600  # 会话保留 7 天 (30 分钟太短, 演示/历史报告会被误清)

    # ── Tools ──
    ANALYSIS_MAX_ROWS: int = 500
    TOOL_CACHE_MAX_SIZE: int = 16
    ANOMALY_THRESHOLD: float = 3.0
    CLUSTER_N_CLUSTERS: int = 3

    # ── Database ──
    QUERY_TIMEOUT: int = 30

    # ── Storage ──
    STORAGE_DIR: str = str(BASE_DIR / "storage")
    STORAGE_OUTPUT_DIR: str = str(BASE_DIR / "storage" / "output")

    # ── App ──
    APP_NAME: str = "Data Intelligence Agent"
    APP_VERSION: str = "2.0.0"
    APP_DEBUG: bool = True
    APP_API_KEY: str = ""  # 单用户 API Key 鉴权 (空 = 不鉴权, 本地开发); 生产必须配置
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )


settings = Settings()
