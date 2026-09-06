import warnings
from contextlib import asynccontextmanager

import pytest
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated; install `httpx2` instead\.",
    category=StarletteDeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r"The anyio\.abc\.BlockingPortal alias is deprecated, use anyio\.from_thread\.BlockingPortal instead\.",
    category=DeprecationWarning,
)

from fastapi.testclient import TestClient

from fastapi_cordis import Plugin, PluginError, PluginManager, install, service


def test_install_composes_lifespans_and_service_dependency() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def host_lifespan(app: FastAPI):
        events.append("host start")
        yield {"marker": "preserved"}
        events.append("host stop")

    app = FastAPI(lifespan=host_lifespan)

    async def setup(context):
        events.append("plugin start")

        async def cleanup() -> None:
            events.append("plugin stop")

        context.on_dispose(cleanup)
        return "hello"

    manager = PluginManager([Plugin(name="greeting", setup=setup)])
    install(app, manager)

    @app.get("/greeting")
    async def greeting(
        request: Request, value: str = Depends(service("greeting"))
    ) -> dict[str, str]:
        events.append("request")
        return {"value": value, "marker": request.state.marker}

    with TestClient(app) as client:
        assert events == ["plugin start", "host start"]

        response = client.get("/greeting")

        assert response.status_code == 200
        assert response.json() == {"value": "hello", "marker": "preserved"}
        assert events == ["plugin start", "host start", "request"]

    assert events == [
        "plugin start",
        "host start",
        "request",
        "host stop",
        "plugin stop",
    ]


def test_default_install_lists_active_plugins_without_config_and_no_management() -> (
    None
):
    app = FastAPI()
    manager = PluginManager(
        [
            Plugin(
                name="greeting",
                setup=lambda context: "hello",
                config={"secret": "super-secret"},
            )
        ]
    )
    install(app, manager)

    with TestClient(app) as client:
        response = client.get("/plugins")

        assert response.status_code == 200
        assert response.json() == {
            "plugins": [
                {
                    "name": "greeting",
                    "description": "",
                    "requires": [],
                    "enabled": True,
                    "managed": True,
                    "state": "active",
                    "inFlight": 0,
                    "error": None,
                }
            ]
        }
        assert "super-secret" not in response.text
        assert client.post("/plugins/greeting/disable").status_code in {404, 405}


class GreetingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


def test_management_routes_validate_config_and_control_service() -> None:
    app = FastAPI()

    def setup(context):
        return context.config.value

    manager = PluginManager(
        [
            Plugin(
                name="greeting",
                setup=setup,
                config={"value": "old"},
                config_model=GreetingConfig,
            )
        ]
    )
    install(app, manager, management=True)

    @app.get("/greeting")
    async def greeting(value: str = Depends(service("greeting"))) -> dict[str, str]:
        return {"value": value}

    with TestClient(app) as client:
        unknown = client.put("/plugins/missing/config", json={"value": "new"})
        assert unknown.status_code == 404

        invalid = client.put(
            "/plugins/greeting/config",
            json={"value": "new", "unexpected": "rejected"},
        )
        assert invalid.status_code == 422
        assert client.get("/greeting").json() == {"value": "old"}

        valid = client.put("/plugins/greeting/config", json={"value": "new"})
        assert valid.status_code == 200
        assert client.get("/greeting").json() == {"value": "new"}

        disabled = client.post("/plugins/greeting/disable")
        assert disabled.status_code == 200
        assert client.get("/greeting").status_code == 503

        enabled = client.post("/plugins/greeting/enable")
        assert enabled.status_code == 200
        assert client.get("/greeting").json() == {"value": "new"}


def test_install_rejects_duplicate_and_keeps_manager_snapshot_accessible() -> None:
    app = FastAPI()
    manager = PluginManager([Plugin(name="greeting", setup=lambda context: "hello")])
    install(app, manager)

    with pytest.raises(PluginError):
        install(app, manager)

    snapshot = app.state.plugin_manager.snapshot()
    assert snapshot[0]["name"] == "greeting"
    assert snapshot[0]["state"] == "registered"


def test_valid_config_with_failed_setup_reports_server_error_and_private_diagnostics():
    def setup(ctx):
        if ctx.config.get("fail"):
            raise ValueError("connection secret must not reach HTTP response")
        return "ok"

    app = FastAPI()
    manager = PluginManager([Plugin("service", setup)])
    install(app, manager, management=True)
    with TestClient(app) as client:
        response = client.put("/plugins/service/config", json={"fail": True})
        assert response.status_code == 500
        assert "connection secret" not in response.text
        plugin = client.get("/plugins").json()["plugins"][0]
        assert plugin["state"] == "failed"
        assert plugin["error"] == "setup_failed"
