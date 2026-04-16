import json
import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
API_KEYS_FILE = DATA_DIR / "api_keys.json"


class Settings(BaseSettings):
    """应用级运行配置，统一承载环境变量与默认值。"""

    PORT: int = int(os.getenv("PORT", 7860))
    WORKERS: int = int(os.getenv("WORKERS", 3))
    ADMIN_KEY: str = os.getenv("ADMIN_KEY", "admin")
    REGISTER_SECRET: str = os.getenv("REGISTER_SECRET", "")
    ENGINE_MODE: str = os.getenv("ENGINE_MODE", "hybrid")
    NATIVE_TOOL_PASSTHROUGH: bool = os.getenv("NATIVE_TOOL_PASSTHROUGH", "true").lower() in ("1", "true", "yes", "on")
    BROWSER_POOL_SIZE: int = int(os.getenv("BROWSER_POOL_SIZE", 2))
    MAX_INFLIGHT_PER_ACCOUNT: int = int(os.getenv("MAX_INFLIGHT", 1))
    STREAM_KEEPALIVE_INTERVAL: int = int(os.getenv("STREAM_KEEPALIVE_INTERVAL", 5))
    MAX_RETRIES: int = 2
    TOOL_MAX_RETRIES: int = 2
    EMPTY_RESPONSE_RETRIES: int = 1
    ACCOUNT_MIN_INTERVAL_MS: int = int(os.getenv("ACCOUNT_MIN_INTERVAL_MS", 1200))
    REQUEST_JITTER_MIN_MS: int = int(os.getenv("REQUEST_JITTER_MIN_MS", 120))
    REQUEST_JITTER_MAX_MS: int = int(os.getenv("REQUEST_JITTER_MAX_MS", 360))
    RATE_LIMIT_BASE_COOLDOWN: int = int(os.getenv("RATE_LIMIT_BASE_COOLDOWN", 600))
    RATE_LIMIT_MAX_COOLDOWN: int = int(os.getenv("RATE_LIMIT_MAX_COOLDOWN", 3600))
    RATE_LIMIT_COOLDOWN: int = RATE_LIMIT_BASE_COOLDOWN
    ACCOUNTS_FILE: str = os.getenv("ACCOUNTS_FILE", str(DATA_DIR / "accounts.json"))
    USERS_FILE: str = os.getenv("USERS_FILE", str(DATA_DIR / "users.json"))
    CAPTURES_FILE: str = os.getenv("CAPTURES_FILE", str(DATA_DIR / "captures.json"))
    CONFIG_FILE: str = os.getenv("CONFIG_FILE", str(DATA_DIR / "config.json"))
    REGISTER_LOG_FILE: str = os.getenv("REGISTER_LOG_FILE", str(DATA_DIR / "register_logs.json"))
    REGISTER_LOG_ARCHIVE_DIR: str = os.getenv("REGISTER_LOG_ARCHIVE_DIR", str(DATA_DIR / "register_logs"))
    REGISTER_LOG_MAX_BYTES: int = int(os.getenv("REGISTER_LOG_MAX_BYTES", 262144))
    REGISTER_LOG_SLICE_SIZE: int = int(os.getenv("REGISTER_LOG_SLICE_SIZE", 200))
    REGISTER_LOG_ARCHIVE_KEEP: int = int(os.getenv("REGISTER_LOG_ARCHIVE_KEEP", 20))
    REGISTER_LOG_PAGE_SIZE: int = int(os.getenv("REGISTER_LOG_PAGE_SIZE", 20))
    PROXY_URL: str = os.getenv("PROXY_URL", "")
    PROXY_ENABLED: bool = os.getenv("PROXY_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    BATCH_REGISTER_MAX: int = int(os.getenv("BATCH_REGISTER_MAX", 10))
    MODELS_RATE_LIMIT_COUNT: int = int(os.getenv("MODELS_RATE_LIMIT_COUNT", 30))
    MODELS_RATE_LIMIT_WINDOW: int = int(os.getenv("MODELS_RATE_LIMIT_WINDOW", 60))

    # 日志
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # 上下文与文件管理
    CONTEXT_INLINE_MAX_CHARS: int = int(os.getenv("CONTEXT_INLINE_MAX_CHARS", 4000))
    CONTEXT_FORCE_FILE_MAX_CHARS: int = int(os.getenv("CONTEXT_FORCE_FILE_MAX_CHARS", 10000))
    CONTEXT_ATTACHMENT_TTL_SECONDS: int = int(os.getenv("CONTEXT_ATTACHMENT_TTL_SECONDS", 1800))
    CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS: int = int(os.getenv("CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS", 60))
    CONTEXT_GENERATED_DIR: str = os.getenv("CONTEXT_GENERATED_DIR", str(DATA_DIR / "context_files"))
    CONTEXT_CACHE_FILE: str = os.getenv("CONTEXT_CACHE_FILE", str(DATA_DIR / "context_cache.json"))
    UPLOADED_FILES_FILE: str = os.getenv("UPLOADED_FILES_FILE", str(DATA_DIR / "uploaded_files.json"))
    CONTEXT_AFFINITY_FILE: str = os.getenv("CONTEXT_AFFINITY_FILE", str(DATA_DIR / "session_affinity.json"))
    CONTEXT_ALLOWED_GENERATED_EXTS: str = os.getenv("CONTEXT_ALLOWED_GENERATED_EXTS", "txt,md,json,log")
    CONTEXT_ALLOWED_USER_EXTS: str = os.getenv("CONTEXT_ALLOWED_USER_EXTS", "txt,md,json,log,xml,yaml,yml,csv,html,css,py,js,ts,java,c,cpp,cs,php,go,rb,sh,zsh,ps1,bat,cmd,pdf,doc,docx,ppt,pptx,xls,xlsx,png,jpg,jpeg,webp,gif,tiff,bmp,svg")

    class Config:
        """Pydantic settings 配置。"""

        env_file = ".env"


def load_api_keys() -> set[str]:
    """从本地 JSON 文件加载管理 API Keys。"""
    if not API_KEYS_FILE.exists():
        return set()
    try:
        data = json.loads(API_KEYS_FILE.read_text(encoding="utf-8"))
        return set(data.get("keys", []))
    except Exception:
        return set()


def save_api_keys(keys: set[str]) -> None:
    """将管理 API Keys 持久化到本地 JSON 文件。"""
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"keys": list(keys)}
    API_KEYS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


API_KEYS = load_api_keys()
VERSION = "2.0.0"
settings = Settings()
MODEL_MAP = {
    "gpt-4o": "qwen3.6-plus",
    "gpt-4o-mini": "qwen3.6-plus",
    "gpt-4-turbo": "qwen3.6-plus",
    "gpt-4": "qwen3.6-plus",
    "gpt-4.1": "qwen3.6-plus",
    "gpt-4.1-mini": "qwen3.6-plus",
    "gpt-3.5-turbo": "qwen3.6-plus",
    "gpt-5": "qwen3.6-plus",
    "o1": "qwen3.6-plus",
    "o1-mini": "qwen3.6-plus",
    "o3": "qwen3.6-plus",
    "o3-mini": "qwen3.6-plus",
    "claude-opus-4-6": "qwen3.6-plus",
    "claude-sonnet-4-6": "qwen3.6-plus",
    "claude-sonnet-4-5": "qwen3.6-plus",
    "claude-3-opus": "qwen3.6-plus",
    "claude-3-5-sonnet": "qwen3.6-plus",
    "claude-3-5-sonnet-latest": "qwen3.6-plus",
    "claude-3-sonnet": "qwen3.6-plus",
    "claude-3-haiku": "qwen3.6-plus",
    "claude-3-5-haiku": "qwen3.6-plus",
    "claude-3-5-haiku-latest": "qwen3.6-plus",
    "claude-haiku-4-5": "qwen3.6-plus",
    "gemini-2.5-pro": "qwen3.6-plus",
    "gemini-2.5-flash": "qwen3.6-plus",
    "gemini-1.5-pro": "qwen3.6-plus",
    "gemini-1.5-flash": "qwen3.6-plus",
    "qwen": "qwen3.6-plus",
    "qwen-max": "qwen3.6-plus",
    "qwen-plus": "qwen3.6-plus",
    "qwen-turbo": "qwen3.6-plus",
    "deepseek-chat": "qwen3.6-plus",
    "deepseek-reasoner": "qwen3.6-plus",
}
IMAGE_MODEL_DEFAULT = "qwen3.6-plus"


def resolve_model(name: str) -> str:
    """将外部模型别名映射到系统内部实际使用的模型名。"""
    return MODEL_MAP.get(name, name)
