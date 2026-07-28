#! /usr/bin/env python3

import sys
import os
import re
import fnmatch
import copy
import shutil
import argparse
import subprocess
import math
import time
import platform
import configparser
import concurrent.futures
import threading

"""
glibc-tools.py is a script that configures, builds, and checks multiple
glibc builds using different compilers targeting different architectures.
"""

PATHS = {}

SUFFIX = ""

ACTIONS = (
  'configure',
  'copylibs',
  'make',
  'check',
  'check-parallel',
  'check-abi',
  'update-abi',
  'bench-build',
  'update-syscall-lists',
  'run-cmd',
  'list')

def read_config(srcdir, suffix):
  config = configparser.RawConfigParser()
  cfgpath = os.path.expanduser("~/.glibc-tools.ini")
  config.read(cfgpath)
  if 'glibc-tools' not in config.sections() \
     or 'srcdir' not in config['glibc-tools'] \
     or 'builddir' not in config['glibc-tools'] \
     or 'logsdir' not in config['glibc-tools'] \
     or 'compilers' not in config['glibc-tools']:
    print("error: config invalid, run glibc-tools-config.py")
    sys.exit(1)
  global PATHS, SUFFIX
  PATHS = dict(config['glibc-tools'])
  if srcdir:
    PATHS['srcdir'] = os.path.join (
      os.path.dirname(os.path.normpath(PATHS['srcdir'])), srcdir)
  if suffix:
    SUFFIX = '-' + suffix

# A trailing -gcc<version> on an ABI name selects a specific gcc version for
# that ABI (with the same meaning as the global --gccversion option): the
# compiler is looked up under "<compilers>/<version>/..." and the build
# directory and display name carry a "-gcc<version>" tag.  ABI names never
# contain "-gcc" otherwise, so splitting on the last occurrence is safe.
GCCVERSION_RE = re.compile(r'^(?P<base>.+)-gcc(?P<version>.+)$')

def split_gccversion(name, default):
  m = GCCVERSION_RE.match(name)
  if m:
    return m.group('base'), m.group('version')
  return name, default

def gccversion_tag(gccversion):
  return '-gcc' + gccversion if gccversion else ''

def canonical_name(base_name, gccversion):
  return base_name + gccversion_tag(gccversion)

def expand_configs(tokens):
  """Expand the requested config tokens, resolving SPECIAL_LISTS names.

  A -gcc<version> tag on a special-list name (e.g. "sparc-gcc12") is applied
  to every member of the list, so "sparc-gcc12" builds the whole sparc list
  with gcc 12.  Individual ABI names are passed through unchanged (their tag,
  if any, is handled later during resolution)."""
  result = []
  for token in tokens:
    base, gccversion = split_gccversion(token, '')
    members = SPECIAL_LISTS.get(base)
    if members is None:
      result.append(token)
    else:
      result.extend(canonical_name(m, gccversion) for m in members)
  return result

def match_configs(patterns, known):
  known = sorted(known)
  result = []
  errors = []
  for pattern in patterns:
    base, gccversion = split_gccversion(pattern, '')
    matches = fnmatch.filter(known, base)
    if not matches:
      errors.append("error: no configuration matches '%s'" % pattern)
      continue
    result.extend(canonical_name(m, gccversion) for m in matches)
  return result, errors

def remove_dirs(*args):
  """Remove directories and their contents if they exist."""
  for dir in args:
    shutil.rmtree(dir, ignore_errors=True)

def remove_recreate_dirs(*args):
  """Remove directories if they exist, and create them as empty."""
  remove_dirs(*args)
  for dir in args:
    os.makedirs(dir, exist_ok=True)

def create_file(filename):
  os.makedirs(os.path.dirname(filename), exist_ok=True)
  return open(filename, "w")

def build_dir(abi):
  # abi is the canonical name, so it already carries any -gcc<version> tag.
  return PATHS['builddir'] + '/' + abi + SUFFIX

PLATFORM_MAP = { "ppc64le" : "powerpc64le" }

def build_triplet():
  platstr = platform.machine()
  if platstr in PLATFORM_MAP:
    platstr = PLATFORM_MAP[platstr]
  return platstr + "-linux-gnu"

class Config:
  """A configuration for building a compiler and associated libraries."""

  def __init__(self, ctx, arch, os_name, variant=None, glibcs=None,
               extra_glibcs=None):
    """Initialize a Config object."""
    self.ctx = ctx
    self.arch = arch
    self.os = os_name
    self.variant = variant
    if variant is None:
      self.name = '%s-%s' % (arch, os_name)
    else:
      self.name = '%s-%s-%s' % (arch, os_name, variant)
    self.triplet = '%s-glibc-%s' % (arch, os_name)
    if glibcs is None:
      glibcs = [{'variant': variant}]
    if extra_glibcs is None:
      extra_glibcs = []
    glibcs = [Glibc(self, **g) for g in glibcs]
    extra_glibcs = [Glibc(self, **g) for g in extra_glibcs]
    self.all_glibcs = glibcs + extra_glibcs
    self.glibcs = glibcs


class bcolors:
  HEADER = '\033[95m'
  OKBLUE = '\033[94m'
  OKCYAN = '\033[36m'
  OKGREEN = '\033[92m'
  WARNING = '\033[93m'
  FAIL = '\033[91m'
  ENDC = '\033[0m'
  BOLD = '\033[1m'
  UNDERLINE = '\033[4m'

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

def print_timings(steps, timings, total_elapsed):
  """Print a per-step, per-ABI and total wall-clock timing summary.

  timings maps each ABI display name to a {step: elapsed_seconds} dict; the
  per-ABI total is the sum of its steps (steps run serially within an ABI)
  while total_elapsed is the wall-clock time of the whole invocation (ABIs
  run concurrently, so it is typically far less than the sum of the rows).
  """
  cols = list(steps) + ['total']
  rows = []
  for name in sorted(timings):
    times = timings[name]
    cells = [format_duration(times[s]) if s in times else '-' for s in steps]
    cells.append(format_duration(sum(times.values())))
    rows.append((name, cells))
  if not rows:
    return
  namew = max(len(n) for n, _ in rows)
  colw = [max(len(cols[i]), max(len(cells[i]) for _, cells in rows))
          for i in range(len(cols))]

  def fmt_row(name_field, cells):
    return name_field + '  ' + '  '.join('%*s' % (colw[i], cells[i])
                                         for i in range(len(cols)))

  print(bcolors.BOLD + 'Timings:' + bcolors.ENDC)
  print(fmt_row(' ' * namew, cols))
  for name, cells in rows:
    name_field = bcolors.BOLD + bcolors.OKCYAN + ('%-*s' % (namew, name)) \
                 + bcolors.ENDC
    print(fmt_row(name_field, cells))
  print('%stotal:%s %s' % (bcolors.BOLD, bcolors.ENDC,
                           format_duration(total_elapsed)))

class Reporter:
  """Live one-line-per-ABI progress display.

  Every ABI owns a fixed line, listed in alphabetical order and updated in
  place as the ABI advances through its build steps:

    <abi>:            | <current action> | <PASS|FAIL> | <time>

  The ABI name is shown in bold cyan and the action in yellow; the action
  column tracks the step being run, the status column is empty while the
  step is in progress and shows the step's result once it finishes (in blue
  for a success and red for a failure), and the time column shows how long
  the finished step took.

  When the output is not a terminal (or has more ABIs than fit on it), the
  in-place redraw is dropped and the line is printed whenever a step
  finishes instead.
  """

  _ABI_COLOR = bcolors.BOLD + bcolors.OKCYAN
  _ACT_COLOR = bcolors.WARNING

  # status -> (label, color)
  _STATUS = {
    'run':  ('', ''),
    'pass': ('PASS', bcolors.OKBLUE),
    'fail': ('FAIL', bcolors.FAIL),
  }

  def __init__(self, entries, steps):
    # entries: list of (abi, display name); steps: ordered step names.
    self.order = sorted((name for _, name in entries))
    self.abi_name = dict(entries)
    self.namew = max((len(n) for n in self.order), default=0) + 1
    self.actw = max((len(s) for s in steps), default=0)
    self.state = {name: ('', 'run', None) for name in self.order}
    self.lock = threading.Lock()

    rows = shutil.get_terminal_size((0, 0)).lines
    self.live = sys.stdout.isatty() and 0 < len(self.order) < rows
    if self.live:
      sys.stdout.write(''.join(self._line(n) + '\n' for n in self.order))
      sys.stdout.flush()

  @staticmethod
  def _paint(text, color):
    return color + text + bcolors.ENDC if color else text

  def _line(self, name):
    action, status, elapsed = self.state[name]
    label, scolor = self._STATUS[status]
    abi = self._paint('%-*s' % (self.namew, name + ':'), self._ABI_COLOR)
    act = self._paint('%-*s' % (self.actw, action), self._ACT_COLOR)
    stat = self._paint('%-4s' % label, scolor)
    tstr = format_duration(elapsed) if elapsed is not None else ''
    return '%s | %s | %s | %s' % (abi, act, stat, tstr)

  def _redraw(self):
    # Move to the top of the block and rewrite every line in place.
    sys.stdout.write('\033[%dA' % len(self.order))
    for name in self.order:
      sys.stdout.write('\r\033[K' + self._line(name) + '\n')
    sys.stdout.flush()

  def running(self, abi, step):
    name = self.abi_name[abi]
    with self.lock:
      self.state[name] = (step, 'run', None)
      if self.live:
        self._redraw()

  def finished(self, abi, step, ok, elapsed=None):
    name = self.abi_name[abi]
    with self.lock:
      self.state[name] = (step, 'pass' if ok else 'fail', elapsed)
      if self.live:
        self._redraw()
      else:
        print(self._line(name))

  def failed(self, abi, step='<exception>'):
    self.finished(abi, step, False)


def create_outfile(strtype, abi, action, suffix):
  filename = PATHS[strtype] + '/' + abi
  filename += SUFFIX
  return create_file(filename + '_' + action + suffix)

def run_cmd(abi, action, cmd):
  builddir = build_dir (abi)
  with create_outfile('logsdir', abi, action, '.out') as outfile, \
       create_outfile('logsdir', abi, action, '.err') as errfile:
    proc = subprocess.run(cmd, cwd=builddir, stdout=outfile, stderr=errfile)
  return proc.returncode

# Actions that run the test suite and leave a summary in tests.sum.
TEST_ACTIONS = ('check', 'check-parallel')

def failing_tests(abi):
  # The failing tests of a "make check" run are the FAIL: lines of the test
  # summary left in the build directory.
  sumfile = build_dir (abi) + '/tests.sum'
  try:
    with open(sumfile) as f:
      return [line.rstrip('\n') for line in f if line.startswith('FAIL:')]
  except OSError:
    return []


class Context:
  def __init__ (self, opts):
    self.parallelize = opts.parallelize[0]
    self.build_jobs = opts.parallelize[1]

    # Total make-job budget for the whole invocation.  current_jobs() hands
    # each build an even share of it based on how many builds are running, so
    # the make -j of a build grows into the cores freed as other builds
    # finish instead of staying pinned at build_jobs; with the pool full the
    # share is exactly build_jobs and the last build left running gets the
    # whole budget.
    self.budget = self.parallelize * self.build_jobs
    self.active = 0
    self.active_lock = threading.Lock()

    self.run_built_tests = 'yes' if opts.run_built_tests else 'no'

    self.timeoutfactor = opts.timeoutfactor

    self.extra_config_opts = []
    self.extra_config_opts.append("--enable-stack-protector={}".format(opts.enable_stackprot))
    if opts.disable_pie:
      self.extra_config_opts.append("--disable-default-pie")
    self.extra_config_opts.append("--enable-bind-now={}".format(opts.enable_bind_now))
    self.extra_config_opts.append("--enable-profile={}".format(opts.enable_profile))
    self.extra_config_opts.append("--enable-fortify-source={}".format(opts.enable_fortify))
    if not opts.enable_multiarch:
      self.extra_config_opts.append("--disable-multi-arch")
    if opts.disable_werror:
      self.extra_config_opts.append("--disable-werror")
    if opts.with_kernel:
      self.extra_config_opts.append("--enable-kernel={}".format(opts.with_kernel))
    if opts.hardcoded:
      self.extra_config_opts.append("--enable-hardcoded-path-in-tests")
    if opts.enable_sframe:
      self.extra_config_opts.append("--enable-sframe")
    if opts.cflags:
      self.extra_config_opts.append("CFLAGS={}".format(opts.cflags))
      self.extra_config_opts.append("CXXFLAGS={}".format(opts.cflags))

    if opts.test_cc:
      self.extra_config_opts.append("TEST_CC={}".format(opts.test_cc))
    if opts.test_cxx:
      self.extra_config_opts.append("TEST_CXX={}".format(opts.test_cxx))

    self.keep = opts.keep
    self.status_log_list = []
    self.glibc_configs = {}
    self.configs = {}
    # Per-run map (canonical name -> Glibc), filled in by run/resolve_configs.
    self.resolved = {}
    self.add_all_configs()

  def build_started(self):
    with self.active_lock:
      self.active += 1

  def build_finished(self):
    with self.active_lock:
      self.active -= 1

  def current_jobs(self):
    # The CPU budget is split evenly across the builds currently running, so
    # the value is build_jobs while the pool is full and rises as builds
    # finish, up to the whole budget for the last build left running.
    with self.active_lock:
      active = self.active
    return max(1, math.ceil(self.budget / max(1, active)))

  # Keyed by canonical ABI name; self.resolved is the per-run map from that
  # name to its Glibc instance (which knows its selected gcc version).
  CMD_MAP = {
    "copylibs":
      (lambda self, abi : self.resolved[abi].copylibs(),
       []),
    "configure":
      (lambda self, abi : self.resolved[abi].configure(self.extra_config_opts),
       ["configure", "copylibs"]),
    "update-syscall-lists":
      (lambda self, abi : self.resolved[abi].update_syscall_lists(),
       ["configure", "copylibs", "update-syscall-lists"]),
    "make":
      (lambda self, abi : self.resolved[abi].build(),
       ["configure", "copylibs", "make"]),
    "check":
      (lambda self, abi : self.resolved[abi].check(),
       ["configure", "copylibs", "make", "check"]),
    "check-parallel":
      (lambda self, abi : self.resolved[abi].check_parallel(),
       ["configure", "copylibs", "make", "check-parallel"]),
    "check-abi":
      (lambda self, abi : self.resolved[abi].check_abi(),
       ["configure", "make", "check-abi"]),
    "update-abi":
      (lambda self, abi : self.resolved[abi].update_abi(),
       ["configure", "make", "update-abi"]),
    "bench-build":
      (lambda self, abi : self.resolved[abi].bench_build(),
       ["configure", "copylibs", "make", "bench-build"]),
    "run-cmd":
      (lambda self, abi : self.resolved[abi].run_cmd(),
       ["configure", "make", "run-cmd"]),
  }

  def resolve_configs(self, requests, default_gccversion, check_compiler):
    """Each request may carry a trailing -gcc<version> selecting a gcc version
    for that ABI (overriding default_gccversion, which comes from the global
    --gccversion).  Returns (resolved, errors) where resolved maps each
    canonical name to its Glibc instance (a clone of the base config with its
    gccversion set) and errors is a list of human-readable messages for
    unknown ABIs and, when check_compiler is set, missing toolchains.  The
    order of requests is preserved and duplicates collapse."""
    resolved = {}
    errors = []
    for request in requests:
      base_name, gccversion = split_gccversion(request, default_gccversion)
      base = self.glibc_configs.get(base_name)
      if base is None:
        errors.append("error: unknown configuration '%s'" % request)
        continue
      glibc = copy.copy(base)
      glibc.gccversion = gccversion
      glibc.name = canonical_name(base_name, gccversion)
      if check_compiler and not os.path.exists(glibc.gcc_path()):
        errors.append("error: %s: gcc%s not found: %s"
                      % (glibc.name, gccversion or ' (default)',
                         os.path.normpath(glibc.gcc_path())))
        continue
      resolved[glibc.name] = glibc
    return resolved, errors

  def run(self, opts, glibcs):
    if not glibcs:
      glibcs = sorted(self.glibc_configs.keys())

    action = opts.action

    def abiname(abi):
      return '{}{}'.format(abi,
                           '-{}'.format(opts.suffix) if opts.suffix else '')

    # Every build action configures from the glibc source tree, so its
    # configure script is a global prerequisite (the tree is the same for all
    # ABIs).  Check it once, before any build directory is touched, so a bad
    # -s or a stale configured path gives a clear message rather than a cryptic
    # "No such file or directory" once configure runs -- and does not wipe a
    # build directory on the way there.
    if action != "list":
      configure = os.path.join(PATHS["srcdir"], "configure")
      if not os.path.isfile(configure):
        print("error: no glibc source tree at %s: %s not found "
              "(set it with -s or glibc-tools-config.py)"
              % (PATHS["srcdir"], configure))
        return 1

    # The list action takes its arguments as globs rather than as exact ABI
    # names, which is how a triple is searched for when only part of its
    # spelling is known.
    glob_errors = []
    if action == "list":
      glibcs, glob_errors = match_configs(glibcs, self.glibc_configs.keys())

    # Resolve requested ABI names (which may carry a -gcc<version> tag) into
    # per-run Glibc instances, reporting unknown ABIs and missing toolchains
    # up front instead of letting them surface as opaque build failures.  The
    # list action only needs the names, so it skips the toolchain check.
    resolved, config_errors = self.resolve_configs(glibcs, opts.gccversion,
                                                    action != "list")
    config_errors = glob_errors + config_errors
    self.resolved = resolved
    glibcs = list(resolved.keys())

    # -k reuses each build directory in place instead of wiping and recreating
    # it, so a missing directory means there is nothing to keep.  Report it
    # clearly here rather than letting the build command fail later with a bare
    # "No such file or directory" when it cannot chdir into the directory (and,
    # for actions that make, silently starting a full build from scratch).
    if self.keep and action != "list":
      present = []
      for abi in glibcs:
        if os.path.isdir(build_dir(abi)):
          present.append(abi)
        else:
          config_errors.append(
            "error: %s: no build directory to keep, run without -k first: %s"
            % (abiname(abi), os.path.normpath(build_dir(abi))))
      glibcs = present

    for msg in config_errors:
      print(msg)

    if action == "list":
      self.list_configs(glibcs)
      return len(config_errors)

    if not glibcs:
      return len(config_errors) or 1

    # The steps required to reach the requested action, in canonical order.
    needed = set(self.CMD_MAP[action][1])
    steps = [act for act in ACTIONS if act in needed]

    if not self.keep:
      for abi in glibcs:
        remove_recreate_dirs(build_dir (abi))

    reporter = Reporter([(abi, abiname(abi)) for abi in glibcs], steps)

    # Set on Ctrl-C so in-flight builds stop after their current step instead
    # of marching through the remaining ones.
    abort = threading.Event()

    # Collects the failing tests of any ABI whose check step fails.
    test_fails = {}

    # Per-ABI {step: elapsed_seconds}, filled in as each step finishes.  Each
    # run_abi thread only touches its own entry, so no locking is needed.
    timings = {}

    # Run the whole step chain for a single ABI, stopping at the first failure
    # so a broken configure does not spawn a doomed make.  Pipelining per ABI
    # (rather than one action across all ABIs with a barrier between actions)
    # lets a build in its parallel make phase fill the cores left idle by
    # another build's serial configure phase, keeping the CPUs busy without
    # raising the peak number of concurrent jobs.  The reporter shows each
    # ABI's progress live as the steps complete.  Each ABI counts as an active
    # build for its whole lifetime so current_jobs() can spread the CPU budget
    # over however many builds are still running.
    def run_abi(abi):
      abi_times = {}
      timings[abiname(abi)] = abi_times
      self.build_started()
      try:
        for act in steps:
          if abort.is_set():
            return 1
          reporter.running(abi, act)
          cmd = self.CMD_MAP[act][0](self, abi)
          start = time.monotonic()
          resultcode = run_cmd(abi, act, cmd)
          elapsed = time.monotonic() - start
          abi_times[act] = elapsed
          if abort.is_set():
            return 1
          reporter.finished(abi, act, resultcode == 0, elapsed)
          if resultcode != 0:
            if act in TEST_ACTIONS:
              tests = failing_tests(abi)
              if tests:
                test_fails[abiname(abi)] = tests
            return 1
        return 0
      finally:
        self.build_finished()

    failures = 0
    errors = []
    start_time = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallelize) \
         as executor:
      future_to_abi = {executor.submit(run_abi, abi) : abi for abi in glibcs}
      try:
        for future in concurrent.futures.as_completed(future_to_abi):
          abi = future_to_abi[future]
          try:
            failures += future.result()
          except Exception as exc:
            reporter.failed(abi)
            errors.append('%r generated an exception: %s' % (abiname(abi), exc))
            failures += 1
      except KeyboardInterrupt:
        # Drop the queued builds and stop the running ones; the executor's
        # shutdown then only waits for the already-signalled builds to exit.
        abort.set()
        for future in future_to_abi:
          future.cancel()
        raise
    total_elapsed = time.monotonic() - start_time

    # Printed after the live block so the messages land below it, not amid it.
    for msg in errors:
      print(msg)
    for name in sorted(test_fails):
      tests = test_fails[name]
      print('%s%s: %d test failure%s%s'
            % (bcolors.FAIL, name, len(tests), '' if len(tests) == 1 else 's',
               bcolors.ENDC))
      for line in tests:
        print('  ' + line)

    print_timings(steps, timings, total_elapsed)

    # Configs that could not be resolved (unknown ABI or missing toolchain)
    # were never built but must still make the invocation fail.
    return failures + len(config_errors)


  def add_config(self, **args):
    """Add an individual build configuration."""
    cfg = Config(self, **args)
    if cfg.name in self.configs:
      print('error: duplicate config %s' % cfg.name)
      sys.exit(1)
    self.configs[cfg.name] = cfg
    for c in cfg.all_glibcs:
      if c.name in self.glibc_configs:
        print('error: duplicate glibc config %s' % c.name)
        sys.exit(1)
      self.glibc_configs[c.name] = c


  def add_all_configs(self):
    """Add all known glibc build configurations."""
    self.add_config(arch='aarch64',
                    os_name='linux-gnu',
                    glibcs=[{'cfg' : ['--enable-memory-tagging']},
                            {'variant': 'disable-multi-arch',
                             'cfg' : ["--disable-multi-arch"]}])
    self.add_config(arch='aarch64_be',
                    os_name='linux-gnu')
    self.add_config(arch='arc',
                    os_name='linux-gnu')
    self.add_config(arch='arc',
                    os_name='linux-gnuhf')
    self.add_config(arch='arceb',
                    os_name='linux-gnu')
    self.add_config(arch='alpha',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'ev6', 'ccopts': '-mcpu=ev6'},
                            {'variant': 'ev67', 'ccopts': '-mcpu=ev67'}])
    self.add_config(arch='arm',
                    os_name='linux-gnueabi',
                    glibcs=[{},
                            {'arch' : 'armv7a', 'ccopts': '-march=armv7-a'}])
    self.add_config(arch='arm',
                    os_name='linux-gnueabihf',
                    glibcs=[{},
                            {'arch' : 'armv5',
                             'ccopts': '-march=armv5te -mfpu=vfpv3'},
                            {'arch' : 'armv6',
                             'ccopts': '-march=armv6 -mfpu=vfpv3'},
                            {'arch' : 'armv6t2',
                             'ccopts': '-march=armv6t2 -mfpu=vfpv3'},
                            {'arch' : 'armv7a',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3'},
                            {'arch' : 'armv7a',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3',
                             'variant' : 'disable-multi-arch',
                             'cfg'  : ["--disable-multi-arch"]},
                            {'arch' : 'armv7a-thumb',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3 -mthumb'},
                            {'arch' : 'armv7a-neon',
                             'ccopts': '-march=armv7-a -mfpu=neon'},
                            {'arch' : 'armv7a-neonhard',
                             'ccopts': '-march=armv7-a -mfpu=neon -mfloat-abi=hard'},
                            {'arch' : 'armv7a-neonvfpv4',
                             'ccopts': '-march=armv7-a -mfpu=neon-vfpv4'},
                            {'arch' : 'armv7a-neonhardvpfv4',
                             'ccopts': '-march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard'}])
    self.add_config(arch='armeb',
                    os_name='linux-gnueabihf',
                    glibcs=[{},
                            {'arch' : 'armeb-v5',
                             'ccopts': '-march=armv5te -mfpu=vfpv3'},
                            {'arch' : 'armeb-v6',
                             'ccopts': '-march=armv6 -mfpu=vfpv3'},
                            {'arch' : 'armeb-v6t2',
                             'ccopts': '-march=armv6t2 -mfpu=vfpv3'},
                            {'arch' : 'armeb-v7a',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3'},
                            {'arch' : 'armeb-v7a',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3',
                             'variant' : 'disable-multi-arch',
                             'cfg'  : ["--disable-multi-arch"]},
                            {'arch' : 'armeb-v7a-neon',
                             'ccopts': '-march=armv7-a -mfpu=neon'},
                            {'arch' : 'armeb-v7neonhard',
                             'ccopts': '-march=armv7-a -mfpu=neon -mfloat-abi=hard'}])
    self.add_config(arch='hppa',
                    os_name='linux-gnu')
    self.add_config(arch='ia64',
                    os_name='linux-gnu')
    self.add_config(arch='i686',
                    os_name='gnu')
    self.add_config(arch='m68k',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'm68030', 'ccopts': '-mcpu=68030'},
                            {'variant': 'm68040', 'ccopts': '-mcpu=68040'},
                            {'variant': 'm68060', 'ccopts': '-mcpu=68060'}])
    self.add_config(arch='m68k',
                    os_name='linux-gnu',
                    variant='coldfire')
    self.add_config(arch='m68k',
                    os_name='linux-gnu',
                    variant='coldfire-soft')
    self.add_config(arch='loongarch32',
                    os_name='linux-gnu')
    self.add_config(arch='loongarch32',
                    os_name='linux-gnusf')
    self.add_config(arch='loongarch64',
                    os_name='linux-gnuf64')
    self.add_config(arch='loongarch64',
                    os_name='linux-gnu',
                    variant='lp64s')
    self.add_config(arch='microblaze',
                    os_name='linux-gnu')
    self.add_config(arch='microblazeel',
                    os_name='linux-gnu')
    self.add_config(arch='mips64',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'mips64-n32',
                             'ccopts': '-mabi=n32'},
                            {'arch': 'mips',
                             'ccopts': '-mabi=32'},
                            {'arch': 'mips',
                             'variant' : 'mips16',
                             'ccopts': '-mabi=32 -mips16'},
                            {'arch': 'mips64',
                             'ccopts': '-mabi=64'}])
    self.add_config(arch='mips64',
                    os_name='linux-gnu',
                    variant='soft',
                    glibcs=[{'arch': 'mips', 'variant' : 'soft', 'ccopts': '-mabi=32'},
                            {'arch': 'mips64', 'variant' : 'soft', 'ccopts' :'-mabi=64'},
                            {'arch': 'mips64-n32', 'variant' : 'soft', 'ccopts' :'-mabi=n32'}])
    self.add_config(arch='mips64el',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'mips64el-n32',
                             'ccopts' : '-mabi=n32'},
                            {'arch': 'mipsel',
                             'ccopts': '-mabi=32'},
                            {'arch': 'mipsel',
                             'variant' : 'mips16',
                             'ccopts': '-mabi=32 -mips16'},
                            {'arch': 'mips64el',
                             'ccopts': '-mabi=64'}])
    self.add_config(arch='mipsisa64r5el',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'mipsisa64r5el',
                             'ccopts': '-mabi=64'},
                            {'arch': 'mipsisa64r5el',
                             'variant': "micromips",
                             'ccopts': '-mabi=64 -mmicromips'},
                            {'arch': 'mipsisa32r5el',
                             'ccopts': '-mabi=32'},
                            {'arch': 'mipsisa32r5el',
                             'variant': "micromips",
                             'ccopts': '-mabi=32 -mmicromips'},
                            {'arch': 'mipsisa64r5el-n32',
                             'ccopts': '-mabi=n32'},
                            {'arch': 'mipsisa64r5el-n32',
                             'variant': 'micromips',
                             'ccopts': '-mabi=n32 -mmicromips'}])
    self.add_config(arch='mipsisa64r6el',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'mipsisa64r6el'},
                            {'arch': 'mipsisa32r6el',
                             'ccopts': '-mabi=32'},
                            {'arch': 'mipsisa64r6el-n64',
                             'ccopts': '-mabi=64'}])
    self.add_config(arch='nios2',
                    os_name='linux-gnu')
    self.add_config(arch='or1k',
                    os_name='linux-gnu',
                    glibcs=[{'variant': 'soft'},
                            {'variant': 'hard',  'ccopts': '-mhard-float'}])
    self.add_config(arch='powerpc',
                    os_name='linux-gnu',
                    variant='soft')
    self.add_config(arch='powerpc',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'power4',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4"]},
                            {'variant': 'power5',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5"]},
                            {'variant': 'power5+', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+"]},
                            {'variant': 'power6',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6"]},
                            {'variant': 'power6x', 'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6"]},
                            {'variant': 'power7',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7"]},
                            {'variant': 'power8',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8"]},
                            {'variant': 'power9',  'ccopts': '-mcpu=power9',  'cfg' : ["--with-cpu=power9"]},
                            {'variant': 'power10',  'ccopts': '-mcpu=power10',  'cfg' : ["--with-cpu=power10"]},
                            {'variant': 'power4-disable-multi-arch',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4",  "--disable-multi-arch"]},
                            {'variant': 'power5-disable-multi-arch',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5",  "--disable-multi-arch"]},
                            {'variant': 'power5+-disable-multi-arch', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+", "--disable-multi-arch"]},
                            {'variant': 'power6-disable-multi-arch',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6",  "--disable-multi-arch"]},
                            {'variant': 'power6x-disable-multi-arch', 'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6",  "--disable-multi-arch"]},
                            {'variant': 'power7-disable-multi-arch',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7",  "--disable-multi-arch"]},
                            {'variant': 'power8-disable-multi-arch',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8",  "--disable-multi-arch"]}])
    self.add_config(arch='powerpc64',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'power4',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4"]},
                            {'variant': 'power5',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5"]},
                            {'variant': 'power5+', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+"]},
                            {'variant': 'power6',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6"]},
                            {'variant': 'power6x', 'ccopts': '-mcpu=power6x', 'cfg' : ["--with-cpu=power6x"]},
                            {'variant': 'power7',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7"]},
                            {'variant': 'power8',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8"]},
                            {'variant': 'power4-disable-multi-arch',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4",  "--disable-multi-arch"]},
                            {'variant': 'power5-disable-multi-arch',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5",  "--disable-multi-arch"]},
                            {'variant': 'power5+-disable-multi-arch', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+", "--disable-multi-arch"]},
                            {'variant': 'power6-disable-multi-arch',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6",  "--disable-multi-arch"]},
                            {'variant': 'power6x-disable-multi-arch', 'ccopts': '-mcpu=power6x', 'cfg' : ["--with-cpu=power6x", "--disable-multi-arch"]},
                            {'variant': 'power7-disable-multi-arch',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7",  "--disable-multi-arch"]},
                            {'variant': 'power8-disable-multi-arch',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8",  "--disable-multi-arch"]},
                            {'variant': 'disable-multi-arch', 'cfg' : ["--disable-multi-arch"]}])
    self.add_config(arch='powerpc64le',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'power8', 'ccopts': '-mcpu=power8', 'cfg' : ["--with-cpu=power8"]},
                            {'variant': 'power9', 'ccopts': '-mcpu=power9', 'cfg' : ["--with-cpu=power9"]},
                            {'variant': 'power10', 'ccopts': '-mcpu=power10', 'cfg' : ["--with-cpu=power10"]},
                            {'variant': 'power7-disable-multi-arch', 'ccopts': '-mcpu=power7', 'cfg' : ["--with-cpu=power7", "--disable-multi-arch"]},
                            {'variant': 'power8-disable-multi-arch', 'ccopts': '-mcpu=power8', 'cfg' : ["--with-cpu=power8", "--disable-multi-arch"]},
                            {'variant': 'power9-disable-multi-arch', 'ccopts': '-mcpu=power9', 'cfg' : ["--with-cpu=power9", "--disable-multi-arch"]},
                            {'variant': 'power10-disable-multi-arch', 'ccopts': '-mcpu=power10', 'cfg' : ["--with-cpu=power10", "--disable-multi-arch"]},
                            {'variant': 'disable-multi-arch', 'cfg' : ["--disable-multi-arch"]}])
    self.add_config(arch='riscv32',
                    os_name='linux-gnu',
                    variant='rv32imac-ilp32')
    self.add_config(arch='riscv32',
                    os_name='linux-gnu',
                    variant='rv32imafdc-ilp32')
    self.add_config(arch='riscv32',
                    os_name='linux-gnu',
                    variant='rv32imafdc-ilp32d')
    self.add_config(arch='riscv64',
                    os_name='linux-gnu',
                    variant='rv64imac-lp64')
    self.add_config(arch='riscv64',
                    os_name='linux-gnu',
                    variant='rv64imafdc-lp64')
    self.add_config(arch='riscv64',
                    os_name='linux-gnu',
                    variant='rv64imafdc-lp64d')
    self.add_config(arch='s390x',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant' : 'disable-multi-arch', 'cfg' : ['--disable-multi-arch']},
                            {'variant': 'z900', 'ccopts': '-march=z900'}, # arch5
                            {'variant': 'z10', 'ccopts': '-march=z10'},   # arch8
                            {'variant': 'z196', 'ccopts': '-march=z196'}, # arch9
                            {'variant': 'z13', 'ccopts': '-march=z13'},   # arch?
                            {'arch'   : 's390', 'ccopts': '-m31'},
                            {'arch'   : 's390', 'variant' : 'disable-multi-arch', 'cfg' : ['--disable-multi-arch'], 'ccopts' : '-m31'}])
    self.add_config(arch='csky',
                    os_name='linux-gnuabiv2',
                    variant='soft')
    self.add_config(arch='csky',
                    os_name='linux-gnuabiv2')
    self.add_config(arch='sh3',
                    os_name='linux-gnu')
    self.add_config(arch='sh3eb',
                    os_name='linux-gnu')
    self.add_config(arch='sh4',
                    os_name='linux-gnu')
    self.add_config(arch='sh4eb',
                    os_name='linux-gnu')
    self.add_config(arch='sh4',
                    os_name='linux-gnu',
                    variant='soft')
    self.add_config(arch='sh4eb',
                    os_name='linux-gnu',
                    variant='soft')
    self.add_config(arch='sparc64',
                    os_name='linux-gnu',
                    glibcs=[{'ccopts' : "-mcpu=niagara"},
                            {'arch': 'sparc',
                             'ccopts': '-m32 -mlong-double-128 -mcpu=leon3'},
                            {'arch': 'sparcv8',
                             'ccopts': '-m32 -mlong-double-128 -mcpu=leon3'},
                            {'arch': 'sparcv9',
                             'ccopts': '-m32 -mlong-double-128 -mcpu=v9'}],
                    extra_glibcs=[{'variant': 'disable-multi-arch',
                                   'cfg': ['--disable-multi-arch']},
                                  {'variant': 'disable-multi-arch',
                                   'arch': 'sparcv9',
                                   'ccopts': '-m32 -mlong-double-128 -mcpu=v9',
                                   'cfg': ['--disable-multi-arch']}])
    self.add_config(arch='x86_64',
                    os_name='gnu')
    self.add_config(arch='x86_64',
                    os_name='linux-gnu',
                    glibcs=[{'cfg': [], 'ccopts' : '-march=x86-64'},
                            {'variant': 'x32', 'ccopts': '-mx32'},
                            {'arch': 'i686', 'ccopts': '-m32 -march=i686'},
                            {'variant': 'v2', 'ccopts' : '-march=x86-64-v2'},
                            {'variant': 'v3', 'ccopts' : '-march=x86-64-v3'},
                            {'variant': 'v4', 'ccopts' : '-march=x86-64-v4'}],
                    extra_glibcs=[{'variant': 'disable-multi-arch',
                                   'ccopts' : '-march=x86-64',
                                   'cfg': ['--disable-multi-arch']},
                                  {'variant': 'v2-disable-multi-arch',
                                   'ccopts' : '-march=x86-64-v2',
                                   'cfg': ['--disable-multi-arch']},
                                  {'variant': 'v3-disable-multi-arch',
                                   'ccopts' : '-march=x86-64-v3',
                                   'cfg': ['--disable-multi-arch']},
                                  {'variant': 'disable-multi-arch',
                                   'arch': 'i686',
                                   'ccopts': '-m32 -march=i686',
                                   'cfg': ['--disable-multi-arch']},
                                  {'arch': 'i486',
                                   'ccopts': '-m32 -march=i486'},
                                  {'arch': 'i586',
                                   'ccopts': '-m32 -march=i586'},
                                  {'variant': 'fp',
                                   'arch': 'i686',
                                   'ccopts': '-m32 -march=i686 -fno-omit-frame-pointer'},
                                  {'variant': 'sse',
                                   'arch': 'i686',
                                   'ccopts': '-m32 -march=i686 -msse2 -mfpmath=sse'}])

  def list_configs(self, glibcs):
    for abi in glibcs:
      print(abi)


class Glibc:
  """A configuration for building glibc."""

  def __init__(self, compiler, arch=None, os_name=None, variant=None,
               cfg=None, ccopts=None):
    """Initialize a Glibc object."""
    self.ctx = compiler.ctx
    self.compiler = compiler
    if arch is None:
      self.arch = compiler.arch
    else:
      self.arch = arch
    if os_name is None:
      self.os = compiler.os
    else:
      self.os = os_name
    self.variant = variant
    if variant is None:
      self.name = '%s-%s' % (self.arch, self.os)
    else:
      self.name = '%s-%s-%s' % (self.arch, self.os, variant)
    self.triplet = '%s-glibc-%s' % (self.arch, self.os)
    self.host_triplet = '%s-%s' % (self.arch, self.os)
    if cfg is None:
      self.cfg = []
    else:
      self.cfg = cfg
    self.ccopts = ccopts
    # gcc version selected for this build ('' for the default toolchain); set
    # per requested ABI by Context.resolve_configs.
    self.gccversion = ''

  def compiler_bindir(self):
    return '%s%s/%s/bin' % (PATHS["compilers"], self.gccversion,
                            self.compiler.name)

  def gcc_path(self):
    return '%s/%s-gcc' % (self.compiler_bindir(), self.compiler.triplet)

  def tool_name(self, tool):
    """Return the name of a cross-compilation tool."""
    ctool = '%s/%s-%s' % (self.compiler_bindir(), self.compiler.triplet, tool)
    if self.ccopts and (tool == 'gcc' or tool == 'g++'):
      ctool = '%s %s' % (ctool, self.ccopts)
    return ctool

  def configure(self, extra_config_opts):
    cmd = [PATHS["srcdir"] + '/configure',
           '--prefix=/usr',
           '--build=%s' % build_triplet(),
           '--host=%s' % self.host_triplet,
           'CC=%s' % self.tool_name('gcc'),
           'CXX=%s' % self.tool_name('g++'),
           'AR=%s' % self.tool_name('ar'),
           'AS=%s' % self.tool_name('as'),
           'LD=%s' % self.tool_name('ld'),
           'NM=%s' % self.tool_name('nm'),
           'OBJCOPY=%s' % self.tool_name('objcopy'),
           'OBJDUMP=%s' % self.tool_name('objdump'),
           'RANLIB=%s' % self.tool_name('ranlib'),
           'READELF=%s' % self.tool_name('readelf'),
           'STRIP=%s' % self.tool_name('strip')]
    if self.os == 'gnu':
       cmd += ['MIG=%s' % self.tool_name('mig')]
    cmd = cmd + extra_config_opts
    cmd += self.cfg
    return cmd

  def lib_name(self, lib):
    cmd = self.tool_name("gcc") + " -print-file-name={}".format(lib)
    stdout = subprocess.getoutput(cmd)
    return stdout if os.path.exists (stdout) else ""

  def copylibs(self):
    libgcc = ""
    for version in [ 1, 2, 4 ]:
      libgcc = self.lib_name("libgcc_s.so.{}".format(version))
      if libgcc:
        break
    libstdcxx = self.lib_name("libstdc++.so.6")
    libs = [lib for lib in (libgcc, libstdcxx) if lib]
    return ["cp"] + libs + [build_dir (self.name)]

  def build(self):
    return ['make',
            '-j%d' % (self.ctx.current_jobs())]

  def timeoutfactor_opt(self):
    if self.ctx.timeoutfactor:
      return ['TIMEOUTFACTOR=%s' % (self.ctx.timeoutfactor)]
    return []

  def check(self):
    return ['make',
            'check',
            'run-built-tests=%s' % (self.ctx.run_built_tests),
            '-j%d' % (self.ctx.current_jobs())] + self.timeoutfactor_opt()

  def check_parallel(self):
    return ['make',
            'check-parallel',
            'run-built-tests=%s' % (self.ctx.run_built_tests),
            '-j%d' % (self.ctx.current_jobs())] + self.timeoutfactor_opt()

  def check_abi(self):
    return ['make',
            'check-abi',
            '-j%d' % (self.ctx.current_jobs())]

  def update_abi(self):
    return ['make',
            'update-abi',
            '-j%d' % (self.ctx.current_jobs())]

  def bench_build(self):
    return ['make',
            'bench-build',
            '-j%d' % (self.ctx.current_jobs())]

  def update_syscall_lists(self):
    return ['make',
            'update-syscall-lists']

  def run_cmd(self):
    return ['make',
            '-j%d' % (self.ctx.current_jobs()),
            'update-syscall-lists']


def parallelize_type(string):
  if string == 'auto':
    return ['auto']
  fields = string.split(':')
  if len(fields) == 1:
    return [ int(fields[0]), 1 ]
  return [ int(fields[0]), int(fields[1]) ]

def available_cpus():
  try:
    return len(os.sched_getaffinity(0))
  except AttributeError:
    return os.cpu_count() or 1

# Upper bound on the make -j jobs per build that "-p auto" targets.  Newer
# glibc versions build well in parallel but different ABIs takes different
# times to build, so running one build per core (make -j1) leaves cores idle
# once the short builds are done and only the long ones remain.  Giving each
# build a healthy make -j and running fewer of them concurrently keeps those
# trailing builds spread across the freed cores; this is a good sweet spot
# before the serial link/ar steps of a single build limit its own speedup.
JOBS_PER_BUILD = 6

DEFAULT_OVERCOMMIT=40

def default_jobs_per_build(ncpus):
  # Default make -j per build for "-p auto".  On machines with few cores the
  # JOBS_PER_BUILD sweet spot is tuned down to half the cores so that at least
  # a couple of builds still run concurrently instead of collapsing into a
  # single serial build.
  return min(JOBS_PER_BUILD, max(1, ncpus // 2))

def auto_parallelize(nconfigs, overcommit=0, jobs_per_build=None):
  # Autotune the number of ABIs built in parallel and the make -j jobs given
  # to each build from the number of ABIs to build and the available CPUs.
  # Aim for jobs_per_build jobs per build and run as many such builds as the
  # CPU budget (the CPU count plus the overcommit) allows, capped by the
  # number of configurations, then hand each build an even share of the
  # budget.  The overcommit keeps the CPUs busy during the mostly serial
  # configure phases without heavily oversubscribing the already
  # well-parallelized builds.
  ncpus = available_cpus()
  nconfigs = max(1, nconfigs)
  if jobs_per_build is None:
    jobs_per_build = default_jobs_per_build(ncpus)
  jobs_per_build = max(1, jobs_per_build)
  budget = max(1, round(ncpus * (1 + overcommit / 100)))
  parallelize = max(1, min(nconfigs, budget // jobs_per_build))
  # Round the per-build jobs up so the overcommit is not lost to integer
  # division: with 24 CPUs the 10% budget of 26 must show up as an extra -j
  # (3 builds of -j9) rather than being floored back to -j8.
  build_jobs = max(1, math.ceil(budget / parallelize))
  return [ parallelize, build_jobs ]

SPECIAL_LISTS = {
  # Most of the supported Linux ABIs
  "linux" : [
    "aarch64-linux-gnu",
    "alpha-linux-gnu",
    "arc-linux-gnuhf",
    "armv7a-linux-gnueabihf",
    "csky-linux-gnuabiv2",
    "hppa-linux-gnu",
    "i686-linux-gnu",
    "loongarch64-linux-gnuf64",
    "m68k-linux-gnu",
    "microblaze-linux-gnu",
    "mips64el-linux-gnu",
    "mips64el-n32-linux-gnu",
    "mipsel-linux-gnu",
    "or1k-linux-gnu-soft",
    "powerpc64le-linux-gnu",
    "powerpc64-linux-gnu",
    "powerpc-linux-gnu-power4",
    "riscv32-linux-gnu-rv32imafdc-ilp32d",
    "riscv64-linux-gnu-rv64imafdc-lp64d",
    "s390x-linux-gnu",
    "sh4-linux-gnu",
    "sparc64-linux-gnu",
    "sparcv9-linux-gnu",
    "x86_64-linux-gnu",
    "x86_64-linux-gnu-x32",
  ],

  # All support triple ABIs with *.abilist (used to update-abi command).
  "abi" : [
    "aarch64-linux-gnu",
    "alpha-linux-gnu",
    "arc-linux-gnuhf",
    "arm-linux-gnueabihf",
    "armeb-linux-gnueabihf",
    "armv7a-linux-gnueabihf",
    "csky-linux-gnuabiv2",
    "hppa-linux-gnu",
    "i686-linux-gnu",
    "loongarch64-linux-gnuf64",
    "m68k-linux-gnu",
    "m68k-linux-gnu-coldfire",
    "microblaze-linux-gnu",
    "microblazeel-linux-gnu",
    "mips64el-linux-gnu",
    "mips64el-n32-linux-gnu",
    "mipsel-linux-gnu",
    "mips-linux-gnu-soft",
    "or1k-linux-gnu-soft",
    "powerpc64le-linux-gnu",
    "powerpc64-linux-gnu",
    "powerpc-linux-gnu",
    "powerpc-linux-gnu-power4",
    "powerpc-linux-gnu-soft",
    "riscv32-linux-gnu-rv32imafdc-ilp32d",
    "riscv64-linux-gnu-rv64imafdc-lp64d",
    "s390x-linux-gnu",
    "sh4-linux-gnu",
    "sh4eb-linux-gnu",
    "sparc64-linux-gnu",
    "sparcv9-linux-gnu",
    "x86_64-linux-gnu",
    "x86_64-linux-gnu-x32",

    "i686-gnu",
    "x86_64-gnu",
  ],

  "abi32" : [
    "arm-linux-gnueabihf",
    "armeb-linux-gnueabihf",
    "csky-linux-gnuabiv2",
    "hppa-linux-gnu",
    "i686-linux-gnu",
    "m68k-linux-gnu",
    "m68k-linux-gnu-coldfire",
    "microblaze-linux-gnu",
    "microblazeel-linux-gnu",
    "mips64-n32-linux-gnu",
    "mips-linux-gnu",
    "mips-linux-gnu-soft",
    #"nios2-linux-gnu",
    "or1k-linux-gnu-soft",
    "powerpc-linux-gnu",
    "powerpc-linux-gnu-soft",
    "sh4-linux-gnu",
    "sh4eb-linux-gnu",
    "sparcv9-linux-gnu",
  ],

  "powerpc64le" : [
    "powerpc64le-linux-gnu",
    "powerpc64le-linux-gnu-disable-multi-arch",
    "powerpc64le-linux-gnu-power10",
    "powerpc64le-linux-gnu-power9",
    "powerpc64le-linux-gnu-power8",
    "powerpc64le-linux-gnu-power10-disable-multi-arch",
    "powerpc64le-linux-gnu-power9-disable-multi-arch",
    "powerpc64le-linux-gnu-power8-disable-multi-arch",
  ],

  "powerpc64": [
    "powerpc64-linux-gnu-power8",
    "powerpc64-linux-gnu-power7",
    "powerpc64-linux-gnu-power6x",
    "powerpc64-linux-gnu-power6",
    "powerpc64-linux-gnu-power5+",
    "powerpc64-linux-gnu-power5",
    "powerpc64-linux-gnu-power4",
    "powerpc64-linux-gnu-power8-disable-multi-arch",
    "powerpc64-linux-gnu-power7-disable-multi-arch",
    "powerpc64-linux-gnu-power6x-disable-multi-arch",
    "powerpc64-linux-gnu-power6-disable-multi-arch",
    "powerpc64-linux-gnu-power5+-disable-multi-arch",
    "powerpc64-linux-gnu-power5-disable-multi-arch",
    "powerpc64-linux-gnu-power4-disable-multi-arch",
    "powerpc64-linux-gnu",
    "powerpc64-linux-gnu-disable-multi-arch",
  ],

  "powerpc": [
    "powerpc-linux-gnu-power8",
    "powerpc-linux-gnu-power7",
    "powerpc-linux-gnu-power6x",
    "powerpc-linux-gnu-power6",
    "powerpc-linux-gnu-power5+",
    "powerpc-linux-gnu-power5",
    "powerpc-linux-gnu-power4",
    "powerpc-linux-gnu-power8-disable-multi-arch",
    "powerpc-linux-gnu-power7-disable-multi-arch",
    "powerpc-linux-gnu-power6x-disable-multi-arch",
    "powerpc-linux-gnu-power6-disable-multi-arch",
    "powerpc-linux-gnu-power5+-disable-multi-arch",
    "powerpc-linux-gnu-power5-disable-multi-arch",
    "powerpc-linux-gnu-power4-disable-multi-arch",
    "powerpc-linux-gnu",
  ],

  "zseries": [
    "s390x-linux-gnu",
    "s390x-linux-gnu-disable-multi-arch",
    "s390x-linux-gnu-z10",
    "s390x-linux-gnu-z196",
    "s390x-linux-gnu-z900",
    "s390x-linux-gnu-z13",
  ],

  "arm": [
     "arm-linux-gnueabi",
     "arm-linux-gnueabihf",
     "armv5-linux-gnueabihf",
     "armv6-linux-gnueabihf",
     "armv6t2-linux-gnueabihf",
     "armv7a-linux-gnueabi",
     "armv7a-linux-gnueabihf",
     "armv7a-linux-gnueabihf-disable-multi-arch",
     "armv7a-neon-linux-gnueabihf",
     "armv7a-neonvfpv4-linux-gnueabihf",
     "armv7a-neonhard-linux-gnueabihf",
     "armv7a-neonhardvpfv4-linux-gnueabihf",
     "armv7a-thumb-linux-gnueabihf",
  ],

  "armeb": [
    "armeb-linux-gnueabihf",
    "armeb-v5-linux-gnueabihf",
    "armeb-v6-linux-gnueabihf",
    "armeb-v6t2-linux-gnueabihf",
    "armeb-v7-linux-gnueabihf",
    "armeb-v7neon-linux-gnueabihf",
    "armeb-v7neonhard-linux-gnueabihf",
  ],

  "sparc": [
    "sparc-linux-gnu",
    "sparc64-linux-gnu",
    "sparc64-linux-gnu-disable-multi-arch",
    "sparcv8-linux-gnu",
    "sparcv9-linux-gnu",
    "sparcv9-linux-gnu-disable-multi-arch",
  ],

  "mips": [
    "mips-linux-gnu",
    "mips-linux-gnu-mips16",
    "mips-linux-gnu-soft",
    "mipsisa32r6el-linux-gnu",
    "mips64-linux-gnu",
    "mips64-n32-linux-gnu",
    "mipsel-linux-gnu",
    "mips64el-linux-gnu",
    "mips64el-n32-linux-gnu",
    "mipsisa64r6el-linux-gnu",
    "mipsisa64r6el-n64-linux-gnu",
  ],

  "x86_64": [
    "x86_64-linux-gnu",
    "x86_64-linux-gnu-disable-multi-arch",
    "x86_64-linux-gnu-v2",
    "x86_64-linux-gnu-v3",
    "x86_64-linux-gnu-v4",
    "x86_64-linux-gnu-x32"
  ],

  "i686": [
    "i486-linux-gnu",
    "i586-linux-gnu",
    "i686-linux-gnu",
    "i686-linux-gnu-disable-multi-arch",
    "i686-linux-gnu-fp",
  ],
}

# Cap the --help width so the text does not stretch across the full width of a
# wide terminal, matching the fixed-width help of coreutils tools (ls --help);
# narrower terminals are still honoured.
HELP_WIDTH = 80

class CappedHelpFormatter(argparse.HelpFormatter):
  def __init__(self, *args, **kwargs):
    kwargs.setdefault('width',
                      min(shutil.get_terminal_size().columns - 2, HELP_WIDTH))
    super().__init__(*args, **kwargs)

def get_parser():
  parser = argparse.ArgumentParser(description=__doc__,
                                   formatter_class=CappedHelpFormatter)
  parser.add_argument('-p', dest='parallelize',
                      help='Run a number of parallel builds with make -j, as '
                           'PARALLEL[:JOBS] (JOBS defaults to 1). Use "auto" to '
                           'autotune both values from the number of ABIs to '
                           'build and the available CPUs (default: %(default)s)',
                      type=parallelize_type, default="auto")
  parser.add_argument('--overcommit', dest='overcommit',
                      help='Percentage of CPU overcommit used by "-p auto" '
                           '(default: {})'.format(DEFAULT_OVERCOMMIT),
                      type=float, default=DEFAULT_OVERCOMMIT)
  parser.add_argument('--jobs-per-build', dest='jobs_per_build',
                      help='make -j jobs per build targeted by "-p auto" '
                           '(default: auto-tuned from the CPU count)',
                      type=int, default=None)
  parser.add_argument('-k', dest='keep',
                      help='Keep old file and just run the command',
                      action='store_true', default=False)
  parser.add_argument('-t', dest='run_built_tests',
                      help='Run built tests',
                      action='store_true', default=False)
  parser.add_argument('--timeoutfactor', dest='timeoutfactor',
                      help='Set the TIMEOUTFACTOR for make check', default='')
  parser.add_argument('-s', dest='srcdir', default='',
                      help='glibc source tree to use; a plain name is resolved '
                           'beside the configured source tree (default: the '
                           'configured one)')
  parser.add_argument('-u', dest='suffix', default='',
                      help='suffix appended to the build directory and log file '
                           'names, to keep separate builds apart')
  parser.add_argument('--enable-stack-protector', dest='enable_stackprot',
                      help='Enable stack protection (default: %(default)s)',
                      choices=('no', 'yes', 'all', 'strong'), default='all')
  parser.add_argument('--disable-default-pie', dest='disable_pie',
                      help='Disable PIE (default is yes)',
                      action='store_true', default=False)
  parser.add_argument('--enable-bind-now', dest='enable_bind_now',
                      help='Enable bind now (default is yes)',
                      choices=('yes', 'no'), default='yes')
  parser.add_argument('--enable-profile', dest='enable_profile',
                      help='Enable profile (default is no)',
                      choices=('yes', 'no'), default='no')
  parser.add_argument('--enable-fortify-source', dest='enable_fortify',
                      help='Use -D_FORTIFY_SOURCE (default: %(default)s)',
                      choices=('1', '2', '3', 'yes', 'no'), default='2')
  parser.add_argument('--disable-multi-arch', dest='enable_multiarch',
                      help='Disable iFUNC sysdep selection',
                      action='store_false', default=True)
  parser.add_argument('--disable-werror', dest='disable_werror',
                      help='Do not use -Werror',
                      action='store_true', default=False)
  parser.add_argument('--disable-hardcoded-path-in-tests', dest='hardcoded',
                      help='Hardcode newly built glibc path in tests',
                      action='store_false', default=True)
  parser.add_argument('--enable-kernel', dest='with_kernel',
                      help='Build with --enable-kernel')
  parser.add_argument('--gccversion', dest='gccversion',
                      help='Use a different gcc version for all ABIs (a per-ABI '
                           'version can also be given by appending -gcc<version> '
                           'to the ABI name',
                      default='')
  parser.add_argument('--enable-sframe', dest='enable_sframe',
                      help='Build with --enable-sframe',
                      action='store_true', default=False)
  parser.add_argument('--cflags', dest='cflags',
                      help='Add the CFLAGS on build configuration', default='')
  parser.add_argument('--test_cc', dest='test_cc',
                      help='Whether to use a different CC than default use to build test',
                      default='')
  parser.add_argument('--test_cxx', dest='test_cxx',
                      help='Whether to use a different CXX than default use to build test',
                      default='')
  parser.add_argument('action', metavar='action',
                      help='What to do; one of: ' + ', '.join(ACTIONS),
                      choices=ACTIONS)
  parser.add_argument('configs',
                      help='Configurations to build (ex. x86_64-linux-gnu); '
                           'append -gcc<version> to pick a gcc version for that ABI. '
                           'The list action takes them as globs instead (ex. '
                           '"arm*"), and lists every configuration if none is given',
                      nargs='*')
  return parser


def main(argv):
  parser = get_parser()
  opts = parser.parse_args(argv)

  read_config (opts.srcdir, opts.suffix)

  configs = expand_configs(opts.configs)

  autotuned = opts.parallelize == ['auto']
  if autotuned:
    opts.parallelize = auto_parallelize(len(configs), opts.overcommit,
                                        opts.jobs_per_build)

  if opts.action != "list":
    parallelize, build_jobs = opts.parallelize
    prefix = "auto-parallelize" if autotuned else "parallelize"
    if parallelize > 1:
      msg = "%s: %d ABIs in parallel, make -j%d each (up to -j%d as builds " \
            "finish)" % (prefix, parallelize, build_jobs,
                         parallelize * build_jobs)
    else:
      msg = "%s: 1 ABI at a time, make -j%d" % (prefix, build_jobs)
    print(msg)

  ctx = Context(opts)
  failures = ctx.run(opts, configs)
  sys.exit(1 if failures else 0)

if __name__ == "__main__":
  try:
    main(sys.argv[1:])
  except KeyboardInterrupt:
    print('\n' + bcolors.WARNING + 'Interrupted, aborting builds.' + bcolors.ENDC)
    sys.exit(130)
