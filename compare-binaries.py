#! /usr/bin/env python3

import sys
import argparse
import subprocess
import tempfile
import configparser
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
  # /bin/ls: ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV),
  # dynamically linked, interpreter /lib/ld-linux-aarch64.so.1,
  # for GNU/Linux 3.7.0, BuildID[sha1]=..., stripped
  fields = fields.split(':')[1].split(',')
  if not fields[0].strip().startswith('ELF'):
    return None
  return fields[1].strip()

def objdump_tool(arch):
  architectures = {
    "ARM aarch64" : [ "aarch64-linux-gnu", "aarch64-glibc-linux-gnu" ]
  };
  if not arch in architectures:
    return None
  return PATHS['compilers'] + '/' + architectures[arch][0] + '/bin/' \
         + architectures[arch][1] + '-objdump'

def run_objdump_diff(file1, file2, symbol):
  arch1 = elf_architecture (file1)
  arch2 = elf_architecture (file2)
  if arch1 != arch2:
    print("error: ELF files have different architectures (%s, %s)" %
          arch1, arch2)
    return

  objdump = objdump_tool(arch1)
  mode = None
  try:
    tmpb = tempfile.NamedTemporaryFile(delete=False)
    outb = subprocess.check_output([objdump, "-d", file1])
    tmpb.write(outb)

    tmpp = tempfile.NamedTemporaryFile(delete=False)
    outp = subprocess.check_output([objdump, "-d", file2])
    tmpp.write(outp)

    diffp = subprocess.Popen(["diff", "-u", tmpb.name, tmpp.name],
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
