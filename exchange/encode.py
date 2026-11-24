from base64 import b64encode
from dataclasses import asdict
import json

from device.issue import IssuedPage
from device.relations import RelationRow
from exchange.manifest import Manifest, encode_fixed_table
from tables.catalog import CodeTable


def _encode_json(document) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def encode_relations(relations: tuple[RelationRow, ...]) -> bytes:
    documents = [list(relation.as_tuple()) for relation in relations]
    return _encode_json(documents)


def encode_exchange(
    manifest: Manifest,
    table: CodeTable,
    pages: tuple[IssuedPage, ...],
    relations: tuple[RelationRow, ...],
) -> bytes:
    """Encode the verified manifest, fixed table, original byte payloads and relations."""
    page_documents = [
        {
            'batch_id': page.batch_id,
            'table_id': page.table_id,
            'sequence': page.sequence,
            'page_id': page.page_id,
            'body': b64encode(page.body).decode('ascii'),
        }
        for page in pages
    ]
    return (b'{"version":1,"manifest":' + _encode_json(asdict(manifest))
            + b',"pages":' + _encode_json(page_documents)
            + b',"relations":' + encode_relations(relations)
            + b',"table":' + encode_fixed_table(table) + b'}')
