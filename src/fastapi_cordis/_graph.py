"""Pure dependency planning: validate definitions before changing live resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._errors import DependencyError
from ._services import ServiceKey

if TYPE_CHECKING:
    from ._runtime import Plugin


@dataclass(frozen=True)
class DependencyGraph:
    providers: dict[str, str]
    dependencies: dict[str, tuple[str, ...]]
    missing: dict[str, tuple[str, ...]]
    order: tuple[str, ...]


def build_graph(plugins: dict[str, Plugin]) -> DependencyGraph:
    providers = {name: name for name in plugins}
    for name, plugin in plugins.items():
        if plugin.provides is not None:
            key = plugin.provides.name
            if key in providers and providers[key] != name:
                raise DependencyError(f"Multiple providers for service {key!r}")
            providers[key] = name
    dependencies: dict[str, tuple[str, ...]] = {}
    missing: dict[str, tuple[str, ...]] = {}
    for name, plugin in plugins.items():
        resolved = []
        absent = []
        for reference in plugin.requires:
            key = reference.name if isinstance(reference, ServiceKey) else reference
            if key not in providers:
                absent.append(key)
                continue
            provider = providers[key]
            if (
                isinstance(reference, ServiceKey)
                and plugins[provider].provides != reference
            ):
                raise DependencyError(f"Incompatible contract for service {key!r}")
            resolved.append(provider)
        dependencies[name] = tuple(dict.fromkeys(resolved))
        missing[name] = tuple(dict.fromkeys(absent))

    ordered = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise DependencyError(f"Dependency cycle at {name!r}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in dependencies[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in plugins:
        visit(name)
    return DependencyGraph(providers, dependencies, missing, tuple(ordered))
