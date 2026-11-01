"""Managed model reads must not mutate the manual database configuration."""

import json

import pytest
from sqlalchemy import select

from app.models.entities.model import Model
from app.models.entities.model_provider import ModelProvider
from app.models.managed import PROVIDER_ID, read_catalog
from app.models.repos import model_provider_repo, model_repo
from app.storage.models.setting import Setting
from app.storage.repos import setting_repo


@pytest.fixture
def catalog_file(tmp_path, monkeypatch):
    path = tmp_path / "magpie.json"
    monkeypatch.setenv("OPENFIC_MAGPIE_CONFIG", str(path))
    return path


def config(default="chatgpt-api/gpt-6.1-sol"):
    return {
        "model": default,
        "magpie": {
            "version": 1, "url": "http://127.0.0.1:3425/v1", "api_key": "private-test-key",
            "models": [
                {"id": "chatgpt-api/gpt-6.1-sol", "name": "GPT-6.1 Sol", "context": 500000,
                 "pricing": {"prompt": "0.000003", "completion": "0.000012"}},
                {"id": "zhipu/glm-5.3", "name": "GLM-5.3", "context": 1000000},
            ],
        },
    }


@pytest.mark.asyncio
async def test_overlay_and_disconnect_preserve_database(session, catalog_file):
    provider = ModelProvider(id="manual", name="Manual", provider_type="openai-compatible",
                             url="http://127.0.0.1:3425/v1", api_key_encrypted="unchanged")
    model = Model(id="stable-id", name="Old Name", provider_id="manual",
                  model_id="chatgpt-api/gpt-6.1-sol", temperature=0.4)
    embedding = Model(id="embed", name="Local embedding", provider_id="manual",
                      model_id="embed", task_type="embedding")
    setting = Setting(key="default_model", value="stable-id")
    session.add_all([provider, model, embedding, setting])
    await session.flush()
    before = [m.model_dump(mode="json") for m in (await session.execute(select(Model))).scalars()]
    catalog_file.write_text(json.dumps(config()))
    managed = await model_repo.get_all(session)
    gpt = next(m for m in managed if m.id == "stable-id")
    assert (gpt.name, gpt.provider_id, gpt.temperature, gpt.input_price) == ("GPT-6.1 Sol", PROVIDER_ID, 0.4, 3.0)
    assert next(m for m in managed if m.id == "embed").model_dump() == embedding.model_dump()
    assert (await setting_repo.get_by_key(session, "default_model")).value == "stable-id"
    assert (await model_provider_repo.get_by_id(session, PROVIDER_ID)).url.endswith("/v1")
    assert (await model_provider_repo.get_by_id(session, "manual")).id == "manual"
    await session.flush()
    assert [m.model_dump(mode="json") for m in (await session.execute(select(Model))).scalars()] == before
    assert (await session.execute(select(ModelProvider))).scalars().one().api_key_encrypted == "unchanged"
    # New sessions see the new file, existing resolutions keep one consistent snapshot.
    catalog_file.write_text(json.dumps(config("zhipu/glm-5.3")))
    assert (await setting_repo.get_by_key(session, "default_model")).value == "stable-id"
    session.info.clear()
    assert (await setting_repo.get_by_key(session, "default_model")).value.startswith("magpie-")
    catalog_file.write_text('{"magpie": {}}')
    session.info.clear()
    assert [m.name for m in await model_repo.get_all(session)] == ["Old Name", "Local embedding"]
    assert (await setting_repo.get_by_key(session, "default_model")).value == "stable-id"


@pytest.mark.asyncio
async def test_reject_mutations_and_subscription_refs(session, catalog_file):
    catalog_file.write_text(json.dumps(config()))
    rows = await model_repo.get_all(session)
    with pytest.raises(ValueError, match="Magpie"):
        await model_repo.delete_by_id(session, rows[0].id)
    with pytest.raises(ValueError, match="Magpie"):
        await model_repo.update(session, rows[0].id, name="Override")
    with pytest.raises(ValueError, match="Magpie"):
        await model_provider_repo.delete_by_id(session, PROVIDER_ID)
    with pytest.raises(ValueError, match="Magpie"):
        await setting_repo.upsert(session, "default_model", "other")
    await setting_repo.upsert(session, "language", "zh-CN")
    assert (await setting_repo.get_by_key(session, "language")).value == "zh-CN"
    with pytest.raises(ValueError, match="Magpie"):
        await model_repo.get_by_id(session, "codex/gpt-6.1-sol")
    with pytest.raises(ValueError, match="Magpie"):
        await setting_repo.upsert(session, "light_model", rows[0].id)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    [], {"model": "still-connected", "magpie": {}}, {"magpie": "not-an-object"},
    *[{"model": value, "magpie": {}} for value in [None, False, 0, ""]],
    {**config(), "model": "codex/gpt-6.1-sol"},
    {**config(), "magpie": {**config()["magpie"], "url": "https://other.example/v1"}},
    {**config(), "magpie": {**config()["magpie"], "models": [{"id": "zcode/GLM-5.3", "name": "bad"}]}},
    {**config(), "magpie": {**config()["magpie"], "models": [{"id": "group/hermes-daily", "name": "bad"}]}},
    {**config(), "magpie": {**config()["magpie"], "models": [config()["magpie"]["models"][0]] * 2}},
    {**config(), "magpie": {**config()["magpie"], "api_key": ""}},
])
async def test_invalid_config_fails_closed_without_key_in_error(session, catalog_file, bad):
    catalog_file.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="Magpie") as exc:
        read_catalog(session)
    assert "private-test-key" not in str(exc.value)


@pytest.mark.asyncio
async def test_missing_and_malformed_config(session, catalog_file):
    assert read_catalog(session) is None
    session.info.clear()
    catalog_file.write_text('{"magpie":')
    with pytest.raises(ValueError):
        read_catalog(session)
