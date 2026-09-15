from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://stockroom:stockroom@db:5432/stockroom"
    cors_origins: str = "http://localhost:3000"
    cognito_region: str = ""
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    receipt_bucket_name: str = ""
    redis_url: str = ""
    cache_ttl_seconds: int = 60
    mongo_url: str = ""
    mongo_database: str = "stockroom"
    aws_region: str = "us-east-1"
    max_receipt_size_bytes: int = 10 * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
