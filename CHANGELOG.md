# Changelog

## 0.2.2 — 2026-10-14 (PyPI)

- Publish the first release on the production PyPI index.
- Document direct installation with `pip install fastapi-cordis`.
- Runtime behavior and dependencies are unchanged from 0.2.1.

## 0.2.1 — 2026-10-14 (TestPyPI)

- Set distribution author and copyright attribution to `byfb`.
- Runtime behavior and dependencies are unchanged from 0.2.0.

## 0.2.0 — 2026-10-14 (TestPyPI)

This experimental TestPyPI release extends the runtime with reactive dependencies
and owned effects. It remains an independent Cordis-inspired Python implementation.
This release is not published to the production PyPI index.

### Added

- Blocked dependency state, automatic recovery, explicit failure retry, and
  runtime definition registration/removal.
- `EffectScope`, lifetime-owned cleanup, context managers, child scopes, and
  background tasks with cancellation-safe disposal.
- Typed `EventKey` events, automatic listener cleanup, leased invocations,
  concurrent dispatch, and aggregated failures.
- `ServiceKey` contracts, multiple capabilities through `ctx.provide()`, and
  hierarchical service namespaces with provider replacement and fallback.
- Read-only configuration previews, equivalent-update detection, configuration
  revisions, service generations, lifecycle timings, and task diagnostics.
- Opt-in FastAPI retry and configuration-plan endpoints, scoped service
  dependencies, guides, and an executable reactive example.
- Guards against lifecycle callbacks, tasks, nested event handlers, resource
  acquisitions, and child cleanups awaiting their own teardown.

### Migration from 0.1.0

1. Missing or disabled providers now leave consumers `blocked` instead of making
   construction/startup fail. Call `manager.validate()` before startup if a
   complete, enabled dependency graph is required.
2. `disable(name)` now stops active dependents and preserves their enabled intent;
   it no longer rejects a plugin merely because active dependents exist. They
   resume when the provider returns. Use `disable(name, cascade=True)` to record
   disabled intent for the whole dependent group, including blocked consumers.
3. Failed plugins require `retry(name)` rather than `enable(name)`. Configuration
   corrections retain failed state until explicit retry. Restoring a dependency
   does not retry failures. A fresh startup attempt retries enabled startup failures.
4. Equivalent validated configuration is a no-op. Do not use an unchanged
   `configure()` call as a restart command; disable and enable explicitly when
   a restart is needed.
5. Snapshots add fields and the `blocked` state. With typed services, `requires`
   lists resolved provider IDs and `provides` describes the capabilities. Scoped
   plugin IDs use `scope/plugin-name`; unscoped names remain unchanged.
6. `ctx.on_dispose()` now accepts registrations throughout the plugin lifetime and
   returns an async disposer for early cleanup. Existing calls that ignore the
   return value still register cleanup. Task/cleanup failures may be grouped.

Python 3.11 remains the minimum version. Legacy string service references, setup
return values, threaded leases, FastAPI installation, and host-owned protection
remain supported. Management state is local and in memory; source reloads,
package installation, durable configuration, and multi-process coordination
remain application responsibilities.

## 0.1.0 — initial development baseline

- Explicit plugin definitions, ordered startup and cleanup, Pydantic settings,
  service leases, configuration replacement, and FastAPI lifespan/route integration.
- Standalone packaging, guides, examples, and lifecycle regression tests.
