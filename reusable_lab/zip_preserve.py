"""Validate and transplant artifact-authored values without reserializing a sheet.

Only mapped result values and their row heights may change. No other ZIP
entry payload is rewritten. ZIP container compression can
differ; payload bytes and original cell styles/other XML are verified.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
try:
    from .workbook_io import RESULT_FIELDS, normalize_id, validate_columns
except ImportError:  # Script invocation by the existing artifact writer.
    from workbook_io import RESULT_FIELDS, normalize_id, validate_columns

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
LAB = Path(__file__).resolve().parent
ROOT = LAB / 'outputs'
CELL = re.compile(rb'<(?:[A-Za-z_][\w.-]*:)?c\b[^>]*?(?:/>|>.*?</(?:[A-Za-z_][\w.-]*:)?c\s*>)', re.S)
REF = re.compile(rb'\br\s*=\s*["\']([A-Z]+[0-9]+)["\']')
TYPE = re.compile(rb'\s+t\s*=\s*(?:"[^"]*"|\'[^\']*\')')
ROW = re.compile(rb'<(?:[A-Za-z_][\w.-]*:)?row\b[^>]*>')
ROW_HEIGHT = re.compile(rb'\s+(?:ht|customHeight)\s*=\s*(?:"[^"]*"|\'[^\']*\')')
VALUE = re.compile(rb'<(?:[A-Za-z_][\w.-]*:)?(?:f|v|is)\b[^>]*?(?:/>|>.*?</(?:[A-Za-z_][\w.-]*:)?(?:f|v|is)\s*>)', re.S)


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def within(p, parent):
    try:
        p.relative_to(parent)
        return p != parent
    except ValueError:
        return False


def sheet_path(z, sheet_name):
    book = ET.fromstring(z.read('xl/workbook.xml'))
    matches = [s for s in book.findall('s:sheets/s:sheet', NS) if s.get('name') == sheet_name]
    require(len(matches) == 1, 'Expected exactly one mapped worksheet.')
    rid = matches[0].get('{' + NS['r'] + '}id')
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    target = next((r.get('Target') for r in rels if r.get('Id') == rid), None)
    require(target and not target.startswith(('http:', 'https:')), 'Invalid sheet relationship.')
    p = target.lstrip('/') if target.startswith('/') else str(PurePosixPath('xl') / target)
    require('..' not in PurePosixPath(p).parts and p in z.namelist(), 'Invalid worksheet path.')
    return p


def strings(z):
    if 'xl/sharedStrings.xml' not in z.namelist():
        return []
    root = ET.fromstring(z.read('xl/sharedStrings.xml'))
    return [''.join(el.itertext()) for el in root.findall('s:si', NS)]


def values(z, part):
    ss = strings(z)
    result = {}
    for c in ET.fromstring(z.read(part)).findall('.//s:sheetData/s:row/s:c', NS):
        ref, typ = c.get('r'), c.get('t')
        require(c.find('s:f', NS) is None or ref not in result, 'Duplicate cell.')
        if typ == 's':
            v = c.findtext('s:v', namespaces=NS)
            value = ss[int(v)] if v is not None else None
        elif typ == 'inlineStr':
            el = c.find('s:is', NS)
            value = ''.join(el.itertext()) if el is not None else None
        else:
            v = c.findtext('s:v', namespaces=NS)
            if v is None:
                value = None
            elif typ in ('str', 'e'):
                value = v
            elif typ == 'b':
                value = v == '1'
            else:
                value = float(v)
        require(ref not in result, 'Duplicate cell reference.')
        result[ref] = value
    return result


def load_request(filename):
    req = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
    source = Path(req['source']).resolve(strict=True)
    destination = Path(req['destination']).resolve()
    preview = Path(req.get('preview_dir') or destination.with_suffix('')).resolve()
    root = ROOT.resolve()
    require(source.suffix.lower() == '.xlsx' and destination.suffix.lower() == '.xlsx', 'XLSX only.')
    require(source != destination, 'Cannot overwrite source.')
    require(within(destination, root) and within(preview, root), 'Output outside reusable_lab/outputs.')
    require(not destination.exists(), 'Destination already exists.')
    require(not preview.is_file(), 'Preview path is a file.')
    if req.get('source_hash') is not None:
        require(digest(source.read_bytes()) == req['source_hash'], 'Source differs from discovery hash.')
    require(isinstance(req.get('sheet_name'), str) and bool(req['sheet_name']), 'Mapped sheet name required.')
    columns = validate_columns(req.get('columns'))
    rows = req.get('results')
    require(isinstance(rows, list) and bool(rows), 'Expected at least one result.')
    expected, seen, seen_ids = {}, set(), set()
    for item in rows:
        sid, row, data = item.get('sample_id'), item.get('row'), item.get('values')
        sid = normalize_id(sid)
        require(sid not in seen_ids, 'Duplicate sample ID.')
        seen_ids.add(sid)
        require(type(row) is int and 1 <= row <= 1048576 and row not in seen, 'Invalid or duplicate sample row.')
        seen.add(row)
        require(isinstance(data, dict) and set(data) == set(RESULT_FIELDS), 'All seven result fields required.')
        for field in RESULT_FIELDS:
            value = data[field]
            if value == '':
                value = None
            if field == 'fee':
                require(value is None or (type(value) in (int, float) and math.isfinite(value)), 'Fee must be numeric or blank.')
            else:
                require(value is None or isinstance(value, str), 'Status/comment values must be text or blank.')
            if isinstance(value, str):
                require(len(value) <= 32767 and not value.startswith('='), 'Invalid text value.')
                require(not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value), 'Invalid XML character.')
            expected[f'{columns[field]}{row}'] = value
    with zipfile.ZipFile(source) as z:
        require(len(z.namelist()) == len(set(z.namelist())), 'Duplicate archive entries.')
        part = sheet_path(z, req['sheet_name'])
        source_values = values(z, part)
        tree = ET.fromstring(z.read(part))
        existing_rows = {int(row.get('r')) for row in tree.findall('s:sheetData/s:row', NS)}
        for item in rows:
            require(item['row'] in existing_rows, 'Target row not present.')
            ref = f"{columns['sample_id']}{item['row']}"
            require(normalize_id(source_values.get(ref)) == normalize_id(item['sample_id']), 'Source sample ID does not match target row.')
        for merge in tree.findall('s:mergeCells/s:mergeCell', NS):
            bounds = merge.get('ref', '').split(':')
            if len(bounds) == 2:
                for ref in expected:
                    require(not in_range(ref, *bounds), 'Target overlaps a merged cell.')
    return req, source, destination, preview, part, expected


def coords(ref):
    m = re.fullmatch(r'([A-Z]+)([0-9]+)', ref)
    require(m is not None, 'Invalid cell coordinate.')
    col = 0
    for ch in m[1]:
        col = 26 * col + ord(ch) - 64
    return col, int(m[2])


def in_range(ref, first, last):
    c, r = coords(ref)
    a, b = coords(first)
    x, y = coords(last)
    return a <= c <= x and b <= r <= y


def spans(xml):
    result = {}
    for match in CELL.finditer(xml):
        ref = REF.search(match.group())
        if ref:
            ref = ref[1].decode('ascii')
            require(ref not in result, 'Duplicate XML cell.')
            result[ref] = match
    return result


def replace_value(old, ref, value, prefix=b''):
    if old is None:
        start, body = b'<' + prefix + b'c r="' + ref.encode() + b'">', b''
    else:
        end = old.index(b'>')
        start = old[:end + 1]
        prefix = re.match(rb'<([\w.-]+:)?c\b', start)[1] or b''
        body = b'' if start.endswith(b'/>') else old[end + 1:old.rfind(b'</')]
        start = TYPE.sub(b'', start)
        start = re.sub(rb'/?>$', b'>', start)
        body = VALUE.sub(b'', body)
    if value is None:
        content = b''
    elif isinstance(value, str):
        start = start[:-1] + b' t="inlineStr">'
        text = escape(value).replace('\r', '&#13;').encode('utf-8')
        content = b'<' + prefix + b'is><' + prefix + b't xml:space="preserve">' + text + b'</' + prefix + b't></' + prefix + b'is>'
    else:
        content = b'<' + prefix + b'v>' + repr(value).encode('ascii') + b'</' + prefix + b'v>'
    # v/is precede extLst; keep any non-value child XML intact.
    return start + content + body + b'</' + prefix + b'c>'


def transplant(original, typed):
    xml = original
    for ref, value in typed.items():
        cells = spans(xml)
        match = cells.get(ref)
        if match:
            replacement = replace_value(match.group(), ref, value)
            xml = xml[:match.start()] + replacement + xml[match.end():]
            continue
        row = coords(ref)[1]
        row_rx = re.compile(rb'<(?P<prefix>[\w.-]+:)?row\b(?P<attrs>[^>]*\br=["\']' + str(row).encode() + rb'["\'][^>]*)>(?P<body>.*?)</(?:[\w.-]+:)?row\s*>', re.S)
        rm = row_rx.search(xml)
        require(rm is not None, 'Cannot insert target cell into row.')
        prefix = rm['prefix'] or b''
        position = rm.end('body')
        for cm in CELL.finditer(rm['body']):
            cell_ref = REF.search(cm.group())
            if cell_ref and coords(cell_ref[1].decode())[0] > coords(ref)[0]:
                position = rm.start('body') + cm.start()
                break
        xml = xml[:position] + replace_value(None, ref, value, prefix) + xml[position:]
    return xml


def without_targets(xml, refs):
    xml = CELL.sub(lambda m: b'' if REF.search(m.group()) and REF.search(m.group())[1].decode() in refs else m.group(), xml)
    rows = {coords(ref)[1] for ref in refs}
    def strip_height(match):
        row = re.search(rb'\br=["\']([0-9]+)["\']', match.group())
        return ROW_HEIGHT.sub(b'', match.group()) if row and int(row[1]) in rows else match.group()
    return ROW.sub(strip_height, xml)


def apply_heights(xml, authored_root, refs):
    rows = {coords(ref)[1] for ref in refs}
    original_root = ET.fromstring(xml)
    default = original_root.find('s:sheetFormatPr', NS)
    default_height = float(default.get('defaultRowHeight', '15')) if default is not None else 15.0
    authored = {int(row.get('r')): row for row in authored_root.findall('s:sheetData/s:row', NS)}
    changes = []
    def replace(match):
        old = match.group()
        rm = re.search(rb'\br=["\']([0-9]+)["\']', old)
        if not rm or int(rm[1]) not in rows:
            return old
        number = int(rm[1])
        before = re.search(rb'\bht=["\']([0-9.]+)["\']', old)
        before = float(before[1]) if before else default_height
        after = round(float(authored[number].get('ht', str(before))), 2)
        require(math.isfinite(after) and 0 < after <= 409.5, 'Invalid authored row height.')
        if after <= before:
            return old
        changes.append({'row': number, 'before': before, 'after': after})
        return ROW_HEIGHT.sub(b'', old)[:-1] + f' ht="{after:g}" customHeight="1">'.encode()
    return ROW.sub(replace, xml), changes


def preflight(filename):
    req, source, destination, preview, part, expected = load_request(filename)
    return {'source': str(source), 'destination': str(destination), 'preview_dir': str(preview),
            'worksheet': req['sheet_name'], 'worksheet_part': part, 'rows': len(req['results']), 'cells': len(expected),
            'target_cells': list(expected), 'source_hash': digest(source.read_bytes())}


def splice(filename, authored):
    req, source, destination, preview, part, expected = load_request(filename)
    source_bytes = source.read_bytes()
    original_hash = digest(source_bytes)
    with zipfile.ZipFile(authored) as az:
        authored_values = values(az, sheet_path(az, req['sheet_name']))
        authored_xml = ET.fromstring(az.read(sheet_path(az, req['sheet_name'])))
        acells = {c.get('r'): c for c in authored_xml.findall('.//s:sheetData/s:row/s:c', NS)}
        for ref, value in expected.items():
            require(authored_values.get(ref) == value, 'Authored value differs from request.')
            require(ref not in acells or acells[ref].find('s:f', NS) is None, 'Authored target contains a formula.')
        typed = {ref: authored_values.get(ref) for ref in expected}
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix='.wire_draft_', suffix='.xlsx', dir=destination.parent)
    os.close(fd)
    temp = Path(tmpname)
    try:
        with zipfile.ZipFile(source) as original:
            original_xml = original.read(part)
            new_xml, height_changes = apply_heights(transplant(original_xml, typed), authored_xml, expected)
            require(without_targets(original_xml, expected) == without_targets(new_xml, expected), 'Unrequested worksheet XML changed.')
            # Parse whole documents so namespace bindings on the worksheet root
            # remain available; standalone cell fragments may use x: prefixes.
            before_cells = {c.get('r'): c for c in ET.fromstring(original_xml).findall('.//s:sheetData/s:row/s:c', NS)}
            after_cells = {c.get('r'): c for c in ET.fromstring(new_xml).findall('.//s:sheetData/s:row/s:c', NS)}
            for ref in expected:
                if ref in before_cells:
                    before_attrs = dict(before_cells[ref].attrib)
                    after_attrs = dict(after_cells[ref].attrib)
                    before_attrs.pop('t', None)
                    after_attrs.pop('t', None)
                    require(before_attrs == after_attrs, 'Target cell style or attributes changed.')
            with zipfile.ZipFile(temp, 'w') as target:
                target.comment = original.comment
                for info in original.infolist():
                    target.writestr(copy.copy(info), new_xml if info.filename == part else original.read(info.filename))
            with zipfile.ZipFile(temp) as final:
                require(original.namelist() == final.namelist(), 'Archive part inventory changed.')
                for name in original.namelist():
                    if name != part:
                        require(original.read(name) == final.read(name), 'Unrelated archive part changed.')
                actual = values(final, part)
                require(all(actual.get(ref) == value for ref, value in expected.items()), 'Final typed values mismatch.')
                require(final.testzip() is None, 'Archive CRC check failed.')
                parts_preserved = len(final.namelist()) - 1
        require(digest(source.read_bytes()) == original_hash, 'Original source changed.')
        require(not destination.exists(), 'Destination created concurrently.')
        os.replace(temp, destination)
        return {'source_sha256': original_hash, 'output_sha256': digest(destination.read_bytes()),
                'source_unchanged': True, 'rows_written': len(req['results']), 'cells_written': len(expected),
                'other_zip_payloads_unchanged': parts_preserved, 'outside_target_cells_unchanged': True,
                'target_styles_unchanged': True, 'worksheet_part': part,
                'formatting_exceptions': {'row_height_increases': height_changes,
                                          'column_widths_changed': False, 'cell_styles_changed': False}}
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        mode, request = sys.argv[1:3]
        result = preflight(request) if mode == 'preflight' else splice(request, sys.argv[3]) if mode == 'splice' else None
        require(result is not None, 'Unknown operation.')
        print(json.dumps(result))
    except Exception as exc:
        # All own validation messages are content-free; hide library exceptions.
        print(json.dumps({'status': 'ERROR', 'error': str(exc) if isinstance(exc, ValueError) else 'XLSX preservation failed.',
                          'error_type': type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
