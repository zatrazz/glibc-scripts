#! /usr/bin/env python3

"""Run the testsuite of one or more glibc build trees, natively, under
qemu-user, or on a remote machine over ssh, and wrap the git commands that
come up while working on glibc."""

import argparse
import collections
import concurrent.futures
import configparser
import fnmatch
import glob
import io
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

from glibc_abis import SPECIAL_LISTS

HELP_WIDTH = 80

# Every log this script writes lands here, relative to the build tree.
LOGDIR = 'glibc-dev-logs'

# ChangeLog files are generated and match nearly everything, so keep them out
# of the way of the grep command.
GREP_EXCLUDE = ':!ChangeLog*/*'

# make -j level under --ssh or --qemu, where the bottleneck is the remote
# board or the emulator rather than the host.
WRAPPED_JOBS = 2

# The --qemu COMMAND (and the value of a bare --qemu) that stands for
# running the tests as they are, through the qemu-user the kernel's
# binfmt_misc is set up with for the foreign binaries.
QEMU_BINFMT = 'binfmt'

# make -j jobs per native tree that "-p auto" aims for: the sweet spot
# glibc-tools.py uses for its builds, enough to keep a tree's own make busy
# before its serial steps limit it and few enough that several trees run at
# once.
JOBS_PER_TREE = 6

# Set on Ctrl-C so the trees still running stop after their current step
# instead of marching through the remaining ones.
ABORT = threading.Event()


class Error(Exception):
  """A fatal error whose message is meant for the user."""


class bcolors:
  OKGREEN = '\033[92m'
  WARNING = '\033[93m'
  FAIL = '\033[91m'
  BOLD = '\033[1m'
  ENDC = '\033[0m'


USE_COLOR = False

def colorize(text, color):
  if not USE_COLOR:
    return text
  return color + text + bcolors.ENDC

# The verdicts glibc's test driver and merge-test-results.sh produce, plus the
# BUILD-ERROR this script invents for a test that never got as far as running.
PASS_VERDICTS = ('PASS', 'XFAIL')
SKIP_VERDICTS = ('UNSUPPORTED',)
FAIL_VERDICTS = ('FAIL', 'XPASS', 'UNRESOLVED', 'ERROR', 'BUILD-ERROR')
VERDICTS = PASS_VERDICTS + SKIP_VERDICTS + FAIL_VERDICTS

VERDICT_COLOR = {
  'PASS': bcolors.OKGREEN,
  'XFAIL': bcolors.OKGREEN,
  'UNSUPPORTED': bcolors.WARNING,
  'XPASS': bcolors.WARNING,
}

def verdict_of(line):
  """The verdict word of a "PASS: nptl/tst-foo" status line, '' if there is
  none: the summary files also carry headers and blank lines."""
  word = line.split(':', 1)[0].strip()
  return word if word in VERDICTS else ''

def format_status(line):
  return colorize(line, VERDICT_COLOR.get(verdict_of(line), bcolors.FAIL))

def format_duration(seconds):
  """Format a wall-clock duration as a compact HhMmSs string."""
  seconds = int(round(seconds))
  h, rem = divmod(seconds, 3600)
  m, s = divmod(rem, 60)
  if h:
    return '%dh%02dm%02ds' % (h, m, s)
  if m:
    return '%dm%02ds' % (m, s)
  return '%ds' % s

def print_summary(npass, nfail, nskip, ntests, elapsed):
  summary = 'summary: %d passed, %d failed, %d unsupported (of %d tests)' \
            ' in %s' % (npass, nfail, nskip, ntests, format_duration(elapsed))
  print(colorize(summary, bcolors.FAIL if nfail else bcolors.OKGREEN))

def available_cpus():
  try:
    return len(os.sched_getaffinity(0))
  except AttributeError:
    return os.cpu_count() or 1

def read_config(section='glibc-tools'):
  """A section of ~/.glibc-tools.ini as a dict, empty if there is none.

  [glibc-tools] is shared with glibc-tools.py, which is what fills it in;
  the [ssh] and [qemu] sections map build trees to where their tests run
  (see where_to_run), and glibc-tools-config.py writes them all.
  """
  config = configparser.RawConfigParser()
  # Build tree names are matched as spelled, not lowercased.
  config.optionxform = str
  config.read(os.path.expanduser('~/.glibc-tools.ini'))
  if section not in config.sections():
    return {}
  return dict(config[section])


def expand_abis(patterns, builddir, suffix=''):
  # glibc-tools.py -u appends "-<suffix>" to the directory of every tree it
  # builds; the same suffix here selects those trees.
  tag = '-' + suffix if suffix else ''
  try:
    entries = sorted(os.listdir(builddir))
  except OSError:
    entries = []
  trees = [name for name in entries
           if os.path.isfile(os.path.join(builddir, name, 'config.make'))]
  names = {}
  for pattern in patterns:
    members = SPECIAL_LISTS.get(pattern)
    if members is not None:
      members = [name + tag for name in members]
      built = [name for name in members if name in trees]
      if not built:
        raise Error("no build tree under %s for any member of '%s'"
                    % (builddir, pattern + tag))
      missing = [name for name in members if name not in trees]
      if missing:
        print(colorize('warning: %s: no build tree for %s'
                       % (pattern, ', '.join(missing)), bcolors.WARNING))
      for name in built:
        names[name] = True
    elif any(c in pattern for c in '*?['):
      matched = fnmatch.filter(trees, pattern + tag)
      if not matched:
        raise Error("no build tree under %s matches '%s'"
                    % (builddir, pattern + tag))
      for name in matched:
        names[name] = True
    else:
      names[pattern + tag] = True
  return list(names)


class BuildTree:
  """The glibc build tree the commands act on."""

  def __init__(self, path):
    self.path = os.path.abspath(path)
    self.name = os.path.basename(self.path)
    if not os.path.isfile(self.file('config.make')):
      raise Error("'%s' is not a glibc build tree (no config.make)" % path)
    self.srcdir = self._read_srcdir()
    self.subdirs = self._read_subdirs()

  @classmethod
  def find_all(cls, opts):
    if not opts.abis:
      return [cls(opts.builddir)]
    builddir = read_config().get('builddir', '')
    if not builddir:
      raise Error('--abi needs a builddir, run glibc-tools-config.py')
    patterns = [name for arg in opts.abis for name in arg.split(',') if name]
    return [cls(os.path.join(builddir, name))
            for name in expand_abis(patterns, builddir, opts.suffix)]

  def file(self, *parts):
    return os.path.join(self.path, *parts)

  def _read_srcdir(self):
    """The source tree this build tree was configured from.

    configure generates the build directory Makefile from Makefile.in, and it
    starts with "srcdir = <path>".  Asking the tree beats assuming a source
    directory: several build trees of different checkouts usually coexist.
    """
    try:
      with open(self.file('Makefile')) as f:
        for line in f:
          match = re.match(r'srcdir\s*=\s*(\S.*?)\s*$', line)
          if match:
            return match.group(1)
    except OSError:
      pass
    return read_config().get('srcdir', '')

  def _read_subdirs(self):
    """The subdirectories the build knows about, None if it never recorded
    them.

    sysd-sorted is generated by the build itself, so the list also covers the
    subdirectories pulled in by sysdeps (mathvec, nptl_db, ...).
    """
    try:
      with open(self.file('sysd-sorted')) as f:
        for line in f:
          match = re.match(r'sorted-subdirs\s*:?=\s*(.*)$', line)
          if match:
            return match.group(1).split()
    except OSError:
      pass
    return None

  def is_subdir(self, name):
    """True if NAME is a whole subdirectory of the build tree rather than an
    individual test."""
    name = name.rstrip('/')
    if '/' in name or not os.path.isdir(self.file(name)):
      return False
    # Without sysd-sorted there is nothing to check the name against, so take
    # the directory at face value.
    if self.subdirs is None:
      return True
    return name in self.subdirs

  def resolve_test(self, name):
    """Resolve NAME to the "<subdir>/<test>" form "make test t=" expects.

    Tests whose source lives under sysdeps/ are built in a different
    subdirectory (sysdeps/pthread/tst-robust7 is built as nptl/tst-robust7),
    so fall back to looking the name up in the build tree.
    """
    test = name[2:] if name.startswith('./') else name
    for suffix in ('.c', '.out'):
      if test.endswith(suffix):
        test = test[:-len(suffix)]
    base = os.path.basename(test)

    if '/' in test and not test.startswith('sysdeps/') \
       and os.path.isdir(self.file(os.path.dirname(test))):
      return test

    for subdir in sorted(os.listdir(self.path)):
      if not os.path.isdir(self.file(subdir)):
        continue
      if any(os.path.exists(self.file(subdir, base + ext))
             for ext in ('', '.o')):
        return '%s/%s' % (subdir, base)
    return test

  def logfile(self, name):
    """Path of one of this script's log files, creating the log directory on
    the way."""
    logdir = self.file(LOGDIR)
    os.makedirs(logdir, exist_ok=True)
    return os.path.join(logdir, name)


def run_make(tree, targets, variables=(), log=None, jobs=1, keep_going=False,
             stream=False):
  """Run make in the build tree and return its exit status.

  What make and the tests write goes to LOG and nowhere else, so that the
  verdicts stay readable; --stream echoes it to the terminal as well, for
  when watching a long run matters more.
  """
  cmd = ['make', '-j%d' % jobs]
  if keep_going:
    cmd.append('-k')
  cmd += list(targets)
  cmd += ['%s=%s' % (name, value) for name, value in variables]

  with open(log, 'w') as logfile:
    logfile.write('# %s\n' % ' '.join(shlex.quote(arg) for arg in cmd))
    logfile.flush()
    if not stream:
      return subprocess.call(cmd, cwd=tree.path, stdout=logfile,
                             stderr=subprocess.STDOUT)
    proc = subprocess.Popen(cmd, cwd=tree.path, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            universal_newlines=True)
    for line in proc.stdout:
      sys.stdout.write(line)
      logfile.write(line)
    return proc.wait()

def test_variables(wrapper, opts):
  """The make variables that turn a build target into a test run."""
  # --no-run only exists on the check command; the test command always runs
  # what it builds.
  run = 'no' if getattr(opts, 'no_run', False) else 'yes'
  variables = [('run-built-tests', run)]
  if wrapper:
    variables.append(('test-wrapper', wrapper))
  if opts.timeoutfactor:
    variables.append(('TIMEOUTFACTOR', opts.timeoutfactor))
  return variables

class Timer:
  """How long a command has been running, and what it wrote while it did."""

  def __init__(self):
    self.begin = time.monotonic()
    # Cutoff for telling the files this run wrote from the ones an earlier
    # run left behind.  The second of slack covers filesystems that keep
    # coarse timestamps; no glibc test run is that short.
    self.cutoff = time.time() - 1

  def elapsed(self):
    return time.monotonic() - self.begin

class JobBudget:
  """The make -j level of each tree of a run.

  An explicit -j applies to every tree.  Otherwise a tree whose tests run
  under --ssh or --qemu gets WRAPPED_JOBS, the board or the emulator being
  the bottleneck rather than the host, and the native trees split the
  remaining cpus evenly between however many of them are running, the way
  glibc-tools.py spreads its build budget: the whole machine for a single
  tree, and a growing share as the others finish.
  """

  def __init__(self, jobs):
    self.jobs = jobs
    self.ncpus = available_cpus()
    # wrapped -> trees currently running
    self.active = {False: 0, True: 0}
    self.lock = threading.Lock()

  def started(self, wrapped):
    with self.lock:
      self.active[wrapped] += 1

  def finished(self, wrapped):
    with self.lock:
      self.active[wrapped] -= 1

  def jobs_for(self, wrapped):
    if self.jobs:
      return self.jobs
    if wrapped:
      return WRAPPED_JOBS
    with self.lock:
      native, remote = self.active[False], self.active[True]
    cpus = self.ncpus - remote * WRAPPED_JOBS
    return max(1, math.ceil(cpus / max(1, native)))

def parallel_type(string):
  if string == 'auto':
    return None
  try:
    value = int(string)
  except ValueError:
    value = 0
  if value < 1:
    raise argparse.ArgumentTypeError("expected a number of trees or 'auto'")
  return value

def parallel_trees(opts, ntrees, nwrapped, budget):
  """How many trees run at once: -p, or with "auto" as many as fit the cpus
  at the make -j each tree is going to get."""
  if opts.parallel:
    return opts.parallel
  # --stream echoes the make output as it comes, which only reads when a
  # single tree writes it.
  if ntrees == 1 or opts.stream:
    return 1
  if budget.jobs:
    per_tree = budget.jobs
  elif nwrapped == ntrees:
    per_tree = WRAPPED_JOBS
  else:
    per_tree = min(JOBS_PER_TREE, max(1, budget.ncpus // 2))
  return max(1, min(ntrees, budget.ncpus // per_tree))

def describe_jobs(budget, ntrees, nwrapped):
  if budget.jobs:
    return 'make -j%d each' % budget.jobs
  parts = []
  if nwrapped < ntrees:
    parts.append('the %d cpus shared by the native trees' % budget.ncpus)
  if nwrapped:
    parts.append('make -j%d under ssh/qemu' % WRAPPED_JOBS)
  return ', '.join(parts)

def build(tree, jobs, opts):
  """Bring the tree up to date, the way "make" alone would.  Raises if make
  fails: there is no point running tests against a half-built tree."""
  log = tree.logfile('build.log')
  if opts.verbose:
    print('building (full log: %s)' % log)
  if run_make(tree, [], log=log, jobs=jobs, stream=opts.stream) != 0:
    raise Error('make failed, see %s' % log)


QEMU_WRAPPER = """\
#!/bin/bash
# Generated by glibc-dev.py -- do not edit.
env_args=()
if [ "${1##*/}" = env ]; then
  env_args+=("$1"); shift
  while [ $# -gt 0 ]; do
    case $1 in
      *=*) env_args+=("$1"); shift ;;
      -u)  env_args+=("$1" "$2"); shift 2 ;;
      -*)  env_args+=("$1"); shift ;;
      *)   break ;;
    esac
  done
fi
exec "${env_args[@]}" %s "$@"
"""

def find_qemu(name):
  """Resolve NAME to the qemu-user binary to run the tests with.

  A bare architecture is expanded to qemu-<arch>-static and then qemu-<arch>;
  the architecture is never probed as a command of its own, because util-linux
  ships /usr/bin/x86_64 (and i386, s390x, ...) as setarch symlinks, which
  would silently run the tests natively instead of under qemu.  The result is
  an absolute path: with "env -i" the PATH is cleared before qemu is exec'ed,
  so a bare name would no longer be found.
  """
  if '/' in name:
    candidates = [name]
  elif name.startswith('qemu-'):
    candidates = [name, name + '-static']
  else:
    candidates = ['qemu-%s-static' % name, 'qemu-%s' % name]

  for candidate in candidates:
    if '/' in candidate:
      if os.access(candidate, os.X_OK):
        return os.path.realpath(candidate)
    else:
      found = shutil.which(candidate)
      if found:
        return os.path.abspath(found)

  raise Error("no qemu-user binary found for '%s' (tried: %s)"
              % (name, ', '.join(candidates)))

def qemu_wrapper(tree, command, sysroot, verbose=False):
  """Write the test-wrapper that runs the tests under qemu-user and return
  its path.

  COMMAND is a qemu command line: the binary, optionally followed by options
  for qemu itself ("qemu-x86_64 -cpu Nehalem").

  glibc builds the test command as
    $(test-wrapper) env [-i] [VAR=VAL...] <rtld> --library-path ... <prog>
  (see test-wrapper-env in Makeconfig).  /usr/bin/env is a host binary, so it
  cannot be passed to qemu-user; the wrapper reorders the command so that env
  runs on the host and qemu is inserted just before the program.
  """
  argv = shlex.split(command)
  if not argv:
    raise Error('empty --qemu command')
  argv[0] = find_qemu(argv[0])
  if sysroot:
    argv += ['-L', sysroot]

  path = tree.logfile('qemu-test-wrapper.sh')
  with open(path, 'w') as wrapper:
    wrapper.write(QEMU_WRAPPER % ' '.join(shlex.quote(arg) for arg in argv))
  os.chmod(path, 0o755)
  if verbose:
    print('using %s' % ' '.join(argv))
  return path

def cross_ssh_wrapper(tree, machine):
  """The test-wrapper that runs the tests on MACHINE over ssh.

  The script comes from the source tree this build tree was configured from,
  falling back to the configured srcdir: a checkout is sometimes moved or
  deleted from under a build tree that still works, and cross-test-ssh.sh is
  generic enough that any reasonably recent copy will do.
  """
  candidates = [srcdir for srcdir in (tree.srcdir,
                                      read_config().get('srcdir', '')) if srcdir]
  if not candidates:
    raise Error('cannot tell which source tree %s was configured from'
                % tree.path)
  for srcdir in candidates:
    script = os.path.join(srcdir, 'scripts', 'cross-test-ssh.sh')
    if os.path.isfile(script):
      return '%s %s' % (script, machine)
  raise Error('cross-test-ssh.sh not found (tried: %s)'
              % ', '.join(candidates))

def split_mapping(arg):
  """Split a --ssh/--qemu argument into (pattern, value).

  "aarch64*=debian-aarch64" applies to the trees matching the glob and a
  bare "debian-aarch64" to every tree.  A qemu command line can carry an
  "=" of its own ("qemu-aarch64 -cpu max,sve=on"), so only a name without
  whitespace ahead of the first "=" counts as a pattern.
  """
  pattern, sep, value = arg.partition('=')
  if sep and pattern and not re.search(r'\s', pattern):
    return pattern, value.strip()
  return '*', arg.strip()

def where_to_run(trees, opts):
  """Where each tree runs its tests, as {tree name: ('ssh', machine),
  ('qemu', command) or None for a native run}.

  The --ssh and --qemu options come first; a tree they do not name falls
  back to the [ssh] and [qemu] sections of ~/.glibc-tools.ini (which
  glibc-tools-config.py --ssh/--qemu fill in), and then to running
  natively.  Within each of the two, the last matching entry wins, so a
  specific name given after a glob overrides it, and an empty value
  ("x86_64*=") pins the tree to a native run.  A tree cannot be under both
  ssh and qemu.

  A pattern matches either the tree name or, under -u, the ABI name without
  the suffix, so "aarch64-linux-gnu" also selects aarch64-linux-gnu-work.
  """
  config = [(kind, pattern, value) for kind in ('ssh', 'qemu')
            for pattern, value in read_config(kind).items()]
  given = [(kind,) + split_mapping(arg)
           for kind, args in (('ssh', opts.ssh), ('qemu', opts.qemu))
           for arg in args]

  tag = '-' + opts.suffix if opts.suffix else ''
  def matches(tree, pattern):
    if fnmatch.fnmatchcase(tree.name, pattern):
      return True
    return bool(tag) and tree.name.endswith(tag) \
      and fnmatch.fnmatchcase(tree.name[:-len(tag)], pattern)

  wheres = {}
  for tree in trees:
    where = None
    for entries, source in ((given, 'the command line'),
                            (config, '~/.glibc-tools.ini')):
      found = [(kind, value) for kind, pattern, value in entries
               if matches(tree, pattern)]
      if not found:
        continue
      kinds = dict(found)
      if len(kinds) > 1:
        raise Error("%s: both ssh (%s) and qemu (%s) apply to it, from %s"
                    % (tree.name, kinds['ssh'], kinds['qemu'], source))
      kind, value = found[-1]
      where = (kind, value) if value else None
      break
    wheres[tree.name] = where

  # A pattern that selects no tree is most likely a typo.
  for kind, pattern, value in given:
    if not any(matches(tree, pattern) for tree in trees):
      print(colorize("warning: --%s %s=%s matches none of the trees"
                     % (kind, pattern, value), bcolors.WARNING))
  return wheres

def describe_where(where):
  if where is None:
    return 'natively'
  if where == ('qemu', QEMU_BINFMT):
    return 'qemu via binfmt'
  return '%s %s' % where

def make_wrapper(tree, where, opts):
  """The make test-wrapper that runs the tests WHERE (see where_to_run),
  None for a native run.

  Under binfmt there is no wrapper either: the tests are run as they are and
  the kernel hands the foreign binaries (the build tree's ld.so first of
  all) to the qemu-user registered for them.
  """
  if where is None or where == ('qemu', QEMU_BINFMT):
    return None
  kind, value = where
  if kind == 'ssh':
    return cross_ssh_wrapper(tree, value)
  return qemu_wrapper(tree, value, opts.sysroot, opts.verbose)

def reclaim_qemu_arguments(opts, trees, known):
  """The positionals a bare --qemu swallowed, taken back out of it.

  --qemu takes an optional COMMAND, so in "check --qemu math" argparse hands
  math to the option rather than to the subdirectories.  A value that is no
  qemu this machine has, but that every tree knows as KNOWN(tree, name), is
  such a positional: the option is read as a bare --qemu (binfmt) and the
  value is returned to be put back in front of the positionals.
  """
  reclaimed = []
  for index, value in enumerate(opts.qemu):
    pattern, command = split_mapping(value)
    if pattern != '*' or command in ('', QEMU_BINFMT) \
       or re.search(r'\s', command):
      continue
    try:
      find_qemu(command)
      continue
    except Error:
      pass
    if all(known(tree, command) for tree in trees):
      opts.qemu[index] = QEMU_BINFMT
      reclaimed.append(command)
  return reclaimed


TARGET_RE = re.compile(r'\*\*\* \[([^]]*)\]')

def read_lines(path):
  try:
    with open(path, errors='replace') as f:
      return [line.rstrip('\n') for line in f]
  except OSError:
    return []

def failed_targets(log, subdir):
  """The SUBDIR targets make reported as failed, without their suffix.

  make writes "*** [<rule>: <objdir>/<subdir>/<test>.out] Error <n>" (older
  make versions leave out the rule), which is the only trace a test that never
  got as far as producing a result leaves behind.
  """
  names = set()
  for line in read_lines(log):
    match = TARGET_RE.search(line)
    if not match:
      continue
    target = match.group(1).rsplit(': ', 1)[-1]
    index = target.rfind('/' + subdir + '/')
    if index >= 0:
      target = target[index + 1:]
    if not target.startswith(subdir + '/'):
      continue
    name, ext = os.path.splitext(target)
    # Anything else under the subdirectory (a shared object, the recursive
    # "<subdir>/tests" target itself) is not a test.
    if ext not in ('', '.o', '.out') or name.rsplit('/')[-1] in ('tests',
                                                                 'xtests'):
      continue
    names.add(name)
  return sorted(names)


class Report:
  """The verdicts of a test run.

  Only the failures are printed; what a test wrote is left in the logs, which
  is where you end up looking anyway.  --verbose prints every verdict.
  """

  def __init__(self, tree, verbose=False, timer=None):
    self.tree = tree
    self.verbose = verbose
    self.timer = timer or Timer()
    self.nfail = 0
    self.npass = 0
    self.nskip = 0
    self.ntests = 0
    # (status, name, log, shared) of every failure, for the cross-tree
    # summary; shared marks a log covering a whole subdirectory rather than
    # the single test.
    self.failures = []

  def record(self, status, name, log, shared=False):
    """Note the verdict of one test."""
    self.ntests += 1
    verdict = verdict_of(status)
    if verdict in PASS_VERDICTS:
      self.npass += 1
    elif verdict in SKIP_VERDICTS:
      self.nskip += 1
    else:
      self.nfail += 1
      self.failures.append((status, name, log, shared))
    if self.verbose or verdict in FAIL_VERDICTS:
      print(format_status(status))

  def finish(self):
    """Print the summary; returns the exit status."""
    print_summary(self.npass, self.nfail, self.nskip, self.ntests,
                  self.timer.elapsed())
    if self.nfail:
      print('logs: %s' % self.tree.file(LOGDIR))
    return 1 if self.nfail else 0


# How much of a failed test's output the cross-tree summary shows; the full
# files are on disk and their paths are printed alongside.
FAILURE_TAIL_LINES = 30

def print_tail(path, label):
  lines = read_lines(path)
  if not lines:
    print('%s: empty or missing (%s)' % (label, path))
    return
  tail = lines[-FAILURE_TAIL_LINES:]
  if len(tail) < len(lines):
    print('%s (last %d of %d lines): %s' % (label, len(tail), len(lines),
                                            path))
  else:
    print('%s: %s' % (label, path))
  for line in tail:
    print('  ' + line)

def print_trees_report(trees, results, elapsed):
  """The cross-tree summary of the test command; RESULTS holds the (Report,
  error) of every tree, the report being None for a tree that hit an
  error."""
  reports = [report for report, _ in results if report is not None]
  print()
  print(colorize('results by build tree:', bcolors.BOLD))
  namew = max(len(tree.name) for tree in trees) + 1
  for tree, (report, error) in zip(trees, results):
    if report is None:
      line = '%-*s error: %s' % (namew, tree.name + ':', error)
      print(colorize(line, bcolors.FAIL))
      continue
    line = '%-*s %d passed, %d failed, %d unsupported (of %d tests)' \
           % (namew, tree.name + ':', report.npass, report.nfail,
              report.nskip, report.ntests)
    print(colorize(line, bcolors.FAIL if report.nfail else bcolors.OKGREEN))
  print_summary(sum(r.npass for r in reports), sum(r.nfail for r in reports),
                sum(r.nskip for r in reports), sum(r.ntests for r in reports),
                elapsed)
  for report in reports:
    for status, name, log, shared in report.failures:
      print()
      print(colorize('--- %s: %s ---' % (report.tree.name, status),
                     bcolors.BOLD))
      print_tail(report.tree.file(name + '.out'), name + '.out')
      if shared:
        print('make output (stdout+stderr): %s' % log)
      else:
        print_tail(log, 'make output (stdout+stderr)')


def remove_files(*paths):
  for path in paths:
    try:
      os.remove(path)
    except OSError:
      pass

def read_test_result(tree, name):
  """The verdict a test left in <name>.test-result, None if it did not get
  that far."""
  lines = read_lines(tree.file(name + '.test-result'))
  return lines[0] if lines else None

def run_one_test(tree, name, wrapper, jobs, opts, report):
  """Run one test with a make invocation of its own."""
  log = tree.logfile(name.replace('/', '_') + '.log')
  remove_files(tree.file(name + '.out'), tree.file(name + '.test-result'))

  variables = test_variables(wrapper, opts) + [('t', name)]
  # "make test" only fails when the test could not be built or run at all;
  # the PASS/FAIL verdict itself lands in <name>.test-result.
  status = run_make(tree, ['test'], variables, log=log, jobs=jobs,
                    stream=opts.stream)
  result = read_test_result(tree, name)
  if result is None:
    result = '%s: %s' % ('BUILD-ERROR' if status else 'UNRESOLVED', name)
  report.record(result, name, log)

def run_subdir(tree, subdir, wrapper, jobs, opts, report):
  """Run every test of SUBDIR with a single "make <subdir>/tests".

  That is far cheaper than one make invocation per test, and the verdicts are
  collected from the <test>.test-result files the run leaves behind.
  """
  log = tree.logfile(subdir + '-tests.log')

  # The .out files are the make targets, so a subdirectory that has already
  # been checked would be up to date and nothing would be run again.
  for pattern in ('*.out', '*.test-result'):
    remove_files(*glob.glob(tree.file(subdir, pattern)))

  if opts.verbose:
    print('=== %s (full log: %s)' % (colorize(subdir, bcolors.BOLD), log))
  # keep_going: a test that does not build must not stop the remaining ones.
  run_make(tree, ['%s/tests' % subdir], test_variables(wrapper, opts),
           log=log, jobs=jobs, keep_going=True, stream=opts.stream)

  for result in sorted(glob.glob(tree.file(subdir, '*.test-result'))):
    name = os.path.relpath(result, tree.path)[:-len('.test-result')]
    status = read_test_result(tree, name) or 'UNRESOLVED: %s' % name
    report.record(status, name, log, shared=True)

  # A test that failed to build leaves no .test-result behind.
  for name in failed_targets(log, subdir):
    if read_test_result(tree, name) is None:
      report.record('BUILD-ERROR: %s' % name, name, log, shared=True)

def report_build(status, log, timer):
  """Report the outcome of a --no-run run, where make's exit status is all
  there is: nothing was run, so there are no verdicts.  Returns True if the
  build succeeded."""
  if status:
    print(colorize('build failed in %s, see %s'
                   % (format_duration(timer.elapsed()), log), bcolors.FAIL))
    return False
  print(colorize('build ok in %s' % format_duration(timer.elapsed()),
                 bcolors.OKGREEN))
  return True

def report_sum(tree, sumfile, log, timer, verbose=False):
  """Print the failures listed in a test summary file, and count its
  verdicts.  Returns True if everything in it passed.

  A successful run always rewrites the summary, since merge-test-results.sh
  runs unconditionally once the tests it covers are done.  One left over from
  an earlier run therefore means this one died before running them, and
  reporting it would pass off stale verdicts as current.
  """
  path = tree.file(sumfile)
  fresh = os.path.exists(path) and os.path.getmtime(path) >= timer.cutoff
  lines = read_lines(path) if fresh else []
  if not lines:
    print(colorize('error: %s was not written by this run, see %s'
                   % (sumfile, log), bcolors.FAIL))
    return False

  counts = collections.Counter()
  for line in lines:
    verdict = verdict_of(line)
    if not verdict:
      continue
    counts[verdict] += 1
    if verbose or verdict in FAIL_VERDICTS:
      print(format_status(line))

  npass = sum(counts[verdict] for verdict in PASS_VERDICTS)
  nskip = sum(counts[verdict] for verdict in SKIP_VERDICTS)
  nfail = sum(counts[verdict] for verdict in FAIL_VERDICTS)
  print_summary(npass, nfail, nskip, npass + nskip + nfail, timer.elapsed())
  if nfail:
    print('logs: %s' % log)
  return nfail == 0


class Progress:
  """Live one-line-per-tree display of a parallel run, after the one of
  glibc-tools.py:

    <tree>:            | <step> | <PASS|FAIL> | <time>

  The step column tracks what the tree is on, the status column fills in
  once that step finishes and the time column shows how long it took.  What
  the trees print meanwhile is held back (see ThreadOutput) so the block can
  be redrawn in place; when the output is not a terminal, or the block does
  not fit on it, a line is printed whenever a step finishes instead.
  """

  # status -> (label, color)
  _STATUS = {
    'run': ('', ''),
    'pass': ('PASS', bcolors.OKGREEN),
    'fail': ('FAIL', bcolors.FAIL),
  }

  def __init__(self, names, steps, out):
    self.out = out
    self.order = list(names)
    self.namew = max(len(name) for name in names) + 1
    self.stepw = max((len(step) for step in steps), default=0)
    # name -> (step, status, elapsed)
    self.state = {name: ('', 'run', None) for name in names}
    self.began = {}
    self.lock = threading.Lock()

    rows = shutil.get_terminal_size((0, 0)).lines
    self.live = out.isatty() and 0 < len(self.order) < rows
    if self.live:
      out.write(''.join(self._line(name) + '\n' for name in self.order))
      out.flush()

  def _line(self, name):
    step, status, elapsed = self.state[name]
    label, color = self._STATUS[status]
    return '%s | %s | %s | %s' % (
      colorize('%-*s' % (self.namew, name + ':'), bcolors.BOLD),
      colorize('%-*s' % (self.stepw, step), bcolors.WARNING),
      colorize('%-4s' % label, color) if color else '%-4s' % label,
      format_duration(elapsed) if elapsed is not None else '')

  def _redraw(self):
    # Move to the top of the block and rewrite every line in place.
    self.out.write('\033[%dA' % len(self.order))
    for name in self.order:
      self.out.write('\r\033[K' + self._line(name) + '\n')
    self.out.flush()

  def running(self, name, step):
    with self.lock:
      self.began[name] = time.monotonic()
      self.state[name] = (step, 'run', None)
      if self.live:
        self._redraw()

  def finished(self, name, step, ok):
    with self.lock:
      elapsed = time.monotonic() - self.began.get(name, time.monotonic())
      self.state[name] = (step, 'pass' if ok else 'fail', elapsed)
      if self.live:
        self._redraw()
      else:
        self.out.write(self._line(name) + '\n')
        self.out.flush()

  def failed(self, name):
    """The tree hit an error in whatever step it was on."""
    self.finished(name, self.state[name][0] or '<error>', False)


class NoProgress:
  """Stand-in for Progress when the trees run one at a time and print as
  they go, which is progress enough."""

  def running(self, name, step):
    pass

  def finished(self, name, step, ok):
    pass

  def failed(self, name):
    pass


class ThreadOutput:
  """A sys.stdout stand-in that keeps what each worker thread prints in a
  buffer of its own, so that the reports of trees running concurrently
  neither interleave nor break the Progress block; they are printed tree by
  tree once the run is over.  A thread without a buffer (the main one)
  writes straight through."""

  def __init__(self, real):
    self.real = real
    self.local = threading.local()

  def capture(self):
    """Start buffering what the calling thread prints."""
    self.local.buffer = io.StringIO()

  def collected(self):
    return self.local.buffer.getvalue()

  def write(self, text):
    getattr(self.local, 'buffer', self.real).write(text)

  def flush(self):
    self.real.flush()

  def isatty(self):
    return self.real.isatty()


def run_trees(trees, opts, steps, worker):
  """Run WORKER over the build trees, -p of them at a time.

  WORKER(tree, wrapper, wrapped, budget, progress) does the work of one tree
  and returns its result; an Error it raises is printed as the tree's
  output and counts as the tree failing.  Returns the (result, error) of
  every tree in order, the result being None for a tree that raised.

  With a single tree, or -p 1, the trees run in turn and print as they go.
  Otherwise the trees run from a thread pool behind a Progress block, each
  tree's output being held back and printed below the block once all are
  done.
  """
  wheres = where_to_run(trees, opts)
  # The wrappers are set up before anything runs, so that a missing
  # cross-test-ssh.sh or qemu binary is reported before hours of testing.
  wrappers = [make_wrapper(tree, wheres[tree.name], opts) for tree in trees]
  # A tree under binfmt has no wrapper but is emulated all the same.
  nwrapped = sum(1 for where in wheres.values() if where is not None)
  budget = JobBudget(opts.jobs)
  nparallel = parallel_trees(opts, len(trees), nwrapped, budget)

  def header(tree):
    return '=== %s ===' % colorize('%s (%s)' % (tree.name,
                                                describe_where(wheres[tree.name])),
                                   bcolors.BOLD)

  def run(tree, wrapper, progress):
    wrapped = wheres[tree.name] is not None
    budget.started(wrapped)
    try:
      return worker(tree, wrapper, wrapped, budget, progress), None
    except Error as exc:
      progress.failed(tree.name)
      print(colorize('error: %s' % exc, bcolors.FAIL))
      return None, exc
    finally:
      budget.finished(wrapped)

  if nparallel == 1:
    results = []
    for tree, wrapper in zip(trees, wrappers):
      if len(trees) > 1:
        print(header(tree))
      results.append(run(tree, wrapper, NoProgress()))
    return results

  print('running %d trees, %d at a time (%s)'
        % (len(trees), nparallel, describe_jobs(budget, len(trees), nwrapped)))
  out = sys.stdout
  progress = Progress([tree.name for tree in trees], steps, out)
  output = ThreadOutput(out)
  outputs = {}
  results = {}

  def run_captured(tree, wrapper):
    output.capture()
    try:
      results[tree.name] = run(tree, wrapper, progress)
    finally:
      outputs[tree.name] = output.collected()

  sys.stdout = output
  try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=nparallel) \
         as executor:
      futures = [executor.submit(run_captured, tree, wrapper)
                 for tree, wrapper in zip(trees, wrappers)]
      try:
        for future in concurrent.futures.as_completed(futures):
          future.result()
      except KeyboardInterrupt:
        # Drop the queued trees and stop the running ones after their
        # current step; the executor's shutdown then only waits for those.
        ABORT.set()
        for future in futures:
          future.cancel()
        raise
  finally:
    sys.stdout = out

  for tree in trees:
    print()
    print(header(tree))
    sys.stdout.write(outputs.get(tree.name, ''))
  return [results[tree.name] for tree in trees]

def exit_status(results, failed):
  """The exit status of a command: 2 for an error, 1 for a failed test."""
  if any(error is not None for _, error in results):
    return 2
  return 1 if failed else 0


def test_tree(tree, wrapper, wrapped, budget, opts, work, timer, progress):
  """The test command on one tree; returns its Report."""
  if opts.build:
    progress.running(tree.name, 'build')
    build(tree, budget.jobs_for(wrapped), opts)
    progress.finished(tree.name, 'build', True)
  report = Report(tree, opts.verbose, timer)
  for is_subdir, name in work:
    if ABORT.is_set():
      break
    progress.running(tree.name, name)
    nfail = report.nfail
    jobs = budget.jobs_for(wrapped)
    if is_subdir:
      run_subdir(tree, name, wrapper, jobs, opts, report)
    else:
      run_one_test(tree, name, wrapper, jobs, opts, report)
    progress.finished(tree.name, name, report.nfail == nfail)
  report.finish()
  return report

def cmd_test(opts):
  """Run individual tests and whole subdirectories, one verdict per test,
  in one or more build trees."""
  trees = BuildTree.find_all(opts)
  # Any name can be a test, so whatever is no qemu goes back to the tests.
  opts.tests = reclaim_qemu_arguments(opts, trees, lambda tree, name: True) \
               + opts.tests
  if not opts.tests:
    raise Error('the test command needs a test or a subdirectory to run')
  # Started before anything runs, so that --build counts towards the total;
  # with several trees each tree's summary shows the time up to that point,
  # the cross-tree summary at the end being the total.
  timer = Timer()

  # Classify the arguments against every tree up front, so that a directory
  # that is not part of one of the build trees is reported before anything is
  # run.  The resolution is per tree: the subdirectory a test is built in can
  # differ between configurations.
  works = {}
  for tree in trees:
    work = []
    for name in opts.tests:
      if tree.is_subdir(name):
        work.append((True, name.rstrip('/')))
      elif os.path.isdir(tree.file(name)):
        raise Error("'%s' is not a subdirectory of build tree %s"
                    % (name, tree.path))
      else:
        work.append((False, tree.resolve_test(name)))
    works[tree.name] = work

  steps = (['build'] if opts.build else []) \
          + [name for work in works.values() for _, name in work]

  def worker(tree, wrapper, wrapped, budget, progress):
    return test_tree(tree, wrapper, wrapped, budget, opts, works[tree.name],
                     timer, progress)

  results = run_trees(trees, opts, steps, worker)
  if len(trees) > 1:
    print_trees_report(trees, results, timer.elapsed())
  return exit_status(results, any(report is None or report.nfail
                                  for report, _ in results))


def check_tree(tree, wrapper, wrapped, budget, opts, subdirs, timer,
               progress):
  """The check command on one tree; returns True if everything passed."""
  if opts.build:
    progress.running(tree.name, 'build')
    build(tree, budget.jobs_for(wrapped), opts)
    progress.finished(tree.name, 'build', True)
  variables = test_variables(wrapper, opts)

  # (step, make targets, log, summary file)
  if not subdirs:
    runs = [('check', ['check'], 'check.log', 'tests.sum')]
  else:
    runs = [(subdir, ['%s/tests' % subdir], subdir + '-check.log',
             '%s/subdir-tests.sum' % subdir) for subdir in subdirs]

  ok = True
  for step, targets, logname, sumfile in runs:
    if ABORT.is_set():
      break
    log = tree.logfile(logname)
    if opts.verbose:
      if subdirs:
        print('=== %s (full log: %s)' % (colorize(step, bcolors.BOLD), log))
      else:
        print('=== make check (full log: %s)' % log)
    progress.running(tree.name, step)
    status = run_make(tree, targets, variables, log=log,
                      jobs=budget.jobs_for(wrapped), stream=opts.stream)
    if opts.no_run:
      passed = report_build(status, log, timer)
    else:
      passed = report_sum(tree, sumfile, log, timer, opts.verbose)
    progress.finished(tree.name, step, passed)
    ok = ok and passed
  return ok

def print_trees_verdict(trees, results):
  failed = [tree.name for tree, (ok, _) in zip(trees, results) if not ok]
  print()
  if failed:
    print(colorize('%d of %d build trees failed: %s'
                   % (len(failed), len(trees), ', '.join(failed)),
                   bcolors.FAIL))
  else:
    print(colorize('all %d build trees passed' % len(trees),
                   bcolors.OKGREEN))

def cmd_check(opts):
  """Run the testsuite the way make does, and report the failures from the
  summary files it leaves behind."""
  trees = BuildTree.find_all(opts)
  # Started before anything runs, so that --build counts towards the total;
  # with several subdirectories or trees each summary shows the time up to
  # that point, the last one being the total for the command.
  timer = Timer()

  subdirs = reclaim_qemu_arguments(opts, trees, BuildTree.is_subdir) \
            + [name.rstrip('/') for name in opts.subdirs]
  for tree in trees:
    for name in subdirs:
      if not tree.is_subdir(name):
        raise Error("'%s' is not a subdirectory of build tree %s "
                    "(individual tests go to the test command)"
                    % (name, tree.path))

  steps = (['build'] if opts.build else []) + (subdirs or ['check'])

  def worker(tree, wrapper, wrapped, budget, progress):
    return check_tree(tree, wrapper, wrapped, budget, opts, subdirs, timer,
                      progress)

  results = run_trees(trees, opts, steps, worker)
  if len(trees) > 1:
    print_trees_verdict(trees, results)
  return exit_status(results, not all(ok for ok, _ in results))

def cmd_grep(opts):
  """git grep in the tree the cwd belongs to, minus the ChangeLog noise."""
  if not opts.args:
    raise Error('grep needs a pattern')
  return subprocess.call(['git', 'grep', '-n'] + opts.args
                         + ['--', GREP_EXCLUDE])

def git_output(args):
  try:
    return subprocess.check_output(['git'] + args, universal_newlines=True)
  except (subprocess.CalledProcessError, OSError) as exc:
    raise Error('git %s failed: %s' % (' '.join(args), exc))

def cmd_reviewed_by(opts):
  """Amend HEAD with a Reviewed-by trailer.

  The identity comes from the reviewer setting of ~/.glibc-tools.ini, which
  glibc-tools-config.py writes.
  """
  reviewer = opts.reviewer or read_config().get('reviewer', '')
  if not reviewer:
    raise Error('no reviewer configured, set one with '
                '\'glibc-tools-config.py -r "Name <mail>"\' or pass '
                '--reviewer')
  trailer = 'Reviewed-by: %s' % reviewer
  if trailer in git_output(['log', '-1', '--format=%B']).splitlines():
    print('%s is already on HEAD' % trailer)
    return 0
  return subprocess.call(['git', 'commit', '--amend', '--no-edit',
                          '--trailer', trailer])


class CappedHelpFormatter(argparse.HelpFormatter):
  def __init__(self, *args, **kwargs):
    kwargs.setdefault('width',
                      min(shutil.get_terminal_size().columns - 2, HELP_WIDTH))
    super().__init__(*args, **kwargs)

def get_parser():
  # Options shared by the two commands that run tests.
  common = argparse.ArgumentParser(add_help=False)
  common.add_argument('-C', dest='builddir', metavar='DIR', default='.',
                      help='Build tree to act on (default: the current '
                           'directory)')
  common.add_argument('--abi', dest='abis', metavar='NAME', action='append',
                      default=[],
                      help='Build tree to act on, as a directory name under '
                           'the builddir of ~/.glibc-tools.ini (as built by '
                           'glibc-tools.py).  May be given several times or '
                           'as a comma-separated list, and each name may be '
                           'a glob ("x86_64*") or the name of an ABI group '
                           'shared with glibc-tools.py ("linux"), to act on '
                           'each matching tree in turn')
  common.add_argument('-u', dest='suffix', default='',
                      help='Suffix appended to the directory names selected '
                           'with --abi, to act on trees built with the -u '
                           'of glibc-tools.py')
  common.add_argument('-j', dest='jobs', metavar='N', type=int,
                      help='make -j level of every tree (default: the '
                           'available cpus, shared evenly by the trees '
                           'running natively at the time, and %d for a tree '
                           'under --ssh/--qemu)' % WRAPPED_JOBS)
  common.add_argument('-p', dest='parallel', metavar='N', type=parallel_type,
                      default=None,
                      help='Number of --abi trees to act on at once, or '
                           '"auto" to fit as many as the cpus allow at the '
                           'make -j each tree gets (the default).  With 1 '
                           'the trees run in turn and print as they go; '
                           'otherwise a live line per tree shows its '
                           'progress and what each tree printed follows '
                           'once all are done')
  common.add_argument('-v', '--verbose', dest='verbose', action='store_true',
                      help='Print the full summary: every verdict, not just '
                           'the failures')
  common.add_argument('--stream', dest='stream', action='store_true',
                      help='Echo make output to the terminal as it runs; it '
                           'always goes to the log either way')
  common.add_argument('--build', dest='build', action='store_true',
                      help='Bring the tree up to date with make before '
                           'running anything')
  common.add_argument('--timeoutfactor', dest='timeoutfactor', metavar='N',
                      default='',
                      help='Set TIMEOUTFACTOR for the run, which slow '
                           'emulated or remote runs usually need')
  common.add_argument('--ssh', dest='ssh', metavar='[TREE=]MACHINE',
                      action='append', default=[],
                      help='Run the tests on MACHINE over ssh, through the '
                           'cross-test-ssh.sh of the source tree the build '
                           'tree was configured from.  With TREE= (a tree '
                           'name or a glob, as for --abi) only the matching '
                           'trees run there; may be given several times, the '
                           'last matching one winning.  Trees named by '
                           'neither --ssh nor --qemu fall back to the [ssh] '
                           'and [qemu] sections of ~/.glibc-tools.ini, which '
                           'glibc-tools-config.py --ssh/--qemu fill in; an '
                           'empty MACHINE ("x86_64*=") runs the tree '
                           'natively regardless')
  common.add_argument('--qemu', dest='qemu', metavar='[TREE=]COMMAND',
                      action='append', default=[], nargs='?',
                      const=QEMU_BINFMT,
                      help='Run the tests under qemu-user, with the same '
                           'TREE= selection as --ssh.  COMMAND is a bare '
                           'architecture ("aarch64", expanded to '
                           'qemu-<arch>-static then qemu-<arch>), a qemu '
                           'binary, or a whole command line with options for '
                           'qemu itself ("qemu-x86_64 -cpu Nehalem").  '
                           'Without COMMAND (or with "%s") the tests are '
                           'run as they are, the binfmt_misc of the kernel '
                           'being set up to hand the foreign binaries to '
                           'qemu-user; "--qemu math" still reads math as a '
                           'subdirectory' % QEMU_BINFMT)
  common.add_argument('--sysroot', dest='sysroot', metavar='DIR',
                      default=os.environ.get('GLIBC_QEMU_SYSROOT', ''),
                      help='Pass -L DIR to qemu (default: '
                           '$GLIBC_QEMU_SYSROOT)')

  parser = argparse.ArgumentParser(description=__doc__,
                                   formatter_class=CappedHelpFormatter)
  commands = parser.add_subparsers(dest='command', metavar='command',
                                   required=True)

  test = commands.add_parser('test', parents=[common],
                             formatter_class=CappedHelpFormatter,
                             help='Run tests and subdirectories, one verdict '
                                  'per test',
                             description=cmd_test.__doc__ + '''

Each argument is either an individual test ("nptl/tst-robust8", or just
"tst-robust8" to have it looked up in the build tree) or a whole subdirectory
("nptl"), which runs every test in it.  The tests are re-run even if they ran
before.  Only the failures are printed; what the tests and make wrote is left
in the logs.  Several --abi trees run in parallel (see -p), each where --ssh,
--qemu or ~/.glibc-tools.ini sends it, and the run ends with a cross-tree
summary that shows what every failed test wrote.''')
  # At least one is required, but a bare --qemu may have taken it (see
  # reclaim_qemu_arguments), so the command checks rather than argparse.
  test.add_argument('tests', nargs='*', metavar='test|subdir',
                    help='Tests and subdirectories to run')
  test.set_defaults(func=cmd_test)

  check = commands.add_parser('check', parents=[common],
                              formatter_class=CappedHelpFormatter,
                              help='Run make check, or the tests of the given '
                                   'subdirectories',
                              description=cmd_check.__doc__ + '''

With no argument this is "make check" over the whole tree; with subdirectory
arguments it is "make <subdir>/tests" for each of them.  Unlike the test
command this only runs what is not up to date, and reports the verdicts make
recorded rather than re-running anything.  Several --abi trees run in
parallel (see -p), each where --ssh, --qemu or ~/.glibc-tools.ini sends it.''')
  check.add_argument('--no-run', dest='no_run', action='store_true',
                     help='Only build the tests (run-built-tests=no) without '
                          'running them; reports whether the build succeeded '
                          'instead of test verdicts')
  check.add_argument('subdirs', nargs='*', metavar='subdir',
                     help='Subdirectories to check (default: the whole tree)')
  check.set_defaults(func=cmd_check)

  grep = commands.add_parser('grep', formatter_class=CappedHelpFormatter,
                             help='git grep -n, without the ChangeLog noise',
                             description=cmd_grep.__doc__)
  grep.add_argument('args', nargs=argparse.REMAINDER, metavar='args',
                    help='Arguments passed on to git grep')
  grep.set_defaults(func=cmd_grep)

  reviewed = commands.add_parser('reviewed-by',
                                 formatter_class=CappedHelpFormatter,
                                 help='Amend HEAD with a Reviewed-by trailer',
                                 description=cmd_reviewed_by.__doc__)
  reviewed.add_argument('--reviewer', dest='reviewer', metavar='IDENTITY',
                        default='',
                        help='Identity to credit, as "Name <mail>" (default: '
                             'the reviewer of ~/.glibc-tools.ini)')
  reviewed.set_defaults(func=cmd_reviewed_by)

  return parser

def main(argv):
  # The grep command hands its arguments to git grep, which has options of
  # its own (-i, -w, -A2, ...) that argparse would try to claim first.  Only
  # its own help is intercepted, everything else goes through untouched.
  if argv[:1] == ['grep'] and argv[1:2] not in (['-h'], ['--help']):
    opts = argparse.Namespace(func=cmd_grep, args=argv[1:])
  else:
    parser = get_parser()
    opts = parser.parse_args(argv)
    # The make output of several trees would only interleave.
    if getattr(opts, 'stream', False) \
       and (getattr(opts, 'parallel', None) or 1) > 1:
      parser.error('--stream needs the trees to run one at a time (-p 1)')

  global USE_COLOR
  USE_COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')

  try:
    return opts.func(opts)
  except Error as exc:
    print('error: %s' % exc, file=sys.stderr)
    return 2
  except KeyboardInterrupt:
    return 130

if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
