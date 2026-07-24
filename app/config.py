from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_base_url: str = "http://localhost:8000"
    app_admin_token: str = "change-me"
    demo_mode: bool = False
    database_path: str = "/data/alerts.db"
    timezone: str = "Asia/Shanghai"
    check_hour: int = 9
    check_minute: int = 0
    travel_buffer_days: int = 3
    warning_days: str = "12,7,3"
    jst_purchase_api_url: str = "https://openapi.jushuitan.com/open/purchase/query"
    jst_purchase_in_api_url: str = (
        "https://openapi.jushuitan.com/open/webapi/wmsapi/purchasein/purchaseinquery"
    )
    jst_app_key: str = ""
    jst_app_secret: str = ""
    jst_access_token: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_admin_open_id: str = ""
    feishu_admin_mobile: str = ""

    @property
    def warning_day_values(self) -> tuple[int, ...]:
        return tuple(sorted({int(x.strip()) for x in self.warning_days.split(",")}, reverse=True))


settings = Settings()
