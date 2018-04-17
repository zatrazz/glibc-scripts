#!/usr/bin/env python3

import sys
import os
import argparse
import subprocess
import fnmatch
import tempfile
from itertools import chain
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


def recursive_glob(rootdir='.', pattern='*'):
  matches = []
  for root, dirnames, filenames in os.walk(rootdir):
    for filename in fnmatch.filter(filenames, pattern):
      matches.append(os.path.join(root, filename))
  return matches

class Config(object):
  """A configuration for building a compiler and associated libraries."""

  def __init__(self, ctx, arch, os_name, glibcs=[]):
    """Initialize a Config object."""
    self.ctx = ctx
    self.arch = arch
    self.os = os_name
    self.name = '%s-%s' % (arch, os_name)
    self.triplet = '%s-glibc-%s' % (arch, os_name)
    self.glibcs = [g["arch"] for g in glibcs]

class Context(object):
  def __init__(self):
    self.configs = {}
    self.add_all_configs()

  def add_config(self, **args):
    cfg = Config(self, **args)
    if cfg.name in self.configs:
      print('error: duplicate config %s' % cfg.name)
      exit(1)
    self.configs[cfg.name] = cfg
    for glibc in cfg.glibcs:
      self.configs["%s-%s" % (glibc, cfg.os)] = cfg

  def add_all_configs(self):
    """Add all known glibc build configurations."""
    self.add_config(arch='aarch64',
                    os_name='linux-gnu')
    self.add_config(arch='alpha',
                    os_name='linux-gnu')
    self.add_config(arch='arm',
                    os_name='linux-gnueabi',
                    glibcs=[{'arch' : 'armv7'}])
    self.add_config(arch='arm',
                    os_name='linux-gnueabihf',
                    glibcs=[{'arch' : 'armv7'},
			    {'arch' : 'armv7-neon'},
			    {'arch' : 'armv7-neonhard'}])
    self.add_config(arch='hppa',
                    os_name='linux-gnu')
    self.add_config(arch='ia64',
                    os_name='linux-gnu')
    self.add_config(arch='m68k',
                    os_name='linux-gnu')
    self.add_config(arch='microblaze',
                    os_name='linux-gnu')
    self.add_config(arch='mips64',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'mips64-n32'},
                            {'arch': 'mips'},
                            {'arch': 'mips64'}])
    self.add_config(arch='nios2',
                    os_name='linux-gnu')
    self.add_config(arch='powerpc',
                    os_name='linux-gnu')
    self.add_config(arch='powerpc64',
                    os_name='linux-gnu')
    self.add_config(arch='powerpc64le',
                    os_name='linux-gnu')
    self.add_config(arch='s390x',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 's390'}])
    self.add_config(arch='sh4',
                    os_name='linux-gnu')
    self.add_config(arch='sparc64',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'sparcv9'}])
    self.add_config(arch='tilegx',
                    os_name='linux-gnu')
    self.add_config(arch='tilepro',
                    os_name='linux-gnu')
    self.add_config(arch='x86_64',
                    os_name='linux-gnu',
		    glibcs=[{'arch': 'i686'}])

  def run(self, base, patched, strip, tofile, abis):
    if not abis:
      abis = sorted(self.configs.keys())
    for c in abis:
      if strip is True:
        self.run_strip (base, patched, self.configs[c], c)
      self.run_objdump_diff (base, patched, tofile, self.configs[c], c)

  def run_strip(self, base, patched, cfg, c):
    strip = PATHS["compilers"] + "/" + cfg.name + "/bin/" + cfg.triplet + "-strip"
    basefiles  = recursive_glob(PATHS["builddir"] + "/" + base, "*.so")
    patchfiles = recursive_glob(PATHS["builddir"] + "/" + patched, "*.so")
    for b,p in zip(basefiles, patchfiles):
      if subprocess.call([strip, b], shell=False) != 0:
        print ("error: %s %s failed" % (strip, b))
      if subprocess.call([strip, p], shell=False) != 0:
        print ("error: %s %s failed" % (strip, p))
    print("info: strip %s done" % (c))

  def run_objdump_diff(self, base, patched, tofile, cfg, c):
    objdump = PATHS["compilers"] + "/" + cfg.name + "/bin/" + cfg.triplet + "-objdump"
    basefiles  = recursive_glob(PATHS["builddir"] + "/" + base, "*.so")
    patchfiles = recursive_glob(PATHS["builddir"] + "/" + patched, "*.so")
      
    mode = None
    for b,p in zip(basefiles, patchfiles):
      try:
        tmpb = tempfile.NamedTemporaryFile(delete=False)
        outb = subprocess.check_output([objdump, "-d", b])
        tmpb.write(outb)

        tmpp = tempfile.NamedTemporaryFile(delete=False)
        outp = subprocess.check_output([objdump, "-d", p])
        tmpp.write(outp)

        diffp = subprocess.Popen(["diff", "-u", tmpb.name, tmpp.name],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        diff, err = diffp.communicate()
        if tofile is True:
          if mode is None:
            mode = "wb"
          else:
            mode = "ab"
          f = open (c + ".out", mode)
          f.write(diff)
          f.close()
        else:
          sys.stdout.buffer.write(diff)
        tmpb.close()
        tmpb.close()
      except subprocess.CalledProcessError as e:
        print ("error: %s failed" % (e.cmd))
    print("info: diff %s done" % (c))
    

def get_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('-s', dest='strip',
                      help='strip shared objects',
                      action='store_true', default=False)
  parser.add_argument('-f', dest='tofile',
                      help='dump output to file',
                      action='store_true', default=False)
  parser.add_argument('base',
                      help='base folder')
  parser.add_argument('patched',
                      help='patched folder')
  parser.add_argument('configs',
                      help='configurations to use (ex. x86_64-linux-gnu)',
                      nargs='*')
  return parser

SPECIAL_LISTS = {
  "abi" : [ "aarch64-linux-gnu", "alpha-linux-gnu", "armv7-linux-gnueabihf",
            "hppa-linux-gnu", "i686-linux-gnu", "ia64-linux-gnu",
	    "m68k-linux-gnu", "microblaze-linux-gnu", "mips64-linux-gnu",
	    "mips64-n32-linux-gnu", "mips-linux-gnu", "nios2-linux-gnu",
            "powerpc64-linux-gnu", "powerpc-linux-gnu", "s390-linux-gnu",
            "s390x-linux-gnu", "sh4-linux-gnu", "sparc64-linux-gnu",
            "sparcv9-linux-gnu", "tilegx-linux-gnu", "x86_64-linux-gnu" ]
}

def main(argv):
  read_config ()

  parser = get_parser()
  opts = parser.parse_args(argv)
  ctx = Context()
  configs = list(chain.from_iterable(SPECIAL_LISTS.get(c, [c]) for c in opts.configs))

  ctx.run(opts.base, opts.patched, opts.strip, opts.tofile, configs)

if __name__ == "__main__":
  main(sys.argv[1:])
