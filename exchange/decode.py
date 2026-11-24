from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass

from device.issue import IssuedPage
from device.page_body import decode_page_body, load_json_bytes
from device.pages import SourcePage
from device.relations import RelationRow
from exchange.encode import encode_exchange
from exchange.manifest import Manifest, PageEntry, verify_manifest
from tables.catalog import CodeTable


@dataclass(frozen=True, slots=True)
class DecodedExchange:
    manifest: Manifest
    table: CodeTable
    pages: tuple[SourcePage, ...]
    relations: tuple[RelationRow, ...] | None
    content_id: str


def _object(value, fields: set[str], label: str) -> dict:
    if type(value) is not dict or value.keys() != fields:
        raise ValueError(f'无效的{label}字段')
    return value


def _array(value, label: str) -> list:
    if type(value) is not list:
        raise ValueError(f'{label}须为数组')
    return value


def _decode_manifest(value) -> Manifest:
    fields = _object(value, {'batch_id', 'table_id', 'table_digest', 'pages'}, '清册')
    entries = tuple(
        PageEntry(**_object(entry, {'sequence', 'page_id', 'byte_length', 'body_digest'}, '清册页'))
        for entry in _array(fields['pages'], '清册页')
    )
    return Manifest(fields['batch_id'], fields['table_id'], fields['table_digest'], entries)


def _decode_issued_page(value) -> IssuedPage:
    page = _object(value, {'batch_id', 'table_id', 'sequence', 'page_id', 'body'}, '签发页')
    if type(page['body']) is not str:
        raise ValueError('原页正文须为 Base64 文本')
    try:
        body = b64decode(page['body'], validate=True)
    except (Base64Error, ValueError) as error:
        raise ValueError('无效的 Base64 原页正文') from error
    return IssuedPage(page['batch_id'], page['table_id'], page['sequence'], page['page_id'], body)


def _decode_relations(value) -> tuple[RelationRow, ...]:
    relations = []
    for item in _array(value, '跨页关系'):
        if type(item) is not list or len(item) != 4:
            raise ValueError('跨页关系须含两个原行出处')
        before_page, before_line, after_page, after_line = item
        if not isinstance(before_page, str) or not isinstance(after_page, str):
            raise ValueError('关系页号须为文本')
        if type(before_line) is not int or type(after_line) is not int:
            raise ValueError('关系行位须为整数')
        if before_line <= 0 or after_line <= 0:
            raise ValueError('关系行位须从 1 起算')
        relations.append(RelationRow(before_page, before_line, after_page, after_line))
    return tuple(relations)


def decode_exchange(payload: bytes) -> DecodedExchange:
    document = load_json_bytes(payload)
    if type(document) is not dict:
        raise ValueError('无效的交换件字段')
    keys = set(document)
    current = {'version', 'manifest', 'table', 'pages', 'relations'}
    if keys != current and keys != {'version', 'manifest', 'table', 'pages'}:
        raise ValueError('无效的交换件字段')
    if type(document['version']) is not int or document['version'] != 1:
        raise ValueError('仅接纳 V1 整批交换件')
    manifest = _decode_manifest(document['manifest'])
    table_doc = _object(document['table'], {'table_id', 'entries'}, '固定表')
    table = CodeTable(table_doc['table_id'], tuple(_array(table_doc['entries'], '固定表条目')))
    pages = tuple(_decode_issued_page(page) for page in _array(document['pages'], '签发页'))
    verify_manifest(manifest, table, pages)
    parsed = tuple(SourcePage(page.sequence, page.page_id, decode_page_body(page.body)) for page in pages)
    # 缺少 relations 段即未附新交接记录的旧件，核证时按缺证处理，旧账照常。
    has_relations = 'relations' in document
    relations = None if not has_relations else _decode_relations(document['relations'])
    from exchange.integrity import digest_bytes
    if has_relations:
        # 现行件：规范化重编码，刷新外层空白不改变内容身份。
        content_id = digest_bytes(encode_exchange(manifest, table, pages, relations))
    else:
        # 旧件无 relations 段，直接对实际收到字节摘要，使其与现行空关系件
        # （同样的页与表但多一个 relations:[]）具有不同内容身份。
        content_id = digest_bytes(payload)
    return DecodedExchange(manifest, table, parsed, relations, content_id)
