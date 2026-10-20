import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from specpack import bundle, runcpu, settings
from specpack.settings import ROOT, SettingsError


def _write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.kit = self.tmp / 'kit'
        _write(self.kit / 'shrc', 'UCSUITE=CPU2026\n')
        _write(self.kit / 'version.txt', '1.0.1\n')
        self.prefix = str(self.tmp / 'tc' / 'riscv64-unknown-linux-gnu-')
        _write(Path(self.prefix + 'gcc'), '', 0o755)
        self.qemu = _write(self.tmp / 'qemu', '', 0o755)

    def settings_text(self, version=2026, extra=''):
        return f'''
[kit.cpu2026]
version  = {version}
spec_dir = "{self.kit}"
config   = "configs/cpu2026-rv64gc-gcc13-static.cfg"
label    = "rv64gc-gcc13-static"
{extra}
[toolchain]
prefix = "{self.prefix}"
[qemu]
riscv64 = "{self.qemu}"
[build]
jobs = 4
[output]
dir = "{self.tmp / 'out'}"
'''

    def load(self, text):
        return settings.load(_write(self.tmp / 'specpack.toml', text))


class SettingsTest(Fixture):
    def test_loads_explicit_paths(self):
        s = self.load(self.settings_text())
        kit = s.kit('cpu2026')
        self.assertEqual((kit.version, kit.spec_dir, kit.label), (2026, self.kit, 'rv64gc-gcc13-static'))
        self.assertEqual(kit.config, ROOT / 'configs/cpu2026-rv64gc-gcc13-static.cfg')
        self.assertEqual(s.bundle_dir(kit, 'test'), self.tmp / 'out/cpu2026/rv64gc-gcc13-static/test')

    def test_version_must_match_kit(self):
        with self.assertRaisesRegex(SettingsError, 'declares CPU2026'):
            self.load(self.settings_text(version=2017))

    def test_unknown_key_rejected(self):
        with self.assertRaisesRegex(SettingsError, 'unknown key'):
            self.load(self.settings_text(extra='size = "test"'))

    def test_unknown_kit(self):
        s = self.load(self.settings_text())
        with self.assertRaisesRegex(SettingsError, r'no \[kit.cpu2017\]'):
            s.kit('cpu2017')


class InstallConfigTest(Fixture):
    def setUp(self):
        super().setUp()
        self.s = self.load(self.settings_text())
        self.k = self.s.kit('cpu2026')
        self.dest = self.k.spec_dir / 'config' / self.k.config.name
        self.dest.parent.mkdir()

    def test_keeps_spec_hashes_when_unchanged(self):
        installed = self.k.config.read_text() + '__HASH__\n801.xz_s=base=x:\nexehash=abc\n'
        self.dest.write_text(installed)
        runcpu.install_config(self.k, log=lambda *_: None)
        self.assertEqual(self.dest.read_text(), installed)

    def test_replaces_changed_config_and_keeps_backup(self):
        self.dest.write_text('label = old\n__HASH__\nexehash=abc\n')
        messages = []
        runcpu.install_config(self.k, log=messages.append)
        self.assertEqual(self.dest.read_text(), self.k.config.read_text())
        backups = list(self.dest.parent.glob(self.dest.name + '.specpack-replaced-*'))
        self.assertEqual(len(backups), 1)
        self.assertIn('label = old', backups[0].read_text())
        self.assertTrue(messages)


class RuncpuOutputTest(unittest.TestCase):
    def parse(self, lines):
        result = runcpu.Result([], 0, Path('x'))
        for line in lines:
            runcpu._parse_line(result, line)
        return result

    def test_parses_setup_build_and_summary_lines(self):
        result = self.parse([
            '  Setting up 801.xz_s test base rv64gc-gcc13-static: run_base_test_rv64gc-gcc13-static.0000',
            '  Setting up 782.lbm_r test base mytest (1 copy): run_base_test_mytest.0001',
            'Build successes for intspeed: 801.xz_s(base), 807.ntest_s(base)',
            'Build errors for intspeed: None',
            'Success: 1x801.xz_s 1x807.ntest_s',
            'Error: 1x835.gem5_s',
            'The log for this run is in /k/result/CPU2026.007.log',
        ])
        self.assertEqual(result.setups, {'801.xz_s': 'run_base_test_rv64gc-gcc13-static.0000',
                                         '782.lbm_r': 'run_base_test_mytest.0001'})
        self.assertEqual(result.built, ['801.xz_s', '807.ntest_s'])
        self.assertEqual(result.build_errors, [])
        self.assertEqual((result.succeeded, result.failed), (['801.xz_s', '807.ntest_s'], ['835.gem5_s']))
        self.assertEqual(result.spec_log, Path('/k/result/CPU2026.007.log'))


class DryRunTest(unittest.TestCase):
    def test_parses_environment_and_commands(self):
        rundir = '/k/benchspec/CPU/821.gcc_s/run/run_base_test_lab.0000'
        text = f'''# specinvoke r4356
# Environment in effect for these runs:
OMP_NUM_THREADS=1
OMP_STACKSIZE=120M
SPEC=/k
EMPTY=''
export OMP_NUM_THREADS OMP_STACKSIZE SPEC EMPTY
unset OLDPWD
cd {rundir}
../run_base_test_lab.0000/cc1_s_base.lab t1.c -O3 -o t1.c.0001.s > t1.c.0001.out 2>> t1.c.0001.err
cd {rundir}
../run_base_test_lab.0000/cc1_s_base.lab t1.c -O2 -o t1.c.0002.s > t1.c.0002.out 2>> t1.c.0002.err
specinvoke exit: rc=0
'''
        env, commands = bundle.parse_dry_run(text, rundir)
        self.assertEqual(env, {'OMP_NUM_THREADS': '1', 'OMP_STACKSIZE': '120M', 'SPEC': '/k', 'EMPTY': ''})
        self.assertEqual(commands[1], {'cwd': '.', 'command':
                                       './cc1_s_base.lab t1.c -O2 -o t1.c.0002.s > t1.c.0002.out 2>> t1.c.0002.err'})
        self.assertEqual(len(commands), 2)


class BundleTest(unittest.TestCase):
    def test_binary_names_follow_spec_naming(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('vpr_base.lab', 'vpr_out_compare_base.lab', 'vpr_base.other', 'control', 'in.blif'):
                Path(tmp, name).write_text('')
            self.assertEqual(bundle.binary_names(Path(tmp), 'lab'), ['vpr_base.lab', 'vpr_out_compare_base.lab'])

    def test_validation_records_whether_binaries_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'validation.json')
            path.write_text(json.dumps({'benchmarks': {'821.gcc_s': {'status': 'pass', 'binaries': {'cc1': 'aa'}}}}))
            same = bundle.validation_for(path, '821.gcc_s', {'cc1': {'debug_sha256': 'aa', 'sha256': 'bb'}})
            other = bundle.validation_for(path, '821.gcc_s', {'cc1': {'debug_sha256': 'cc', 'sha256': 'bb'}})
            self.assertTrue(same['binaries_match'])
            self.assertFalse(other['binaries_match'])
            self.assertIsNone(bundle.validation_for(path, '801.xz_s', {}))


class RvExecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.qemu = _write(self.tmp / 'fake-qemu', '#!/bin/sh\necho "qemu $*"\n', 0o755)
        header = bytearray(64)
        header[0:4] = b'\x7fELF'
        header[18:20] = (243).to_bytes(2, 'little')
        self.riscv = self.tmp / 'prog_base.lab'
        self.riscv.write_bytes(bytes(header))

    def run_rv_exec(self, *argv, qemu=True):
        env = {'PATH': '/usr/bin:/bin'}
        if qemu:
            env['SPECPACK_QEMU'] = str(self.qemu)
        return subprocess.run([str(ROOT / 'bin' / 'rv-exec'), *argv], env=env, capture_output=True, text=True)

    def test_riscv_elf_goes_to_qemu(self):
        self.assertEqual(self.run_rv_exec(str(self.riscv), 'a', 'b').stdout, f'qemu {self.riscv} a b\n')

    def test_other_programs_run_directly(self):
        self.assertEqual(self.run_rv_exec('echo', 'host').stdout, 'host\n')

    def test_requires_qemu_setting(self):
        self.assertEqual(self.run_rv_exec('echo', 'x', qemu=False).returncode, 127)


if __name__ == '__main__':
    unittest.main()
