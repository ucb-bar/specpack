"""Bundles: one benchmark at one input size, made from a fresh SPEC run directory.

<output>/<kit>/<label>/<size>/<benchmark>/
  manifest.json   provenance, environment, invocations, output checks, file hashes
  run/            the run directory: inputs plus stripped binaries
  debug/          the same binaries unstripped
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess

FORMAT = 1
COMMAND_FILES = {'speccmds.cmd': 'invocations', 'pre-compare.cmd': 'pre_compare', 'compare.cmd': 'compare'}
HARNESS_FILES = {f'{stem}.{ext}' for stem in ('speccmds', 'compare', 'pre-compare', 'inputgen')
                 for ext in ('cmd', 'out', 'err', 'stdout')}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def binary_names(rundir, label):
    """SPEC names the executables it copies into a run directory <exe>_<tune>.<label>."""
    pattern = re.compile(rf'.+_(base|peak)\.{re.escape(label)}')
    return sorted(p.name for p in rundir.iterdir() if p.is_file() and pattern.fullmatch(p.name))


def parse_dry_run(text, rundir):
    """Parse `specinvoke -nn`: NAME=value environment lines, `cd DIR`, then one command per line.

    Returns (environment, commands); each command is {"cwd", "command"} with the run directory
    written as "." and SPEC's ../run_xxx/ program prefix removed.
    """
    environment, commands, cwd = {}, [], None
    rundir = str(rundir)
    for line in text.splitlines():
        if not line.strip() or line.startswith('#') or line.startswith('specinvoke exit:'):
            continue
        if re.match(r'^(export|unset) ', line):
            continue
        if (m := re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)):
            value = shlex.split(m.group(2)) if m.group(2) else ['']
            environment[m.group(1)] = value[0] if len(value) == 1 else m.group(2)
            continue
        if line.startswith('cd '):
            target = shlex.split(line[3:])[0]
            cwd = '.' if target.rstrip('/') == rundir.rstrip('/') else target
            continue
        command = line.replace(f'../{Path(rundir).name}/', './')
        commands.append({'cwd': cwd, 'command': command})
    return environment, commands


def _program(command):
    first = command.split(None, 1)
    return first[0], first[1] if len(first) > 1 else ''


def validation_for(path, benchmark, binaries):
    """The validation recorded for `benchmark`, and whether it was made with these exact binaries."""
    if not path.exists():
        return None
    entry = json.loads(path.read_text())['benchmarks'].get(benchmark)
    if not entry:
        return None
    current = {name: digests['debug_sha256'] for name, digests in binaries.items()}
    return {**entry, 'binaries_match': entry['binaries'] == current}


def export(rundir, dest, settings, kit, benchmark, size, dry_runs, base_environment, provenance, validation_path):
    """Write one bundle from `rundir` into `dest` (replaced if it exists)."""
    work = dest.with_name(dest.name + '.partial')
    if work.exists():
        shutil.rmtree(work)
    (work / 'run').mkdir(parents=True)
    (work / 'debug').mkdir()

    binaries = {}
    for name in binary_names(rundir, kit.label):
        source = rundir / name
        shutil.copy2(source, work / 'debug' / name)
        subprocess.run([settings.gcc_prefix + 'strip', '-o', str(work / 'run' / name), str(source)], check=True)
        binaries[name] = {'sha256': sha256(work / 'run' / name), 'debug_sha256': sha256(source)}

    files = {}
    for path in sorted(rundir.rglob('*')):
        rel = path.relative_to(rundir)
        if path.is_dir() or rel.as_posix() in HARNESS_FILES or rel.as_posix() in binaries:
            continue
        target = work / 'run' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files[rel.as_posix()] = {'sha256': sha256(target), 'bytes': target.stat().st_size}

    sections, environment = {}, None
    for command_file, key in COMMAND_FILES.items():
        if command_file not in dry_runs:
            continue
        env, commands = parse_dry_run(dry_runs[command_file], rundir)
        if command_file == 'speccmds.cmd':
            environment = {k: v for k, v in env.items() if k not in base_environment}
        entries = []
        for index, entry in enumerate(commands):
            program, arguments = _program(entry['command'])
            item = {'index': index, 'cwd': entry['cwd'], 'program': program, 'arguments_shell': arguments}
            if key != 'invocations':
                item['program_is_bundled'] = Path(program).name in binaries
            entries.append(item)
        sections[key] = entries

    manifest = {
        'format': FORMAT,
        'benchmark': benchmark,
        'size': size,
        'kit': {'name': kit.name, 'version': kit.version, 'spec_dir': str(kit.spec_dir),
                'release': (kit.spec_dir / 'version.txt').read_text().strip()},
        'label': kit.label,
        **provenance,
        'source_rundir': str(rundir),
        'environment': environment or {},
        **sections,
        'binaries': binaries,
        'files': files,
        'validation': validation_for(validation_path, benchmark, binaries),
    }
    (work / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if dest.exists():
        shutil.rmtree(dest)
    work.rename(dest)
    return manifest
