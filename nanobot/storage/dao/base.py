"""Base Data Access Object."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.storage.sqlite_store import SQLiteStore


class BaseDAO:
    """
    用处: 所有独立业务数据层 (DAO) 的基类。

    功能:
        - 挂载全局 SQLiteStore 实例的连接与事务。
    """

    def __init__(self, store: "SQLiteStore"):
        self.store = store
