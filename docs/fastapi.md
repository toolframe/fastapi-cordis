# FastAPI integration

`install(app, manager)` stores the manager on `app.state.plugin_manager`, composes
with the existing application lifespan, and adds `GET /plugins`. Plugins start
before the existing lifespan and close after it; yielded lifespan state is
preserved. Installing a second manager on the same application raises `PluginError`.

## Inject a service

Use `Depends(service("name"))` in a route to receive a leased service value. The
dependency holds its lease for the request lifetime. If a registered service is
unavailable, the request receives HTTP 503. Disabling a plugin leaves the route
registered, so callers can retry after the service is enabled again.

Background work that outlives the request must acquire its own lease. Synchronous
workers should hold leases inside the worker doing the work; see
[lease lifetime](lifecycle.md#keep-leases-around-the-actual-work).

The [README quick start](../README.md#quick-start) is a complete application with
management writes disabled. `examples/app.py` opts into writes for local use.

## Management routes

Pass `management=True` to `install` or `plugin_router` to expose mutations.
`prefix` defaults to `/plugins` and can be changed by the host.

| Method and path | Behavior |
| --- | --- |
| `GET /plugins` | Return `{"plugins": [...]}` from the current snapshot |
| `POST /plugins/{name}/enable` | Start the named plugin |
| `POST /plugins/{name}/disable` | Stop the named plugin |
| `POST /plugins/{name}/disable?cascade=true` | Also stop active dependents |
| `PUT /plugins/{name}/config` | Replace configuration using a JSON object |

Successful mutations return the updated snapshot. To replace the included example's
greeting configuration while it is running locally:

```sh
curl -X PUT http://127.0.0.1:8000/plugins/greeting/config \
  -H 'Content-Type: application/json' \
  -d '{"prefix":"Welcome"}'
```

`GET /hello/Ada` will then return `{"message":"Welcome, Ada"}`. Omitted fields use
model defaults; the body is a replacement, not a patch.

| HTTP status | Meaning |
| --- | --- |
| 404 | A management operation names an unknown plugin |
| 409 | A lifecycle conflict, protected plugin, drain timeout, or manager that has not started |
| 422 | Invalid configuration or request validation failure |
| 500 | An unexpected setup, cleanup, or other operation error |
| 503 | A route's service dependency is unavailable |

Configuration validation details and unexpected exception text are omitted from
management error responses. Unexpected operation failures are logged through
the `fastapi_cordis` logger; the host controls log access and handling. Snapshots
omit configuration values and service objects.

## Protect management routes

Authentication and authorization belong to the host. For router-specific access
control, compose the lifespan and mount the router yourself. This fragment assumes
`manager` is your `PluginManager` and `require_operator` is your existing FastAPI
authentication dependency:

```python
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi_cordis import plugin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with manager.lifespan():
        yield


app = FastAPI(lifespan=lifespan)
app.state.plugin_manager = manager
app.include_router(
    plugin_router(manager, prefix="/admin/plugins", management=True),
    dependencies=[Depends(require_operator)],
)
```

This replaces `install`; do not also install the same manager or mount another
unprotected copy of its router. If the host has other lifespan work, compose that
work in this lifespan as well.

## Deployment

Each server worker has its own manager and service instances. A management request
changes only the worker that handles it. Coordinate multiple processes externally
and persist desired configuration in the host. Runtime controls do not install
packages, reload source code, or apply database migrations.
