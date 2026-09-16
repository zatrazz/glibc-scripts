#! /usr/bin/env python3

"""Run glibc benchmarks several times, keep the best of each measurement,
and compare the results of two runs.

Examples:
  # Run bench-exp and bench-expf of a build tree 5 times each.
  %(prog)s -C ~/build/x86_64-linux-gnu exp expf

  # Save the results, run again after a change, and compare the two.
  %(prog)s -C ~/build/x86_64-linux-gnu -o before.json exp expf
  %(prog)s -C ~/build/x86_64-linux-gnu -o after.json exp expf
  %(prog)s --compare before.json after.json

  # Run benchmark programs as given, with arguments.
  %(prog)s -n 10 "benchtests/bench-memcpy 64" benchtests/bench-strlen
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

# Exit status of a benchmark whose function the build does not support.
UNSUPPORTED = 77

# The rpath-dirs of Makeconfig, where the test programs find the libraries.
RPATH_DIRS = ('math', 'elf', 'dlfcn', 'nss', 'nis', 'rt', 'resolv', 'mathvec',
              'support', 'misc', 'debug')

# Measurements a higher value is better for; lower is better for the rest.
HIGHER_IS_BETTER = ('throughput', 'max-throughput', 'min-throughput')

# Fields that are not timings, left out of the tables and comparisons.
HIDDEN_FIELDS = ('duration', 'iterations', 'max-throughput', 'min-throughput',
                 'length', 'alignment', 'align1', 'align2')

# Column order of the timings, the rest following alphabetically.
FIELD_ORDER = ('reciprocal-throughput', 'latency', 'mean', 'min', 'max')

BASE_VARIANT = '<base>'


class Error(Exception):
  """A fatal error, reported to the user."""


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

def format_duration(seconds):
  seconds = int(round(seconds))
  h, rem = divmod(seconds, 3600)
  m, s = divmod(rem, 60)
  if h:
    return '%dh%02dm%02ds' % (h, m, s)
  if m:
    return '%dm%02ds' % (m, s)
  return '%ds' % s

def format_number(value):
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return str(value)
  return '%.2f' % value

def is_number(value):
  return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_bench_output(text):
  """Parse the output of a benchmark program, a JSON object that
  bench-skeleton.c prints without its enclosing braces."""
  text = text.strip()
  if not text:
    raise ValueError('empty output')
  if text.startswith('"'):
    text = '{' + text + '}'
  text = re.sub(r',\s*([}\]])', r'\1', text)
  return json.loads(text)

def better(field, a, b):
  if not (is_number(a) and is_number(b)):
    return a
  if field in HIGHER_IS_BETTER:
    return max(a, b)
  return min(a, b)

def merge_best(best, current):
  """Fold the measurements of a run into the best ones so far."""
  for key, value in current.items():
    if isinstance(value, dict):
      merge_best(best.setdefault(key, {}), value)
    elif key in best:
      best[key] = better(key, best[key], value)
    else:
      best[key] = value


class BuildTree:
  """The glibc build tree the benchmarks are looked up in and run with."""

  def __init__(self, path):
    self.path = os.path.abspath(path)
    self.name = os.path.basename(self.path)
    if not os.path.isfile(self.file('config.make')):
      raise Error("'%s' is not a glibc build tree (no config.make)" % path)
    self.hardcoded_path = self._config_flag('build-hardcoded-path-in-tests')

  def file(self, *parts):
    return os.path.join(self.path, *parts)

  def _config_flag(self, name):
    with open(self.file('config.make')) as f:
      for line in f:
        key, sep, value = line.partition('=')
        if sep and key.strip() == name:
          return value.strip() == 'yes'
    return False

  def resolve(self, program):
    """The path of a benchmark of the tree ("exp", "bench-exp"), or of a
    program given by its path."""
    if os.path.dirname(program) or os.path.isfile(program):
      return program
    for candidate in ('bench-' + program, program):
      path = self.file('benchtests', candidate)
      if os.path.isfile(path):
        return path
    raise Error("no benchmark '%s' in %s (run glibc-tools.py bench-build?)"
                % (program, self.file('benchtests')))

  def rtld_prefix(self):
    """The test-via-rtld-prefix of the makefiles, if the tree needs it."""
    if self.hardcoded_path:
      return []
    library_path = ':'.join([self.path] + [self.file(d) for d in RPATH_DIRS])
    return [self.file('elf', 'ld.so'), '--library-path', library_path]

  def timing_type(self):
    program = self.file('benchtests', 'bench-timing-type')
    if not os.path.isfile(program):
      return None
    try:
      result = subprocess.run(self.rtld_prefix() + [program],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              universal_newlines=True)
    except OSError:
      return None
    return result.stdout.strip() if result.returncode == 0 else None


class Benchmark:
  """A benchmark command and the best measurements of its runs."""

  def __init__(self, name, command):
    self.name = name
    self.command = command
    self.results = None
    self.status = 'run'
    self.error = ''
    self.elapsed = 0.0

  def run_once(self):
    """Run the benchmark once; returns pass, fail or skip."""
    try:
      result = subprocess.run(self.command,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              universal_newlines=True)
    except OSError as exc:
      self.error = str(exc)
      return 'fail'
    if result.returncode == UNSUPPORTED:
      self.error = result.stderr.strip()
      return 'skip'
    if result.returncode != 0:
      self.error = 'exit status %d\n%s' % (result.returncode,
                                           result.stderr.strip())
      return 'fail'
    try:
      output = parse_bench_output(result.stdout)
    except ValueError as exc:
      self.error = 'unparsable output (%s):\n%s' % (exc, result.stdout.strip())
      return 'fail'
    if self.results is None:
      self.results = {}
    merge_best(self.results, output)
    return 'pass'

def bench_name(program, args):
  name = os.path.basename(program)
  if name.startswith('bench-'):
    name = name[len('bench-'):]
  return ' '.join([name] + args)

def make_benchmarks(specs, tree):
  benchmarks = []
  names = set()
  for spec in specs:
    words = shlex.split(spec)
    if not words:
      raise Error('empty benchmark command')
    program, args = words[0], words[1:]
    command = [program] + args
    if tree:
      program = tree.resolve(program)
      command = tree.rtld_prefix() + [program] + args
    name = bench_name(program, args)
    if name in names:
      raise Error("benchmark '%s' given twice" % name)
    names.add(name)
    benchmarks.append(Benchmark(name, command))
  return benchmarks


class Progress:
  """Live one-line-per-benchmark display, as in glibc-dev.py:

    <benchmark>:      | run <i>/<n> | <results|FAIL|SKIP> | <time>

  Redrawn in place on a terminal, printed once per benchmark otherwise.
  """

  # status -> (label, color); a pass shows the results instead.
  _STATUS = {
    'run': ('', ''),
    'pass': ('', bcolors.OKGREEN),
    'fail': ('FAIL', bcolors.FAIL),
    'skip': ('SKIP', bcolors.WARNING),
  }

  def __init__(self, names, nruns, out):
    self.out = out
    self.order = list(names)
    self.nruns = nruns
    self.namew = max(len(name) for name in names) + 1
    self.stepw = len(self._step(nruns))
    self.state = {name: ('', 'run', '', None) for name in names}
    self.began = {}

    rows = shutil.get_terminal_size((0, 0)).lines
    self.live = out.isatty() and 0 < len(self.order) < rows
    if self.live:
      out.write(''.join(self._line(name) + '\n' for name in self.order))
      out.flush()

  def _step(self, run):
    return 'run %d/%d' % (run, self.nruns)

  def _line(self, name):
    step, status, results, elapsed = self.state[name]
    label, color = self._STATUS[status]
    label = label or results
    return '%s | %s | %s | %s' % (
      colorize('%-*s' % (self.namew, name + ':'), bcolors.BOLD),
      colorize('%-*s' % (self.stepw, step), bcolors.WARNING),
      colorize('%-4s' % label, color) if color else '%-4s' % label,
      format_duration(elapsed) if elapsed is not None else '')

  def _redraw(self):
    self.out.write('\033[%dA' % len(self.order))
    for name in self.order:
      self.out.write('\r\033[K' + self._line(name) + '\n')
    self.out.flush()

  def running(self, name, run, results=''):
    self.began.setdefault(name, time.monotonic())
    self.state[name] = (self._step(run), 'run', results, None)
    if self.live:
      self._redraw()

  def finished(self, name, run, status, results=''):
    elapsed = time.monotonic() - self.began.get(name, time.monotonic())
    self.state[name] = (self._step(run), status, results, elapsed)
    if self.live:
      self._redraw()
    else:
      self.out.write(self._line(name) + '\n')
      self.out.flush()


def summarize(results):
  """The reciprocal-throughput/latency of the workload variants, or the
  mean of every variant without one, on one line."""
  if not results:
    return ''
  workloads, means = [], []
  for function in results.values():
    for variant, measurements in variants_of(function):
      name = variant or BASE_VARIANT
      rthroughput = measurements.get('reciprocal-throughput')
      latency = measurements.get('latency')
      if is_number(rthroughput) or is_number(latency):
        workloads.append('%s %s/%s' % (name, format_number(rthroughput),
                                       format_number(latency)))
      elif is_number(measurements.get('mean')):
        means.append('%s %s' % (name, format_number(measurements['mean'])))
  parts = workloads or means
  return ', '.join(parts) if parts else 'PASS'

def run_benchmarks(benchmarks, nruns, out):
  progress = Progress([b.name for b in benchmarks], nruns, out)
  for bench in benchmarks:
    began = time.monotonic()
    run = 0
    for run in range(1, nruns + 1):
      progress.running(bench.name, run, summarize(bench.results))
      status = bench.run_once()
      if status != 'pass':
        break
    bench.status = status
    bench.elapsed = time.monotonic() - began
    progress.finished(bench.name, run, status, summarize(bench.results))

def variants_of(function):
  return [(name, fields) for name, fields in function.items()
          if isinstance(fields, dict)]

def table_fields(variants):
  fields = set()
  for _, measurements in variants:
    fields.update(key for key, value in measurements.items()
                  if is_number(value) and key not in HIDDEN_FIELDS)
  order = {name: i for i, name in enumerate(FIELD_ORDER)}
  return sorted(fields, key=lambda f: (order.get(f, len(order)), f))

def print_table(rows, header):
  widths = [max(len(row[i]) for row in [header] + rows)
            for i in range(len(header))]
  def fmt(row):
    cells = ['%-*s' % (widths[0], row[0])]
    cells += ['%*s' % (w, cell) for w, cell in zip(widths[1:], row[1:])]
    return '  '.join(cells).rstrip()
  print(colorize(fmt(header), bcolors.BOLD))
  for row in rows:
    print(fmt(row))

def print_results(function, name):
  variants = variants_of(function)
  fields = table_fields(variants)
  if not variants or not fields:
    # The string and malloc benchmarks have a layout of their own.
    print('%s: %d entries, see -o for the results' % (name, len(function)))
    return
  rows = []
  for variant, measurements in variants:
    row = [variant or BASE_VARIANT]
    row += [format_number(measurements[f]) if f in measurements else '-'
            for f in fields]
    rows.append(row)
  print_table(rows, [name] + fields)

def print_report(benchmarks, verbose):
  """The errors of the benchmarks that failed and, with verbose, the
  measurements of every variant of the others."""
  for bench in benchmarks:
    if bench.status != 'pass':
      label = 'unsupported' if bench.status == 'skip' else 'failed'
      print(colorize('\n%s: %s' % (bench.name, label), bcolors.FAIL))
      for line in bench.error.splitlines():
        print('  ' + line)
    elif verbose:
      for name, function in bench.results.items():
        print()
        print_results(function, name)

def save_results(path, benchmarks, timing_type):
  """Write the results in the layout of the bench.out of "make bench"."""
  functions = {}
  for bench in benchmarks:
    if bench.results:
      functions.update(bench.results)
  document = {'timing_type': timing_type or 'unknown', 'functions': functions}
  with open(path, 'w') as f:
    json.dump(document, f, indent=2)
    f.write('\n')

def command_run(opts):
  tree = BuildTree(opts.tree) if opts.tree else None
  benchmarks = make_benchmarks(opts.benchmarks, tree)
  nruns = opts.nruns
  print('running %d benchmark%s %d time%s each%s'
        % (len(benchmarks), 's' if len(benchmarks) != 1 else '',
           nruns, 's' if nruns != 1 else '',
           ' from %s' % tree.name if tree else ''))
  run_benchmarks(benchmarks, nruns, sys.stdout)
  print_report(benchmarks, opts.verbose)
  npass = sum(1 for b in benchmarks if b.status == 'pass')
  nfail = sum(1 for b in benchmarks if b.status == 'fail')
  if opts.output and npass:
    save_results(opts.output, benchmarks, tree.timing_type() if tree else None)
    print('\nresults: %s' % opts.output)
  return 1 if nfail else 0


def load_results(path):
  """The functions and timing type of a results file."""
  try:
    with open(path) as f:
      document = json.load(f)
  except (OSError, ValueError) as exc:
    raise Error("cannot read '%s': %s" % (path, exc))
  if not isinstance(document, dict):
    raise Error("'%s' does not hold benchmark results" % path)
  if 'functions' in document:
    return document['functions'], document.get('timing_type', 'unknown')
  return document, 'unknown'

def flatten(value, path=()):
  """The numbers of a results tree as (path, value), lists indexed."""
  if isinstance(value, dict):
    items = [(k, v) for k, v in value.items() if k not in HIDDEN_FIELDS]
  elif isinstance(value, list):
    items = enumerate(value)
  else:
    if is_number(value):
      yield path, value
    return
  for key, item in items:
    for entry in flatten(item, path + (str(key),)):
      yield entry

def describe(path):
  """The (function, variant, field, field[index]) of a flattened path."""
  end = len(path) - 1
  while end > 1 and path[end].isdigit():
    end -= 1
  field = path[end]
  name = field + ''.join('[%s]' % i for i in path[end + 1:])
  return path[0], '/'.join(path[1:end]) or BASE_VARIANT, field, name

def change_of(field, old, new):
  """The percent change and whether it is an improvement."""
  if old == 0:
    return None, None
  percent = (new - old) * 100.0 / old
  improved = percent > 0 if field in HIGHER_IS_BETTER else percent < 0
  return percent, improved

def compare_results(old, new, threshold):
  """Print the measurements side by side; returns how many improved and
  regressed beyond the threshold."""
  olds = dict(flatten(old))
  news = dict(flatten(new))
  paths = list(olds)
  paths += [path for path in news if path not in olds]
  rows = []
  nbetter = nworse = 0
  for path in paths:
    a, b = olds.get(path), news.get(path)
    function, variant, field, name = describe(path)
    row = [function, variant, name,
           format_number(a) if a is not None else '-',
           format_number(b) if b is not None else '-']
    if a is None or b is None:
      row.append('')
    else:
      percent, improved = change_of(field, a, b)
      if percent is None:
        row.append('')
      else:
        cell = '%+.2f%%' % percent
        if abs(percent) >= threshold:
          if improved:
            nbetter += 1
            cell = colorize(cell, bcolors.OKGREEN)
          else:
            nworse += 1
            cell = colorize(cell, bcolors.FAIL)
        row.append(cell)
    rows.append(row)
  print_table(rows, ['function', 'variant', 'measurement', 'old', 'new',
                     'change'])
  return nbetter, nworse

def command_compare(opts):
  oldpath, newpath = opts.compare
  old, old_timing = load_results(oldpath)
  new, new_timing = load_results(newpath)
  if old_timing != new_timing:
    print(colorize('warning: timing types differ: %s (%s) vs %s (%s)'
                   % (old_timing, oldpath, new_timing, newpath),
                   bcolors.WARNING))
  print('old: %s\nnew: %s\n' % (oldpath, newpath))
  nbetter, nworse = compare_results(old, new, opts.threshold)
  print()
  summary = 'summary: %d improvements, %d regressions beyond %g%%' \
            % (nbetter, nworse, opts.threshold)
  print(colorize(summary, bcolors.FAIL if nworse else bcolors.OKGREEN))
  return 0


def positive_int(string):
  try:
    value = int(string)
  except ValueError:
    value = 0
  if value < 1:
    raise argparse.ArgumentTypeError('expected a positive number')
  return value

def get_parser():
  parser = argparse.ArgumentParser(
    description=__doc__ % {'prog': os.path.basename(sys.argv[0])},
    formatter_class=argparse.RawDescriptionHelpFormatter,
    usage='%(prog)s [-n N] [-C TREE] [-v] [-o FILE] BENCH [BENCH ...]\n'
          '       %(prog)s --compare OLD NEW [-t PERCENT]')
  parser.add_argument('benchmarks', metavar='BENCH', nargs='*',
                      help='benchmark command: a program and its arguments '
                           '(quote them together), or with -C the name of '
                           'a benchmark of the build tree, such as "exp" '
                           'or "bench-memcpy"')
  parser.add_argument('-n', dest='nruns', metavar='N', type=positive_int,
                      default=5,
                      help='run each benchmark N times and keep the best '
                           'of every measurement (default: %(default)s)')
  parser.add_argument('-C', dest='tree', metavar='TREE',
                      help='glibc build tree to take the benchmarks from '
                           'and run them with')
  parser.add_argument('-v', dest='verbose', action='store_true',
                      help='also print every measurement of each benchmark '
                           'variant after the run')
  parser.add_argument('-o', dest='output', metavar='FILE',
                      help='save the results to FILE as JSON, in the layout '
                           'of the bench.out of "make bench", for --compare')
  parser.add_argument('--compare', metavar=('OLD', 'NEW'), nargs=2,
                      help='instead of running anything, compare the results '
                           'saved by -o in OLD and NEW')
  parser.add_argument('-t', dest='threshold', metavar='PERCENT', type=float,
                      default=5.0,
                      help='with --compare, the change beyond which a '
                           'measurement counts as an improvement or a '
                           'regression (default: %(default)s)')
  return parser

def main(argv):
  parser = get_parser()
  opts = parser.parse_args(argv)
  if opts.compare and opts.benchmarks:
    parser.error('--compare takes no benchmarks')
  if not opts.compare and not opts.benchmarks:
    parser.error('no benchmark given')

  global USE_COLOR
  USE_COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')

  try:
    if opts.compare:
      return command_compare(opts)
    return command_run(opts)
  except Error as exc:
    print('error: %s' % exc, file=sys.stderr)
    return 2
  except KeyboardInterrupt:
    print()
    return 130

if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
