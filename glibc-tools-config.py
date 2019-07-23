#! /usr/bin/env python3

import sys
import argparse
import configparser
from py3compat import *

"""
glibc-tools configuration script, sets user config file for
toolchain, build, logs, and compilers directory.
"""

def get_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('-s', dest='srcdir',
                      help='GLIBC source directory to use',
                      default='')
  parser.add_argument('-b', dest='builddir',
                      help='Build directory to use',
                      default='')
  parser.add_argument('-l', dest='logsdir',
                      help='Directory to dump build/check logs',
                      default='')
  parser.add_argument('-c', dest='compilers',
                      help='Base directory where to find compilers',
                      default='')
  return parser

def main(argv):
  parser = get_parser()
  opts = parser.parse_args(argv)

  cfg = configparser.RawConfigParser()
  cfg.add_section('glibc-tools')
  cfg.set('glibc-tools', 'srcdir', opts.srcdir)
  cfg.set('glibc-tools', 'builddir', opts.builddir)
  cfg.set('glibc-tools', 'logsdir', opts.logsdir)
  cfg.set('glibc-tools', 'compilers', opts.compilers)

  cfgpath = str(Path.home()) + "/.glibc-tools.ini"
  with open(cfgpath, 'w') as cfgfile:
    cfg.write(cfgfile)

if __name__ == "__main__":
  main(sys.argv[1:])
