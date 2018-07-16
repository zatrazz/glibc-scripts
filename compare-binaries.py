#! /usr/bin/env python3

import sys
import subprocess
import tempfile

def run_objdump_diff(base, patched):
  objdump = "objdump"
  mode = None
  try:
    tmpb = tempfile.NamedTemporaryFile(delete=False)
    outb = subprocess.check_output([objdump, "-d", base])
    tmpb.write(outb)

    tmpp = tempfile.NamedTemporaryFile(delete=False)
    outp = subprocess.check_output([objdump, "-d", patched])
    tmpp.write(outp)

    diffp = subprocess.Popen(["diff", "-u", tmpb.name, tmpp.name],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    diff, err = diffp.communicate()
    sys.stdout.buffer.write(diff)
    tmpb.close()
    tmpb.close()
  except subprocess.CalledProcessError as e:
    print ("error: %s failed" % (e.cmd))

def main(argv):
  if len(argv) < 2:
    print("usage: compare-binaries.py <default> <patched>")
    sys.exit(0)
  run_objdump_diff(argv[0], argv[1])

if __name__ == "__main__":
  main(sys.argv[1:])
