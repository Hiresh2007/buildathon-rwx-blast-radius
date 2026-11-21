"""Collect original bytecode reached during a complete, independently started pytest run."""
import argparse
import atexit
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time


def source_manifest(root):
    library = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (root / 'sqlglot').rglob('*.py')}
    tree = hashlib.sha256(json.dumps(library, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return library, tree


def instrument():
    root = os.environ['COLD_ROOT'] + os.sep
    log = Path(os.environ['COLD_LOGS']) / str(os.getpid())
    log.with_suffix('.started').touch(exist_ok=False)
    scopes = {}
    reads = set()
    unknown = set()
    children = 0
    mon = sys.monitoring
    tool = mon.COVERAGE_ID

    def enter(code, offset):
        if code.co_filename.startswith(root + 'sqlglot' + os.sep):
            if code not in scopes:
                scopes[code] = bytearray((len(code.co_code) + 15) // 16)
            mon.set_local_events(tool, code, mon.events.INSTRUCTION)
        return mon.DISABLE

    def instruction(code, offset):
        scopes[code][offset >> 4] |= 1 << ((offset >> 1) & 7)
        return mon.DISABLE

    def thrown(code, offset, exc):
        enter(code, offset)

    def audit(event, args):
        nonlocal children
        if event == 'subprocess.Popen':
            children += 1
            executable, argv, _, env = args
            if (not isinstance(executable, str) or 'python' not in os.path.basename(executable)
                    or not isinstance(argv, (list, tuple)) or any(v in argv for v in ('-I', '-E', '-S'))
                    or env is not None and any(env.get(k) != os.environ[k] for k in ('COLD_ROOT', 'COLD_LOGS', 'PYTHONPATH'))):
                unknown.add('untracked subprocess')
        elif event in ('os.fork', 'os.forkpty', 'os.exec', 'os.posix_spawn'):
            unknown.add(event)
        elif event == 'open' and isinstance(args[0], (bytes, str)):
            name = os.path.abspath(os.fsdecode(args[0]))
            if name.startswith(root) and args[2] & os.O_ACCMODE != os.O_WRONLY:
                caller = sys._getframe(1).f_code
                if not (caller.co_name == 'get_data' and 'importlib' in caller.co_filename):
                    reads.add(name[len(root):])

    def save():
        mon.set_events(tool, 0)
        mon.free_tool_id(tool)
        items = []
        for code, bits in scopes.items():
            key = hashlib.sha256(code.co_code + code.co_exceptiontable).hexdigest()[:20]
            scope = f'{code.co_firstlineno}:{code.co_qualname}'
            items.append([code.co_filename[len(root):], scope, key, bits.hex()])
        pending = log.with_suffix('.pending')
        pending.write_text(json.dumps([items, sorted(reads), sorted(unknown), children], separators=(',', ':')))
        pending.replace(log.with_suffix('.ready'))

    atexit.register(save)
    sys.addaudithook(audit)
    mon.use_tool_id(tool, 'cold pytest coverage')
    mon.register_callback(tool, mon.events.PY_START, enter)
    mon.register_callback(tool, mon.events.PY_RESUME, enter)
    mon.register_callback(tool, mon.events.PY_THROW, thrown)
    mon.register_callback(tool, mon.events.INSTRUCTION, instruction)
    mon.set_events(tool, mon.events.PY_START | mon.events.PY_RESUME | mon.events.PY_THROW)


seen = set()
failed = set()


def pytest_runtest_logreport(report):
    if report.when == 'call':
        seen.add(report.nodeid)
    if report.failed or report.skipped:
        failed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ['COLD_REPORT']).write_text(json.dumps([sorted(seen), sorted(failed)]))


def collect(root, target, timeout):
    with tempfile.TemporaryDirectory() as working:
        temp = Path(working)
        logs = temp / 'logs'
        logs.mkdir()
        shutil.copyfile(__file__, temp / 'cold_profile_build.py')
        (temp / 'sitecustomize.py').write_text('from cold_profile_build import instrument\ninstrument()\n')
        report = temp / 'results.json'
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(temp), str(root))),
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1', SKIP_INTEGRATION='0',
                   PYTHON_CPU_COUNT='2', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
                   COLD_ROOT=str(root), COLD_LOGS=str(logs), COLD_REPORT=str(report))
        subprocess.run([sys.executable, '-m', 'pytest', '-q', '--assert=plain', '-p', 'no:cacheprovider',
                        '-p', 'cold_profile_build', target], cwd=root, env=env, check=True, timeout=timeout)
        ids, failed_ids = json.loads(report.read_bytes())
        if failed_ids or not ids:
            raise ValueError('baseline included failed/skipped tests or collected nothing')
        for _ in range(20):
            started = {f.stem for f in logs.glob('*.started')}
            ready = {f.stem for f in logs.glob('*.ready')}
            if started and started == ready:
                break
            time.sleep(0.05)
        else:
            raise ValueError('missing Python process coverage')
        files = {}
        reads = set()
        unknown = set()
        children = 0
        for pid in ready:
            scopes, observed_reads, untraced, spawned = json.loads((logs / f'{pid}.ready').read_bytes())
            reads.update(observed_reads)
            unknown.update(untraced)
            children += spawned
            for filename, scope, key, encoded in scopes:
                record = files.setdefault(filename, {}).setdefault(scope, [key, 0])
                record[1] |= int.from_bytes(bytes.fromhex(encoded), 'little')
                if record[0] != key:
                    record[0] = None
        if len(ready) != children + 1 or unknown:
            raise ValueError(f'incomplete subprocess coverage: {children} spawns, {len(ready)} receipts, {unknown}')
        for scopes in files.values():
            for scope, (key, mask) in scopes.items():
                scopes[scope] = [key, [i * 2 for i in range(mask.bit_length()) if mask & (1 << i)]]
        _, tree = source_manifest(root)
        test_hash = hashlib.sha256((root / target.split('::')[0]).read_bytes()).hexdigest()
        return dict(target=target, ids=ids, files=files, reads=sorted(reads), unknown=[], tree=tree, test_hash=test_hash,
                    python=sys.implementation.cache_tag, version=list(sys.version_info[:3]),
                    platform=sys.platform, machine=platform.machine())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('target')
    parser.add_argument('output', type=Path)
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    result = collect(args.root.resolve(), args.target, args.timeout)
    args.output.write_bytes(gzip.compress(json.dumps(result, separators=(',', ':')).encode(), mtime=0))
