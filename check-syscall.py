#! /usr/bin/env python3

import sys
import os
import shutil
import argparse
import subprocess
import tempfile
import configparser
from pathlib import Path
from itertools import chain

PATHS = {}

def read_config():
  config = configparser.RawConfigParser()
  cfgpath = str(Path.home()) + "/.glibc-tools.ini"
  config.read(cfgpath)
  if 'glibc-tools' not in config.sections() \
    or 'compilers' not in config['glibc-tools']:
    print("error: config invalid, run glibc-tools-config.py")
    sys.exit(1)
  global PATHS
  PATHS = config._sections['glibc-tools']

ABIS = { "aarch64"    : "aarch64",
         "alpha"      : "alpha",
         "arm"        : "arm-gnueabihf", 
         "hppa"       : "hppa", 
         "i686"       : "x86_64 -m32",
         "ia64"       : "ia64",
         "m68k"       : "m68k",
         "microblaze" : "microblaze",
         "mips64"     : "mips64 -mabi=64",
         "mips64-n32" : "mips64 -mabi=n32",
         "mips"       : "mips64 -mabi=32",
         "nios2"      : "nios2",
         "powerpc64"  : "powerpc64",
         "powerpc"    : "powerpc",
         "powerpcspe" : "powerpc-gnuspe",
         "riscv64"    : "riscv64",
         "s390"       : "s390x -m31",
         "s390x"      : "s390x",
         "sh4"        : "sh4",
         "sparc64"    : "sparc64",
         "sparcv9"    : "sparc64 -m32",
         "x86_64"     : "x86_64",
         "x86_64-x32" : "x86_64 -mx32"
}

def get_compiler_path(abi):
  cfields = ABIS[abi].split()
  cprefix = cfields[0]

  cprefields = cprefix.split('-')
  if len(cprefields) > 1:
    cprefix = cprefields[0]
    csuffix = cprefields[1]
  else:
    csuffix = "gnu"

  cpath = cprefix + "-linux-" + csuffix
  cname = cprefix + "-glibc-linux-" + csuffix

  compiler = PATHS["compilers"] + "/" + cpath + "/bin/" + cname + "-gcc"
  if len(cfields) > 1:
    return [ compiler, cfields[1] ]
  return [ compiler ] 

def create_temp_file(syscall):
  f = tempfile.NamedTemporaryFile("w", suffix=".c")
  f.write("#define _GNU_SOURCE\n")
  f.write("#include <unistd.h>\n")
  f.write("#include <sys/syscall.h>\n")
  f.write("int foo (void)\n")
  f.write("{\n")
  f.write("  return syscall (SYS_%s);\n" % syscall)
  f.write("}\n")
  f.flush()
  return f

def build_check(compiler, sysfile):
  cmd = compiler + [ '-c', sysfile.name ]
  fnull = open(os.devnull, 'w')
  ret = subprocess.run(cmd, check=False, stdout=fnull, stderr=fnull)
  if ret.returncode is not 0:
    return "FAIL"
  return "OK"

def check_syscall(args):
  for syscall in args.syscalls:
    sysfile = create_temp_file(syscall)
    print ("SYSCALL: %s" % syscall)
    for abi in sorted(ABIS.keys()):
      try:
        compiler = get_compiler_path(abi)
        print ("  %20s: %s" % (abi, build_check(compiler, sysfile)))
      except:
        print ("  %20s: compiler not found" % abi)

def check_file(args):
  for prog in args.programs:
    try:
      sysfile = open(prog, 'r')
    except:
      print ("FAIL: file %s can not be opened" % (prog))
      return
    print ("FILE: %s" % prog)
    for abi in sorted(ABIS.keys()):
      try:
        compiler = get_compiler_path(abi)
        print ("  %20s: %s" % (abi, build_check(compiler, sysfile)))
      except:
        print ("  %20s: compiler not found" % abi)

def get_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  subparsers = parser.add_subparsers(help='Functions')
  parser_1 = subparsers.add_parser('syscall', help='syscall to check (ex. openat)')
  parser_1.add_argument('syscalls',
                        help='syscall to check (ex. openat)',
                        nargs='*')
  parser_1.set_defaults(func=check_syscall)
  parser_2 = subparsers.add_parser('program', help='program to run')
  parser_2.add_argument('programs',
                        help='file to build',
                        nargs='*')
  parser_2.set_defaults(func=check_file)
  return parser

def main(argv):
  read_config()
  parser = get_parser()
  args = parser.parse_args(argv)
  try:
    args.func(args)
  except:
    parser.print_help()

if __name__ == "__main__":
  main(sys.argv[1:])
