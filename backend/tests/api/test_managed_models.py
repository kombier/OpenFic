import json

import pytest

from app.models.repos import model_repo
from tests.models.test_managed_models import config


@pytest.mark.asyncio
async def test_managed_catalog_api_and_mutation_guards(
    client, session, tmp_path, monkeypatch
):
    path = tmp_path / "managed.json"
    monkeypatch.setenv("OPENFIC_MAGPIE_CONFIG", str(path))
    path.write_text(json.dumps(config()))
    response = await client.get("/api/v1/models")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    assert all(row["is_managed"] and not row["is_builtin"] for row in rows)
    providers = (await client.get("/api/v1/model-providers")).json()
    assert providers[0]["is_managed"]
    assert providers[0]["supported_task_types"] == ["llm"]
    assert "private-test-key" not in json.dumps(providers)
    available = (
        await client.get("/api/v1/model-providers/magpie-managed/models")
    ).json()
    assert [m["id"] for m in available["models"]] == [
        m["id"] for m in config()["magpie"]["models"]
    ]
    assert (await client.get("/api/v1/settings")).json()["default_model"] == ""
    for key in ["default_model", "light_model", "summary_model", "compaction_model"]:
        response = await client.patch("/api/v1/settings", json={key: rows[1]["id"]})
        assert response.status_code == 200, response.text
    response = await client.put(
        "/api/v1/agent-definitions/writer", json={"model_id": rows[0]["id"]}
    )
    assert response.status_code == 200, response.text
    response = await client.put(
        "/api/v1/models/" + rows[0]["id"],
        json={"name": "My GPT", "temperature": 0.3, "max_tokens": 4096},
    )
    assert response.status_code == 200, response.text
    # API callers can save the same full payload the existing model editor sends.
    payload = {
        k: response.json()[k]
        for k in [
            "name",
            "provider_id",
            "model_id",
            "task_type",
            "context_length",
            "input_price",
            "output_price",
            "cache_read_price",
            "cache_write_price",
            "temperature",
        ]
    }
    assert (
        await client.put("/api/v1/models/" + rows[0]["id"], json=payload)
    ).status_code == 200
    assert (await model_repo.get_by_id(session, rows[0]["id"])).temperature == 0.3
    for method, url, payload in [
        ("put", "/api/v1/models/" + rows[0]["id"], {"model_id": "codex/gpt-6.1-sol"}),
        ("delete", "/api/v1/models/" + rows[0]["id"], None),
        ("delete", "/api/v1/model-providers/magpie-managed", None),
    ]:
        kwargs = {} if payload is None else {"json": payload}
        response = await getattr(client, method)(url, **kwargs)
        assert response.status_code == 400, (url, response.status_code, response.text)
    # Following the system default is a durable native policy, not a virtual ID.
    response = await client.put(
        "/api/v1/agent-definitions/writer",
        json={"model_id": "__system_default_model__"},
    )
    assert response.status_code == 200
