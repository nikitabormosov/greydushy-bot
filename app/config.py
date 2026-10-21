from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Студия дизайна «Грея душу»"
    DATABASE_URL: str = "sqlite+aiosqlite:///./greydushy.db"

    TG_BOT_TOKEN: str = ""
    VK_GROUP_TOKEN: str = ""
    MAX_BOT_TOKEN: str = ""

    EMAIL_API_URL: str = "https://api.greydushy.com/webhook"
    EMAIL_API_KEY: str = ""
    YANDEX_MAPS_API_KEY: str = ""

    PRIVACY_POLICY_URL: str = "https://greydushy.com/privacy.pdf"
    PRIVACY_AGREEMENT_URL: str = "https://greydushy.com/privacy_agreement.pdf"
    SAFETY_PRECAUTIONS_URL: str = "https://greydushy.com/safety_precautions.pdf"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    ADMIN_TG_IDS: str = "713064774"
    ADMIN_PASSWORD: str = "GPQm$tN33:9#"


settings = Settings()
