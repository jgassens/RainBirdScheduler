"""Generic JSON (de)serialization for the scheduler's dataclasses.

The persistent stores hold plain JSON. This module converts the frozen and
mutable dataclasses in :mod:`models` to and from JSON-safe structures driven
entirely by their type hints, so models never hand-maintain mappers.

Supported field types: ``str``, ``int``, ``float``, ``bool``, ``Decimal``,
``datetime``, ``date``, ``time``, ``Enum`` subclasses, nested dataclasses,
``list``/``tuple``/``set``/``frozenset``/``dict`` of the above, optionals and
``X | None`` unions.
"""

from __future__ import annotations

import dataclasses
import types
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from functools import cache
from typing import (
    Any,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

_NONE_TYPE = type(None)


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def dump(value: Any) -> Any:
    """Convert a value into a JSON-safe structure."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: dump(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {_dump_key(key): dump(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        try:
            ordered = sorted(value)
        except TypeError:
            ordered = list(value)
        return [dump(item) for item in ordered]
    if isinstance(value, list | tuple):
        return [dump(item) for item in value]
    raise TypeError(f"Cannot serialize value of type {type(value).__name__}")


def _dump_key(key: Any) -> str:
    if isinstance(key, Enum):
        return str(key.value)
    return str(key)


@overload
def load[T](target: type[T], data: Any) -> T: ...


@overload
def load(target: Any, data: Any) -> Any: ...


def load(target: Any, data: Any) -> Any:
    """Convert a JSON-safe structure back into ``target``."""
    if target is Any:
        return data

    origin = get_origin(target)

    if origin in (Union, types.UnionType):
        args = get_args(target)
        if data is None:
            if _NONE_TYPE in args:
                return None
            raise ValueError(f"None is not valid for {target}")
        for arg in args:
            if arg is not _NONE_TYPE:
                return load(arg, data)
        return None

    if data is None:
        raise ValueError(
            f"None is not valid for non-optional "
            f"{getattr(target, '__name__', target)!r}"
        )

    if origin in (list, set, frozenset):
        (item_type,) = get_args(target) or (Any,)
        items = [load(item_type, item) for item in data]
        return origin(items)

    if origin is tuple:
        args = get_args(target)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(load(args[0], item) for item in data)
        return tuple(load(arg, item) for arg, item in zip(args, data, strict=True))

    if origin is dict:
        key_type, value_type = get_args(target) or (str, Any)
        return {
            _load_key(key_type, key): load(value_type, value)
            for key, value in data.items()
        }

    if isinstance(target, type):
        if dataclasses.is_dataclass(target):
            hints = _hints(target)
            kwargs: dict[str, Any] = {}
            for field in dataclasses.fields(target):
                if field.name in data:
                    try:
                        kwargs[field.name] = load(
                            hints[field.name], data[field.name]
                        )
                    except ValueError as err:
                        raise ValueError(
                            f"{target.__name__}.{field.name}: {err}"
                        ) from err
                elif (
                    field.default is dataclasses.MISSING
                    and field.default_factory is dataclasses.MISSING
                ):
                    raise ValueError(
                        f"Missing required field {field.name!r} for "
                        f"{target.__name__}"
                    )
            return target(**kwargs)
        if issubclass(target, Enum):
            return target(data)
        if target is datetime:
            return datetime.fromisoformat(data)
        if target is date:
            return date.fromisoformat(data)
        if target is time:
            return time.fromisoformat(data)
        if target is Decimal:
            return Decimal(str(data))
        if target is bool:
            return bool(data)
        if target is int:
            return int(data)
        if target is float:
            return float(data)
        if target is str:
            return str(data)

    raise TypeError(f"Cannot deserialize into {target!r}")


def _load_key(key_type: Any, key: str) -> Any:
    if key_type is int:
        return int(key)
    if isinstance(key_type, type) and issubclass(key_type, Enum):
        return key_type(key)
    return key
