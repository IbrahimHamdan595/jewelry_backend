import re
from datetime import datetime

from pydantic import BaseModel, field_validator


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class CategoryCreate(BaseModel):
    name_en: str
    name_ar: str = ""
    slug: str = ""

    @field_validator("slug", mode="before")
    @classmethod
    def auto_slug(cls, v: str, info) -> str:
        if not v:
            name = (info.data or {}).get("name_en", "")
            return _slugify(name)
        return _slugify(v)


class CategoryUpdate(BaseModel):
    name_en: str | None = None
    name_ar: str | None = None
    slug: str | None = None
    is_active: bool | None = None


class CategoryOut(BaseModel):
    id: str
    name_en: str
    name_ar: str
    slug: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
