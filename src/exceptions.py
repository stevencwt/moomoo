"""
Custom exceptions for the options trading bot.
All connector errors derive from ConnectorError for easy catching.
"""


class ConnectorError(Exception):
    """Base class for all connector errors."""
    pass


class ConnectionError(ConnectorError):
    """Raised when unable to connect to OpenD or yfinance."""
    pass


class ReconnectError(ConnectorError):
    """Raised when all reconnect attempts are exhausted."""
    pass


class DataError(ConnectorError):
    """Raised when API returns unexpected or missing data."""
    pass


class OrderError(ConnectorError):
    """Raised when order placement or cancellation fails."""
    pass


class ConfigError(Exception):
    """Raised when config is missing required fields or has invalid values."""
    pass
