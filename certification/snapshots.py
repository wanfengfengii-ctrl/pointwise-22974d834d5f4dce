"""核证单固定快照的持久化与重开复领。"""
from dataclasses import dataclass
import sqlite3

from certification.ordering import OrderEdge, OrderRow
from storage import integer_text, restore_integer

CONCLUSION_MET = '必达'
CONCLUSION_MISSED = '未达'
CONCLUSION_PENDING = '待查'


@dataclass(frozen=True, slots=True)
class Witness:
    """一份可复算的完整原行顺序。"""
    duration: int
    order: tuple[OrderRow, ...]


@dataclass(frozen=True, slots=True)
class Certification:
    cert_id: str
    batch_id: str
    segment: str
    minimum: int
    min_duration: int
    max_duration: int
    conclusion: str
    relations: tuple[OrderEdge, ...]
    min_witness: Witness
    max_witness: Witness


def initialize_certifications(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS certifications (
            cert_id TEXT PRIMARY KEY NOT NULL,
            batch_id TEXT NOT NULL REFERENCES accepted_batches(batch_id),
            segment TEXT NOT NULL,
            minimum TEXT NOT NULL,
            min_duration TEXT NOT NULL,
            max_duration TEXT NOT NULL,
            conclusion TEXT NOT NULL
                CHECK (conclusion IN ('必达', '未达', '待查')),
            UNIQUE (batch_id, segment, minimum)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS certification_relations (
            cert_id TEXT NOT NULL REFERENCES certifications(cert_id),
            position INTEGER NOT NULL,
            before_page TEXT NOT NULL,
            before_line INTEGER NOT NULL,
            after_page TEXT NOT NULL,
            after_line INTEGER NOT NULL,
            PRIMARY KEY (cert_id, position)
        )
    ''')
    for witness_kind in ('min', 'max'):
        db.execute(f'''
            CREATE TABLE IF NOT EXISTS certification_witness_{witness_kind} (
                cert_id TEXT NOT NULL REFERENCES certifications(cert_id),
                position INTEGER NOT NULL,
                page_id TEXT NOT NULL,
                line_no INTEGER NOT NULL,
                segment TEXT NOT NULL,
                milliseconds TEXT NOT NULL,
                PRIMARY KEY (cert_id, position)
            )
        ''')


def _save_witness(db, cert_id, witness: Witness, kind: str) -> None:
    db.execute(f'''
        INSERT INTO certification_witness_{kind}
            (cert_id, position, page_id, line_no, segment, milliseconds)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (cert_id, 0, '', 0, '', integer_text(witness.duration)))
    db.executemany(f'''
        INSERT INTO certification_witness_{kind}
            (cert_id, position, page_id, line_no, segment, milliseconds)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', ((cert_id, position, row.page_id, row.line_no, row.segment,
           integer_text(row.milliseconds))
          for position, row in enumerate(witness.order, 1)))


def save_certification(db: sqlite3.Connection, certification: Certification) -> None:
    db.execute('''
        INSERT INTO certifications
            (cert_id, batch_id, segment, minimum, min_duration, max_duration, conclusion)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (certification.cert_id, certification.batch_id, certification.segment,
          integer_text(certification.minimum), integer_text(certification.min_duration),
          integer_text(certification.max_duration), certification.conclusion))
    db.executemany('''
        INSERT INTO certification_relations
            (cert_id, position, before_page, before_line, after_page, after_line)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', ((certification.cert_id, position, edge.before_page, edge.before_line,
           edge.after_page, edge.after_line)
          for position, edge in enumerate(certification.relations, 1)))
    _save_witness(db, certification.cert_id, certification.min_witness, 'min')
    _save_witness(db, certification.cert_id, certification.max_witness, 'max')


def _load_witness(db, cert_id, kind: str) -> Witness:
    records = db.execute(f'''
        SELECT position, page_id, line_no, segment, milliseconds
        FROM certification_witness_{kind} WHERE cert_id = ? ORDER BY position
    ''', (cert_id,)).fetchall()
    duration_record = next(record for record in records if record[0] == 0)
    duration = restore_integer(duration_record[4])
    order = tuple(
        OrderRow(page_id, line_no, segment, restore_integer(milliseconds))
        for position, page_id, line_no, segment, milliseconds in records
        if position > 0
    )
    return Witness(duration, order)


def load_certification(db: sqlite3.Connection, cert_id: str) -> Certification | None:
    header = db.execute('''
        SELECT batch_id, segment, minimum, min_duration, max_duration, conclusion
        FROM certifications WHERE cert_id = ?
    ''', (cert_id,)).fetchone()
    if header is None:
        return None
    batch_id, segment, minimum, min_duration, max_duration, conclusion = header
    relations = tuple(
        OrderEdge(before_page, before_line, after_page, after_line)
        for _, before_page, before_line, after_page, after_line in db.execute('''
            SELECT position, before_page, before_line, after_page, after_line
            FROM certification_relations WHERE cert_id = ? ORDER BY position
        ''', (cert_id,))
    )
    return Certification(
        cert_id, batch_id, segment, restore_integer(minimum),
        restore_integer(min_duration), restore_integer(max_duration), conclusion,
        relations, _load_witness(db, cert_id, 'min'), _load_witness(db, cert_id, 'max'),
    )
