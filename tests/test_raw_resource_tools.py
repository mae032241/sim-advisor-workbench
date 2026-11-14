import hashlib
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest

from advisor.raw_resource_tools import RawResourceTools, TOOL_NAMES, tool_definitions


def mph(path, xml):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('modelinfo.xml', '<modelInfo title="Original fixture"/>')
        archive.writestr('dmodel.xml', xml)
        archive.writestr('solution.bin', b'never read binary solution')
    return path


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / 'originals'
    root.mkdir()
    sub = root / 'nested'
    sub.mkdir()
    visible = root / 'visible.html'
    visible.write_text('<html><body>Alpha original.\nBeta boundary.</body></html>', encoding='utf-8')
    nested = sub / 'native.java'
    nested.write_text('Native ordinary source. Alpha', encoding='utf-8')
    excluded = root / 'heldout.txt'
    excluded.write_text('Alpha private material', encoding='utf-8')
    unknown = root / 'unapproved.txt'
    unknown.write_text('Alpha sibling must stay unavailable', encoding='utf-8')
    model = mph(root / 'original.mph', '<Model><View tag="v1"/><PhysicsList><Physics tag="a" op="NativeType">'
        '<PhysicsFeature tag="f1" op="UnknownNativeFeature"><active>false</active>'
        '<propertyValue name="p:value" value="0.42"/></PhysicsFeature></Physics></PhysicsList></Model>')
    tools = RawResourceTools(files=[visible, nested, excluded, model], roots=[root], excluded=[excluded])
    return SimpleNamespace(root=root, visible=visible, nested=nested, excluded=excluded, unknown=unknown, model=model, tools=tools)


def collect_text(tools, name, arguments):
    pieces, offset = [], 0
    while True:
        page = tools.call(name, dict(arguments, offset=offset, length=3200))
        assert len(json.dumps(page, ensure_ascii=False).encode('utf-8')) <= 7000
        assert page['offset'] == offset and page['returned_length'] == len(page['text'])
        pieces.append(page['text'])
        if page['next_offset'] is None:
            return ''.join(pieces), page
        assert page['next_offset'] == page['end_offset'] > offset
        offset = page['next_offset']


def test_dynamic_contract_and_unknown_arguments_cannot_execute_code(fixture, monkeypatch):
    definitions = fixture.tools.tool_definitions()
    assert {row['name'] for row in definitions} == TOOL_NAMES == {row['name'] for row in tool_definitions()}
    assert all(row['inputSchema']['additionalProperties'] is False for row in definitions)
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('ordinary tools must not launch processes'))
    for name, arguments in [('exec', {'code': 'print(1)'}), ('read_original_file', {'path': str(fixture.visible), 'code': 'print(1)'}),
                            ('list_original_files', {'path': str(fixture.root), 'limit': True})]:
        with pytest.raises(ValueError):
            fixture.tools.call(name, arguments)
    assert fixture.tools.call('read_original_file', {'path': str(fixture.visible)})['text'].startswith('<html>')


def test_exact_files_are_default_and_roots_only_locate_navigation(fixture):
    mounts = fixture.tools.call('list_original_files', {})
    assert mounts['items'] == [{'path': str(fixture.root), 'kind': 'directory', 'size_bytes': None}]
    page = fixture.tools.call('list_original_files', {'path': str(fixture.root)})
    assert {Path(item['path']).name for item in page['items']} == {'nested', 'visible.html', 'original.mph'}
    nested = fixture.tools.call('list_original_files', {'path': str(fixture.nested.parent)})
    assert nested['items'][0]['path'] == str(fixture.nested)
    for path in (fixture.excluded, fixture.unknown):
        with pytest.raises(PermissionError):
            fixture.tools.call('read_original_file', {'path': str(path)})
    roots_only = RawResourceTools(roots=[fixture.root])
    with pytest.raises(PermissionError):
        roots_only.call('read_original_file', {'path': str(fixture.visible)})


def test_only_explicit_non_strict_mode_opens_root_files_and_exclusions_still_win(fixture):
    tools = RawResourceTools(roots=[fixture.root], strict_files=False, excluded=[fixture.excluded])
    assert tools.call('read_original_file', {'path': str(fixture.unknown)})['text'].startswith('Alpha sibling')
    with pytest.raises(PermissionError):
        tools.call('read_original_file', {'path': str(fixture.excluded)})


@pytest.mark.parametrize('suffix', ['.m', '.java', '.txt', '.csv', '.xml', '.html'])
def test_ordinary_text_formats_preserve_original_characters(tmp_path, suffix):
    path = tmp_path / ('source' + suffix)
    text = '原始 αβ\r\nsecond line\n"quoted"'
    path.write_bytes(text.encode('utf-8'))
    result = RawResourceTools(files=[path]).call('read_original_file', {'path': str(path)})
    assert result['text'] == text
    assert result['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result['is_read_evidence'] is False and result['read_scope'] == 'original_text_source'


def test_unicode_byte_budget_and_true_offsets_reconstruct_complete_long_text(tmp_path):
    path = tmp_path / 'long.txt'
    text = ('漢字😀"\\\n' * 1700) + 'complete ending'
    path.write_bytes(text.encode('utf-8'))
    tools = RawResourceTools(files=[path])
    actual, final = collect_text(tools, 'read_original_file', {'path': str(path)})
    assert actual == text and final['end_offset'] == final['total_characters'] == len(text)
    assert max(row['output_bytes'] for row in tools.audit) <= 7000
    assert len(tools.audit) > 3
    with pytest.raises(ValueError, match='beyond'):
        tools.call('read_original_file', {'path': str(path), 'offset': len(text) + 1})


def test_declared_encoding_and_bom_are_decoded_losslessly(tmp_path):
    path = tmp_path / 'utf16.txt'
    path.write_bytes('原文 Ω'.encode('utf-16'))
    assert RawResourceTools(files=[path]).call('read_original_file', {'path': str(path)})['text'] == '原文 Ω'
    html = tmp_path / 'declared.html'
    html.write_bytes('<meta charset="cp1252">caf\xe9'.encode('latin-1'))
    assert RawResourceTools(files=[html]).call('read_original_file', {'path': str(html)})['text'].endswith('café')


def test_traversal_outside_paths_and_alternate_streams_are_rejected(fixture, tmp_path):
    private = tmp_path / 'private.txt'
    private.write_text('never visible', encoding='utf-8')
    for path in (private, str(fixture.root / '..' / 'private.txt'), str(fixture.visible) + ':hidden'):
        with pytest.raises((PermissionError, ValueError)):
            fixture.tools.call('read_original_file', {'path': str(path)})
    for tool in ('list_original_files', 'search_original_files'):
        arguments = {'path': str(tmp_path)}
        if tool == 'search_original_files':
            arguments['query'] = 'never'
        with pytest.raises(PermissionError):
            fixture.tools.call(tool, arguments)


def test_reparse_junction_ancestor_is_never_read_or_listed(fixture, monkeypatch):
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == fixture.nested.parent:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return result
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(PermissionError, match='reparse'):
        fixture.tools.call('read_original_file', {'path': str(fixture.nested)})
    page = fixture.tools.call('list_original_files', {'path': str(fixture.root)})
    assert all(Path(row['path']).name != 'nested' for row in page['items'])


def test_symbolic_file_and_directory_links_fail_closed(tmp_path):
    root, outside = tmp_path / 'mount', tmp_path / 'outside'
    root.mkdir(); outside.mkdir()
    target = outside / 'secret.txt'
    target.write_text('not approved through aliases', encoding='utf-8')
    file_link, directory_link = root / 'file.txt', root / 'linked'
    try:
        file_link.symlink_to(target)
        directory_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Host does not grant symbolic-link creation')
    tools = RawResourceTools(files=[file_link, directory_link / 'secret.txt'], roots=[root])
    for path in (file_link, directory_link / 'secret.txt'):
        with pytest.raises(PermissionError):
            tools.call('read_original_file', {'path': str(path)})
    assert tools.call('list_original_files', {'path': str(root)})['total'] == 0


def test_search_is_allowlisted_scoped_and_returns_exact_first_match_ranges(fixture):
    page = fixture.tools.call('search_original_files', {'query': 'Alpha', 'path': str(fixture.root)})
    assert {row['path'] for row in page['items']} == {str(fixture.visible), str(fixture.nested)}
    assert page['next_offset'] is None
    for item in page['items']:
        text = Path(item['path']).read_text(encoding='utf-8')
        assert text[item['match_start']:item['match_end']] == 'Alpha'
    regex = fixture.tools.call('search_original_files', {'query': '(alpha|boundary)', 'regex': True, 'path': str(fixture.visible)})
    assert regex['returned'] == 1
    named = fixture.tools.call('search_original_files', {'query': 'java$', 'regex': True, 'search_in': 'path'})
    assert named['items'][0]['path'] == str(fixture.nested)
    for query in ('(a+)+$', r'(a)\1', '(?=Alpha)'):
        with pytest.raises(ValueError):
            fixture.tools.call('search_original_files', {'query': query, 'regex': True})


def test_search_empty_page_advances_true_file_cursor_and_never_drops_files(tmp_path):
    paths = []
    for index in range(205):
        path = tmp_path / ('file-%03d.txt' % index)
        path.write_text('last target' if index == 204 else 'ordinary', encoding='utf-8')
        paths.append(path)
    tools = RawResourceTools(files=paths, roots=[tmp_path])
    first = tools.call('search_original_files', {'query': 'target'})
    assert first['returned'] == 0 and first['next_offset'] == 200 and first['scanned_files'] == 200
    second = tools.call('search_original_files', {'query': 'target', 'offset': first['next_offset']})
    assert second['returned'] == 1 and second['items'][0]['path'] == str(paths[-1])
    assert second['next_offset'] is None


def test_path_search_scans_the_whole_allowlist_and_guards_only_actual_hits(tmp_path, monkeypatch):
    paths = []
    for index in range(250):
        path = tmp_path / ('file-%03d.txt' % index)
        path.write_text('ordinary original', encoding='utf-8')
        paths.append(path)
    tools = RawResourceTools(files=paths, roots=[tmp_path])
    guard, calls = tools._guard, []
    def checked(path, **kwargs):
        calls.append(path)
        return guard(path, **kwargs)
    monkeypatch.setattr(tools, '_guard', checked)
    page = tools.call('search_original_files', {'query': 'file-249', 'search_in': 'path'})
    assert page['returned'] == 1 and page['items'][0]['path'] == str(paths[-1])
    assert page['scanned_files'] == 250 and page['next_offset'] is None
    assert calls == [paths[-1]]


def test_mph_uses_unfiltered_original_xml_and_never_binary_solution_or_prebuilt_cache(fixture, monkeypatch):
    from advisor import compact_model, model_reader
    monkeypatch.setattr(compact_model, 'parse_compact_model', lambda *a, **k: pytest.fail('no compact semantic filtering'))
    monkeypatch.setattr(model_reader, 'inspect_model', lambda *a, **k: pytest.fail('no external parser/process'))
    original = zipfile.ZipFile.open
    def opened(archive, member, *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        assert name != 'solution.bin'
        return original(archive, member, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'open', opened)
    first = fixture.tools.call('inspect_original_mph', {'path': str(fixture.model)})
    assert [item['element'] for item in first['items']] == ['View', 'PhysicsList']
    assert first['cache_reused'] is False and first['first_parse_elapsed_ms'] >= 0
    assert first['physical_validation'] == first['effective_activation'] == 'not_evaluated'
    assert all('stage' not in item and 'route' not in item for item in first['items'])
    feature = fixture.tools.call('inspect_original_mph', {'path': str(fixture.model), 'node_path': '/1/0/0'})
    assert [item['element'] for item in feature['items']] == ['active', 'propertyValue']
    assert feature['cache_reused'] is True and feature['first_parse_elapsed_ms'] == first['first_parse_elapsed_ms']
    assert feature['parse_elapsed_ms'] == 0
    value = fixture.tools.call('inspect_original_mph', {'path': str(fixture.model), 'node_path': '/1/0/0/1', 'view': 'xml'})
    assert 'value="0.42"' in value['text'] and value['representation'] == 'serialized_xml_subtree'


def test_original_mph_xml_paging_preserves_full_large_values_and_source_member(tmp_path):
    text = '<Model><propertyValue name="large" value="' + ('漢😀alpha' * 2000) + '"/></Model>'
    path = mph(tmp_path / 'large.mph', text)
    tools = RawResourceTools(files=[path])
    actual, final = collect_text(tools, 'inspect_original_mph', {'path': str(path), 'view': 'xml'})
    assert actual == text and final['representation'] == 'original_decoded_xml_member'
    child, _ = collect_text(tools, 'inspect_original_mph', {'path': str(path), 'view': 'xml', 'node_path': '/0'})
    assert ET.fromstring(child).get('value') == '漢😀alpha' * 2000
    with pytest.raises(ValueError, match='outside'):
        tools.call('inspect_original_mph', {'path': str(path), 'node_path': '/99'})
    with pytest.raises(ValueError):
        tools.call('inspect_original_mph', {'path': str(path), 'node_path': '/../private'})


def test_original_mph_change_and_deletion_invalidate_session_cache(fixture):
    first = fixture.tools.call('inspect_original_mph', {'path': str(fixture.model)})
    mph(fixture.model, '<Model><NativeNew tag="new"/></Model>')
    changed = fixture.tools.call('inspect_original_mph', {'path': str(fixture.model)})
    assert changed['cache_reused'] is False and changed['sha256'] != first['sha256']
    assert changed['items'][0]['element'] == 'NativeNew'
    fixture.model.unlink()
    with pytest.raises(FileNotFoundError):
        fixture.tools.call('inspect_original_mph', {'path': str(fixture.model)})


def test_pdf_ordinary_page_text_paging_and_parse_timing(tmp_path, monkeypatch):
    path = tmp_path / 'original.pdf'
    path.write_bytes(b'%PDF synthetic reader fixture')
    texts = ['PDF ordinary text ' + '漢😀' * 3000, 'Second original page.']
    class Reader:
        is_encrypted = False
        def __init__(self, stream):
            assert stream.read().startswith(b'%PDF')
            self.pages = [SimpleNamespace(extract_text=lambda text=text: text) for text in texts]
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=Reader))
    tools = RawResourceTools(files=[path])
    actual, page = collect_text(tools, 'read_original_file', {'path': str(path), 'page': 1})
    assert actual == texts[0] and page['page'] == 1 and page['page_count'] == 2 and page['next_page'] == 2
    assert page['image_content_not_transcribed'] is True
    assert page['first_parse_elapsed_ms'] >= 0 and page['cache_reused'] is True
    second = tools.call('read_original_file', {'path': str(path), 'page': 2})
    assert second['text'] == texts[1] and second['next_page'] is None
    hit = tools.call('search_original_files', {'query': 'Second original'})
    assert hit['items'][0]['page'] == 2
    with pytest.raises(ValueError, match='beyond'):
        tools.call('read_original_file', {'path': str(path), 'page': 3})


def test_budgeted_directory_and_search_pages_have_true_progress(tmp_path):
    paths = []
    for index in range(30):
        path = tmp_path / ('long-original-' + str(index) + '-' + 'x' * 100 + '.txt')
        path.write_text('target ' + '漢' * 300, encoding='utf-8')
        paths.append(path)
    tools = RawResourceTools(files=paths, roots=[tmp_path], max_bytes=1800)
    for name, arguments in [('list_original_files', {'path': str(tmp_path)}), ('search_original_files', {'query': 'target'})]:
        offset, seen = 0, []
        while True:
            page = tools.call(name, dict(arguments, offset=offset, limit=20))
            assert len(json.dumps(page, ensure_ascii=False).encode('utf-8')) <= 1800
            seen.extend(item['path'] for item in page['items'])
            if page['next_offset'] is None:
                break
            assert page['next_offset'] > offset
            offset = page['next_offset']
        assert len(seen) == len(set(seen)) == len(paths)


def test_denied_hashes_block_renamed_content_at_read_without_inventory_hashing(tmp_path, monkeypatch):
    private = b'blocked original content must never appear'
    copies = [tmp_path / 'renamed.txt', tmp_path / 'another-name.html', tmp_path / 'renamed.pdf']
    for path in copies:
        path.write_bytes(private)
    ordinary = tmp_path / 'ordinary.txt'
    ordinary.write_text('ordinary approved content', encoding='utf-8')
    digest = hashlib.sha256(private).hexdigest()
    tools = RawResourceTools(files=[*copies, ordinary], roots=[tmp_path], denied_hashes=[digest])
    original = tools._read_bytes
    with monkeypatch.context() as ctx:
        ctx.setattr(tools, '_read_bytes', lambda *a, **k: pytest.fail('inventory/path search may not read original bytes'))
        assert tools.call('list_original_files', {'path': str(tmp_path)})['total'] == 4
        assert tools.call('search_original_files', {'query': 'renamed', 'search_in': 'path'})['returned'] == 2
    assert tools._read_bytes == original
    for path in copies:
        with pytest.raises(PermissionError, match='SHA256'):
            tools.call('read_original_file', {'path': str(path)})
    page = tools.call('search_original_files', {'query': 'blocked original'})
    assert page['items'] == [] and page['unavailable_files'] == 3
    assert tools.call('read_original_file', {'path': str(ordinary)})['text'] == 'ordinary approved content'
    assert not tools._cache
    assert all('denied_hashes' not in row['inputSchema']['properties'] for row in tools.tool_definitions())


def test_denied_mph_content_never_reaches_parser_or_session_cache(fixture, monkeypatch):
    from advisor import model_reader
    digest = hashlib.sha256(fixture.model.read_bytes()).hexdigest()
    tools = RawResourceTools(files=[fixture.model], denied_hashes=[digest])
    monkeypatch.setattr(model_reader, '_safe_container', lambda *a, **k: pytest.fail('denied original must not be parsed'))
    with pytest.raises(PermissionError, match='SHA256'):
        tools.call('inspect_original_mph', {'path': str(fixture.model)})
    assert not tools._cache


def test_many_mount_roots_use_ancestor_membership_without_pairwise_path_comparisons(tmp_path, monkeypatch):
    roots = [tmp_path / ('original-root-' + str(index)) for index in range(51)]
    files = [roots[index % len(roots)] / 'nested' / ('file-' + str(index) + '.txt') for index in range(27000)]
    outside = tmp_path / 'separate-exact-files' / 'original.txt'
    def pairwise(*args, **kwargs):
        pytest.fail('mount construction must not compare every file against every root')
    monkeypatch.setattr(type(tmp_path), 'is_relative_to', pairwise)
    tools = RawResourceTools(files=[*files, outside], roots=roots)
    assert tools._mounts == (*roots, outside.parent)
    assert len(tools.files) == 27001 and not tools._cache
