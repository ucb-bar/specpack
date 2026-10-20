"""specpack: build SPEC CPU for RISC-V, validate it on the host through QEMU, export bundles.

  python3 -m specpack [--settings FILE] build    --kit KIT BENCHMARK...
  python3 -m specpack [--settings FILE] validate --kit KIT --size SIZE BENCHMARK...
  python3 -m specpack [--settings FILE] export   --kit KIT --size SIZE BENCHMARK...

BENCHMARK is anything runcpu accepts: benchmark names or suites such as intspeed.
"""
import argparse
import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys

from . import bundle, runcpu, settings as settings_mod
from .settings import ROOT, SettingsError

SIZES = ('test', 'train', 'ref')


def _stamp():
    return datetime.datetime.now().strftime('%Y%m%dT%H%M%S')


def _first_line(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        return None
    return proc.stdout.splitlines()[0] if proc.returncode == 0 and proc.stdout else None


def _specpack_commit():
    head = _first_line(['git', '-C', str(ROOT), 'rev-parse', '--verify', '-q', 'HEAD'])
    dirty = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], capture_output=True, text=True).stdout
    return f'{head}{"-dirty" if dirty.strip() else ""}' if head else None


def _provenance(settings, kit):
    return {
        'specpack': _specpack_commit(),
        'config': {'file': kit.config.name, 'sha256': bundle.sha256(kit.config)},
        'toolchain': {'prefix': settings.gcc_prefix, 'version': _first_line([settings.gcc_prefix + 'gcc', '--version'])},
        'qemu': {'path': str(settings.qemu), 'version': _first_line([str(settings.qemu), '--version'])},
    }


def _logs(settings, kit, action):
    """Where this command keeps runcpu's output and a copy of SPEC's own log."""
    base = settings.output / kit.name / kit.label / 'logs' / f'{_stamp()}-{action}'
    return base.with_suffix('.runcpu.log'), base.with_suffix('.spec.log')


def _keep_spec_log(result, copy):
    if result.spec_log and result.spec_log.exists():
        shutil.copy2(result.spec_log, copy)
        return str(copy)
    return None


def _exe_hashes(kit, benchmark):
    exe = kit.spec_dir / 'benchspec' / 'CPU' / benchmark / 'exe'
    return {p.name: bundle.sha256(p) for p in sorted(exe.glob(f'*.{kit.label}'))} if exe.is_dir() else {}


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')


def cmd_build(settings, kit, args):
    output, spec_copy = _logs(settings, kit, 'build')
    result = runcpu.runcpu(settings, kit, 'build', args.benchmarks, output)
    record = {'time': _stamp(), 'benchmarks': args.benchmarks, **_provenance(settings, kit),
              'built': result.built, 'up_to_date': result.up_to_date, 'errors': result.build_errors,
              'runcpu_output': str(output), 'spec_log': _keep_spec_log(result, spec_copy),
              'binaries': {b: _exe_hashes(kit, b) for b in result.built + result.up_to_date}}
    _write_json(settings.output / kit.name / kit.label / 'build.json', record)
    print(f'specpack: built {len(result.built)}, up to date {len(result.up_to_date)}, errors {len(result.build_errors)}'
          f'{": " + ", ".join(result.build_errors) if result.build_errors else ""}')
    return 0 if (result.built or result.up_to_date) and not result.build_errors else 1


def cmd_validate(settings, kit, args):
    output, spec_copy = _logs(settings, kit, f'validate-{args.size}')
    result = runcpu.runcpu(settings, kit, 'run', args.benchmarks, output, size=args.size, host_setup=True,
                           host_run=True, extra=['--nobuild', '--noreportable', '--iterations', '1'])
    path = settings.bundle_dir(kit, args.size) / 'validation.json'
    previous = json.loads(path.read_text())['benchmarks'] if path.exists() else {}
    spec_log = _keep_spec_log(result, spec_copy)
    for benchmark in result.succeeded + result.failed:
        previous[benchmark] = {'status': 'pass' if benchmark in result.succeeded else 'fail', 'time': _stamp(),
                               'binaries': _exe_hashes(kit, benchmark), 'spec_log': spec_log,
                               'qemu': _first_line([str(settings.qemu), '--version'])}
    _write_json(path, {'size': args.size, 'benchmarks': dict(sorted(previous.items()))})
    print(f'specpack: passed {len(result.succeeded)}, failed {len(result.failed)}'
          f'{": " + ", ".join(result.failed) if result.failed else ""}  ({path})')
    return 0 if result.succeeded and not result.failed else 1


def cmd_export(settings, kit, args):
    output, spec_copy = _logs(settings, kit, f'export-{args.size}')
    result = runcpu.runcpu(settings, kit, 'runsetup', args.benchmarks, output, size=args.size, host_setup=True,
                           extra=['--nobuild', '--noreportable'])
    spec_log = _keep_spec_log(result, spec_copy)
    if not result.setups or result.build_errors:
        print(f'specpack: runsetup set up {len(result.setups)} benchmark(s); build errors: '
              f'{", ".join(result.build_errors) or "none"} (see {output})')
        return 1
    base_environment = runcpu.shrc_environment(settings, kit)
    provenance = {**_provenance(settings, kit), 'spec_log': spec_log}
    validation_path = settings.bundle_dir(kit, args.size) / 'validation.json'
    for benchmark, rundir_name in sorted(result.setups.items()):
        rundir = kit.spec_dir / 'benchspec' / 'CPU' / benchmark / 'run' / rundir_name
        dry_runs = {name: runcpu.specinvoke_dry_run(settings, kit, rundir, name)
                    for name in bundle.COMMAND_FILES if (rundir / name).exists()}
        dest = settings.bundle_dir(kit, args.size) / benchmark
        manifest = bundle.export(rundir, dest, settings, kit, benchmark, args.size, dry_runs, base_environment,
                                 provenance, validation_path)
        state = manifest['validation']
        print(f'specpack: {benchmark}: {len(manifest["invocations"])} invocation(s), {len(manifest["files"])} file(s), '
              f'validation {state["status"] + ("" if state["binaries_match"] else " (other binaries)") if state else "none"}'
              f' -> {dest}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='specpack', description=__doc__.splitlines()[0])
    parser.add_argument('--settings', type=Path, default=settings_mod.DEFAULT,
                        help=f'settings file (default: {settings_mod.DEFAULT})')
    sub = parser.add_subparsers(dest='command', required=True)
    for name, needs_size in (('build', False), ('validate', True), ('export', True)):
        p = sub.add_parser(name)
        p.add_argument('--kit', required=True, help='a [kit.NAME] table in the settings file')
        if needs_size:
            p.add_argument('--size', required=True, choices=SIZES)
        p.add_argument('benchmarks', nargs='+')
    args = parser.parse_args(argv)
    try:
        settings = settings_mod.load(args.settings)
        kit = settings.kit(args.kit)
    except SettingsError as error:
        print(f'specpack: {error}', file=sys.stderr)
        return 2
    print(f'specpack: settings {settings.path}, kit {kit.name} (CPU{kit.version}, {kit.spec_dir}), label {kit.label}')
    return {'build': cmd_build, 'validate': cmd_validate, 'export': cmd_export}[args.command](settings, kit, args)


if __name__ == '__main__':
    sys.exit(main())
