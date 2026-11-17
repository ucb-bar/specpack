import json
import os
import shutil
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


class FireMarshalTest(Fixture):
    def setUp(self):
        super().setUp()
        from specpack.adapters import firemarshal
        self.fm = firemarshal
        self.s = self.load(self.settings_text())
        self.k = self.s.kit('cpu2026')
        self.bundle = self.tmp / 'bundle' / '821.gcc_s'
        _write(self.bundle / 'run' / 'cc1_s_base.lab', 'bin')
        _write(self.bundle / 'run' / 't1.c', 'int main;')
        _write(self.bundle / 'run' / 'text-sha256', '#!perl')
        self.manifest = {
            'benchmark': '821.gcc_s', 'size': 'test', 'kit': {'name': 'cpu2026'}, 'label': 'rv64gc-gcc13-static',
            'specpack': 'abc', 'validation': None, 'environment': {'OMP_NUM_THREADS': '1', 'OMP_STACKSIZE': '120M'},
            'invocations': [{'index': 0, 'cwd': '.', 'program': './cc1_s_base.lab',
                             'arguments_shell': 't1.c -O3 -o t1.c.0001.s > t1.out 2>> t1.err'}],
            'pre_compare': [{'index': 0, 'cwd': '.', 'program': '/k/bin/specperl', 'program_is_bundled': False,
                             'arguments_shell': 'text-sha256 t1.c.0001.s > t1.c.0001.s.sha256'}],
            'compare': [{'index': 0, 'cwd': '.', 'program': './check_base.lab', 'program_is_bundled': True,
                         'arguments_shell': '-l 10 /k/ref/t1.c.0001.s.sha256 t1.c.0001.s.sha256'}],
            'binaries': {'cc1_s_base.lab': {'sha256': 'aa', 'debug_sha256': 'bb'}},
            'files': {'t1.c': {'sha256': 'cc', 'bytes': 9}, 'text-sha256': {'sha256': 'dd', 'bytes': 6}},
        }
        _write(self.bundle / 'manifest.json', json.dumps(self.manifest))
        self.launcher = _write(self.tmp / 'interval-submit', 'launcher')

    def test_layout_matches_staging_contract(self):
        dest = self.tmp / 'fm' / '821.gcc_s'
        image = self.fm.package(self.bundle, dest, self.s, self.k, self.launcher, {'start': [1], 'end': [2]})
        name = 'cpu2026-rv64gc-gcc13-static-test-821-gcc_s'
        self.assertEqual(image['workload'], f'{name}.json')
        config = json.loads((dest / f'{name}.json').read_text())
        self.assertEqual((config['base'], config['command'], config['outputs']), ('br-base.json', './run-interval.sh', ['/output']))
        self.assertEqual(config['rootfs-size'], '2GiB')
        self.assertTrue((dest / 'overlay' / '821.gcc_s' / 't1.c').is_file())
        meta = json.loads((dest / 'benchmark.json').read_text())
        self.assertEqual(meta, json.loads((dest / 'overlay' / 'benchmark.json').read_text()))
        self.assertEqual(meta['invocations'], [{'epoch': 1, 'executable': 'cc1_s_base.lab',
                                                'arguments_shell': 't1.c -O3 -o t1.c.0001.s > t1.out 2>> t1.err'}])
        self.assertEqual(meta['spec_dir'], str(self.kit))
        self.assertEqual(len(meta['comparisons']), 2)
        self.assertIn('rv-exec', meta['comparisons'][1])
        self.assertIn(str(self.bundle / 'run' / 'check_base.lab'), meta['comparisons'][1])

    def test_run_script_markers_environment_and_outputs(self):
        dest = self.tmp / 'fm' / '821.gcc_s'
        self.fm.package(self.bundle, dest, self.s, self.k, self.launcher, {'start': [1], 'end': [2]})
        script = (dest / 'overlay' / 'run-interval.sh').read_text()
        self.assertIn('export OMP_STACKSIZE=120M', script)
        self.assertIn('/interval-submit --status-file /output/status/0000 ./cc1_s_base.lab t1.c -O3', script)
        self.assertIn("echo 'PASS spec-interval 821.gcc_s invocations=1'", script)
        for name in ('text-sha256', 't1.c.0001.s', 't1.c.0001.s.sha256'):
            self.assertIn(f'cp {name} /output/{name}', script)
        self.assertNotIn('/k/ref', script)
        self.assertIn('grep -vxF -f /specpack-inputs.list', script)
        inputs = (dest / 'overlay' / 'specpack-inputs.list').read_text().split()
        self.assertEqual(inputs, ['cc1_s_base.lab', 't1.c', 'text-sha256'])

    def test_no_aslr_turns_randomisation_off_before_the_first_invocation(self):
        pcs = {'start': [1], 'end': [2]}
        self.fm.package(self.bundle, self.tmp / 'fm' / '821.gcc_s', self.s, self.k, self.launcher, pcs)
        default = (self.tmp / 'fm' / '821.gcc_s' / 'overlay' / 'run-interval.sh').read_text()
        self.assertNotIn('randomize_va_space', default)
        dest = self.tmp / 'fm-noaslr' / '821.gcc_s'
        image = self.fm.package(self.bundle, dest, self.s, self.k, self.launcher, pcs, aslr=False)
        self.assertEqual(image['workload'], 'cpu2026-rv64gc-gcc13-static-test-noaslr-821-gcc_s.json')
        script = (dest / 'overlay' / 'run-interval.sh').read_text()
        off = '\n'.join(self.fm.ASLR_OFF) + '\n'
        self.assertIn('echo 0 > /proc/sys/kernel/randomize_va_space\n', off)
        # the only difference from the default script, after ulimit and before anything runs
        self.assertEqual(script, default.replace('ulimit -s unlimited\n', 'ulimit -s unlimited\n' + off))
        self.assertLess(script.index(off), script.index('/interval-submit'))
        self.assertIs(json.loads((dest / 'benchmark.json').read_text())['address_space_randomization'], False)
        self.assertIs(json.loads((self.tmp / 'fm' / '821.gcc_s' / 'benchmark.json').read_text())
                      ['address_space_randomization'], True)

    def test_run_script_copies_back_every_created_file(self):
        dest = self.tmp / 'fm' / '821.gcc_s'
        self.fm.package(self.bundle, dest, self.s, self.k, self.launcher, {'start': [1], 'end': [2]})
        overlay = dest / 'overlay'
        script = (overlay / 'run-interval.sh').read_text()
        copy_back = script[script.index('find . -type f'):script.index('echo "$overall" > /output/exitcode')]
        root = self.tmp / 'guest'
        shutil.copytree(overlay / '821.gcc_s', root / 'bench')
        _write(root / 'bench' / 't1.c.0001.s', 'asm')
        _write(root / 'bench' / 'sub' / 'r.sha512', 'h')
        copy_back = copy_back.replace('/specpack-inputs.list', str(overlay / 'specpack-inputs.list'))
        copy_back = copy_back.replace('/output', str(root / 'output')).replace('/tmp/specpack-outputs', str(root / 'list'))
        subprocess.run(['sh', '-c', copy_back], cwd=root / 'bench', check=True)
        copied = sorted(p.relative_to(root / 'output').as_posix() for p in (root / 'output').rglob('*') if p.is_file())
        self.assertEqual(copied, ['sub/r.sha512', 't1.c.0001.s', 'text-sha256'])

    def test_rejects_bundle_from_another_kit(self):
        self.manifest['label'] = 'other'
        _write(self.bundle / 'manifest.json', json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'bundle is for'):
            self.fm.package(self.bundle, self.tmp / 'fm' / 'x', self.s, self.k, self.launcher, {})
