from pathlib import Path
from typing import Annotated, TypedDict

from alembic.config import Config as AlembicConfig
from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings.main import SettingsConfigDict


class G0v0ServerDatabaseConfig(BaseSettings):
    model_config: SettingsConfigDict = SettingsConfigDict(
        extra="ignore",
        env_file_encoding="utf-8",
    )

    mysql_host: Annotated[
        str,
        Field(default="localhost", description="MySQL server address"),
        "Database Settings",
    ]
    mysql_port: Annotated[
        int,
        Field(default=3306, description="MySQL server port"),
        "Database Settings",
    ]
    mysql_database: Annotated[
        str,
        Field(default="osu_api", description="MySQL database name"),
        "Database Settings",
    ]
    mysql_user: Annotated[
        str,
        Field(default="osu_api", description="MySQL username"),
        "Database Settings",
    ]
    mysql_password: Annotated[
        str,
        Field(default="password", description="MySQL password"),
        "Database Settings",
    ]

    @property
    def database_url(self) -> str:
        return f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"


class ContextObject(TypedDict):
    g0v0_server_path: Path
    plugin_path: Path
    alembic_config: AlembicConfig
    plugin_id: str | None
