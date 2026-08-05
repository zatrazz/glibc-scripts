#! /usr/bin/env python3

"""Run the testsuite of one or more glibc build trees, natively, under
qemu-user, or on a remote machine over ssh, and wrap the git commands that
come up while working on glibc."""

import argparse
import collections
import configparser
import fnmatch
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HELP_WIDTH = 80

# Every log this script writes lands here, relative to the build tree.
LOGDIR = 'glibc-dev-logs'

# ChangeLog files are generated and match nearly everything, so keep them out
# of the way of the grep command.
GREP_EXCLUDE = ':!ChangeLog*/*'

# make -j level under --ssh or --qemu, where the bottleneck is the remote
# board or the emulator rather than the host.
WRAPPED_JOBS = 2


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

def read_config():
  """The [glibc-tools] section of ~/.glibc-tools.ini, empty if there is
  none.  Shared with glibc-tools.py, which is what fills it in."""
  config = configparser.RawConfigParser()
  config.read(os.path.expanduser('~/.glibc-tools.ini'))
  if 'glibc-tools' not in config.sections():
    return {}
  return dict(config['glibc-tools'])


def expand_abis(patterns, builddir):
  try:
    entries = sorted(os.listdir(builddir))
  except OSError:
    entries = []
  trees = [name for name in entries
           if os.path.isfile(os.path.join(builddir, name, 'config.make'))]
  names = {}
  for pattern in patterns:
    if any(c in pattern for c in '*?['):
      matched = fnmatch.filter(trees, pattern)
      if not matched:
        raise Error("no build tree under %s matches '%s'"
                    % (builddir, pattern))
      for name in matched:
        names[name] = True
    else:
      names[pattern] = True
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
    return [cls(os.path.join(builddir, name))
            for name in expand_abis(opts.abis, builddir)]

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
  variables = [('run-built-tests', 'yes')]
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

def jobs_for(opts):
  """make -j level: the whole machine locally, but a modest default under
  --ssh or --qemu."""
  if opts.jobs:
    return opts.jobs
  if opts.ssh or opts.qemu:
    return WRAPPED_JOBS
  return available_cpus()

def build(tree, opts):
  """Bring the tree up to date, the way "make" alone would.  Raises if make
  fails: there is no point running tests against a half-built tree."""
  log = tree.logfile('build.log')
  if opts.verbose:
    print('building (full log: %s)' % log)
  if run_make(tree, [], log=log, jobs=jobs_for(opts),
              stream=opts.stream) != 0:
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

def make_wrapper(tree, opts):
  """The make test-wrapper implied by --ssh/--qemu, None for a native run."""
  if opts.ssh:
    return cross_ssh_wrapper(tree, opts.ssh)
  if opts.qemu:
    return qemu_wrapper(tree, opts.qemu, opts.sysroot, opts.verbose)
  return None


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

def print_trees_report(reports, elapsed):
  print()
  print(colorize('results by build tree:', bcolors.BOLD))
  namew = max(len(report.tree.name) for report in reports) + 1
  for report in reports:
    line = '%-*s %d passed, %d failed, %d unsupported (of %d tests)' \
           % (namew, report.tree.name + ':', report.npass, report.nfail,
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

def run_one_test(tree, name, wrapper, opts, report):
  """Run one test with a make invocation of its own."""
  log = tree.logfile(name.replace('/', '_') + '.log')
  remove_files(tree.file(name + '.out'), tree.file(name + '.test-result'))

  variables = test_variables(wrapper, opts) + [('t', name)]
  # "make test" only fails when the test could not be built or run at all;
  # the PASS/FAIL verdict itself lands in <name>.test-result.
  status = run_make(tree, ['test'], variables, log=log, jobs=jobs_for(opts),
                    stream=opts.stream)
  result = read_test_result(tree, name)
  if result is None:
    result = '%s: %s' % ('BUILD-ERROR' if status else 'UNRESOLVED', name)
  report.record(result, name, log)

def run_subdir(tree, subdir, wrapper, opts, report):
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
           log=log, jobs=jobs_for(opts), keep_going=True,
           stream=opts.stream)

  for result in sorted(glob.glob(tree.file(subdir, '*.test-result'))):
    name = os.path.relpath(result, tree.path)[:-len('.test-result')]
    status = read_test_result(tree, name) or 'UNRESOLVED: %s' % name
    report.record(status, name, log, shared=True)

  # A test that failed to build leaves no .test-result behind.
  for name in failed_targets(log, subdir):
    if read_test_result(tree, name) is None:
      report.record('BUILD-ERROR: %s' % name, name, log, shared=True)

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


def cmd_test(opts):
  """Run individual tests and whole subdirectories, one verdict per test,
  in one or more build trees."""
  trees = BuildTree.find_all(opts)
  # Started before anything runs, so that --build counts towards the total;
  # with several trees each tree's summary shows the time up to that point,
  # the cross-tree summary at the end being the total.
  timer = Timer()

  # Classify the arguments against every tree up front, so that a directory
  # that is not part of one of the build trees is reported before anything is
  # run.  The resolution is per tree: the subdirectory a test is built in can
  # differ between configurations.
  works = []
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
    works.append(work)

  reports = []
  for tree, work in zip(trees, works):
    if len(trees) > 1:
      print('=== %s ===' % colorize(tree.name, bcolors.BOLD))
    wrapper = make_wrapper(tree, opts)
    if opts.build:
      build(tree, opts)
    report = Report(tree, opts.verbose, timer)
    for is_subdir, name in work:
      if is_subdir:
        run_subdir(tree, name, wrapper, opts, report)
      else:
        run_one_test(tree, name, wrapper, opts, report)
    report.finish()
    reports.append(report)

  if len(trees) > 1:
    print_trees_report(reports, timer.elapsed())
  return 1 if any(report.nfail for report in reports) else 0

def cmd_check(opts):
  """Run the testsuite the way make does, and report the failures from the
  summary files it leaves behind."""
  trees = BuildTree.find_all(opts)
  # Started before anything runs, so that --build counts towards the total;
  # with several subdirectories or trees each summary shows the time up to
  # that point, the last one being the total for the command.
  timer = Timer()

  subdirs = [name.rstrip('/') for name in opts.subdirs]
  for tree in trees:
    for name in subdirs:
      if not tree.is_subdir(name):
        raise Error("'%s' is not a subdirectory of build tree %s "
                    "(individual tests go to the test command)"
                    % (name, tree.path))

  jobs = jobs_for(opts)
  failed = False

  for tree in trees:
    if len(trees) > 1:
      print('=== %s ===' % colorize(tree.name, bcolors.BOLD))
    wrapper = make_wrapper(tree, opts)
    if opts.build:
      build(tree, opts)
    variables = test_variables(wrapper, opts)

    if not subdirs:
      log = tree.logfile('check.log')
      if opts.verbose:
        print('=== make check (full log: %s)' % log)
      run_make(tree, ['check'], variables, log=log, jobs=jobs,
               stream=opts.stream)
      if not report_sum(tree, 'tests.sum', log, timer, opts.verbose):
        failed = True
      continue

    for subdir in subdirs:
      log = tree.logfile(subdir + '-check.log')
      if opts.verbose:
        print('=== %s (full log: %s)' % (colorize(subdir, bcolors.BOLD), log))
      run_make(tree, ['%s/tests' % subdir], variables, log=log, jobs=jobs,
               stream=opts.stream)
      if not report_sum(tree, '%s/subdir-tests.sum' % subdir, log, timer,
                        opts.verbose):
        failed = True
  return 1 if failed else 0

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
                           'glibc-tools.py).  May be given several times and '
                           'may be a glob ("x86_64*"), to act on each '
                           'matching tree in turn')
  common.add_argument('-j', dest='jobs', metavar='N', type=int,
                      help='make -j level (default: the number of available '
                           'cpus, or %d under --ssh/--qemu)' % WRAPPED_JOBS)
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
  where = common.add_mutually_exclusive_group()
  where.add_argument('--ssh', dest='ssh', metavar='MACHINE', default='',
                     help='Run the tests on MACHINE over ssh, through the '
                          'cross-test-ssh.sh of the source tree this build '
                          'tree was configured from')
  where.add_argument('--qemu', dest='qemu', metavar='COMMAND', default='',
                     help='Run the tests under qemu-user.  COMMAND is a bare '
                          'architecture ("aarch64", expanded to '
                          'qemu-<arch>-static then qemu-<arch>), a qemu '
                          'binary, or a whole command line with options for '
                          'qemu itself ("qemu-x86_64 -cpu Nehalem")')
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
in the logs.  With several --abi trees the run ends with a cross-tree
summary that shows what every failed test wrote.''')
  test.add_argument('tests', nargs='+', metavar='test|subdir',
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
recorded rather than re-running anything.''')
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
    opts = get_parser().parse_args(argv)

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
