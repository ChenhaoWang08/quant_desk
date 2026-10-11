"""这段代码是量化系统 Backend 的 SQLite 数据存储层，负责数据库初始化和数据统计。
1. sqlite3：连接 SQLite 数据库并执行 SQL；Path 管理文件路径；closing 确保数据库连接使用后关闭。
2. SQLiteStore：封装数据库操作，避免其他模块直接管理 SQL 和连接。
3. __init__(db_path)：接收并保存数据库路径，不会立即创建数据库。
4. initialize()：创建数据库目录、连接 SQLite、建立 observations 表，最后 commit() 提交并关闭连接。
5. observations：包含 series_id（指标名称，如 GDP）、date（观测日期）、value（数值）。
6. PRIMARY KEY(series_id, date)：联合主键，保证同一指标、同一日期只能有一条记录，防止重复。
7. count_observations()：执行 SELECT COUNT(*) 统计全部观测记录数；fetchone()[0] 提取整数结果。
整体逻辑： 接收路径 → 创建数据库与表 → 保存表结构 → 按需查询记录总数。
当前局限： 只支持初始化与统计，不支持数据插入、更新及历史数据查询"""

"""Database I/O only. UI modules must never execute SQL."""

import sqlite3
from contextlib import closing
from pathlib import Path


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    series_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (series_id, date)
                )
            """)
            db.commit()

    def count_observations(self, series_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM observations"
        params = ()

        if series_id is not None:
            sql += " WHERE series_id = ?"
            params = (series_id,)

        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql, params).fetchone()[0]