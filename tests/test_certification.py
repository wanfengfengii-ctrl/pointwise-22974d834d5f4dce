from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from certification.apply import apply_certification, certification_identifier
from certification.errors import (
    DanglingRelationError,
    MissingEvidenceError,
    RelationConflictError,
)
from certification.read import read_certification
from device.batches import open_batch
from device.record import append_row
from device.relations import register_relation
from device.seal import seal_batch
from exchange.export import export_batch
from receiving.accept import receive_batch
from reporting.publish import publish_report
from storage import open_device, open_receiving
from tables.register import register_table


TABLE = {7: '预热', 9: '稳态', 2: '停机'}


class CertificationFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.device = open_device(self.root / 'device.sqlite')
        self.receiving = open_receiving(self.root / 'receiving.sqlite')
        self.addCleanup(lambda: self.device.close())
        self.addCleanup(lambda: self.receiving.close())

    def reopen_receiving(self):
        self.receiving.close()
        self.receiving = open_receiving(self.root / 'receiving.sqlite')

    def transfer(self, batch, rows, relations=(), table_id='A'):
        register_table(self.device, table_id, TABLE)
        open_batch(self.device, batch, table_id)
        for page, code, milliseconds in rows:
            append_row(self.device, batch, page, code, milliseconds)
        for before_page, before_line, after_page, after_line in relations:
            register_relation(self.device, batch, before_page, before_line,
                              after_page, after_line)
        seal_batch(self.device, batch)
        payload = export_batch(self.device, batch)
        return receive_batch(self.receiving, payload)

    def run_of(self, witness):
        best = current = 0
        for row in witness.order:
            if row.segment == '预热':
                current += row.milliseconds
                best = max(best, current)
            else:
                current = 0
        return best

    def test_relation_validation_on_device(self):
        register_table(self.device, 'A', TABLE)
        open_batch(self.device, 'B01', 'A')
        append_row(self.device, 'B01', 'P1', 7, 5)
        append_row(self.device, 'B01', 'P2', 7, 4)
        # 指向尚不存在的原行
        with self.assertRaises(ValueError):
            register_relation(self.device, 'B01', 'P1', 1, 'P2', 2)
        # 页内关系拒绝
        with self.assertRaises(ValueError):
            register_relation(self.device, 'B01', 'P1', 1, 'P1', 1)
        # 非整数行位
        with self.assertRaises(ValueError):
            register_relation(self.device, 'B01', 'P1', '1', 'P2', 1)
        # 合法跨页关系，重复登记幂等
        register_relation(self.device, 'B01', 'P1', 1, 'P2', 1)
        register_relation(self.device, 'B01', 'P1', 1, 'P2', 1)
        seal_batch(self.device, 'B01')
        # 封批后不能再登记
        with self.assertRaises(ValueError):
            register_relation(self.device, 'B01', 'P2', 1, 'P1', 1)

    def test_met_pending_pair_with_same_totals(self):
        # 两份固定件：预热合计相同（14），证据不同导致结论分别必达、待查。
        # 件甲：跨页关系把两段预热锁成相邻连续 -> 最小也达 14 -> 必达。
        rows = [('P1', 7, 10), ('P2', 2, 0), ('P1', 7, 4)]
        relations = [('P1', 2, 'P2', 1)]  # P1 末行先于唯一停机行
        self.transfer('C01', rows, relations)
        report = publish_report(self.receiving, 'C01')
        self.assertEqual(report.totals, (('预热', 14), ('停机', 0)))
        cert = apply_certification(self.receiving, 'C01', '预热', 14)
        self.assertEqual((cert.min_duration, cert.max_duration), (14, 14))
        self.assertEqual(cert.conclusion, '必达')
        # 见证是完整原行顺序且复算达到 14
        self.assertEqual(len(cert.min_witness.order), 3)
        self.assertEqual(self.run_of(cert.min_witness), 14)

        # 件乙：同样 14 毫秒预热，但无跨页关系，停机零行可夹在中间 -> 待查。
        rows2 = [('Q1', 7, 10), ('Q2', 2, 0), ('Q1', 7, 4)]
        self.transfer('C02', rows2, ())
        report2 = publish_report(self.receiving, 'C02')
        self.assertEqual(report2.totals, (('预热', 14), ('停机', 0)))
        cert2 = apply_certification(self.receiving, 'C02', '预热', 14)
        self.assertEqual(cert2.conclusion, '待查')
        self.assertEqual(cert2.min_duration, 10)
        self.assertEqual(cert2.max_duration, 14)
        self.assertEqual(self.run_of(cert2.min_witness), 10)
        self.assertEqual(self.run_of(cert2.max_witness), 14)
        self.assertEqual(len(cert2.min_witness.order), 3)

    def test_missed_conclusion(self):
        rows = [('P1', 7, 5), ('P2', 2, 0), ('P1', 7, 4)]
        self.transfer('D01', rows, ())
        cert = apply_certification(self.receiving, 'D01', '预热', 20)
        self.assertEqual(cert.conclusion, '未达')
        self.assertTrue(cert.max_duration < 20)

    def test_conflicting_relations_reject_cert_but_report_ok(self):
        rows = [('P1', 7, 5), ('P2', 7, 4), ('P1', 7, 2)]
        # P1:2 -> P2:1 -> P1:1 与页链 P1:1 -> P1:2 成环
        relations = [('P1', 2, 'P2', 1), ('P2', 1, 'P1', 1)]
        self.transfer('E01', rows, relations)
        with self.assertRaises(RelationConflictError):
            apply_certification(self.receiving, 'E01', '预热', 1)
        # 原工段账仍可签
        report = publish_report(self.receiving, 'E01')
        self.assertEqual(report.totals, (('预热', 11),))

    def test_dangling_relation_rejected_at_cert(self):
        # 设备侧已挡悬点；这里直接造一条带悬点关系的实收批需要绕过设备校验，
        # 改由算法层单测覆盖。这里验证设备侧不会放行。
        register_table(self.device, 'A', TABLE)
        open_batch(self.device, 'F01', 'A')
        append_row(self.device, 'F01', 'P1', 7, 5)
        with self.assertRaises(ValueError):
            register_relation(self.device, 'F01', 'P1', 1, 'P9', 1)

    def test_legacy_payload_is_missing_evidence_but_report_works(self):
        # 手工构造不含 relations 段的旧件
        register_table(self.device, 'A', TABLE)
        open_batch(self.device, 'G01', 'A')
        append_row(self.device, 'G01', 'P1', 7, 5)
        append_row(self.device, 'G01', 'P2', 7, 4)
        seal_batch(self.device, 'G01')
        import json
        payload = export_batch(self.device, 'G01')
        document = json.loads(payload)
        del document['relations']
        legacy = json.dumps(document, separators=(',', ':')).encode('utf-8')
        receive_batch(self.receiving, legacy)
        with self.assertRaises(MissingEvidenceError):
            apply_certification(self.receiving, 'G01', '预热', 1)
        # 旧账照常签领
        report = publish_report(self.receiving, 'G01')
        self.assertEqual(report.totals, (('预热', 9),))

    def test_cert_id_fixed_and_reopen_reissue(self):
        rows = [('P1', 7, 5), ('P2', 7, 4)]
        self.transfer('H01', rows, ())
        cert = apply_certification(self.receiving, 'H01', '预热', 8)
        expected_id = certification_identifier('H01', '预热', 8)
        self.assertEqual(cert.cert_id, expected_id)
        # 同申请复领固定件
        again = apply_certification(self.receiving, 'H01', '预热', 8)
        self.assertEqual(again, cert)
        self.reopen_receiving()
        reissued = read_certification(self.receiving, expected_id)
        self.assertEqual(reissued, cert)
        with self.assertRaises(KeyError):
            read_certification(self.receiving, 'cert-unknown')

    def test_unknown_batch_and_bad_minimum(self):
        with self.assertRaises(ValueError):
            apply_certification(self.receiving, 'NOPE', '预热', 1)
        self.transfer('I01', [('P1', 7, 5)], ())
        for bad in (-1, 1.0, '1', True, None):
            with self.assertRaises(ValueError):
                apply_certification(self.receiving, 'I01', '预热', bad)

    def test_absent_target_is_zero_met(self):
        rows = [('P1', 2, 0), ('P2', 2, 3)]
        self.transfer('J01', rows, ())
        cert = apply_certification(self.receiving, 'J01', '预热', 0)
        self.assertEqual((cert.min_duration, cert.max_duration), (0, 0))
        self.assertEqual(cert.conclusion, '必达')

    def test_relations_do_not_cross_batches(self):
        # 第二批的关系只指向本批原行；重投/迟到不混入异批。
        self.transfer('K01', [('P1', 7, 5)], ())
        self.transfer('K02', [('P1', 7, 6), ('P2', 7, 7)],
                      (('P1', 1, 'P2', 1),))
        cert1 = apply_certification(self.receiving, 'K01', '预热', 5)
        cert2 = apply_certification(self.receiving, 'K02', '预热', 13)
        self.assertEqual(cert1.relations, ())
        self.assertEqual(len(cert2.relations), 1)
        self.assertEqual(cert2.min_duration, 13)

    def test_witness_uses_original_sources_and_order_rules(self):
        # 四页交错稀疏：录入顺序、页号、发页序都不能定跨页先后，只有登记关系可以。
        rows = [
            ('P3', 7, 1), ('P1', 7, 2), ('P2', 2, 0),
            ('P3', 9, 3), ('P1', 7, 4), ('P2', 7, 5),
        ]
        relations = [('P1', 1, 'P2', 1)]
        self.transfer('L01', rows, relations)
        cert = apply_certification(self.receiving, 'L01', '预热', 1)
        # 见证必须覆盖全部六个原行出处
        sources = {(row.page_id, row.line_no) for row in cert.max_witness.order}
        self.assertEqual(len(sources), 6)
        # 所据关系固定在核证单上
        self.assertEqual(len(cert.relations), 1)
        edge = cert.relations[0]
        self.assertEqual((edge.before_page, edge.before_line,
                          edge.after_page, edge.after_line), ('P1', 1, 'P2', 1))


class CertificationRollbackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.device = open_device(self.root / 'device.sqlite')
        self.receiving = open_receiving(self.root / 'receiving.sqlite')
        self.addCleanup(lambda: self.device.close())
        self.addCleanup(lambda: self.receiving.close())

    def test_failed_commit_leaves_no_certification(self):
        import sqlite3
        register_table(self.device, 'A', TABLE)
        open_batch(self.device, 'M01', 'A')
        append_row(self.device, 'M01', 'P1', 7, 5)
        append_row(self.device, 'M01', 'P2', 7, 4)
        seal_batch(self.device, 'M01')
        receive_batch(self.receiving, export_batch(self.device, 'M01'))
        self.receiving.execute('''
            CREATE TEMP TRIGGER stop_cert BEFORE INSERT ON certification_witness_min
            BEGIN SELECT RAISE(ABORT, 'cert interrupted'); END
        ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'cert interrupted'):
            apply_certification(self.receiving, 'M01', '预热', 9)
        for table in ('certifications', 'certification_relations',
                      'certification_witness_min', 'certification_witness_max'):
            self.assertEqual(
                self.receiving.execute(f'SELECT count(*) FROM {table}').fetchone(), (0,))
        # 失败后可重试成功
        self.receiving.close()
        self.receiving = open_receiving(self.root / 'receiving.sqlite')
        cert = apply_certification(self.receiving, 'M01', '预热', 9)
        self.assertEqual(cert.conclusion, '必达')


class DanglingAlgorithmTests(unittest.TestCase):
    def test_dangling_detected_in_algorithm(self):
        from certification.ordering import OrderEdge, OrderRow, evaluate_continuity

        rows = (OrderRow('P1', 1, '预热', 5), OrderRow('P2', 1, '预热', 4))
        with self.assertRaises(DanglingRelationError):
            evaluate_continuity(
                rows, (OrderEdge('P1', 1, 'P9', 1),), '预热')


if __name__ == '__main__':
    unittest.main()
