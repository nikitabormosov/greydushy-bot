from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    PROJECT_NAME: str
    DATABASE_URL: str
    WEBAPP_URL: str

    TG_BOT_TOKEN: str
    VK_GROUP_TOKEN: str = ""
    MAX_BOT_TOKEN: str = ""

    EMAIL_API_URL: str
    EMAIL_API_KEY: str = ""
    YANDEX_MAPS_API_KEY: str = ""

    PRIVACY_POLICY_URL: str
    PRIVACY_AGREEMENT_URL: str
    SAFETY_PRECAUTIONS_URL: str

    ADMIN_TG_IDS: str
    ADMIN_PASSWORD: str

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
