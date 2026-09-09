#! /usr/bin/env python3

"""
glibc-tools configuration script, sets user config file for
toolchain, build, logs, and compilers directory, the identity
glibc-dev.py credits in a Reviewed-by trailer, and the machine or qemu
command glibc-dev.py runs the tests of each build tree with.
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
  parser.add_argument('--ssh', dest='ssh', metavar='TREE=MACHINE',
                      action='append', default=[],
                      help='Have glibc-dev.py run the tests of build tree '
                           'TREE (a directory name under the build '
                           'directory, or a glob such as "aarch64*") on '
                           'MACHINE over ssh; an empty MACHINE ("TREE=") '
                           'drops the entry.  May be given several times')
  parser.add_argument('--qemu', dest='qemu', metavar='TREE=COMMAND',
                      action='append', default=[],
                      help='Likewise under qemu-user, COMMAND being what '
                           'glibc-dev.py --qemu takes ("aarch64", a qemu '
                           'command line, or "binfmt" to run the tests as '
                           'they are through the binfmt_misc of the kernel)')
  return parser

def set_mappings(parser, cfg, section, entries):
  """Record the TREE=VALUE entries of --ssh/--qemu in their section, an
  empty value removing the tree's entry."""
  for entry in entries:
    tree, sep, value = entry.partition('=')
    if not sep or not tree.strip():
      parser.error('--%s expects TREE=..., got %r' % (section, entry))
    tree, value = tree.strip(), value.strip()
    if not cfg.has_section(section):
      cfg.add_section(section)
    if value:
      cfg.set(section, tree, value)
    else:
      cfg.remove_option(section, tree)

def main(argv):
  parser = get_parser()
  opts = parser.parse_args(argv)

  cfgpath = os.path.expanduser(CFGPATH)
  cfg = configparser.RawConfigParser()
  # Build tree names are kept as spelled: glibc-dev.py matches them as is.
  cfg.optionxform = str
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

  # Where glibc-dev.py runs the tests of each build tree.
  set_mappings(parser, cfg, 'ssh', opts.ssh)
  set_mappings(parser, cfg, 'qemu', opts.qemu)

  with open(cfgpath, 'w') as cfgfile:
    cfg.write(cfgfile)

if __name__ == "__main__":
  main(sys.argv[1:])
