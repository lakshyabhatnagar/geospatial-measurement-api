from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServiceError(Exception):
    code: str
    message: str
    status: int = 422
    details: list[dict[str, Any]] = field(default_factory=list)
    file_id: str | None = None

    def __str__(self):
        return f"{self.code}: {self.message}"


@dataclass
class FeatureIssue(Exception):
    code: str
    message: str


def issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def error_body(error: ServiceError, request_id: str) -> dict:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "request_id": request_id,
            "file_id": error.file_id,
            "details": error.details,
        }
    }
