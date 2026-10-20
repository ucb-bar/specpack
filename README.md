# specpack

Build SPEC CPU for a RISC-V Linux target, validate it on the x86 host through QEMU
user mode, and export one self-contained bundle per benchmark and input size for
simulators (FireSim, NEMU) to run.

`runcpu` does the SPEC work (build flags, run-directory setup, command lines, output
checks). specpack supplies an explicit settings file, runs SPEC tools in a fixed minimal
environment, and turns SPEC's run directories into bundles. It is intended to replace
`software/spec2017` (speckle and `tools/interval-trace/prepare-spec-tests.py`); until
then it lives alongside it.

Python 3.11+ standard library only. Licensed SPEC content (kits, run directories,
bundles) never goes into this repository.

## Settings

Copy `specpack.example.toml` to `specpack.toml` (ignored by Git) and edit it, or pass
another file with `--settings`. Every path is stated; nothing is searched for.

| Table | Keys |
| --- | --- |
| `[kit.NAME]` | `version` (2017 or 2026, checked against the kit's `shrc`), `spec_dir` (an installed kit), `config` (a file under `configs/`), `label` (SPEC label) |
| `[toolchain]` | `prefix`: cross toolchain prefix, `<prefix>gcc` must exist |
| `[qemu]` | `riscv64`: plain QEMU user mode |
| `[build]` | `jobs`: parallel compiles within one benchmark |
| `[output]` | `dir`: where records and bundles go |

Every command names its kit with `--kit NAME`; there is no default kit.

## Commands

```sh
python3 -m specpack build    --kit cpu2026 intspeed
python3 -m specpack validate --kit cpu2026 --size test intspeed
python3 -m specpack export   --kit cpu2026 --size test intspeed
```

Benchmarks are anything `runcpu` accepts: names (`821.gcc_s`) or suites (`intspeed`).

- **build** copies the config into the kit and runs `runcpu --action build`. Writes
  `<dir>/<kit>/<label>/build.json`.
- **validate** runs the benchmarks on the host (`runcpu --action run`, one iteration):
  input generators, benchmarks and output checks run through `bin/rv-exec`, which sends
  RISC-V programs to QEMU and runs SPEC's own x86 tools directly. Records pass or fail
  and the binaries' hashes in `<dir>/<kit>/<label>/<size>/validation.json`.
- **export** sets up fresh run directories (`runcpu --action runsetup`, input generators
  through QEMU, benchmarks not run) and writes one bundle per benchmark.

`build` must come first; `validate` and `export` never build (`--nobuild`).

## Bundles

```
<dir>/<kit>/<label>/<size>/<benchmark>/
  manifest.json
  run/      the run directory: inputs and stripped binaries
  debug/    the same binaries, unstripped
```

`manifest.json`:

| Field | Content |
| --- | --- |
| `benchmark`, `size`, `kit`, `label` | what this is |
| `specpack`, `config`, `toolchain`, `qemu`, `spec_log` | how it was made (commit, config hash, versions, SPEC's log) |
| `environment` | variables SPEC sets for the run beyond its own tool environment (for example `OMP_NUM_THREADS`, `OMP_STACKSIZE`) |
| `invocations` | in order: `cwd` (`.` is `run/`), `program` (`./<binary>`), `arguments_shell` (arguments and redirections, shell syntax) |
| `pre_compare`, `compare` | SPEC's output checks, in order: run them on the host, inside the kit's `shrc` environment (`kit.spec_dir`), in a directory holding `run/` plus the outputs; `program_is_bundled` marks RISC-V programs that need QEMU. A check passed when it exits 0 |
| `binaries` | sha256 of each stripped binary and of its unstripped copy |
| `files` | sha256 and size of every other file in `run/` |
| `validation` | the recorded validation for this benchmark and size, and whether it was made with these exact binaries |

Invocations, environment and checks come from SPEC itself: `specinvoke -nn` on the
command files that `runsetup` wrote.

## Environment

SPEC copies the caller's environment into its command files and logs. specpack
therefore starts every SPEC tool from `PATH=/usr/bin:/bin`, `HOME`, `USER`, `LOGNAME`,
`LANG=C.UTF-8` and `SPECPACK_QEMU` only, plus what the kit's `shrc` adds, with
`ulimit -s unlimited`.

## Configs

`configs/cpu2026-rv64gc-gcc13-static.cfg` is SPEC's `Example-gcc-linux-riscv64.cfg`
with: label, toolchain prefix and build jobs from `--define`; `threads = 1`; `-static`;
`deletework`; and `host_setup` / `host_run` sections that route through `rv-exec`. SPEC
appends build hashes after a `__HASH__` line in the kit's copy; specpack keeps that copy
while everything above the line equals the file here, and otherwise replaces it (with a
backup), which makes SPEC rebuild.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The tests use synthetic files only; no SPEC kit is needed.
