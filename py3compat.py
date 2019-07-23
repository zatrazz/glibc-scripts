# python3 compat adjustments

from pathlib import Path
import os

# Path.home was added in version 3.5.
try:
  Path.home()
except:
  def _gethomedir():
    try:
      return os.environ['HOME']
    except KeyError:
      import pwd
      return pwd.getpwuid(os.getuid()).pw_dir
  Path.home = _gethomedir
