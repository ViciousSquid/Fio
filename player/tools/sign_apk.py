"""Sign a release APK using step-scoped GitHub Actions credentials.

No credentials means an explicitly unsigned artifact. Partial or invalid
credentials fail the job. Passwords never appear in command arguments.
"""
import argparse
import base64
import os
from pathlib import Path
import subprocess
import tempfile


SECRET_NAMES = (
    'FIO_ANDROID_KEYSTORE_BASE64',
    'FIO_ANDROID_KEY_ALIAS',
    'FIO_ANDROID_KEYSTORE_PASSWORD',
    'FIO_ANDROID_KEY_PASSWORD',
)


def sign_apk(unsigned, signed, build_tools, *, environ=None):
    env = dict(os.environ if environ is None else environ)
    configured = [name for name in SECRET_NAMES if env.get(name)]
    if not configured:
        print('No release signing secrets configured; keeping an explicitly unsigned release APK.')
        return 'unsigned'
    missing = [name for name in SECRET_NAMES if not env.get(name)]
    if missing:
        raise ValueError('Incomplete signing configuration; missing secret names: ' + ', '.join(missing))
    try:
        # Accept line wrapping and the trailing newline added by some shells.
        encoded = ''.join(env[SECRET_NAMES[0]].split())
        key_bytes = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValueError('FIO_ANDROID_KEYSTORE_BASE64 is not valid base64') from None
    if not key_bytes:
        raise ValueError('FIO_ANDROID_KEYSTORE_BASE64 decoded to an empty keystore')
    unsigned, signed, build_tools = Path(unsigned), Path(signed), Path(build_tools)
    if unsigned.resolve() == signed.resolve():
        raise ValueError('Signed output must differ from unsigned input')
    # Do not forward the base64 key to child processes. apksigner reads the two
    # password variables through its env: interface.
    env.pop(SECRET_NAMES[0], None)
    with tempfile.TemporaryDirectory(prefix='fio-sign-', dir=env.get('RUNNER_TEMP')) as directory:
        directory = Path(directory)
        keystore = directory / 'release.keystore'
        with keystore.open('xb') as output:
            keystore.chmod(0o600)
            output.write(key_bytes)
        aligned, staged = directory / 'aligned.apk', directory / 'signed.apk'
        subprocess.run([str(build_tools / 'zipalign'), '-P', '16', '-f', '4',
                        str(unsigned), str(aligned)], check=True, env=env)
        subprocess.run([str(build_tools / 'apksigner'), 'sign', '--ks', str(keystore),
                        '--ks-key-alias', env[SECRET_NAMES[1]],
                        '--ks-pass', 'env:FIO_ANDROID_KEYSTORE_PASSWORD',
                        '--key-pass', 'env:FIO_ANDROID_KEY_PASSWORD',
                        '--out', str(staged), str(aligned)], check=True, env=env)
        subprocess.run([str(build_tools / 'apksigner'), 'verify', '--verbose',
                        '--print-certs', str(staged)], check=True, env=env)
        subprocess.run([str(build_tools / 'zipalign'), '-c', '-P', '16', '4',
                        str(staged)], check=True, env=env)
        # Publish only the verified result. Preserve the unsigned input if a
        # password, alias, keystore or verification check fails.
        signed.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copyfile(staged, signed)
        unsigned.unlink()
    return 'signed'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('unsigned')
    parser.add_argument('signed')
    parser.add_argument('--build-tools', default=os.environ.get('FIO_ANDROID_BUILD_TOOLS'))
    args = parser.parse_args(argv)
    try:
        mode = sign_apk(args.unsigned, args.signed, args.build_tools or '')
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Release signing failed: {exc}\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'mode={mode}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
