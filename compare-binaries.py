#! /usr/bin/env python3

import sys
import argparse
import subprocess
import tempfile
import configparser
import re
from pathlib import Path

PATHS = {}

def read_config():
  config = configparser.RawConfigParser()
  cfgpath = str(Path.home()) + "/.glibc-tools.ini"
  config.read(cfgpath)
  if 'glibc-tools' not in config.sections() \
     or 'srcdir' not in config['glibc-tools'] \
     or 'builddir' not in config['glibc-tools'] \
     or 'logsdir' not in config['glibc-tools'] \
     or 'compilers' not in config['glibc-tools']:
    print("error: config invalid, run glibc-tools-config.py")
    sys.exit(1)
  global PATHS
  PATHS = config._sections['glibc-tools']

def elf_architecture(filename):
  fields = subprocess.check_output(['file', filename]).decode("utf-8")
  fields = fields.split(':')[1].split(',')
  if not fields[0].strip().startswith('ELF'):
    return None
  return fields[1].strip()

def tool_path(arch, tool):
  architectures = {
    "ARM aarch64" : [ "aarch64-linux-gnu", "aarch64-glibc-linux-gnu" ]
  };
  if not arch in architectures:
    return None
  return PATHS['compilers'] + '/' + architectures[arch][0] + '/bin/' \
         + architectures[arch][1] + '-' + tool

def symbol_in_list(symbol, symbols):
  r = re.compile(r'.*\b' + symbol + r'\b')
  for s in list(filter(None.__ne__, [r.search(s) for s in symbols ])):
    fields = s.group(0).split()
    start = int(fields[0], 16)
    size  = int(fields[1], 16)
    return [ start, start + size ]

def run_objdump_diff(file1, file2, symbol):
  arch1 = elf_architecture (file1)
  arch2 = elf_architecture (file2)
  if arch1 != arch2:
    print("error: ELF files have different architectures (%s, %s)" %
          arch1, arch2)
    return
  if not arch1 or not arch2:
    print("error: invalid input object file")
    return

  objdump = tool_path(arch1, 'objdump')
  objdump_args1 = []
  objdump_args2 = []

  if symbol:
    nm = tool_path(arch1, 'nm')

    out1 = subprocess.check_output([nm, "-S", '--size-sort', file1])
    out1 = out1.decode('utf-8').splitlines()
    range1 = symbol_in_list (symbol, out1)

    out2 = subprocess.check_output([nm, "-S", '--size-sort', file2])
    out2 = out2.decode('utf-8').splitlines()
    range2 = symbol_in_list (symbol, out2)

    objdump_args1 += [ '--section=.text',
                       '--start-address=0x%x' % range1[0],
                       '--stop-address=0x%x' % range1[1] ]
    objdump_args2 += [ '--section=.text',
                       '--start-address=0x%x' % range2[0],
                       '--stop-address=0x%x' % range2[1] ]


  mode = None
  try:
    tmpb = tempfile.NamedTemporaryFile(delete=False)
    outb = subprocess.check_output([objdump, "-d", file1] + objdump_args1)
    tmpb.write(outb)

    tmpp = tempfile.NamedTemporaryFile(delete=False)
    outp = subprocess.check_output([objdump, "-d", file2] + objdump_args2)
    tmpp.write(outp)

    diffp = subprocess.Popen(["diff", "-y", tmpb.name, tmpp.name],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    diff, err = diffp.communicate()
    sys.stdout.buffer.write(diff)
    tmpb.close()
    tmpb.close()
  except subprocess.CalledProcessError as e:
    print ("error: %s failed" % (e.cmd))


def parser_arguments(argv):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('-s', dest='symbol',
                      help='symbol to disassemble')
  parser.add_argument('file1', nargs=1, metavar='file')
  parser.add_argument('file2', nargs='+', metavar='file',
                      help=argparse.SUPPRESS)
  opts = parser.parse_args(argv)
  opts.files = opts.file1 + opts.file2
  return opts


def main(argv):
  read_config()

  opts = parser_arguments(argv)

  run_objdump_diff(opts.files[0], opts.files[1], opts.symbol)

if __name__ == "__main__":
  main(sys.argv[1:])
