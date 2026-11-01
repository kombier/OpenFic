"""Magpie supplies the connection/catalog; OpenFic owns selections and tuning.

Each session reads one file snapshot. New catalog entries acquire durable local
identities through the application ORM, so all native model references work.
"""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import EncryptionService
from app.models.entities.model import Model
from app.models.entities.model_provider import ModelProvider
from app.settings import settings

PROVIDER_ID = "magpie-managed"
API_PROVIDERS = frozenset({"zhipu", "minimax-cn", "chatgpt-api"})
_SESSION_KEY = "openfic_magpie_catalog"


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    context: int = Field(default=0, ge=0, le=2000000)
    output: int = Field(default=0, ge=0)
    pricing: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def api_channel(self):
        provider, separator, model = self.id.partition("/")
        if provider not in API_PROVIDERS or not separator or not model:
            raise ValueError("Only explicit API models are supported")
        for value in self.pricing.values():
            number = float(value)
            if not 0 <= number < 1:
                raise ValueError("Invalid per-token price")
        return self


class ManagedConnection(BaseModel):
    version: int = Field(ge=1, le=2)
    url: str
    api_key: SecretStr
    models: list[CatalogModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def gateway(self):
        url = urlsplit(self.url)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.path.rstrip("/") != "/v1"
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise ValueError("Expected the local Magpie gateway /v1 URL")
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)) or not self.api_key.get_secret_value():
            raise ValueError("Invalid managed catalog")
        return self


class ManagedCatalog(BaseModel):
    magpie: ManagedConnection

    async def models(self, session: AsyncSession) -> list[Model]:
        # Reuse manual Magpie record IDs so existing roles/settings keep working.
        stored = list((await session.execute(select(Model))).scalars().all())
        providers = list((await session.execute(select(ModelProvider))).scalars().all())
        gateway_ids = {
            p.id
            for p in providers
            if p.provider_type == "openai-compatible"
            and p.url.rstrip("/") == self.magpie.url.rstrip("/")
        }
        result = [item for item in stored if item.task_type != "llm"]
        # A local record is needed for defaults, roles, session resumption and
        # sampling parameters. This is native application state, not a sync job.
        local_provider = next((p for p in providers if p.id in gateway_ids), None)
        stored_by_id = {m.id: m for m in stored}
        insert = (
            postgres_insert
            if session.get_bind().dialect.name == "postgresql"
            else sqlite_insert
        )
        if local_provider is None and self.magpie.models:
            local_provider = ModelProvider(
                id="magpie-local-"
                + hashlib.sha256(self.magpie.url.encode()).hexdigest()[:16],
                name="Magpie",
                url=self.magpie.url,
                provider_type="openai-compatible",
                # The accepted loopback gateway uses a placeholder when unmanaged.
                # Never copy a supplier credential into OpenFic.
                api_key_encrypted=EncryptionService(settings.encryption_key).encrypt(
                    "magpie"
                ),
            )
            await session.execute(
                insert(ModelProvider)
                .values(**local_provider.model_dump())
                .on_conflict_do_nothing(index_elements=["id"])
            )

        for item in self.magpie.models:
            matches = [
                m
                for m in stored
                if m.task_type == "llm"
                and m.provider_id in gateway_ids
                and m.model_id == item.id
            ]
            existing = matches[0] if len(matches) == 1 else None
            model_id = (
                existing.id
                if existing
                else "magpie-" + hashlib.sha256(item.id.encode()).hexdigest()[:24]
            )
            existing = existing or stored_by_id.get(model_id)
            price = item.pricing
            prices = dict(
                input_price=float(price.get("prompt", "0")) * 1000000,
                output_price=float(price.get("completion", "0")) * 1000000,
                cache_read_price=float(price.get("input_cache_read", "0")) * 1000000,
                cache_write_price=float(price.get("input_cache_write", "0")) * 1000000,
            )
            if existing is None:
                row = Model(
                    id=model_id,
                    name=item.name,
                    provider_id=local_provider.id,
                    model_id=item.id,
                    task_type="llm",
                    context_length=item.context or 128000,
                    **prices,
                )
                await session.execute(
                    insert(Model)
                    .values(**row.model_dump())
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                existing = (
                    await session.execute(select(Model).where(Model.id == model_id))
                ).scalar_one()

            # New detached instances: never mutate SQLAlchemy's persisted objects.
            values = existing.model_dump() if existing else {}
            values.update(
                id=model_id,
                name=existing.name,
                model_id=item.id,
                provider_id=PROVIDER_ID,
                task_type="llm",
                is_builtin=False,
                context_length=item.context or 128000,
                max_tokens=existing.max_tokens,
                **prices,
            )
            result.append(Model(**values))
        return result

    def provider(self) -> ModelProvider:
        encryption = EncryptionService(settings.encryption_key)
        return ModelProvider(
            id=PROVIDER_ID,
            name="Magpie",
            url=self.magpie.url,
            provider_type="openai-compatible",
            api_key_encrypted=encryption.encrypt(
                self.magpie.api_key.get_secret_value()
            ),
        )


def read_catalog(session: AsyncSession) -> ManagedCatalog | None:
    if _SESSION_KEY not in session.info:
        path = Path(
            os.environ.get("OPENFIC_MAGPIE_CONFIG", "~/.config/openfic/magpie.json")
        ).expanduser()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # A connect-only plugin has no model field; native disconnect
            # removes its connection keys and leaves an empty namespace.
            if not isinstance(raw, dict):
                raise ValueError("Expected a config object")
            active = "magpie" in raw and raw["magpie"] != {}
            session.info[_SESSION_KEY] = (
                ManagedCatalog.model_validate(raw) if active else None
            )
        except FileNotFoundError:
            session.info[_SESSION_KEY] = None
        except (OSError, ValueError, ValidationError):
            # Never include config or validation input (the gateway key) in errors.
            raise ValueError(
                "Magpie 受管模型配置无效；请在 Magpie 重新连接 OpenFic"
            ) from None
    return session.info[_SESSION_KEY]


def require_manual_models(session: AsyncSession) -> None:
    if read_catalog(session):
        raise ValueError(
            "可用模型列表和连接由 Magpie 提供，请在 Magpie 调整开放范围或断开连接"
        )
