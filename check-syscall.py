#! /usr/bin/env python3

import sys
import os
import shutil
import argparse
import subprocess
import tempfile
from itertools import chain

PATHS = {
  "compilers" : "/opt/cross"
}

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
         "tilegx"     : "tilegx",
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

def check_syscall(syscalls):
  for syscall in syscalls:
    sysfile = create_temp_file(syscall)
    print ("SYSCALL: %s" % syscall)
    for abi in sorted(ABIS.keys()):
      compiler = get_compiler_path(abi)
      print ("  %20s: %s" % (abi, build_check(compiler, sysfile)))

def get_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('syscalls',
                      help='syscall to check (ex. openat)',
                      nargs='*')
  return parser

def main(argv):
  parser = get_parser()
  opts = parser.parse_args(argv)
  check_syscall(opts.syscalls)

if __name__ == "__main__":
  main(sys.argv[1:])
