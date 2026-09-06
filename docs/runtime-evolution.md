# Reactive runtime contract

This change extends the Cordis-inspired runtime while retaining leases around
actual work, reverse dependency cleanup, failure cleanup, serialized mutations,
and cancellation-safe management operations.

## Dependency intent and availability

`enabled` expresses operator intent. An enabled plugin whose dependencies are
missing or unavailable is `blocked`, with diagnostic dependency reasons. Disabled
and failed plugins are distinct: restoring a provider resumes blocked consumers,
but never enables an explicitly disabled plugin or retries a failed setup.

Ordinary provider disable drains and stops affected consumers while retaining
their enabled intent. `cascade=True` explicitly disables the dependent group.
Enable reconciles available dependencies. Retry is explicit for failed plugins.
Definition registration and removal validate the candidate graph before mutation.
Cycles and conflicting providers are errors; absent providers may be supplied
later. Strict validation is available for applications requiring a complete graph.

## Resource ownership

An effect scope owns disposal callbacks, acquired context managers, child scopes,
and asyncio tasks throughout its lifetime. Closing rejects new resources, cancels
and joins owned tasks, then attempts all resource cleanups in reverse order.
Callbacks may be synchronous or asynchronous. Async resource acquisition racing
with close must settle and release its resource. Cleanup failures remain visible.

Plugin contexts retain declared dependency access during setup and support owned
resources afterward. Captured contexts cannot create resources after disposal.
Long-lived owned tasks use their plugin's declared dependencies; ordinary callers
continue to hold leases. Lifecycle callbacks cannot recursively await mutations on
the same manager.

## Events

An `EventKey[T]` names a typed payload contract. `ctx.on(key, callback)` registers
a handler owned by the plugin's effect scope. `await ctx.emit(key, payload)` waits
for all handlers and aggregates failures. Each handler invocation leases its
owning plugin, protecting it and its dependencies until the callback finishes.
Inactive handlers do not start new work. Registration ordering is deterministic;
completion ordering is not. Disposal unregisters handlers, including after failed
setup and repeated restarts. This is an in-process event interface.

## Service contracts and scopes

Service keys identify capabilities independently from plugin instance names.
Legacy string references and setup return values remain supported. A plugin can
declare multiple service keys and publish their values through its context.
Named hierarchical scopes resolve local providers before ancestor providers,
allowing workspace-specific implementations and test substitutes in one manager.
Provider changes reconcile affected consumers using the same lease/drain rules.
These scopes provide dependency isolation, not process or security isolation.

## Operations and diagnostics

Snapshots include readiness reasons, configuration revision, service generation,
and lifecycle timing without exposing configuration or service values. A read-only
configuration plan validates proposed settings and lists affected plugins.
Equivalent validated settings are a no-op. Failed replacement setup retains the
attempted configuration; retry is explicit and external writes are not rolled back.
FastAPI exposes diagnostics and opt-in management operations consistently.

## Delivery and acceptance

Each feature is committed with its behavioral tests and public documentation using
Conventional Commits. Release metadata and examples are updated together. Verify
the complete suite, lint and formatting, build both distributions, and run the
wheel's tests on Python 3.11 as well as the development interpreter.

Regression coverage includes transitive and threaded leases, drain timeout,
provider loss/recovery, disabled-versus-failed intent, acquisition/disposal races,
task cancellation, handler teardown and in-flight dispatch, scoped overrides,
definition changes, configuration no-ops, and private HTTP error responses.

Source-code HMR, package installation, distributed coordination, durable loader
configuration, and application-specific catalog adapters remain host concerns.
