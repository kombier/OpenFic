"""Magpie controls exposure; OpenFic owns all local uses of available models."""

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


def config():
    return {
        "magpie": {
            "version": 2,
            "url": "http://127.0.0.1:3425/v1",
            "api_key": "private-test-key",
            "models": [
                {
                    "id": "chatgpt-api/gpt-6.1-sol",
                    "name": "GPT-6.1 Sol",
                    "context": 500000,
                    "pricing": {"prompt": "0.000003", "completion": "0.000012"},
                },
                {"id": "zhipu/glm-5.3", "name": "GLM-5.3", "context": 1000000},
            ],
        },
    }


@pytest.mark.asyncio
async def test_catalog_keeps_local_choices_parameters_and_durable_ids(
    session, catalog_file
):
    provider = ModelProvider(
        id="manual",
        name="Manual",
        provider_type="openai-compatible",
        url="http://127.0.0.1:3425/v1",
        api_key_encrypted="unchanged",
    )
    model = Model(
        id="stable-id",
        name="My GPT",
        provider_id="manual",
        model_id="chatgpt-api/gpt-6.1-sol",
        temperature=0.4,
        max_tokens=4096,
    )
    embedding = Model(
        id="embed",
        name="Local embedding",
        provider_id="manual",
        model_id="embed",
        task_type="embedding",
    )
    setting = Setting(key="default_model", value="stable-id")
    session.add_all([provider, model, embedding, setting])
    await session.flush()
    original = model.model_dump(mode="json")
    catalog_file.write_text(json.dumps(config()))
    managed = await model_repo.get_all(session)
    gpt = next(m for m in managed if m.id == "stable-id")
    assert (
        gpt.name,
        gpt.provider_id,
        gpt.temperature,
        gpt.max_tokens,
        gpt.input_price,
    ) == ("My GPT", PROVIDER_ID, 0.4, 4096, 3.0)
    assert (
        next(m for m in managed if m.id == "embed").model_dump()
        == embedding.model_dump()
    )
    glm = next(m for m in managed if m.model_id == "zhipu/glm-5.3")
    assert (
        await session.execute(select(Model).where(Model.id == glm.id))
    ).scalar_one().provider_id == "manual"
    assert model.model_dump(mode="json") == original
    await setting_repo.upsert(session, "default_model", glm.id)
    await setting_repo.upsert(session, "light_model", glm.id)
    await model_repo.update(
        session, glm.id, name="My GLM", temperature=0.2, max_tokens=2048
    )
    assert (await setting_repo.get_by_key(session, "default_model")).value == glm.id
    # Refresh supplies metadata, never overwrites OpenFic usage choices or tuning.
    data = config()
    data["magpie"]["models"][1]["context"] = 900000
    catalog_file.write_text(json.dumps(data))
    session.info.clear()
    changed = await model_repo.get_by_id(session, glm.id)
    assert (
        changed.name,
        changed.temperature,
        changed.max_tokens,
        changed.context_length,
    ) == ("My GLM", 0.2, 2048, 900000)
    assert (await setting_repo.get_by_key(session, "default_model")).value == glm.id
    assert len((await session.execute(select(Model))).scalars().all()) == 3
    catalog_file.write_text('{"magpie": {}}')
    session.info.clear()
    local = await model_repo.get_by_id(session, glm.id)
    assert (local.name, local.temperature, local.max_tokens, local.provider_id) == (
        "My GLM",
        0.2,
        2048,
        "manual",
    )
    assert (await setting_repo.get_by_key(session, "default_model")).value == glm.id
    assert (
        await model_provider_repo.get_by_id(session, "manual")
    ).api_key_encrypted == "unchanged"


@pytest.mark.asyncio
async def test_magpie_owns_exposure_and_connection_only(session, catalog_file):
    catalog_file.write_text(json.dumps(config()))
    rows = await model_repo.get_all(session)
    assert await setting_repo.get_by_key(session, "default_model") is None
    with pytest.raises(ValueError, match="Magpie"):
        await model_repo.delete_by_id(session, rows[0].id)
    with pytest.raises(ValueError, match="Magpie"):
        await model_repo.update(
            session, rows[0].id, model_identifier="codex/gpt-6.1-sol"
        )
    with pytest.raises(ValueError, match="Magpie"):
        await model_provider_repo.delete_by_id(session, PROVIDER_ID)
    for key in ["default_model", "light_model", "summary_model", "compaction_model"]:
        await setting_repo.upsert(session, key, rows[1].id)
        assert (await setting_repo.get_by_key(session, key)).value == rows[1].id
    with pytest.raises(ValueError, match="Magpie"):
        await model_repo.get_by_id(session, "codex/gpt-6.1-sol")
    data = config()
    data["magpie"]["models"] = []
    catalog_file.write_text(json.dumps(data))
    session.info.clear()
    assert await model_repo.get_all(session) == []
    with pytest.raises(ValueError, match="Magpie"):
        await model_repo.get_by_id(session, rows[1].id)
    assert (await setting_repo.get_by_key(session, "default_model")).value == rows[1].id
    catalog_file.write_text('{"magpie": {}}')
    session.info.clear()
    gpt = await model_repo.get_by_id(session, rows[0].id)
    assert gpt.input_price == 3.0 and gpt.output_price == 12.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"magpie": "not-an-object"},
        {"magpie": {"version": 2}},
        {
            **config(),
            "magpie": {**config()["magpie"], "url": "https://other.example/v1"},
        },
        {
            **config(),
            "magpie": {
                **config()["magpie"],
                "models": [{"id": "zcode/GLM-5.3", "name": "bad"}],
            },
        },
        {
            **config(),
            "magpie": {
                **config()["magpie"],
                "models": [{"id": "group/hermes-daily", "name": "bad"}],
            },
        },
        {
            **config(),
            "magpie": {
                **config()["magpie"],
                "models": [config()["magpie"]["models"][0]] * 2,
            },
        },
        {**config(), "magpie": {**config()["magpie"], "api_key": ""}},
    ],
)
async def test_invalid_config_fails_closed_without_key_in_error(
    session, catalog_file, bad
):
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
