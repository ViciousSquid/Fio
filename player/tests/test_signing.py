import base64
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from player.tools.sign_apk import SECRET_NAMES, sign_apk


def credentials(tmp_path):
    return dict(zip(SECRET_NAMES, (
        base64.b64encode(b'private-test-key').decode(), 'fio-player',
        'store-password', 'key-password',
    )), RUNNER_TEMP=str(tmp_path))


def test_absent_credentials_preserve_unsigned(tmp_path):
    unsigned = tmp_path / 'unsigned.apk'
    unsigned.write_bytes(b'apk')
    with patch('subprocess.run') as run:
        assert sign_apk(unsigned, tmp_path / 'signed.apk', tmp_path, environ={}) == 'unsigned'
        run.assert_not_called()
    assert unsigned.read_bytes() == b'apk'


def test_partial_configuration_reports_names_without_values(tmp_path):
    with pytest.raises(ValueError) as error:
        sign_apk('unused', 'unused2', tmp_path,
                 environ={SECRET_NAMES[0]: 'do-not-log-this'})
    assert SECRET_NAMES[1] in str(error.value)
    assert 'do-not-log-this' not in str(error.value)


@pytest.mark.parametrize('corrupt', ['not base64!', ''])
def test_invalid_key_fails_before_invoking_tools(tmp_path, corrupt):
    env = credentials(tmp_path)
    env[SECRET_NAMES[0]] = corrupt
    with patch('subprocess.run') as run, pytest.raises(ValueError):
        sign_apk('unsigned', 'signed', tmp_path, environ=env)
    run.assert_not_called()


def test_signs_verifies_and_removes_private_key(tmp_path):
    unsigned, signed = tmp_path / 'unsigned.apk', tmp_path / 'signed.apk'
    unsigned.write_bytes(b'unsigned')
    key_paths, commands = [], []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs['check'] is True
        assert SECRET_NAMES[0] not in kwargs['env']
        assert 'store-password' not in ' '.join(command)
        assert 'key-password' not in ' '.join(command)
        if 'sign' in command:
            key = Path(command[command.index('--ks') + 1])
            assert key.read_bytes() == b'private-test-key'
            assert key.stat().st_mode & 0o777 == 0o600
            key_paths.append(key)
            Path(command[command.index('--out') + 1]).write_bytes(b'signed')

    env = credentials(tmp_path)
    # PowerShell and web-form pastes can include a trailing CRLF.
    env[SECRET_NAMES[0]] += '\r\n'
    with patch('subprocess.run', side_effect=run):
        assert sign_apk(unsigned, signed, tmp_path, environ=env) == 'signed'
    assert signed.read_bytes() == b'signed'
    assert not unsigned.exists()
    assert key_paths and all(not p.exists() for p in key_paths)
    assert any('verify' in c for c in commands)
    assert '-c' in commands[-1]


def test_failed_signing_keeps_unsigned_and_removes_private_key(tmp_path):
    unsigned, signed = tmp_path / 'unsigned.apk', tmp_path / 'signed.apk'
    unsigned.write_bytes(b'unsigned')
    key_paths = []

    def run(command, **kwargs):
        if 'sign' in command:
            key_paths.append(Path(command[command.index('--ks') + 1]))
            raise subprocess.CalledProcessError(1, command)

    with patch('subprocess.run', side_effect=run), pytest.raises(subprocess.CalledProcessError):
        sign_apk(unsigned, signed, tmp_path, environ=credentials(tmp_path))
    assert unsigned.read_bytes() == b'unsigned'
    assert not signed.exists()
    assert key_paths and all(not p.exists() for p in key_paths)
