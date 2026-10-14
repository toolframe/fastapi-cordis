# fastapi-cordis

An independent Python library for managing plugin services in FastAPI and asyncio
applications. Define how each service starts, which other services it needs, and
how it closes. The runtime handles ordering and waits for callers before disposing
resources.

**Status: experimental TestPyPI release.** Version `0.2.1` is intended for testing;
there is no production PyPI release. The distribution name is `fastapi-cordis`
and the Python import is `fastapi_cordis`.

Inspired by [Cordis](https://github.com/cordiverse/cordis), this is an independent
implementation, not an official port. It does not run JavaScript Cordis plugins.

## What it provides

- Explicit plugin definitions and optional Pydantic configuration validation.
- Typed service keys that let callers keep the same contract when providers change.
- Multiple capabilities per plugin and scoped provider overrides.
- Dependency-ordered startup and reverse-order cleanup, including failed setup.
- Owned resource scopes, context managers, and cancellable background tasks.
- Typed events with owned listeners and leases around running handlers.
- Reactive dependency recovery, runtime registration, and configuration replacement.
- Configuration previews, equivalent-update detection, and lifecycle diagnostics.
- Service leases that protect resources and their dependencies during async or
  worker-thread calls.
- FastAPI lifespan integration, service injection, and optional management routes.
- An asyncio lifecycle API for applications without an HTTP server.

## Requirements and installation

Python 3.11+ and asyncio are required. The package depends on FastAPI 0.135.2+
and Pydantic 2.12.5+; `pyproject.toml` defines the supported version ranges.

Install the [TestPyPI release](https://test.pypi.org/project/fastapi-cordis/0.2.1/)
in a virtual environment. Download this package from TestPyPI, then install its
wheel with runtime dependencies from the normal PyPI index:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip download --index-url https://test.pypi.org/simple/ --no-deps --only-binary=:all: --dest dist fastapi-cordis==0.2.1
python -m pip install --index-url https://pypi.org/simple/ dist/fastapi_cordis-0.2.1-py3-none-any.whl
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell instead.
TestPyPI is for testing and may remove packages; it is not a production index.
The source archive on the release page includes the guides, examples, and tests.
Cloning the GitHub repository requires repository access.

Alternatively, from the root of a source checkout:

```sh
python -m pip install .
```

For development, install the package and test tools together:

```sh
uv sync --python 3.14
```

To build and install a local distribution:

```sh
uv build
python -m pip install dist/fastapi_cordis-0.2.1-py3-none-any.whl
```

Building these files does not publish them.

## Quick start

Save this as `demo.py` after installing the package:

```python
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi_cordis import Plugin, PluginContext, PluginManager, install, service
from pydantic import BaseModel, ConfigDict


class GreetingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = "Hello"


async def greeting(ctx: PluginContext):
    return lambda name: f"{ctx.config.prefix}, {name}"


manager = PluginManager([Plugin("greeting", greeting, config_model=GreetingConfig)])
app = FastAPI()
install(app, manager)


@app.get("/hello/{name}")
async def hello(
    name: str,
    greet: Annotated[Callable[[str], str], Depends(service("greeting"))],
):
    return {"message": greet(name)}
```

Install an ASGI server and start the app:

```sh
python -m pip install uvicorn
python -m uvicorn demo:app --host 127.0.0.1
```

`GET /hello/Ada` returns `{"message":"Hello, Ada"}`. `GET /plugins` reports the
plugin's state. `install` starts plugins before the existing application lifespan
and closes them after it. `service("greeting")` holds a lease while the request
uses the service, so a concurrent stop waits for that request to finish.

The source checkout also includes `examples/app.py`, which enables management
writes for local experimentation:

```sh
uv run --with uvicorn uvicorn examples.app:app --host 127.0.0.1
```

For a serverless example of late registration, typed events, owned resources,
configuration previews, and scoped provider replacement:

```sh
uv run python examples/reactive.py
```

## Runtime management

Use `install(app, manager, management=True)` to enable mutation routes. The host
application supplies authentication and authorization for these routes.

| Endpoint | Behavior |
| --- | --- |
| `GET /plugins` | List states, dependencies, and in-flight counts |
| `POST /plugins/{name}/enable` | Enable a plugin; start when its dependencies become available |
| `POST /plugins/{name}/disable` | Stop a plugin and block its enabled dependents |
| `POST /plugins/{name}/disable?cascade=true` | Explicitly disable the dependent group |
| `POST /plugins/{name}/retry` | Explicitly retry a failed plugin |
| `PUT /plugins/{name}/config` | Validate replacement configuration and restart the affected active plugins |
| `POST /plugins/{name}/config/plan` | Preview affected plugins and protected dependents without applying settings |

Ordinary disable preserves dependents' enabled intent, so they resume when their
provider returns. Cascading disable explicitly disables the dependent group.
Missing providers leave plugins blocked; `manager.validate()` provides an optional
strict check before startup. `register()` and `unregister()` change definitions at
runtime; they do not install packages or reload source code.

Equivalent validated configuration is a no-op. Snapshots report configuration
revisions, service generations, lifecycle timings, and owned task counts. Plans
are advisory; applying an update checks the current graph again.

Configuration values and service objects are omitted from diagnostics. Runtime
changes stay in this manager; the host owns persistence and coordination across
processes. After a cascading disable, enable providers before their dependents.

## Scope and documentation

The runtime manages trusted Python services on one owning asyncio event loop.
Callers must use leases correctly; leases do not make a service itself thread-safe.
Package installation, automatic discovery, source reloads, database migrations,
plugin isolation, and coordination between server workers belong to the host.
Disabling a plugin does not remove its HTTP routes.

These guides are included in the source checkout and source distribution:

| Document | Covers |
| --- | --- |
| `docs/plugins.md` | Authoring plugins, declaring dependencies, and cleaning up resources |
| `docs/lifecycle.md` | Leases, state transitions, runtime changes, and failure recovery |
| `docs/events.md` | Typed payloads, owned listeners, and asynchronous dispatch |
| `docs/services.md` | Service contracts, multiple capabilities, and scoped providers |
| `docs/fastapi.md` | Lifespan integration, HTTP behavior, and protected management routes |
| `CONTRIBUTING.md` | Development, artifact checks, and release preparation |
| `docs/ci.md` | Isolated local CI, Python matrix, and manual TestPyPI/PyPI publishing |
| `CHANGELOG.md` | Version changes and migration from 0.1.0 |

## License

MIT. See `LICENSE` in the source distribution or the license included in the wheel.
