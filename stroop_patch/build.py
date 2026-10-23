"""Build the patched STROOP.dll ("Recommended" picks romhack maps) from the patch in this folder.

    python stroop_patch/build.py [--src STROOP_GIT_CLONE] [--base TAG_OR_COMMIT] [--work DIR] [--keep]

Uses the STROOP clone --src (else clones github.com/FramePerfection/STROOP into --work), makes a detached git worktree
of --base (default v0.9.2, the release the patch is made for), applies hack-maps-0.9.2.patch, runs
`dotnet build ./STROOP.sln -c Release`, and copies STROOP.dll and STROOP.pdb into stroop_patch/bin/ together with
patched.json (what stroop_install.set_patch checks before swapping the dll in). The worktree is removed afterwards
unless --keep. Needs git and the .NET SDK.
"""
import argparse, json, os, shutil, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
STROOP_GIT = 'https://github.com/FramePerfection/STROOP.git'
BASE = 'v0.9.2'
BASE_TEXT = 'Refactor 0.9.2'                     # the version text of the base build (StroopMainForm._version)
PATCH = os.path.join(HERE, 'hack-maps-0.9.2.patch')
BIN = os.path.join(HERE, 'bin')
GIT = ['git', '-c', 'core.longpaths=true']          # STROOP's tree has paths past Windows' 260 characters


def run(cmd, cwd=None, timeout=900):
    """A command's output; any failure ends the build with its output."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise SystemExit(f'{" ".join(cmd)} failed ({p.returncode}):\n{p.stdout[-2500:]}\n{p.stderr[-2500:]}')
    return p.stdout


def main():
    """Build the patched STROOP.dll into bin/ (see the module docstring)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--src', help='a git clone of FramePerfection/STROOP (default: cloned into --work)')
    ap.add_argument('--base', default=BASE, help=f'the tag or commit to patch (default {BASE})')
    ap.add_argument('--work', default=os.path.join(HERE, 'work'), help='where the clone and build go (not in git)')
    ap.add_argument('--keep', action='store_true', help='keep the build worktree')
    a = ap.parse_args()
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    src = os.path.abspath(a.src) if a.src else os.path.join(work, 'STROOP')
    if not a.src and not os.path.isdir(os.path.join(src, '.git')):
        print('cloning', STROOP_GIT, flush=True)
        run(GIT + ['clone', '--quiet', STROOP_GIT, src], timeout=1800)
    wt = os.path.join(work, 'src')
    out = os.path.join(work, 'out')
    if os.path.exists(wt):
        run(GIT + ['-C', src, 'worktree', 'remove', '--force', wt])
    run(GIT + ['-C', src, 'worktree', 'add', '--detach', wt, a.base])
    try:
        run(GIT + ['apply', '--ignore-whitespace', '--whitespace=nowarn', PATCH], cwd=wt)
        shutil.rmtree(out, ignore_errors=True)
        t = time.time()
        run(['dotnet', 'build', './STROOP.sln', '-c', 'Release', '-o', out], cwd=wt, timeout=1800)
        print(f'built in {time.time() - t:.0f} s')
        os.makedirs(BIN, exist_ok=True)
        for fn in ('STROOP.dll', 'STROOP.pdb'):
            shutil.copy2(os.path.join(out, fn), os.path.join(BIN, fn))
        commit = run(GIT + ['-C', wt, 'rev-parse', '--short', 'HEAD']).strip()
        with open(os.path.join(BIN, 'patched.json'), 'w') as f:
            json.dump({'base': a.base, 'base_commit': commit, 'base_text': BASE_TEXT, 'patch': os.path.basename(PATCH),
                       'built': time.strftime('%Y-%m-%d %H:%M:%S')}, f, indent=1)
        print('wrote', BIN)
    finally:
        if not a.keep:
            run(GIT + ['-C', src, 'worktree', 'remove', '--force', wt])
            shutil.rmtree(out, ignore_errors=True)


if __name__ == '__main__':
    main()
