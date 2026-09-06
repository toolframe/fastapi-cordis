"""Behavioral tests for typed service references."""

from dataclasses import FrozenInstanceError
from typing import Any, Protocol, runtime_checkable

import pytest

from fastapi_cordis._services import ServiceKey


class Logger:
    """Concrete service contract used by the tests."""

    def write(self, message: str) -> None:
        del message


@runtime_checkable
class SupportsClose(Protocol):
    """Structural service contract used by the tests."""

    def close(self) -> None: ...


class FileLike:
    """Object satisfying the structural contract."""

    def close(self) -> None:
        pass


class PrivateService:
    """Object whose representation must not appear in validation errors."""

    def __repr__(self) -> str:
        return "PRIVATE_SERVICE_VALUE"


class NotRuntimeCheckable(Protocol):
    """Protocol that cannot be used with isinstance."""

    def close(self) -> None: ...


def test_normal_class_contract_returns_the_same_service() -> None:
    key = ServiceKey[Logger]("logger", Logger)
    service = Logger()

    assert key.validate(service) is service


def test_runtime_checkable_protocol_contract_accepts_structural_service() -> None:
    key = ServiceKey[SupportsClose]("closer", SupportsClose)
    service = FileLike()

    assert key.validate(service) is service


@pytest.mark.parametrize(
    "name", ["", "1logger", "logger/name", "logger name", "élogger", None, 7]
)
def test_invalid_service_names_fail_at_construction(name: object) -> None:
    with pytest.raises(ValueError, match=r"service name must match"):
        ServiceKey(name, Logger)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "contract", [None, Any, list[int], NotRuntimeCheckable, (Logger,)]
)
def test_unsupported_contracts_fail_at_construction(contract: object) -> None:
    with pytest.raises(
        TypeError, match=r"service contract must be a type supporting isinstance checks"
    ):
        ServiceKey("logger", contract)  # type: ignore[arg-type]


def test_contract_mismatch_identifies_name_without_exposing_service_value() -> None:
    key = ServiceKey("private", Logger)

    with pytest.raises(TypeError) as error:
        key.validate(PrivateService())

    message = str(error.value)
    assert "private" in message
    assert "does not implement its declared contract" in message
    assert "PRIVATE_SERVICE_VALUE" not in message
    assert "Logger" not in message


def test_service_keys_are_frozen_and_hash_by_name_and_contract() -> None:
    first = ServiceKey("logger", Logger)
    second = ServiceKey("logger", Logger)
    different_contract = ServiceKey("logger", SupportsClose)

    assert first == second
    assert hash(first) == hash(second)
    assert first != different_contract
    assert {first: "registered"}[second] == "registered"

    with pytest.raises(FrozenInstanceError):
        first.name = "other"  # type: ignore[misc]
