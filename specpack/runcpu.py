"""Drive a SPEC kit: install specpack's config, run runcpu and specinvoke in a minimal environment.

SPEC copies the calling environment into every command file it writes (speccmds.cmd,
compare.cmd) and into its logs, so specpack never runs SPEC tools from the user's shell:
they start from a fixed environment containing only what is listed in _environment().
"""
from dataclasses import dataclass, field
import datetime
import os
from pathlib import Path
import re
import subprocess
import sys

from .settings import ROOT

RV_EXEC = ROOT / 'bin' / 'rv-exec'
HASH_MARKER = re.compile(r'^__HASH__$', re.M)

_LOG = re.compile(r'The log for this run is in (\S+)')
_SETUP = re.compile(r'^\s*Setting up (\S+) (\S+) (\S+) (\S+)(?: \([^)]*\))?: (\S+)\s*$')
_BUILD = re.compile(r'^Build (successes|errors) for \S+: (.*)$')
_SUMMARY = re.compile(r'^(Success|Error): (.*)$')
_UP_TO_DATE = re.compile(r'^\s*Up to date (\S+) (?:base|peak) \S+\s*$')   # SPEC's log only
_PROGRESS = re.compile(r'^\s*(Building|NOT Building|Setting up|Running) |^(Build successes|Build errors|Success|Error):'
                       r'|^ERROR|^WARNING|The log for this run')


@dataclass
class Result:
    command: list
    returncode: int
    output: Path
    spec_log: Path = None
    setups: dict = field(default_factory=dict)        # benchmark -> run directory name
    built: list = field(default_factory=list)
    build_errors: list = field(default_factory=list)
    up_to_date: list = field(default_factory=list)
    succeeded: list = field(default_factory=list)
    failed: list = field(default_factory=list)


def _environment(settings):
    return {
        'HOME': os.environ.get('HOME', '/'),
        'USER': os.environ.get('USER', ''),
        'LOGNAME': os.environ.get('LOGNAME', os.environ.get('USER', '')),
        'PATH': '/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'SPECPACK_QEMU': str(settings.qemu),
    }


def _spec_shell(settings, kit, program, args, cwd=None):
    """argv running `program args` inside kit's shrc environment, stack unlimited."""
    script = 'cd "$1" && . ./shrc > /dev/null && ulimit -s unlimited && cd "$2" && shift 2 && exec "$@"'
    return ['/bin/bash', '--noprofile', '--norc', '-c', script, 'specpack',
            str(kit.spec_dir), str(cwd or kit.spec_dir), program, *args]


def config_name(kit):
    return kit.config.name


def install_config(kit, log=print):
    """Copy specpack's config into the kit, keeping SPEC's build hashes if the content is unchanged.

    runcpu appends a __HASH__ section to the config after each build; that section is SPEC's
    record that the binaries match the config. Everything above it must equal specpack's file.
    """
    source = kit.config.read_text()
    dest = kit.spec_dir / 'config' / config_name(kit)
    if dest.exists():
        installed = dest.read_text()
        match = HASH_MARKER.search(installed)
        body = installed[:match.start()] if match else installed
        if body.rstrip('\n') == source.rstrip('\n'):
            return dest
        stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
        backup = dest.with_name(f'{dest.name}.specpack-replaced-{stamp}')
        dest.rename(backup)
        log(f'specpack: {dest.name} in the kit differs from {kit.config}; previous copy kept as {backup.name}, '
            'SPEC will rebuild')
    dest.write_text(source)
    return dest


def _defines(settings, kit, host_setup=False, host_run=False):
    defines = {'label': kit.label, 'gcc_prefix': settings.gcc_prefix, 'build_ncpus': settings.jobs}
    if host_setup:
        defines['host_setup'] = None
    if host_run:
        defines['host_run'] = None
    if host_setup or host_run:
        defines['rv_exec'] = RV_EXEC
    args = []
    for name, value in defines.items():
        args += ['--define', name if value is None else f'{name}={value}']
    return args


def runcpu(settings, kit, action, benchmarks, output, size=None, host_setup=False, host_run=False,
           extra=(), echo=sys.stdout):
    """Run runcpu once; its full output goes to `output`, progress lines to `echo`."""
    install_config(kit)
    args = ['--config', config_name(kit), '--action', action, *_defines(settings, kit, host_setup, host_run)]
    if size:
        args += ['--size', size]
    args += [*extra, *benchmarks]
    command = _spec_shell(settings, kit, 'runcpu', args)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = Result(command, None, output)
    with output.open('w') as log, subprocess.Popen(command, env=_environment(settings), stdout=subprocess.PIPE,
                                                   stderr=subprocess.STDOUT, text=True, errors='replace') as proc:
        for line in proc.stdout:
            log.write(line)
            if echo and _PROGRESS.search(line):
                echo.write(line)
                echo.flush()
            _parse_line(result, line.rstrip('\n'))
    result.returncode = proc.returncode
    if result.spec_log and result.spec_log.exists():
        with result.spec_log.open(errors='replace') as log:
            result.up_to_date = [m.group(1) for line in log if (m := _UP_TO_DATE.match(line))]
    return result


def _benchmarks(text):
    """'801.xz_s(base), 807.ntest_s(base)' or '1x801.xz_s 1x807.ntest_s' -> benchmark names."""
    if text.strip() == 'None':
        return []
    return re.findall(r'(?:\d+x)?(\d{3}\.[A-Za-z0-9_]+)', text)


def _parse_line(result, line):
    if (m := _LOG.search(line)):
        result.spec_log = Path(m.group(1))
    elif (m := _SETUP.match(line)):
        result.setups[m.group(1)] = m.group(5)
    elif (m := _BUILD.match(line)):
        (result.built if m.group(1) == 'successes' else result.build_errors).extend(_benchmarks(m.group(2)))
    elif (m := _SUMMARY.match(line)):
        (result.succeeded if m.group(1) == 'Success' else result.failed).extend(_benchmarks(m.group(2)))


def shrc_environment(settings, kit):
    """The environment SPEC tools see before runcpu adds anything: specpack's plus shrc's."""
    command = _spec_shell(settings, kit, 'env', ['-0'])
    proc = subprocess.run(command, env=_environment(settings), capture_output=True, check=True)
    return dict(item.split('=', 1) for item in proc.stdout.decode(errors='replace').split('\0') if '=' in item)


def specinvoke_dry_run(settings, kit, rundir, command_file):
    """SPEC's own reading of a command file: `specinvoke -nn` lines (cd, env, commands)."""
    command = _spec_shell(settings, kit, 'specinvoke', ['-nn', command_file], cwd=rundir)
    proc = subprocess.run(command, env=_environment(settings), capture_output=True, text=True, errors='replace')
    if proc.returncode != 0:
        raise RuntimeError(f'specinvoke -nn {command_file} in {rundir} failed: {proc.stderr.strip()}')
    return proc.stdout
