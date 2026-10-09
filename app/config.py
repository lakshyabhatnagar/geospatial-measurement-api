from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    max_file_bytes: int = Field(10 * 1024**2, gt=0)
    max_request_bytes: int = Field(12 * 1024**2, gt=0)
    max_expanded_bytes: int = Field(50 * 1024**2, gt=0)
    max_zip_members: int = Field(100, gt=0)
    max_layers: int = Field(50, gt=0)
    max_features: int = Field(10_000, gt=0)
    max_coordinates: int = Field(500_000, gt=0)
    max_feature_coordinates: int = Field(50_000, gt=0)
    max_extent_km: float = Field(100, gt=0, le=100)
    processing_seconds: float = Field(60, gt=0)

    @model_validator(mode="after")
    def validate_limits(self):
        if self.max_request_bytes <= self.max_file_bytes:
            raise ValueError("Request limit must exceed file limit for multipart overhead")
        return self

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir.resolve() / 'measurements.db'}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir.resolve() / "uploads"
