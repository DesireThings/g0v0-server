from enum import Enum
from typing import Annotated, Any, Literal

from app.models.scoring_mode import ScoringMode

from pydantic import (
    AliasChoices,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class AWSS3StorageSettings(BaseSettings):
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket_name: str
    s3_region_name: str
    s3_public_url_base: str | None = None


class CloudflareR2Settings(BaseSettings):
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket_name: str
    r2_public_url_base: str | None = None


class LocalStorageSettings(BaseSettings):
    local_storage_path: str = "./storage"


class StorageServiceType(str, Enum):
    LOCAL = "local"
    CLOUDFLARE_R2 = "r2"
    AWS_S3 = "s3"


class OldScoreProcessingMode(str, Enum):
    STRICT = "strict"
    NORMAL = "normal"


SPECTATOR_DOC = """
## æ—è§‚æœåŠ¡å™¨è®¾ç½®
| å˜é‡å | æè¿° | ç±»åž‹ | é»˜è®¤å€¼ |
|--------|------|--------|--------|
| `SAVE_REPLAYS` | æ˜¯å¦ä¿å­˜å›žæ”¾ï¼Œè®¾ç½®ä¸º `1` ä¸ºå¯ç”¨ | boolean | `0` |
| `REDIS_HOST` | Redis æœåŠ¡å™¨åœ°å€ | string | `localhost` |
| `SHARED_INTEROP_DOMAIN` | API æœåŠ¡å™¨ï¼ˆå³æœ¬æœåŠ¡ï¼‰åœ°å€ | string (url) | `http://localhost:8000` |
| `SERVER_PORT` | æ—è§‚æœåŠ¡å™¨ç«¯å£ | integer | `8006` |
| `SP_SENTRY_DSN` | æ—è§‚æœåŠ¡å™¨çš„ Sentry DSN | string | `null` |
| `MATCHMAKING_ROOM_ROUNDS` | åŒ¹é…å¯¹æˆ˜æˆ¿é—´çš„å›žåˆæ•° | integer | 5 |
| `MATCHMAKING_ALLOW_SKIP` | æ˜¯å¦å…è®¸ç”¨æˆ·è·³è¿‡åŒ¹é…é˜¶æ®µ | boolean | false |
| `MATCHMAKING_LOBBY_UPDATE_RATE` | æ›´æ–°åŒ¹é…å¤§åŽ…çš„é¢‘çŽ‡ï¼ˆä»¥ç§’ä¸ºå•ä½ï¼‰ | integer | 5 |
| `MATCHMAKING_QUEUE_UPDATE_RATE` | æ›´æ–°åŒ¹é…é˜Ÿåˆ—çš„é¢‘çŽ‡ï¼ˆä»¥ç§’ä¸ºå•ä½ï¼‰ | integer | 1 |
| `MATCHMAKING_QUEUE_BAN_DURATION` | çŽ©å®¶æ‹’ç»é‚€è¯·åŽæš‚æ—¶ç¦æ­¢è¿›å…¥åŒ¹é…é˜Ÿåˆ—çš„æ—¶é—´ï¼ˆä»¥ç§’ä¸ºå•ä½ï¼‰ | integer | 60 |
| `MATCHMAKING_POOL_SIZE` | æ¯ä¸ªåŒ¹é…æˆ¿é—´çš„è°±é¢æ•°é‡ | integer | 50 |
"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="allow",
        json_schema_extra={
            "paragraphs_desc": {
                "Fetcher è®¾ç½®": "Fetcher ç”¨äºŽä»Ž osu! å®˜æ–¹ API èŽ·å–æ•°æ®ï¼Œä½¿ç”¨ osu! å®˜æ–¹ API çš„ OAuth 2.0 è®¤è¯",
                "ç›‘æŽ§è®¾ç½®": (
                    "é…ç½®åº”ç”¨çš„ç›‘æŽ§é€‰é¡¹ï¼Œå¦‚ Sentry å’Œ New Relicã€‚\n\n"
                    "å°† newrelic.ini é…ç½®æ–‡ä»¶æ”¾å…¥é¡¹ç›®æ ¹ç›®å½•å³å¯è‡ªåŠ¨å¯ç”¨ New Relic ç›‘æŽ§ã€‚"
                    "å¦‚æžœé…ç½®æ–‡ä»¶ä¸å­˜åœ¨æˆ– newrelic åŒ…æœªå®‰è£…ï¼Œå°†è·³è¿‡ New Relic åˆå§‹åŒ–ã€‚"
                ),
                "å­˜å‚¨æœåŠ¡è®¾ç½®": """ç”¨äºŽå­˜å‚¨å›žæ”¾æ–‡ä»¶ã€å¤´åƒç­‰é™æ€èµ„æºã€‚

### æœ¬åœ°å­˜å‚¨ (æŽ¨èç”¨äºŽå¼€å‘çŽ¯å¢ƒ)

æœ¬åœ°å­˜å‚¨å°†æ–‡ä»¶ä¿å­˜åœ¨æœåŠ¡å™¨çš„æœ¬åœ°æ–‡ä»¶ç³»ç»Ÿä¸­ï¼Œé€‚åˆå¼€å‘å’Œå°è§„æ¨¡éƒ¨ç½²ã€‚

```bash
STORAGE_SERVICE="local"
STORAGE_SETTINGS='{"local_storage_path": "./storage"}'
```

### Cloudflare R2 å­˜å‚¨ (æŽ¨èç”¨äºŽç”Ÿäº§çŽ¯å¢ƒ)

```bash
STORAGE_SERVICE="r2"
STORAGE_SETTINGS='{
  "r2_account_id": "your_cloudflare_account_id",
  "r2_access_key_id": "your_r2_access_key_id",
  "r2_secret_access_key": "your_r2_secret_access_key",
  "r2_bucket_name": "your_bucket_name",
  "r2_public_url_base": "https://your-custom-domain.com"
}'
```

### AWS S3 å­˜å‚¨

```bash
STORAGE_SERVICE="s3"
STORAGE_SETTINGS='{
  "s3_access_key_id": "your_aws_access_key_id",
  "s3_secret_access_key": "your_aws_secret_access_key",
  "s3_bucket_name": "your_s3_bucket_name",
  "s3_region_name": "us-east-1",
  "s3_public_url_base": "https://your-custom-domain.com"
}'
```
""",
                "è¡¨çŽ°è®¡ç®—è®¾ç½®": """é…ç½®è¡¨çŽ°åˆ†è®¡ç®—å™¨åŠå…¶å‚æ•°ã€‚

### [osu-performance-server](https://github.com/GooGuTeam/osu-performance-server) (é»˜è®¤)

```bash
CALCULATOR="performance_server"
CALCULATOR_CONFIG='{
    "server_url": "http://localhost:5225"
}'
```

### rosu-pp-py

```bash
CALCULATOR="rosu"
CALCULATOR_CONFIG='{}'
```
""",
            }
        },
    )

    # æ•°æ®åº“è®¾ç½®
    mysql_host: Annotated[
        str,
        Field(default="localhost", description="MySQL æœåŠ¡å™¨åœ°å€"),
        "æ•°æ®åº“è®¾ç½®",
    ]
    mysql_port: Annotated[
        int,
        Field(default=3306, description="MySQL æœåŠ¡å™¨ç«¯å£"),
        "æ•°æ®åº“è®¾ç½®",
    ]
    mysql_database: Annotated[
        str,
        Field(default="osu_api", description="MySQL æ•°æ®åº“åç§°"),
        "æ•°æ®åº“è®¾ç½®",
    ]
    mysql_user: Annotated[
        str,
        Field(default="osu_api", description="MySQL ç”¨æˆ·å"),
        "æ•°æ®åº“è®¾ç½®",
    ]
    mysql_password: Annotated[
        str,
        Field(default="password", description="MySQL å¯†ç "),
        "æ•°æ®åº“è®¾ç½®",
    ]
    mysql_root_password: Annotated[
        str,
        Field(default="password", description="MySQL root å¯†ç "),
        "æ•°æ®åº“è®¾ç½®",
    ]
    redis_url: Annotated[
        str,
        Field(default="redis://127.0.0.1:6379", description="Redis è¿žæŽ¥ URL"),
        "æ•°æ®åº“è®¾ç½®",
    ]

    @property
    def database_url(self) -> str:
        return f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"

    # JWT è®¾ç½®
    secret_key: Annotated[
        str,
        Field(
            default="your_jwt_secret_here",
            alias="jwt_secret_key",
            description="JWT ç­¾åå¯†é’¥",
        ),
        "JWT è®¾ç½®",
    ]
    algorithm: Annotated[
        str,
        Field(default="HS256", alias="jwt_algorithm", description="JWT ç®—æ³•"),
        "JWT è®¾ç½®",
    ]
    access_token_expire_minutes: Annotated[
        int,
        Field(default=1440, description="è®¿é—®ä»¤ç‰Œè¿‡æœŸæ—¶é—´ï¼ˆåˆ†é’Ÿï¼‰"),
        "JWT è®¾ç½®",
    ]
    refresh_token_expire_minutes: Annotated[
        int,
        Field(default=21600, description="åˆ·æ–°ä»¤ç‰Œè¿‡æœŸæ—¶é—´ï¼ˆåˆ†é’Ÿï¼‰"),
        "JWT è®¾ç½®",
    ]  # 15 days
    jwt_audience: Annotated[
        str,
        Field(default="5", description="JWT å—ä¼—"),
        "JWT è®¾ç½®",
    ]
    jwt_issuer: Annotated[
        str | None,
        Field(default=None, description="JWT ç­¾å‘è€…"),
        "JWT è®¾ç½®",
    ]

    # OAuth è®¾ç½®
    osu_client_id: Annotated[
        int,
        Field(default=5, description="OAuth å®¢æˆ·ç«¯ ID"),
        "OAuth è®¾ç½®",
    ]
    osu_client_secret: Annotated[
        str,
        Field(
            default="FGc9GAtyHzeQDshWP5Ah7dega8hJACAJpQtw6OXk",
            description="OAuth å®¢æˆ·ç«¯å¯†é’¥",
        ),
        "OAuth è®¾ç½®",
    ]
    osu_web_client_id: Annotated[
        int,
        Field(default=6, description="Web OAuth å®¢æˆ·ç«¯ ID"),
        "OAuth è®¾ç½®",
    ]
    osu_web_client_secret: Annotated[
        str,
        Field(
            default="your_osu_web_client_secret_here",
            description="Web OAuth å®¢æˆ·ç«¯å¯†é’¥",
        ),
        "OAuth è®¾ç½®",
    ]

    # æœåŠ¡å™¨è®¾ç½®
    host: Annotated[
        str,
        Field(default="0.0.0.0", description="æœåŠ¡å™¨ç›‘å¬åœ°å€"),  # noqa: S104
        "æœåŠ¡å™¨è®¾ç½®",
    ]
    port: Annotated[
        int,
        Field(default=8000, description="æœåŠ¡å™¨ç›‘å¬ç«¯å£"),
        "æœåŠ¡å™¨è®¾ç½®",
    ]
    debug: Annotated[
        bool,
        Field(default=False, description="æ˜¯å¦å¯ç”¨è°ƒè¯•æ¨¡å¼"),
        "æœåŠ¡å™¨è®¾ç½®",
    ]
    cors_urls: Annotated[
        list[HttpUrl],
        Field(default=[], description="é¢å¤–çš„ CORS å…è®¸çš„åŸŸååˆ—è¡¨ (JSON æ ¼å¼)"),
        "æœåŠ¡å™¨è®¾ç½®",
    ]
    server_url: Annotated[
        HttpUrl,
        Field(
            default=HttpUrl("http://localhost:8000"),
            description="æœåŠ¡å™¨ URL",
        ),
        "æœåŠ¡å™¨è®¾ç½®",
    ]
    frontend_url: Annotated[
        HttpUrl | None,
        Field(
            default=None,
            description="å‰ç«¯ URLï¼Œå½“è®¿é—®ä»Žæ¸¸æˆæ‰“å¼€çš„ URL æ—¶ä¼šé‡å®šå‘åˆ°è¿™ä¸ª URLï¼Œä¸ºç©ºè¡¨ç¤ºä¸é‡å®šå‘",
        ),
        "æœåŠ¡å™¨è®¾ç½®",
    ]
    enable_rate_limit: Annotated[
        bool,
        Field(default=True, description="æ˜¯å¦å¯ç”¨é€ŸçŽ‡é™åˆ¶"),
        "æœåŠ¡å™¨è®¾ç½®",
    ]

    @property
    def web_url(self):
        if self.frontend_url is not None:
            return str(self.frontend_url)
        elif self.server_url is not None:
            return str(self.server_url)
        else:
            return "/"

    # Fetcher è®¾ç½®
    fetcher_client_id: Annotated[
        str,
        Field(default="", description="Fetcher å®¢æˆ·ç«¯ ID"),
        "Fetcher è®¾ç½®",
    ]
    fetcher_client_secret: Annotated[
        str,
        Field(default="", description="Fetcher å®¢æˆ·ç«¯å¯†é’¥"),
        "Fetcher è®¾ç½®",
    ]
    beatconnect_base_url: Annotated[
        HttpUrl,
        Field(
            default=HttpUrl("https://beatconnect.io"),
            description="BeatConnect API åŸºç¡€ URL",
        ),
        "Fetcher è®¾ç½®",
    ]
    beatconnect_api_token: Annotated[
        str,
        Field(default="", description="BeatConnect API token (Patreon)"),
        "Fetcher è®¾ç½®",
    ]

    # NOTE: Reserve for user-based-fetcher

    # fetcher_scopes: Annotated[
    #     list[str],
    #     Field(default=["public"], description="Fetcher æƒé™èŒƒå›´ï¼Œä»¥é€—å·åˆ†éš”æ¯ä¸ªæƒé™"),
    #     "Fetcher è®¾ç½®",
    #     NoDecode,
    # ]

    # @field_validator("fetcher_scopes", mode="before")
    # @classmethod
    # def validate_fetcher_scopes(cls, v: Any) -> list[str]:
    #     if isinstance(v, str):
    #         return v.split(",")
    #     return v

    # @property
    # def fetcher_callback_url(self) -> str:
    #     return f"{self.server_url}fetcher/callback"

    # æ—¥å¿—è®¾ç½®
    log_level: Annotated[
        str,
        Field(default="INFO", description="æ—¥å¿—çº§åˆ«"),
        "æ—¥å¿—è®¾ç½®",
    ]

    # éªŒè¯æœåŠ¡è®¾ç½®
    enable_totp_verification: Annotated[bool, Field(default=True, description="æ˜¯å¦å¯ç”¨TOTPåŒå› ç´ éªŒè¯"), "éªŒè¯æœåŠ¡è®¾ç½®"]
    totp_issuer: Annotated[
        str | None,
        Field(default=None, description="TOTP è®¤è¯å™¨ä¸­çš„å‘è¡Œè€…åç§°"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    totp_service_name: Annotated[
        str,
        Field(default="g0v0! Lazer Server", description="TOTP è®¤è¯å™¨ä¸­æ˜¾ç¤ºçš„æœåŠ¡åç§°"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    totp_use_username_in_label: Annotated[
        bool,
        Field(default=True, description="åœ¨TOTPæ ‡ç­¾ä¸­ä½¿ç”¨ç”¨æˆ·åè€Œä¸æ˜¯é‚®ç®±"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    enable_turnstile_verification: Annotated[
        bool,
        Field(default=False, description="æ˜¯å¦å¯ç”¨ Cloudflare Turnstile éªŒè¯ï¼ˆä»…å¯¹éž osu! å®¢æˆ·ç«¯ï¼‰"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    ordr_verification_key: Annotated[
        str,
        Field(
            default="devmode_success",
            description="o!rdr API verification key for the replay-to-video render proxy. Server-side only; defaults to the public dev key (simulated renders, no real output) until a real contributor key is set in .env.",
        ),
    ]
    turnstile_secret_key: Annotated[
        str,
        Field(default="", description="Cloudflare Turnstile Secret Key"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    turnstile_dev_mode: Annotated[
        bool,
        Field(default=False, description="Turnstile å¼€å‘æ¨¡å¼ï¼ˆè·³è¿‡éªŒè¯ï¼Œç”¨äºŽæœ¬åœ°å¼€å‘ï¼‰"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    enable_email_verification: Annotated[
        bool,
        Field(default=False, description="æ˜¯å¦å¯ç”¨é‚®ä»¶éªŒè¯åŠŸèƒ½"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    enable_session_verification: Annotated[
        bool,
        Field(default=True, description="æ˜¯å¦å¯ç”¨ä¼šè¯éªŒè¯ä¸­é—´ä»¶"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    enable_multi_device_login: Annotated[
        bool,
        Field(default=True, description="æ˜¯å¦å…è®¸å¤šè®¾å¤‡åŒæ—¶ç™»å½•"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    max_tokens_per_client: Annotated[
        int,
        Field(default=10, description="æ¯ä¸ªç”¨æˆ·æ¯ä¸ªå®¢æˆ·ç«¯çš„æœ€å¤§ä»¤ç‰Œæ•°é‡"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    device_trust_duration_days: Annotated[
        int,
        Field(default=30, description="è®¾å¤‡ä¿¡ä»»æŒç»­å¤©æ•°"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    email_provider: Annotated[
        Literal["smtp", "mailersend", "resend"],
        Field(default="smtp", description="é‚®ä»¶å‘é€æä¾›å•†ï¼šsmtpï¼ˆSMTPï¼‰æˆ– mailersendï¼ˆMailerSendï¼‰"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    smtp_server: Annotated[
        str,
        Field(default="localhost", description="SMTP æœåŠ¡å™¨åœ°å€"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    smtp_port: Annotated[
        int,
        Field(default=587, description="SMTP æœåŠ¡å™¨ç«¯å£"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    smtp_username: Annotated[
        str,
        Field(default="", description="SMTP ç”¨æˆ·å"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    smtp_password: Annotated[
        str,
        Field(default="", description="SMTP å¯†ç "),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    from_email: Annotated[
        str,
        Field(default="toriihalo@shikkesora.com", description="å‘ä»¶äººé‚®ç®±"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    from_name: Annotated[
        str,
        Field(default="Torii Halo", description="å‘ä»¶äººåç§°"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    mailersend_api_key: Annotated[
        str,
        Field(default="", description="MailerSend API Key"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    mailersend_from_email: Annotated[
        str,
        Field(default="", description="MailerSend å‘ä»¶äººé‚®ç®±ï¼ˆéœ€è¦åœ¨ MailerSend ä¸­éªŒè¯ï¼‰"),
        "éªŒè¯æœåŠ¡è®¾ç½®",
    ]
    resend_api_key: Annotated[
        str,
        Field(default="", description="Resend API Key (re_...). Free tier: 3K/month, no per-recipient cap."),
        "Verification Service Settings",
    ]
    resend_from_email: Annotated[
        str,
        Field(default="", description="Resend sender email (must be on a domain verified in your Resend account)."),
        "Verification Service Settings",
    ]

    # ç›‘æŽ§é…ç½®
    sentry_dsn: Annotated[
        HttpUrl | None,
        Field(default=None, description="Sentry DSNï¼Œä¸ºç©ºä¸å¯ç”¨ Sentry"),
        "ç›‘æŽ§è®¾ç½®",
    ]
    new_relic_environment: Annotated[
        str | None,
        Field(default=None, description='New Relic çŽ¯å¢ƒæ ‡è¯†ï¼Œè®¾ç½®ä¸º "production" æˆ– "development"'),
        "ç›‘æŽ§è®¾ç½®",
    ]

    # GeoIP é…ç½®
    maxmind_license_key: Annotated[
        str,
        Field(default="", description="MaxMind License Keyï¼ˆç”¨äºŽä¸‹è½½ç¦»çº¿IPåº“ï¼‰"),
        "GeoIP é…ç½®",
    ]
    geoip_dest_dir: Annotated[
        str,
        Field(default="./geoip", description="GeoIP æ•°æ®åº“å­˜å‚¨ç›®å½•"),
        "GeoIP é…ç½®",
    ]
    geoip_update_day: Annotated[
        int,
        Field(default=1, description="GeoIP æ¯å‘¨æ›´æ–°çš„æ˜ŸæœŸå‡ ï¼ˆ0=å‘¨ä¸€ï¼Œ6=å‘¨æ—¥ï¼‰"),
        "GeoIP é…ç½®",
    ]
    geoip_update_hour: Annotated[
        int,
        Field(default=2, description="GeoIP æ¯å‘¨æ›´æ–°æ—¶é—´ï¼ˆå°æ—¶ï¼Œ0-23ï¼‰"),
        "GeoIP é…ç½®",
    ]

    # æ¸¸æˆè®¾ç½®
    enable_rx: Annotated[
        bool,
        Field(
            default=False,
            validation_alias=AliasChoices("enable_rx", "enable_osu_rx"),
            description="å¯ç”¨ RX mod ç»Ÿè®¡æ•°æ®",
        ),
        "æ¸¸æˆè®¾ç½®",
    ]
    enable_ap: Annotated[
        bool,
        Field(
            default=False,
            validation_alias=AliasChoices("enable_ap", "enable_osu_ap"),
            description="å¯ç”¨ AP mod ç»Ÿè®¡æ•°æ®",
        ),
        "æ¸¸æˆè®¾ç½®",
    ]
    server_authoritative_total_score: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Recompute total_score = round(total_score_without_mods * server mod multiplier) "
                "at submission instead of trusting the client. Makes the mod-multiplier rebalance "
                "apply uniformly across client versions and flags inflated/tampered totals."
            ),
        ),
        "Game Settings",
    ]
    matchmaking_current_season: Annotated[
        str,
        Field(
            default="2026-S1",
            description=(
                "Active matchmaking season id. The comfort star-rating pick is once per "
                "(user, ruleset) per this value; bump it to open a new season."
            ),
        ),
        "Game Settings",
    ]
    comfort_floor_factor: Annotated[
        float,
        Field(
            default=0.75,
            description=(
                "Anti-sandbag: the comfort star-rating pick must be >= this fraction of the "
                "mod-adjusted SR of the player's top play (proportional floor, scales across the "
                "whole range instead of a flat offset that bottoms out for low-SR players)."
            ),
        ),
        "Game Settings",
    ]
    comfort_floor_min: Annotated[
        float,
        Field(
            default=1.0,
            description="The comfort-pick floor is clamped so it never drops below this star rating.",
        ),
        "Game Settings",
    ]
    comfort_pick_max: Annotated[
        float,
        Field(default=7.5, description="Upper bound the comfort star-rating pick is clamped to."),
        "Game Settings",
    ]
    comfort_seed_sigma: Annotated[
        float,
        Field(
            default=150.0,
            description=(
                "Provisional OpenSkill sigma the comfort pick seeds a new player's matchmaking "
                "rating with. Igual al default del sistema (150): antes era 200 y las primeras "
                "partidas swingueaban demasiado (el ELO saltaba a Master y volvia a provisional al "
                "reiniciar). 150 hace que el pick quede mas cerca de lo elegido y estabiliza antes."
            ),
        ),
        "Game Settings",
    ]
    matchmaking_placement_plays: Annotated[
        int,
        Field(
            default=5,
            description=(
                "Ranked matches a player must finish in a pool before their rank tier is final. "
                "Below this the client shows a 'Provisional' badge instead of the seeded tier, so "
                "a fresh star-pick seed does not read as e.g. 'Master' with zero games played."
            ),
        ),
        "Game Settings",
    ]
    enable_supporter_for_all_users: Annotated[
        bool,
        Field(default=False, description="å¯ç”¨æ‰€æœ‰æ–°æ³¨å†Œç”¨æˆ·çš„æ”¯æŒè€…çŠ¶æ€"),
        "æ¸¸æˆè®¾ç½®",
    ]
    enable_all_beatmap_leaderboard: Annotated[
        bool,
        Field(default=False, description="å¯ç”¨æ‰€æœ‰è°±é¢çš„æŽ’è¡Œæ¦œ"),
        "æ¸¸æˆè®¾ç½®",
    ]
    enable_all_beatmap_pp: Annotated[
        bool,
        Field(default=False, description="å…è®¸ä»»ä½•è°±é¢èŽ·å¾— PP"),
        "æ¸¸æˆè®¾ç½®",
    ]
    seasonal_backgrounds: Annotated[
        list[str],
        Field(default=[], description="å­£èŠ‚èƒŒæ™¯å›¾ URL åˆ—è¡¨"),
        "æ¸¸æˆè®¾ç½®",
    ]
    beatmap_tag_top_count: Annotated[
        int,
        Field(default=2, description="æ˜¾ç¤ºåœ¨ç»“ç®—åˆ—è¡¨çš„æ ‡ç­¾æ‰€éœ€çš„æœ€ä½Žç¥¨æ•°"),
        "æ¸¸æˆè®¾ç½®",
    ]
    old_score_processing_mode: Annotated[
        OldScoreProcessingMode,
        Field(
            default=OldScoreProcessingMode.NORMAL,
            description=(
                "æ—§æˆç»©å¤„ç†æ¨¡å¼<br/>strict: åˆ é™¤æ‰€æœ‰ç›¸å…³çš„æˆç»©ã€ppã€ç»Ÿè®¡ä¿¡æ¯ã€å›žæ”¾<br/>normal: åˆ é™¤ pp å’ŒæŽ’è¡Œæ¦œæˆç»©"
            ),
        ),
        "æ¸¸æˆè®¾ç½®",
    ]
    scoring_mode: Annotated[
        ScoringMode,
        Field(
            default=ScoringMode.STANDARDISED,
            description="åˆ†æ•°è®¡ç®—æ¨¡å¼ï¼šstandardisedï¼ˆæ ‡å‡†åŒ–ï¼‰æˆ– classicï¼ˆç»å…¸ï¼‰",
        ),
        "æ¸¸æˆè®¾ç½®",
    ]

    # è¡¨çŽ°è®¡ç®—è®¾ç½®
    calculator: Annotated[
        Literal["rosu", "performance_server"],
        Field(default="performance_server", description="è¡¨çŽ°åˆ†è®¡ç®—å™¨"),
        "è¡¨çŽ°è®¡ç®—è®¾ç½®",
    ]
    calculator_config: Annotated[
        dict[str, Any],
        Field(
            default={"server_url": "http://localhost:5225"},
            description="è¡¨çŽ°åˆ†è®¡ç®—å™¨é…ç½® (JSON æ ¼å¼)ï¼Œå…·ä½“é…ç½®é¡¹è¯·å‚è€ƒä¸Šæ–¹",
        ),
        "è¡¨çŽ°è®¡ç®—è®¾ç½®",
    ]
    fallback_no_calculator_pp: Annotated[
        bool,
        Field(default=False, description="å½“è®¡ç®—å™¨ä¸æ”¯æŒæŸä¸ªæ¨¡å¼æ—¶ï¼Œä½¿ç”¨ç®€åŒ–çš„ pp è®¡ç®—æ–¹æ³•ä½œä¸ºåŽå¤‡"),
        "è¡¨çŽ°è®¡ç®—è®¾ç½®",
    ]
    mania_calculator_config: Annotated[
        dict[str, Any],
        Field(
            default={},
            description="perf-server aparte para la dificultad de mania (osu con Sunny). vacio = usa el principal",
        ),
        "Performance Settings",
    ]
    mania_pp_rework: Annotated[
        Literal["off", "sunny_wip"],
        Field(default="off", description="osu!mania pp rework mode"),
        "Performance Settings",
    ]
    # è°±é¢ç¼“å­˜è®¾ç½®
    enable_beatmap_preload: Annotated[
        bool,
        Field(default=True, description="å¯ç”¨è°±é¢ç¼“å­˜é¢„åŠ è½½"),
        "ç¼“å­˜è®¾ç½®",
        "è°±é¢ç¼“å­˜",
    ]
    beatmap_cache_expire_hours: Annotated[
        int,
        Field(default=24, description="è°±é¢ç¼“å­˜è¿‡æœŸæ—¶é—´ï¼ˆå°æ—¶ï¼‰"),
        "ç¼“å­˜è®¾ç½®",
        "è°±é¢ç¼“å­˜",
    ]
    beatmapset_cache_expire_seconds: Annotated[
        int,
        Field(default=3600, description="Beatmapset ç¼“å­˜è¿‡æœŸæ—¶é—´ï¼ˆç§’ï¼‰"),
        "ç¼“å­˜è®¾ç½®",
        "è°±é¢ç¼“å­˜",
    ]

    # æŽ’è¡Œæ¦œç¼“å­˜è®¾ç½®
    enable_ranking_cache: Annotated[
        bool,
        Field(default=True, description="å¯ç”¨æŽ’è¡Œæ¦œç¼“å­˜"),
        "ç¼“å­˜è®¾ç½®",
        "æŽ’è¡Œæ¦œç¼“å­˜",
    ]
    ranking_cache_expire_minutes: Annotated[
        int,
        Field(default=10, description="æŽ’è¡Œæ¦œç¼“å­˜è¿‡æœŸæ—¶é—´ï¼ˆåˆ†é’Ÿï¼‰"),
        "ç¼“å­˜è®¾ç½®",
        "æŽ’è¡Œæ¦œç¼“å­˜",
    ]
    ranking_cache_refresh_interval_minutes: Annotated[
        int,
        Field(default=10, description="æŽ’è¡Œæ¦œç¼“å­˜åˆ·æ–°é—´éš”ï¼ˆåˆ†é’Ÿï¼‰"),
        "ç¼“å­˜è®¾ç½®",
        "æŽ’è¡Œæ¦œç¼“å­˜",
    ]
    ranking_cache_max_pages: Annotated[
        int,
        Field(default=20, description="æœ€å¤šç¼“å­˜çš„é¡µæ•°"),
        "ç¼“å­˜è®¾ç½®",
        "æŽ’è¡Œæ¦œç¼“å­˜",
    ]
    top_score_cache_max_pages: Annotated[
        int,
        Field(default=3, description="Pages of top-scores private API to cache"),
        "cache settings",
        "top scores cache",
    ]
    ranking_cache_top_countries: Annotated[
        int,
        Field(default=20, description="ç¼“å­˜å‰Nä¸ªå›½å®¶çš„æŽ’è¡Œæ¦œ"),
        "ç¼“å­˜è®¾ç½®",
        "æŽ’è¡Œæ¦œç¼“å­˜",
    ]

    # ç”¨æˆ·ç¼“å­˜è®¾ç½®
    enable_user_cache_preload: Annotated[
        bool,
        Field(default=True, description="å¯ç”¨ç”¨æˆ·ç¼“å­˜é¢„åŠ è½½"),
        "ç¼“å­˜è®¾ç½®",
        "ç”¨æˆ·ç¼“å­˜",
    ]
    user_cache_expire_seconds: Annotated[
        int,
        Field(default=300, description="ç”¨æˆ·ä¿¡æ¯ç¼“å­˜è¿‡æœŸæ—¶é—´ï¼ˆç§’ï¼‰"),
        "ç¼“å­˜è®¾ç½®",
        "ç”¨æˆ·ç¼“å­˜",
    ]
    user_scores_cache_expire_seconds: Annotated[
        int,
        Field(default=60, description="ç”¨æˆ·æˆç»©ç¼“å­˜è¿‡æœŸæ—¶é—´ï¼ˆç§’ï¼‰"),
        "ç¼“å­˜è®¾ç½®",
        "ç”¨æˆ·ç¼“å­˜",
    ]
    user_beatmapsets_cache_expire_seconds: Annotated[
        int,
        Field(default=600, description="ç”¨æˆ·è°±é¢é›†ç¼“å­˜è¿‡æœŸæ—¶é—´ï¼ˆç§’ï¼‰"),
        "ç¼“å­˜è®¾ç½®",
        "ç”¨æˆ·ç¼“å­˜",
    ]
    user_cache_max_preload_users: Annotated[
        int,
        Field(default=200, description="æœ€å¤šé¢„åŠ è½½çš„ç”¨æˆ·æ•°é‡"),
        "ç¼“å­˜è®¾ç½®",
        "ç”¨æˆ·ç¼“å­˜",
    ]

    # èµ„æºä»£ç†è®¾ç½®
    enable_asset_proxy: Annotated[
        bool,
        Field(default=False, description="å¯ç”¨èµ„æºä»£ç†"),
        "èµ„æºä»£ç†è®¾ç½®",
    ]
    custom_asset_domain: Annotated[
        str,
        Field(default="g0v0.top", description="è‡ªå®šä¹‰èµ„æºåŸŸå"),
        "èµ„æºä»£ç†è®¾ç½®",
    ]
    asset_proxy_prefix: Annotated[
        str,
        Field(default="assets-ppy", description="assets.ppy.sh çš„è‡ªå®šä¹‰å‰ç¼€"),
        "èµ„æºä»£ç†è®¾ç½®",
    ]
    avatar_proxy_prefix: Annotated[
        str,
        Field(default="a-ppy", description="a.ppy.sh çš„è‡ªå®šä¹‰å‰ç¼€"),
        "èµ„æºä»£ç†è®¾ç½®",
    ]
    beatmap_proxy_prefix: Annotated[
        str,
        Field(default="b-ppy", description="b.ppy.sh çš„è‡ªå®šä¹‰å‰ç¼€"),
        "èµ„æºä»£ç†è®¾ç½®",
    ]

    # è°±é¢åŒæ­¥è®¾ç½®
    enable_auto_beatmap_sync: Annotated[
        bool,
        Field(default=False, description="å¯ç”¨è‡ªåŠ¨è°±é¢åŒæ­¥"),
        "è°±é¢åŒæ­¥è®¾ç½®",
    ]
    beatmap_sync_interval_minutes: Annotated[
        int,
        Field(default=60, description="è‡ªåŠ¨è°±é¢åŒæ­¥é—´éš”ï¼ˆåˆ†é’Ÿï¼‰"),
        "è°±é¢åŒæ­¥è®¾ç½®",
    ]

    # åä½œå¼Šè®¾ç½®
    suspicious_score_check: Annotated[
        bool,
        Field(default=True, description="å¯ç”¨å¯ç–‘åˆ†æ•°æ£€æŸ¥ï¼ˆpp>3000ï¼‰"),
        "åä½œå¼Šè®¾ç½®",
    ]
    enable_suspicious_mod_alerts: Annotated[
        bool,
        Field(default=True, description="å¯ç”¨å¯ç–‘è´¦å·/æˆç»©çš„ç®¡ç†å‘Šè­¦"),
        "åä½œå¼Šè®¾ç½®",
    ]
    moderation_alert_token: Annotated[
        str,
        Field(default="", description="ToriiHalo è½®è¯¢ç®¡ç†å‘Šè­¦æ—¶ä½¿ç”¨çš„ç§æœ‰ token"),
        "åä½œå¼Šè®¾ç½®",
    ]
    # torii: token propio para el canje de coins de discord. Va aparte del de
    # mod-alerts a proposito: ese solo LEE, este MINTEA puntos, y no queremos que
    # una credencial filtrada haga las dos cosas. Vacio => el endpoint devuelve 503
    # y el canje simplemente no existe.
    discord_redeem_token: Annotated[
        str,
        Field(default="", description="Token privado de ToriiHalo para canjear coins por puntos"),
        "torii",
    ]
    # torii: el dueño del server. 0 => se usa el default historico (3).
    torii_owner_user_id: Annotated[
        int,
        Field(default=0, description="Id del dueño del server, para la amistad automatica"),
        "torii",
    ]
    # Que el dueño siga a todo el que se registre, sin esperar a que lo agreguen
    # a el. Es en un solo sentido: la lista de amigos del otro no cambia.
    torii_owner_follows_everyone: Annotated[
        bool,
        Field(default=False, description="El dueño sigue automaticamente a cada usuario nuevo"),
        "torii",
    ]
    # torii: secreto que usa torii-web para hacer cosas en nombre de un usuario
    # que el sitio ya autentico. Otra vez uno propio y no el de mod-alerts: cada
    # credencial tiene que poder revocarse sola. Vacio => 503 y las rutas /web/
    # no funcionan, que es el default correcto en cualquier instancia que no
    # tenga el sitio al lado.
    # torii: cuentas que pueden entrar con cualquier build. Es para las cuentas de
    # prueba propias, que corren clientes compilados a mano y por definicion tienen
    # un hash que no esta registrado.
    #
    # Lista chica y a mano a proposito: si esto crece, lo que hay que arreglar es el
    # registro de hashes, no seguir agregando excepciones.
    client_check_exempt_user_ids: Annotated[
        list[int],
        Field(default_factory=list, description="Usuarios que pueden entrar con cualquier cliente"),
        "torii",
    ]
    torii_web_token: Annotated[
        str,
        Field(default="", description="Token privado de torii-web para actuar en nombre de un usuario"),
        "torii",
    ]
    suspicious_alert_pp_threshold: Annotated[
        float,
        Field(default=1200.0, description="è¾¾åˆ°è¯¥ pp åŽç›´æŽ¥å‘Šè­¦"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_alert_low_acc_pp_threshold: Annotated[
        float,
        Field(default=800.0, description="ä½Žå‡†ç¡®çŽ‡é«˜ pp å‘Šè­¦é˜ˆå€¼"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_alert_low_accuracy: Annotated[
        float,
        Field(default=0.80, description="ä½Žå‡†ç¡®çŽ‡é«˜ pp å‘Šè­¦é˜ˆå€¼ï¼ˆ0-1ï¼‰"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_alert_new_account_days: Annotated[
        int,
        Field(default=14, description="å°äºŽç­‰äºŽè¯¥å¤©æ•°è§†ä¸ºæ–°è´¦å·"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_alert_new_account_pp_threshold: Annotated[
        float,
        # private-server pp scales way higher than official, so a fresh account
        # cracking 900pp is totally routine for returning players. bumped to a value
        # high enough that this check effectively only fires on something absurd.
        # tune via env if you want it to actually trigger.
        Field(default=9000.0, description="æ–°è´¦å·é«˜ pp å‘Šè­¦é˜ˆå€¼"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_alert_low_playcount_threshold: Annotated[
        int,
        Field(default=250, description="ä½Ž playcount å‘Šè­¦é˜ˆå€¼"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_alert_low_playcount_pp_threshold: Annotated[
        float,
        # same reasoning as new-account threshold above. 700pp on <250 plays
        # happens constantly on private servers. raised so this check no longer
        # alerts on normal smurf/main returners.
        Field(default=7000.0, description="ä½Ž playcount é«˜ pp å‘Šè­¦é˜ˆå€¼"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_shared_ip_user_threshold: Annotated[
        int,
        Field(default=2, description="åŒä¸€ IP å‘½ä¸­å¤šè´¦å·åŽè§¦å‘å‘Šè­¦"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_shared_ip_critical_threshold: Annotated[
        int,
        Field(default=4, description="åŒä¸€ IP å‘½ä¸­å¤šè´¦å·çš„ä¸¥é‡çº§åˆ«é˜ˆå€¼"),
        "åä½œå¼Šè®¾ç½®",
    ]
    suspicious_alerts_ignored_ips: Annotated[
        str,
        Field(default="", description="Comma-separated CIDR/IP list to suppress suspicious-login alerts (admin VPN exits, home ISP, etc)."),
        "Verification Service Settings",
    ]
    suspicious_alerts_ignored_user_ids: Annotated[
        str,
        Field(default="", description="Comma-separated user ids to suppress suspicious-login alerts (admin alt accounts)."),
        "Verification Service Settings",
    ]
    # ─── External anti-cheat service (torii-slitwrist) ─────────────────────
    # Detection logic lives in a separate private repo / container; this
    # service only knows the HTTP contract. Empty URL = feature disabled
    # (default). Forks of g0v0-server get a no-op stub for free.
    anticheat_url: Annotated[
        str,
        Field(
            default="",
            description="Base URL of the private anti-cheat detection service (e.g. http://torii-slitwrist:8082). Empty = feature disabled.",
        ),
        "Verification Service Settings",
    ]
    anticheat_token: Annotated[
        str,
        Field(
            default="",
            description="Shared secret sent as X-AC-Token on every anti-cheat call. Match the value the service is configured to accept.",
        ),
        "Verification Service Settings",
    ]
    anticheat_timeout_sec: Annotated[
        float,
        Field(
            default=8.0,
            description="Read timeout for the anti-cheat call. Fire-and-forget; a hung service must NOT block score submission.",
        ),
        "Verification Service Settings",
    ]
    anticheat_include_replay: Annotated[
        bool,
        Field(
            default=True,
            description="Whether to forward the binary replay (base64-encoded) when available. Disable to reduce bandwidth if the service only does behavioural / score-shape checks.",
        ),
        "Verification Service Settings",
    ]
    anticheat_critical_creates_alert: Annotated[
        bool,
        Field(
            default=True,
            description="When the anti-cheat verdict is 'critical' or 'suspicious', create a SuspiciousAlert row so the existing mod-alert dispatch pipeline picks it up. The score is NEVER auto-rejected regardless of verdict.",
        ),
        "Verification Service Settings",
    ]
    banned_name: Annotated[
        list[str],
        Field(
            default=[
                "mrekk",
                "vaxei",
                "btmc",
                "cookiezi",
                "peppy",
                "torii",
                "saragi",
                "chocomint",
            ],
            description="ç¦æ­¢ä½¿ç”¨çš„ç”¨æˆ·ååˆ—è¡¨",
        ),
        "åä½œå¼Šè®¾ç½®",
    ]
    allow_delete_scores: Annotated[
        bool,
        Field(default=False, description="å…è®¸ç”¨æˆ·åˆ é™¤è‡ªå·±çš„æˆç»©"),
        "åä½œå¼Šè®¾ç½®",
    ]
    check_ruleset_version: Annotated[
        bool,
        Field(default=True, description="æ£€æŸ¥è‡ªå®šä¹‰ ruleset ç‰ˆæœ¬"),
        "åä½œå¼Šè®¾ç½®",
    ]
    check_client_version: Annotated[
        bool,
        Field(default=True, description="æ£€æŸ¥å®¢æˆ·ç«¯ç‰ˆæœ¬"),
        "åä½œå¼Šè®¾ç½®",
    ]
    client_version_urls: Annotated[
        list[str],
        Field(
            default=["https://raw.githubusercontent.com/GooGuTeam/g0v0-client-versions/main/version_list.json"],
            description=(
                "å®¢æˆ·ç«¯ç‰ˆæœ¬åˆ—è¡¨ URL, æŸ¥çœ‹ https://github.com/GooGuTeam/g0v0-client-versions æ¥æ·»åŠ ä½ è‡ªå·±çš„å®¢æˆ·ç«¯"
            ),
        ),
        "åä½œå¼Šè®¾ç½®",
    ]

    client_version_webhook_secret: Annotated[
        str,
        Field(
            default="",
            description=(
                "Secret token for the CI webhook that auto-registers new client build hashes. "
                "Set to a long random string and configure the same value as the GitHub Actions secret "
                "'TORII_VERSION_WEBHOOK_SECRET'. Leave empty to disable the endpoint."
            ),
        ),
        "Anti-Cheat Settings",
    ]

    # ── Donations ────────────────────────────────────────────────────────────
    # Ko-fi sends a `verification_token` field with every webhook so we can
    # cheaply confirm requests are coming from Ko-fi (it's plain-text but
    # paired with HTTPS — sufficient for the threat model). When unset, the
    # Ko-fi webhook endpoint refuses ALL requests.
    kofi_verification_token: Annotated[
        str,
        Field(default="", description="Ko-fi webhook verification token (env: KOFI_VERIFICATION_TOKEN)"),
        "Donations",
    ]
    # Optional Discord webhook URL — when set, every donation event posts
    # a small embed there for live tracking. Leave blank to disable.
    discord_donations_webhook_url: Annotated[
        str,
        Field(default="", description="Discord webhook URL for donation events (optional)"),
        "Donations",
    ]
    # Discord webhook URL for the admin "title feed" channel. When an admin
    # grants or revokes a Torii title (DEV / BUG / GOOF / MOD / etc.) the
    # server posts a single embed naming the user, the actor, and the
    # add/remove diff. Leave blank to disable. Same security model as the
    # donations webhook (best-effort, never blocks the API call).
    discord_title_feed_webhook_url: Annotated[
        str,
        Field(default="", description="Discord webhook URL for admin title-grant feed (optional)"),
        "Donations",
    ]
    high_pp_alert_threshold: Annotated[
        float,
        Field(default=1000.0, description="pp threshold that triggers a moderation alert (0 disables)"),
        "Donations",
    ]
    high_pp_backfill_threshold: Annotated[
        float,
        Field(default=800.0, description="pp threshold used by the one-off historical sweep"),
        "Donations",
    ]
    # relay one-way del chat publico #osu a discord. webhook PROPIO (no el del
    # feed: el chat lo inundaria). vacio = apagado.
    discord_osu_chat_webhook_url: Annotated[
        str,
        Field(default="", description="Discord webhook URL for the #osu public chat relay (optional)"),
        "Donations",
    ]
    # los #1 aparte del resto del feed. incluso de a tandas de 10 son la mayor
    # parte del volumen del canal y tapan los otros eventos, asi que se prenden
    # y se apagan solos.
    discord_feed_number_one_enabled: Annotated[
        bool,
        Field(default=True, description="Post new #1 scores to the Discord feed"),
        "Donations",
    ]
    # y si tienen canal propio, va aca. vacio = usan el webhook del feed.
    discord_number_one_webhook_url: Annotated[
        str,
        Field(default="", description="Dedicated Discord webhook URL for new #1 scores (falls back to the feed webhook)"),
        "Donations",
    ]

    # å­˜å‚¨è®¾ç½®
    storage_service: Annotated[
        StorageServiceType,
        Field(default=StorageServiceType.LOCAL, description="å­˜å‚¨æœåŠ¡ç±»åž‹ï¼šlocalã€r2ã€s3"),
        "å­˜å‚¨æœåŠ¡è®¾ç½®",
    ]
    storage_settings: Annotated[
        LocalStorageSettings | CloudflareR2Settings | AWSS3StorageSettings,
        Field(default=LocalStorageSettings(), description="å­˜å‚¨æœåŠ¡é…ç½® (JSON æ ¼å¼)"),
        "å­˜å‚¨æœåŠ¡è®¾ç½®",
    ]

    @field_validator("storage_settings", mode="after")
    @classmethod
    def validate_storage_settings(
        cls,
        v: LocalStorageSettings | CloudflareR2Settings | AWSS3StorageSettings,
        info: ValidationInfo,
    ) -> LocalStorageSettings | CloudflareR2Settings | AWSS3StorageSettings:
        service = info.data.get("storage_service")
        if service == StorageServiceType.CLOUDFLARE_R2 and not isinstance(v, CloudflareR2Settings):
            raise ValueError("When storage_service is 'r2', storage_settings must be CloudflareR2Settings")
        if service == StorageServiceType.LOCAL and not isinstance(v, LocalStorageSettings):
            raise ValueError("When storage_service is 'local', storage_settings must be LocalStorageSettings")
        if service == StorageServiceType.AWS_S3 and not isinstance(v, AWSS3StorageSettings):
            raise ValueError("When storage_service is 's3', storage_settings must be AWSS3StorageSettings")
        return v


settings = Settings()  # pyright: ignore[reportCallIssue]

