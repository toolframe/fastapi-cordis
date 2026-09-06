"""Replace a provider without changing the consumer or its service reference."""

import asyncio
from typing import Protocol, runtime_checkable

from fastapi_cordis import Plugin, PluginContext, PluginManager, ServiceKey


@runtime_checkable
class Formatter(Protocol):
    def format(self, text: str) -> str: ...


FORMATTER = ServiceKey("formatter", Formatter)


class Uppercase:
    def format(self, text: str) -> str:
        return text.upper()


class Lowercase:
    def format(self, text: str) -> str:
        return text.lower()


def consumer(ctx: PluginContext):
    formatter = ctx.require(FORMATTER)
    return lambda text: {"formatted": formatter.format(text)}


async def main():
    for implementation in (Uppercase, Lowercase):
        manager = PluginManager(
            [
                Plugin("reporter", consumer, requires=(FORMATTER,)),
                Plugin(
                    implementation.__name__,
                    lambda _, implementation=implementation: implementation(),
                    provides=FORMATTER,
                ),
            ]
        )
        async with manager.lifespan():
            with manager.lease("reporter") as report:
                print(report("Hello"))


if __name__ == "__main__":
    asyncio.run(main())
