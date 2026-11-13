"""File transport preserves message contents without changing service limits."""
import io
import json
import sys
from unittest.mock import patch

import pytest

import agent_cli

SID = 'a' * 32


def invoke(arguments, capsys):
    sent = []
    def open_request(request, **kwargs):
        sent.append(json.loads(request.data))
        return io.BytesIO(b'{"accepted":true}')
    with patch.object(sys, 'argv', ['agent_cli.py', *arguments, '--url', 'http://127.0.0.1:9999']), patch.object(agent_cli, 'urlopen', open_request):
        agent_cli.main()
    capsys.readouterr()
    return sent


def test_file_message_exactly_preserves_unicode_and_newlines(tmp_path, capsys):
    value = '公开需求与资料\nquote "x"; literal $name\n'
    path = tmp_path/'message.txt'; path.write_bytes(value.encode('utf-8'))
    assert invoke(['message', SID, '--text-file', str(path)], capsys) == [{'text': value}]


@pytest.mark.parametrize('value', ['', ' \n', 'a' * 12001])
def test_invalid_character_bounds_fail_before_request(tmp_path, capsys, value):
    path = tmp_path/'message.txt'; path.write_text(value, encoding='utf-8')
    with pytest.raises(SystemExit):
        invoke(['message', SID, '--text-file', str(path)], capsys)


def test_file_byte_limit_and_encoding_are_bounded(tmp_path, capsys):
    path = tmp_path/'message.txt'
    for content in (b'a' * 65537, b'\xff\xfe'):
        path.write_bytes(content)
        with pytest.raises(SystemExit):
            invoke(['message', SID, '--text-file', str(path)], capsys)


@pytest.mark.parametrize('extra', [['message', SID, 'duplicate'], ['status', SID]])
def test_file_only_applies_to_message_without_positional_text(tmp_path, capsys, extra):
    path = tmp_path/'message.txt'; path.write_text('test', encoding='utf-8')
    with pytest.raises(SystemExit):
        invoke([*extra, '--text-file', str(path)], capsys)


def test_positional_message_remains_compatible(capsys):
    assert invoke(['message', SID, 'normal message'], capsys) == [{'text': 'normal message'}]
