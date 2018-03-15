#! /usr/bin/env python

import sys

if __name__ == "__main__":
  pid = sys.argv[1]
  addr = int(sys.argv[2], 16)

  maps = open ("/proc/" + pid + "/maps")
  for line in maps:
    fields = line.split()
    ran = fields[0].split('-')
    start = int (ran[0], 16)
    end = int (ran[1], 16)
    if addr >= start and addr < end:
      print (line)
      break
