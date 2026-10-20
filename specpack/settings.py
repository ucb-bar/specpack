"""Settings file: every path specpack uses, stated explicitly (nothing is searched for)."""
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / 'specpack.toml'
VERSIONS = (2017, 2026)


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Kit:
    name: str
    version: int
    spec_dir: Path
    config: Path
    label: str


@dataclass(frozen=True)
class Settings:
    path: Path
    kits: dict
    gcc_prefix: str
    qemu: Path
    jobs: int
    output: Path

    def kit(self, name):
        if name not in self.kits:
            raise SettingsError(f'{self.path}: no [kit.{name}] (kits: {", ".join(sorted(self.kits)) or "none"})')
        return self.kits[name]

    def bundle_dir(self, kit, size):
        return self.output / kit.name / kit.label / size


def _get(table, key, kind, where):
    if key not in table:
        raise SettingsError(f'{where}: missing "{key}"')
    value = table[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise SettingsError(f'{where}: "{key}" must be {kind.__name__}')
    return value


def _existing(path, where, key):
    if not path.exists():
        raise SettingsError(f'{where}: {key} does not exist: {path}')
    return path


def kit_suite_version(spec_dir):
    """The suite year a kit's own shrc declares (UCSUITE=CPU2026), or None."""
    shrc = spec_dir / 'shrc'
    match = re.search(r'^UCSUITE=CPU(\d{4})$', shrc.read_text(errors='replace'), re.M) if shrc.is_file() else None
    return int(match.group(1)) if match else None


def load(path=DEFAULT):
    path = Path(path).resolve()
    if not path.is_file():
        raise SettingsError(f'no settings file {path} (copy specpack.example.toml there and edit it)')
    with path.open('rb') as stream:
        try:
            data = tomllib.load(stream)
        except tomllib.TOMLDecodeError as error:
            raise SettingsError(f'{path}: {error}') from None
    unknown = set(data) - {'kit', 'toolchain', 'qemu', 'build', 'output'}
    if unknown:
        raise SettingsError(f'{path}: unknown table(s): {", ".join(sorted(unknown))}')

    kits = {}
    for name, table in data.get('kit', {}).items():
        where = f'{path} [kit.{name}]'
        if set(table) - {'version', 'spec_dir', 'config', 'label'}:
            raise SettingsError(f'{where}: unknown key(s): {", ".join(sorted(set(table) - {"version", "spec_dir", "config", "label"}))}')
        version = _get(table, 'version', int, where)
        if version not in VERSIONS:
            raise SettingsError(f'{where}: version must be one of {VERSIONS}')
        spec_dir = _existing(Path(_get(table, 'spec_dir', str, where)), where, 'spec_dir')
        declared = kit_suite_version(spec_dir)
        if declared != version:
            raise SettingsError(f'{where}: version {version}, but {spec_dir}/shrc declares '
                                f'{f"CPU{declared}" if declared else "no suite"}')
        config = _existing(ROOT / _get(table, 'config', str, where), where, 'config')
        label = _get(table, 'label', str, where)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', label):
            raise SettingsError(f'{where}: label may use letters, digits, ".", "_" and "-" only')
        kits[name] = Kit(name, version, spec_dir, config, label)

    toolchain = data.get('toolchain', {})
    gcc_prefix = _get(toolchain, 'prefix', str, f'{path} [toolchain]')
    _existing(Path(gcc_prefix + 'gcc'), f'{path} [toolchain]', 'prefix + "gcc"')
    qemu = _existing(Path(_get(data.get('qemu', {}), 'riscv64', str, f'{path} [qemu]')), f'{path} [qemu]', 'riscv64')
    jobs = _get(data.get('build', {}), 'jobs', int, f'{path} [build]')
    output = Path(_get(data.get('output', {}), 'dir', str, f'{path} [output]'))
    return Settings(path, kits, gcc_prefix, qemu, jobs, output)
