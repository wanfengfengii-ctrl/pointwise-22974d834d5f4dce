"""设备侧：封批前登记带现场出处的跨页先后关系。

记录器分写多页，页内原序可靠；跨页先后只有现场记下的部分关系。
关系在活动批期间登记，指向实际原行，只允许跨页；随批封定并进入交换件。
录入顺序、页号和旧账页序（发页序）均不作为跨页先后依据。
"""
from dataclasses import dataclass
import sqlite3

from device.batches import load_batch
from storage import write_transaction
from tables.validate import require_text


@dataclass(frozen=True, slots=True)
class RelationRow:
    """一条跨页先后关系：before 页 before_line 行先于 after 页 after_line 行。"""
    before_page: str
    before_line: int
    after_page: str
    after_line: int

    def as_tuple(self) -> tuple[str, int, str, int]:
        return (self.before_page, self.before_line, self.after_page, self.after_line)


def initialize_relations(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS batch_relations (
            batch_id TEXT NOT NULL,
            before_page TEXT NOT NULL,
            before_line INTEGER NOT NULL CHECK (before_line > 0),
            after_page TEXT NOT NULL,
            after_line INTEGER NOT NULL CHECK (after_line > 0),
            PRIMARY KEY (batch_id, before_page, before_line, after_page, after_line),
            FOREIGN KEY (batch_id, before_page)
                REFERENCES source_pages(batch_id, page_id),
            FOREIGN KEY (batch_id, after_page)
                REFERENCES source_pages(batch_id, page_id)
        )
    ''')


def _require_line(value, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f'{label}须为整数行位')
    if value <= 0:
        raise ValueError(f'{label}须为从 1 起算的行位')
    return value


def register_relation(
    db: sqlite3.Connection,
    batch_id: str,
    before_page: str,
    before_line: int,
    after_page: str,
    after_line: int,
) -> None:
    """登记一条跨页先后关系。关系两端必须指向活动批中已写入的实际原行。"""
    require_text(batch_id, '批号')
    require_text(before_page, '前序页号')
    require_text(after_page, '后序页号')
    _require_line(before_line, '前序行位')
    _require_line(after_line, '后序行位')
    if before_page == after_page:
        raise ValueError('页内先后已由原序确定，只登记跨页关系')
    with write_transaction(db):
        batch = load_batch(db, batch_id)
        if batch is None or batch.directory is not None:
            raise ValueError('只可向活动批登记跨页关系')
        for page_id, line_no in ((before_page, before_line), (after_page, after_line)):
            existing = db.execute('''
                SELECT 1 FROM source_rows
                WHERE batch_id = ? AND page_id = ? AND line_no = ?
            ''', (batch_id, page_id, line_no)).fetchone()
            if existing is None:
                raise ValueError('关系须指向实际原行')
        db.execute('''
            INSERT OR IGNORE INTO batch_relations
                (batch_id, before_page, before_line, after_page, after_line)
            VALUES (?, ?, ?, ?, ?)
        ''', (batch_id, before_page, before_line, after_page, after_line))


def load_relations(db: sqlite3.Connection, batch_id: str) -> tuple[RelationRow, ...]:
    records = db.execute('''
        SELECT before_page, before_line, after_page, after_line
        FROM batch_relations
        WHERE batch_id = ?
        ORDER BY before_page, before_line, after_page, after_line
    ''', (batch_id,))
    return tuple(RelationRow(*record) for record in records)
