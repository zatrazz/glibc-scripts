#! /usr/bin/env python3

import sys
import argparse

"""
find-maps.py is a script to find the memory map of the given address from
a specified pid.
"""

def find_map(pid, address):
  with open ("/proc/" + str (pid) + "/maps", "r") as f:
    for line in f:
      line = line.replace('\n', '')
      fields = line.split()
      ran = fields[0].split('-')
      start = int (ran[0], 16)
      end = int (ran[1], 16)
      if address >= start and address < end:
        print (" => " + line)
      else:
        print ("    " + line)

def get_parser ():
  def auto_int(x):
     return int(x, 0)
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('pid',
                      help='The process id',
                      type=int)
  parser.add_argument('address',
                      help='The memory address',
                      type=auto_int)
  return parser

def main (argv):
  parser = get_parser ()
  opts = parser.parse_args (argv)
  find_map (opts.pid, opts.address)

if __name__ == "__main__":
  main(sys.argv[1:])
