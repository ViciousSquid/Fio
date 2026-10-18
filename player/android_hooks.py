"""p4a hook: apply Android adapters only inside its private application copy."""
import importlib.util
from pathlib import Path


def before_apk_build(toolchain):
    arguments = toolchain.args.unknown_args
    private = None
    for index, argument in enumerate(arguments):
        if argument.startswith('--private='):
            private = argument.split('=', 1)[1]
        elif argument == '--private':
            private = arguments[index + 1]
    if not private:
        raise RuntimeError('python-for-android did not supply a private staging directory')
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location('fio_android_stage', root / 'player/tools/stage_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.prepare_runtime(private, root)
