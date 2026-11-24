"""接班侧：随批实收的跨页先后关系交接记录。

关系只在本批内有效；重投或迟到的关系不会混入异批。
旧件没有 relations 段时只登记“无交接记录”标记，核证据此返回缺证。
"""
from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True, slots=True)
class ReceivedRelation:
    before_page: str
    before_line: int
    after_page: str
    after_line: int


def initialize_relation_handovers(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS relation_handovers (
            batch_id TEXT PRIMARY KEY NOT NULL REFERENCES accepted_batches(batch_id),
            has_relations INTEGER NOT NULL CHECK (has_relations IN (0, 1))
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS accepted_relations (
            batch_id TEXT NOT NULL REFERENCES accepted_batches(batch_id),
            position INTEGER NOT NULL CHECK (position > 0),
            before_page TEXT NOT NULL,
            before_line INTEGER NOT NULL CHECK (before_line > 0),
            after_page TEXT NOT NULL,
            after_line INTEGER NOT NULL CHECK (after_line > 0),
            PRIMARY KEY (batch_id, position)
        )
    ''')


def insert_relation_handover(db: sqlite3.Connection, batch_id: str, relations) -> None:
    """relations 为 None 表示旧件、无新交接记录；空元组表示新件且无现场关系。"""
    has_relations = 0 if relations is None else 1
    db.execute('''
        INSERT INTO relation_handovers (batch_id, has_relations) VALUES (?, ?)
    ''', (batch_id, has_relations))
    if relations is None:
        return
    db.executemany('''
        INSERT INTO accepted_relations
            (batch_id, position, before_page, before_line, after_page, after_line)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', ((batch_id, position, relation.before_page, relation.before_line,
           relation.after_page, relation.after_line)
          for position, relation in enumerate(relations, 1)))


def handover_exists(db: sqlite3.Connection, batch_id: str) -> bool:
    row = db.execute(
        'SELECT has_relations FROM relation_handovers WHERE batch_id = ?', (batch_id,)
    ).fetchone()
    return row is not None and row[0] == 1


def load_received_relations(db: sqlite3.Connection, batch_id: str) -> tuple[ReceivedRelation, ...]:
    records = db.execute('''
        SELECT before_page, before_line, after_page, after_line
        FROM accepted_relations WHERE batch_id = ? ORDER BY position
    ''', (batch_id,))
    return tuple(ReceivedRelation(*record) for record in records)
