"""
Broker Factory
==============
Builds data and execution connectors from config.

Config layout (config.yaml):

    broker:
      data:      "moomoo"   # "moomoo" | "ibkr"
      execution: "moomoo"   # "moomoo" | "ibkr"

Supported modes
───────────────
1. Full MooMoo (paper trading / Singapore accounts):
       data: moomoo   execution: moomoo

2. Hybrid — MooMoo data, IBKR execution (default while account is being funded):
       data: moomoo   execution: ibkr

3. Full IBKR (after IBKR market-data subscription is active):
       data: ibkr     execution: ibkr

When data == execution the same connector instance is shared so only one
connection is made and one set of rate limits consumed.

Backward compatibility
──────────────────────
The old single-string format is still accepted:

    broker: "moomoo"   # treated as data=moomoo, execution=moomoo
    broker: "ibkr"     # treated as data=ibkr,   execution=ibkr
"""

from __future__ import annotations
from typing import Tuple, Any

from src.logger import get_logger

logger = get_logger("connectors.broker_factory")


def build_connectors(config: dict) -> Tuple[Any, Any]:
    """
    Build (data_connector, execution_connector) from config.

    Returns:
        Tuple of (data_connector, execution_connector).
        If both are the same broker type, the same instance is returned for
        both positions — only one connection will be opened.

    Raises:
        ValueError: If an unknown broker name is specified.
    """
    broker_cfg = config.get("broker", "moomoo")

    # Support old single-string format: broker: "moomoo"
    if isinstance(broker_cfg, str):
        data_broker = broker_cfg.lower()
        exec_broker = broker_cfg.lower()
    else:
        data_broker = str(broker_cfg.get("data", "moomoo")).lower()
        exec_broker = str(broker_cfg.get("execution", "moomoo")).lower()

    _validate_broker_name(data_broker, "broker.data")
    _validate_broker_name(exec_broker, "broker.execution")

    logger.info(
        f"Building connectors | data={data_broker} | execution={exec_broker}"
    )

    data_connector = _make_connector(data_broker, config)

    if exec_broker == data_broker:
        # Share the same instance — single connection, single rate-limit bucket
        exec_connector = data_connector
        logger.info(
            f"data == execution ({data_broker}) — sharing single connector instance"
        )
    else:
        exec_connector = _make_connector(exec_broker, config)

    return data_connector, exec_connector


# ── Private helpers ───────────────────────────────────────────────

def _make_connector(broker_name: str, config: dict) -> Any:
    """Instantiate the named connector."""
    if broker_name == "moomoo":
        from src.connectors.moomoo_connector import MooMooConnector
        logger.debug("Instantiating MooMooConnector")
        return MooMooConnector(config)

    if broker_name == "ibkr":
        from src.connectors.ibkr_connector import IBKRConnector
        logger.debug("Instantiating IBKRConnector")
        return IBKRConnector(config)

    # Should never reach here due to _validate_broker_name, but be explicit
    raise ValueError(f"Unknown broker: {broker_name!r}")


def _validate_broker_name(name: str, field: str) -> None:
    """Raise ValueError if broker name is not recognised."""
    valid = {"moomoo", "ibkr"}
    if name not in valid:
        raise ValueError(
            f"config.{field} is {name!r} — must be one of {sorted(valid)}. "
            f"Check config/config.yaml."
        )