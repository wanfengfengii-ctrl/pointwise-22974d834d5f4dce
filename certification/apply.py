"""接班侧申请并签发连续工段核证单。"""
import hashlib
import json
import sqlite3

from certification.errors import MissingEvidenceError
from certification.ordering import (
    OrderEdge,
    OrderRow,
    evaluate_continuity,
)
from certification.snapshots import (
    CONCLUSION_MET,
    CONCLUSION_MISSED,
    CONCLUSION_PENDING,
    Certification,
    Witness,
    load_certification,
    save_certification,
)
from receiving.receipts import find_receipt
from receiving.relations import handover_exists, load_received_relations
from receiving.rows import load_accepted_rows
from storage import integer_text, write_transaction
from tables.validate import require_text


def _require_minimum(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError('连续时长下限须为非负整数')
    return value


def certification_identifier(batch_id: str, segment: str, minimum: int) -> str:
    """固定实收号、工段、门槛决定固定核证号，重开可按原号领取。"""
    document = json.dumps(
        [batch_id, segment, integer_text(minimum)],
        ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')
    return 'cert-' + hashlib.sha256(document).hexdigest()


def _conclude(min_duration: int, max_duration: int, minimum: int) -> str:
    if min_duration >= minimum:
        return CONCLUSION_MET
    if max_duration < minimum:
        return CONCLUSION_MISSED
    return CONCLUSION_PENDING


def apply_certification(
    db: sqlite3.Connection, batch_id: str, segment: str, minimum: int,
) -> Certification:
    require_text(batch_id, '批号')
    require_text(segment, '工段名')
    _require_minimum(minimum)
    with write_transaction(db):
        receipt = find_receipt(db, batch_id)
        if receipt is None:
            raise ValueError('只可为已实收批申请核证单')
        cert_id = certification_identifier(batch_id, segment, minimum)
        existing = load_certification(db, cert_id)
        if existing is not None:
            return existing
        if not handover_exists(db, batch_id):
            # 旧件没有新交接记录：核证缺证拒签，原工段账仍可照常发布与领取。
            raise MissingEvidenceError('该实收批缺少跨页先后交接记录，不能核证')

        accepted = load_accepted_rows(db, batch_id)
        rows = tuple(
            OrderRow(row.page_id, row.line_no, row.segment, row.milliseconds)
            for row in accepted
        )
        relations = tuple(
            OrderEdge(relation.before_page, relation.before_line,
                      relation.after_page, relation.after_line)
            for relation in load_received_relations(db, batch_id)
        )
        result = evaluate_continuity(rows, relations, segment)
        min_witness = Witness(result.min_duration, result.min_order)
        max_witness = Witness(result.max_duration, result.max_order)
        certification = Certification(
            cert_id, batch_id, segment, minimum,
            result.min_duration, result.max_duration,
            _conclude(result.min_duration, result.max_duration, minimum),
            result.edges, min_witness, max_witness,
        )
        save_certification(db, certification)
    return certification
