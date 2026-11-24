from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from certification.apply import apply_certification
from certification.errors import MissingEvidenceError, RelationConflictError
from certification.read import read_certification
from device.batches import open_batch
from device.record import append_row
from device.relations import register_relation
from device.seal import seal_batch
from exchange.export import export_batch
from receiving.accept import receive_batch
from reporting.publish import publish_report
from reporting.read import read_report
from storage import open_device, open_receiving
from tables.register import register_table


def record_and_export(path, batch_id, table_id, segment, rows):
    with closing(open_device(path)) as device:
        register_table(device, table_id, {7: segment})
        open_batch(device, batch_id, table_id)
        for page, code, milliseconds in rows:
            append_row(device, batch_id, page, code, milliseconds)
        seal_batch(device, batch_id)
        return export_batch(device, batch_id)


def report_values(report):
    return {
        'totals': [[segment, ms] for segment, ms in report.totals],
        'contributions': [
            [row.batch_id, row.table_id, row.sequence, row.page_id, row.line_no,
             row.code, row.segment, row.milliseconds]
            for row in report.contributions
        ],
    }


def cert_values(cert):
    return {
        'cert_id': cert.cert_id,
        'batch_id': cert.batch_id,
        'segment': cert.segment,
        'minimum': cert.minimum,
        'min_duration': cert.min_duration,
        'max_duration': cert.max_duration,
        'conclusion': cert.conclusion,
        'relations': [edge.as_tuple() if hasattr(edge, 'as_tuple')
                      else [edge.before_page, edge.before_line,
                            edge.after_page, edge.after_line]
                      for edge in cert.relations],
        'min_order': [[r.page_id, r.line_no, r.segment, r.milliseconds]
                      for r in cert.min_witness.order],
        'max_order': [[r.page_id, r.line_no, r.segment, r.milliseconds]
                      for r in cert.max_witness.order],
    }


def transfer_with_relations(path, batch_id, table_id, mapping, rows, relations):
    with closing(open_device(path)) as device:
        register_table(device, table_id, mapping)
        open_batch(device, batch_id, table_id)
        for page, code, milliseconds in rows:
            append_row(device, batch_id, page, code, milliseconds)
        for before_page, before_line, after_page, after_line in relations:
            register_relation(device, batch_id, before_page, before_line,
                              after_page, after_line)
        seal_batch(device, batch_id)
        return export_batch(device, batch_id)


def certification_demo(root):
    """连续工段核证单：同合计的必达/待查、冲突拒签、旧件缺证、重开复领。"""
    device_path = root / 'cert-device.sqlite'
    receiving_path = root / 'cert-receiving.sqlite'
    mapping = {7: '预热', 2: '停机'}

    # 件甲 C01：预热 10+4=14，停机 0。关系把 P1 末行锁在停机行之前，
    # 于是停机零行只能落在两段预热之外，最长连续在任何顺序下都达 14 -> 必达。
    rows = [('P1', 7, 10), ('P2', 2, 0), ('P1', 7, 4)]
    payload_met = transfer_with_relations(
        device_path, 'C01', 'A', mapping, rows, [('P1', 2, 'P2', 1)])

    # 件乙 C02：预热同样 14，但无跨页关系，停机零行可夹在两段预热之间，
    # 最小连续 10、最大 14，门槛 14 -> 待查。
    rows2 = [('Q1', 7, 10), ('Q2', 2, 0), ('Q1', 7, 4)]
    payload_pending = transfer_with_relations(
        device_path, 'C02', 'B', mapping, rows2, ())

    # 件丙 C03：关系互相矛盾（与页链成环），核证拒签但工段账照常。
    rows3 = [('R1', 7, 5), ('R2', 7, 4), ('R1', 7, 2)]
    payload_conflict = transfer_with_relations(
        device_path, 'C03', 'C', mapping, rows3,
        [('R1', 2, 'R2', 1), ('R2', 1, 'R1', 1)])

    with closing(open_receiving(receiving_path)) as receiver:
        receive_batch(receiver, payload_met)
        receive_batch(receiver, payload_pending)
        receive_batch(receiver, payload_conflict)
        met = apply_certification(receiver, 'C01', '预热', 14)
        pending = apply_certification(receiver, 'C02', '预热', 14)
        report = publish_report(receiver, 'C03')
        try:
            apply_certification(receiver, 'C03', '预热', 1)
            conflict_outcome = 'UNEXPECTED_ACCEPT'
        except RelationConflictError as error:
            conflict_outcome = f'拒签：{error}'
        met_id, pending_id = met.cert_id, pending.cert_id

    # 旧件（无 relations 段）实收后核证缺证，旧账照常。
    legacy_rows = [('P1', 7, 5), ('P2', 7, 4)]
    legacy_payload = record_and_export(device_path, 'G01', 'G', '预热', legacy_rows)
    document = json.loads(legacy_payload)
    del document['relations']
    legacy_payload = json.dumps(document, separators=(',', ':')).encode('utf-8')
    with closing(open_receiving(receiving_path)) as receiver:
        receive_batch(receiver, legacy_payload)
        try:
            apply_certification(receiver, 'G01', '预热', 1)
            legacy_outcome = 'UNEXPECTED_ACCEPT'
        except MissingEvidenceError as error:
            legacy_outcome = f'缺证：{error}'
        legacy_report = publish_report(receiver, 'G01')

    # 重开接班库，按原核证号复领固定件。
    with closing(open_receiving(receiving_path)) as receiver:
        met_again = read_certification(receiver, met_id)
        pending_again = read_certification(receiver, pending_id)

    if met_again != met or pending_again != pending:
        raise RuntimeError('重开复领的核证单与原固定件不符')

    print('POINTWISE_CERT ' + json.dumps({
        'case_id': 'cert-v1',
        'same_total_14': {
            'C01_met': cert_values(met),
            'C02_pending': cert_values(pending),
        },
        'C03_conflict': {
            'report_totals': [list(item) for item in report.totals],
            'certification': conflict_outcome,
        },
        'G01_legacy': {
            'certification': legacy_outcome,
            'report_totals': [list(item) for item in legacy_report.totals],
        },
        'reopen_reissue': {'met_id': met_id, 'pending_id': pending_id},
        'code_paths': [
            'device/relations.py', 'exchange/encode.py', 'exchange/decode.py',
            'receiving/relations.py', 'certification/ordering.py',
            'certification/apply.py', 'certification/snapshots.py',
        ],
        'oracle': '页链加跨页边构成 DAG，枚举其全部线性扩展求最长预热连续的最小/最大值；'
                  '合计同为 14 时，锁序者必达、无关系者待查，成环者拒签，旧件缺证。',
    }, ensure_ascii=False, separators=(',', ':')))


def main():
    old_rows = [('P20', 7, 11), ('P03', 7, 3), ('P20', 7, 0)]
    new_rows = [('P20', 7, 11)]
    expected_old = {
        'totals': [['预热', 14]],
        'contributions': [
            ['A01', 'A', 1, 'P20', 1, 7, '预热', 11],
            ['A01', 'A', 1, 'P20', 2, 7, '预热', 0],
            ['A01', 'A', 2, 'P03', 1, 7, '预热', 3],
        ],
    }
    expected_new = {
        'totals': [['稳态', 11]],
        'contributions': [['A02', 'B', 1, 'P20', 1, 7, '稳态', 11]],
    }
    with TemporaryDirectory() as directory:
        root = Path(directory)
        device_path = root / 'device.sqlite'
        receiving_path = root / 'receiving.sqlite'
        first = record_and_export(device_path, 'A01', 'A', '预热', old_rows)
        with closing(open_receiving(receiving_path)) as receiver:
            first_receipt = receive_batch(receiver, first)
            if receive_batch(receiver, first) != first_receipt:
                raise RuntimeError('整件重投未复领原回执')
            old_report = publish_report(receiver, 'A01')
        second = record_and_export(device_path, 'A02', 'B', '稳态', new_rows)
        with closing(open_receiving(receiving_path)) as receiver:
            receive_batch(receiver, second)
            new_report = publish_report(receiver, 'A02')
        with closing(open_receiving(receiving_path)) as receiver:
            reopened_old = read_report(receiver, old_report.report_id)
            reopened_new = read_report(receiver, new_report.report_id)

        actual_old = report_values(old_report)
        downstream = [report_values(reopened_old), report_values(reopened_new)]
        if actual_old != expected_old or downstream != [expected_old, expected_new]:
            raise RuntimeError('固定工段账与手算结果或原行位置不符')
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'v8',
            'input': {'A01': {'table_id': 'A', 'code_7': '预热', 'rows': old_rows},
                      'A02': {'table_id': 'B', 'code_7': '稳态', 'rows': new_rows}},
            'expected': expected_old,
            'actual': actual_old,
            'downstream_expected': [expected_old, expected_new],
            'downstream_actual': downstream,
            'code_paths': ['device/seal.py', 'exchange/export.py', 'receiving/accept.py',
                           'reporting/publish.py', 'reporting/aggregate.py', 'reporting/read.py'],
            'oracle': '按首见页序手排 P20、P03，预热手加 11+0+3，稳态取次批唯一原行 11，逐行核对固定成果。',
        }, ensure_ascii=False, separators=(',', ':')))

        certification_demo(root)


if __name__ == '__main__':
    main()
