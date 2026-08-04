#! /usr/bin/env python3

"""
glibc-tools configuration script, sets user config file for
toolchain, build, logs, and compilers directory, and the identity
glibc-dev.py credits in a Reviewed-by trailer.
"""

import sys
import os
import argparse
import configparser

CFGPATH = "~/.glibc-tools.ini"

SETTINGS = ('srcdir', 'builddir', 'logsdir', 'compilers', 'reviewer')

def get_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('-s', dest='srcdir',
                      help='GLIBC source directory to use',
                      default=None)
  parser.add_argument('-b', dest='builddir',
                      help='Build directory to use',
                      default=None)
  parser.add_argument('-l', dest='logsdir',
                      help='Directory to dump build/check logs',
                      default=None)
  parser.add_argument('-c', dest='compilers',
                      help='Base directory where to find compilers',
                      default=None)
  parser.add_argument('-r', dest='reviewer', metavar='IDENTITY',
                      help='Identity to credit in a Reviewed-by trailer, as '
                           '"Name <mail>"',
                      default=None)
  return parser

def main(argv):
  parser = get_parser()
  opts = parser.parse_args(argv)

  cfgpath = os.path.expanduser(CFGPATH)
  cfg = configparser.RawConfigParser()
  # Keep the settings that were not given: they are set one at a time as they
  # come up, and rewriting the file from scratch would drop the others.  An
  # explicitly empty value ("-s ''") still clears one.
  cfg.read(cfgpath)
  if not cfg.has_section('glibc-tools'):
    cfg.add_section('glibc-tools')

  for setting in SETTINGS:
    value = getattr(opts, setting)
    if value is not None:
      cfg.set('glibc-tools', setting, value)
    elif not cfg.has_option('glibc-tools', setting):
      cfg.set('glibc-tools', setting, '')

  with open(cfgpath, 'w') as cfgfile:
    cfg.write(cfgfile)

if __name__ == "__main__":
  main(sys.argv[1:])
