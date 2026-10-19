"""MongoDB access (motor). One client per process, created lazily inside the running event loop."""
from motor.motor_asyncio import AsyncIOMotorClient

import config

_client = None


def get_db():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(config.MONGO_URL)
    return _client[config.DB_NAME]


def close_db():
    """Drop the cached client (used by tests, which create a new event loop per test)."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def live_name(machine):
    return "[" + machine + "]-live"


def stat_name(machine):
    return "[" + machine + "]-stat"
