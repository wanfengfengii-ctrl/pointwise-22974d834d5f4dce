"""按固定核证号领取核证单；重开接班库后仍可复领。"""
import sqlite3

from certification.snapshots import Certification, load_certification


def read_certification(db: sqlite3.Connection, cert_id: str) -> Certification:
    if not isinstance(cert_id, str):
        raise KeyError(cert_id)
    certification = load_certification(db, cert_id)
    if certification is None:
        raise KeyError(cert_id)
    return certification
