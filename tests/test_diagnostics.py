import asyncio
import json
from unittest import IsolatedAsyncioTestCase

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from fastapi_cordis import (
    ConfigurationError,
    DrainTimeout,
    Plugin,
    PluginManager,
    install,
)


class Settings(BaseModel):
    value: int = 1
    secret: SecretStr


def snapshot(manager):
    return {item["name"]: item for item in manager.snapshot()}


class DiagnosticTests(IsolatedAsyncioTestCase):
    async def test_preview_has_no_effect_and_reports_protected_dependents(self):
        manager = PluginManager(
            [
                Plugin("provider", lambda ctx: ctx.config),
                Plugin("consumer", lambda ctx: None, requires=("provider",)),
                Plugin("host", lambda ctx: None, requires=("consumer",), managed=False),
                Plugin("unrelated", lambda ctx: None),
            ]
        )
        assert not manager.plan_configure("provider", {"secret": "new"})["canApply"]
        async with manager.lifespan():
            before = manager.snapshot()
            plan = manager.plan_configure("provider", {"secret": "new"})
            assert plan == {
                "name": "provider",
                "changed": True,
                "affected": ["provider", "consumer", "host"],
                "protected": ["host"],
                "managerRunning": True,
                "canApply": False,
                "configRevision": 0,
                "generation": before[0]["generation"],
            }
            assert "secret" not in json.dumps(plan)
            assert manager.snapshot() == before
            noop = manager.plan_configure("provider", {})
            assert noop["affected"] == []
            assert noop["canApply"] is True
            await manager.configure("provider", {})
            assert manager.snapshot() == before

    async def test_equivalent_models_skip_restart_and_preserve_actual_secrets(self):
        acquired = []

        def setup(ctx):
            value = (ctx.config.value, ctx.config.secret.get_secret_value())
            acquired.append(value)
            return value

        manager = PluginManager(
            [Plugin("provider", setup, config_model=Settings, config={"secret": "s1"})]
        )
        async with manager.lifespan():
            before = manager.snapshot()
            assert not manager.plan_configure(
                "provider", {"value": "1", "secret": "s1"}
            )["changed"]
            await manager.configure("provider", {"value": 1, "secret": "s1"})
            assert manager.snapshot() == before
            await manager.configure("provider", {"value": "2", "secret": "s2"})
            with manager.lease("provider") as value:
                assert value == (2, "s2")
            current = snapshot(manager)["provider"]
            assert current["configRevision"] == 1
            assert current["generation"] > before[0]["generation"]
            assert current["lastCleanupMs"] >= 0
            assert current["lastDrainMs"] >= 0
            assert "s2" not in json.dumps(current)
        assert acquired == [(1, "s1"), (2, "s2")]

    async def test_json_equality_preserves_value_types_and_ignores_key_order(self):
        manager = PluginManager(
            [Plugin("provider", lambda ctx: ctx.config, config={"a": True, "b": 2})]
        )
        async with manager.lifespan():
            assert not manager.plan_configure("provider", {"b": 2, "a": True})[
                "changed"
            ]
            await manager.configure("provider", {"a": 1, "b": 2})
            with manager.lease("provider") as config:
                assert type(config["a"]) is int
            assert snapshot(manager)["provider"]["configRevision"] == 1

    async def test_failed_replacement_records_revision_and_requires_explicit_retry(
        self,
    ):
        def setup(ctx):
            ctx.on_dispose(lambda: None)
            if ctx.config.get("fail"):
                raise ValueError("private setup failure")
            return ctx.config

        manager = PluginManager([Plugin("provider", setup)])
        async with manager.lifespan():
            first = snapshot(manager)["provider"]["generation"]
            with pytest.raises(ValueError):
                await manager.configure("provider", {"fail": True})
            failed = snapshot(manager)["provider"]
            assert failed["state"] == "failed"
            assert failed["configRevision"] == 1
            assert failed["generation"] == first
            assert failed["lastSetupMs"] >= 0
            assert failed["lastCleanupMs"] >= 0
            await manager.configure("provider", {"fail": False})
            assert snapshot(manager)["provider"]["state"] == "failed"
            await manager.retry("provider")
            recovered = snapshot(manager)["provider"]
            assert recovered["configRevision"] == 2
            assert recovered["generation"] > first
            assert recovered["error"] is None

    async def test_invalid_config_and_drain_timeout_preserve_revision_and_generation(
        self,
    ):
        manager = PluginManager(
            [
                Plugin(
                    "provider",
                    lambda ctx: ctx.config,
                    config_model=Settings,
                    config={"secret": "s1"},
                )
            ],
            drain_timeout=0.01,
        )
        async with manager.lifespan():
            before = manager.snapshot()
            with pytest.raises(ConfigurationError):
                manager.plan_configure("provider", {"secret": "s1", "value": "bad"})
            assert manager.snapshot() == before
            with manager.lease("provider"), pytest.raises(DrainTimeout):
                await manager.configure("provider", {"secret": "s2"})
            after = snapshot(manager)["provider"]
            assert after["state"] == "active"
            assert after["configRevision"] == 0
            assert after["generation"] == before[0]["generation"]
            assert after["lastDrainMs"] >= 0
            assert after["lastCleanupMs"] is None

    async def test_generation_changes_when_instance_id_is_registered_again(self):
        manager = PluginManager([Plugin("provider", lambda ctx: None)])
        async with manager.lifespan():
            previous = snapshot(manager)["provider"]["generation"]
            await manager.unregister("provider")
            await manager.register(Plugin("provider", lambda ctx: None))
            assert snapshot(manager)["provider"]["generation"] > previous
            assert snapshot(manager)["provider"]["configRevision"] == 0

    async def test_task_diagnostics_include_child_scopes_and_cleanup_reports_failure(
        self,
    ):
        entered = asyncio.Event()
        failed = None

        async def background():
            entered.set()
            await asyncio.Event().wait()

        async def broken():
            raise ValueError("private task failure")

        def setup(ctx):
            nonlocal failed
            child = ctx.scope()
            child.task(background())
            failed = child.task(broken())

        manager = PluginManager([Plugin("provider", setup)])
        await manager.start()
        await entered.wait()
        with pytest.raises(ValueError):
            await failed
        await asyncio.sleep(0)
        current = snapshot(manager)["provider"]
        assert current["ownedTasks"] == 1
        assert current["taskFailures"] == 1
        assert "private" not in json.dumps(current)
        with pytest.raises(ExceptionGroup):
            await manager.close()
        current = snapshot(manager)["provider"]
        assert current["state"] == "failed"
        assert current["error"] == "cleanup_failed"
        assert current["ownedTasks"] == 0
        assert current["lastCleanupMs"] >= 0


def test_http_preview_is_private_validated_and_does_not_restart_scoped_plugin():
    app = FastAPI()
    manager = PluginManager(
        [
            Plugin(
                "provider",
                lambda ctx: ctx.config,
                config_model=Settings,
                config={"secret": "s1"},
                scope="work.a",
            )
        ]
    )
    install(app, manager, management=True)
    path = "/plugins/work.a/provider/config/plan"
    with TestClient(app) as client:
        before = client.get("/plugins").json()
        plan = client.post(path, json={"secret": "proposed-private-secret"})
        assert plan.status_code == 200
        assert plan.json()["affected"] == ["work.a/provider"]
        assert plan.json()["canApply"] is True
        assert "private" not in plan.text
        assert client.get("/plugins").json() == before
        invalid = client.post(path, json={"value": "private-invalid-value"})
        assert invalid.status_code == 422
        assert "private" not in invalid.text
        assert client.post("/plugins/missing/config/plan", json={}).status_code == 404
        assert client.get("/plugins").json() == before
