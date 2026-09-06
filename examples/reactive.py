"""Run: uv run python examples/reactive.py (no external services required)."""

import asyncio
from io import StringIO

from pydantic import BaseModel

from fastapi_cordis import EventKey, Plugin, PluginContext, PluginManager, ServiceKey

PREFIX = ServiceKey("prefix", str)
MESSAGES = ServiceKey("messages", StringIO)
GREET = EventKey("greet", str)


class Settings(BaseModel):
    prefix: str = "Hello"


def memory(ctx: PluginContext):
    messages = ctx.enter_context(StringIO())
    ctx.provide(PREFIX, ctx.config.prefix)
    ctx.provide(MESSAGES, messages)


def greeting(ctx: PluginContext):
    prefix = ctx.require(PREFIX)
    messages = ctx.require(MESSAGES)
    ctx.on(GREET, lambda name: messages.write(f"{prefix}, {name}\n"))
    return messages


def provider(*, scope: str = "", prefix: str = "Hello") -> Plugin:
    return Plugin(
        "memory",
        memory,
        provides=(PREFIX, MESSAGES),
        scope=scope,
        config_model=Settings,
        config={"prefix": prefix},
    )


async def say(manager: PluginManager, name: str) -> None:
    await manager.emit(GREET, name)
    with manager.lease("work.a/greeting") as messages:
        print(messages.getvalue().splitlines()[-1])


async def main() -> None:
    manager = PluginManager(
        [Plugin("greeting", greeting, requires=(PREFIX, MESSAGES), scope="work.a")]
    )
    async with manager.lifespan():
        print("Before provider:", manager.snapshot()[0]["state"])
        await manager.register(provider())
        await say(manager, "Ada")

        plan = manager.plan_configure("memory", {"prefix": "Welcome"})
        print("Configuration affects:", plan["affected"])
        await manager.configure("memory", {"prefix": "Welcome"})
        await say(manager, "Grace")

        # A local provider takes precedence; removal restores the ancestor.
        await manager.register(provider(scope="work.a", prefix="Local"))
        await say(manager, "Lin")
        await manager.disable("work.a/memory")
        waiting = next(p for p in manager.snapshot() if p["name"] == "work.a/greeting")
        print("Provider disabled:", waiting["state"], "enabled:", waiting["enabled"])
        await manager.enable("work.a/memory")
        await say(manager, "Kai")
        await manager.unregister("work.a/memory")
        await say(manager, "Mina")


if __name__ == "__main__":
    asyncio.run(main())
