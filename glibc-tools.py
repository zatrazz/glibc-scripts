#! /usr/bin/env python3

import sys
import os
import shutil
import argparse
import subprocess
import platform
from itertools import chain
import configparser
from py3compat import *
from collections import OrderedDict
import concurrent.futures

"""
glibc-tools.py is a script that configures, build, and check multiple
glibc builds using different compilers targerting different architectures.
"""

PATHS = {}

ACTIONS = (
  'configure',
  'copylibs',
  'make',
  'check',
  'check-abi',
  'update-abi',
  'bench-build',
  'list')

def read_config(gccversion):
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
  PATHS['gccversion'] = "{0}{1}".format("-gcc" if gccversion else "", gccversion)
  PATHS['compilers'] = PATHS['compilers'] + gccversion

def remove_dirs(*args):
  """Remove directories and their contents if they exist."""
  for dir in args:
    shutil.rmtree(dir, ignore_errors=True)

def remove_recreate_dirs(*args):
  """Remove directories if they exist, and create them as empty."""
  remove_dirs(*args)
  for dir in args:
    os.makedirs(dir, exist_ok=True)

def create_file(filename):
  os.makedirs(os.path.dirname(filename), exist_ok=True)
  return open(filename, "w");

def build_dir(abi):
  return PATHS['builddir'] + '/' + abi + PATHS['gccversion']

PLATFORM_MAP = { "ppc64le" : "powerpc64le" };

def build_triplet():
  platstr = platform.machine()
  if platstr in PLATFORM_MAP:
    platstr = PLATFORM_MAP[platstr];
  return platstr + "-linux-gnu"

class Config(object):
  """A configuration for building a compiler and associated libraries."""

  def __init__(self, ctx, arch, os_name, variant=None, glibcs=None,
               extra_glibcs=None):
    """Initialize a Config object."""
    self.ctx = ctx
    self.arch = arch
    self.os = os_name
    self.variant = variant
    if variant is None:
      self.name = '%s-%s' % (arch, os_name)
    else:
      self.name = '%s-%s-%s' % (arch, os_name, variant)
    self.triplet = '%s-glibc-%s' % (arch, os_name)
    if glibcs is None:
      glibcs = [{'variant': variant}]
    if extra_glibcs is None:
      extra_glibcs = []
    glibcs = [Glibc(self, **g) for g in glibcs]
    extra_glibcs = [Glibc(self, **g) for g in extra_glibcs]
    self.all_glibcs = glibcs + extra_glibcs
    self.glibcs = glibcs


class bcolors:
  HEADER = '\033[95m'
  OKBLUE = '\033[94m'
  OKCYAN = '\033[36m'
  OKGREEN = '\033[92m'
  WARNING = '\033[93m'
  FAIL = '\033[91m'
  ENDC = '\033[0m'
  BOLD = '\033[1m'
  UNDERLINE = '\033[4m'


def run_cmd(abi, action, cmd):
  builddir = build_dir (abi)
  outfile = create_file(PATHS["logsdir"] + '/' + abi + '_' + action + '.out')
  errfile = create_file(PATHS["logsdir"] + '/' + abi + '_' + action + '.err')
  proc = subprocess.Popen(cmd, cwd=builddir, stdout=outfile, stderr=errfile)
  proc.wait()
  return (abi, proc.returncode)


class Context(object):
  def __init__ (self, opts):
    self.parallelize = opts.parallelize[0]
    self.build_jobs = opts.parallelize[1]

    self.run_built_tests = 'yes' if opts.run_built_tests else 'no'

    self.extra_config_opts = []
    self.extra_config_opts.append("--enable-stack-protector={}".format(opts.enable_stackprot))
    if opts.disable_pie:
      self.extra_config_opts.append("--disable-default-pie")
    self.extra_config_opts.append("--enable-tunables={}".format(opts.enable_tunables))
    self.extra_config_opts.append("--enable-bind-now={}".format(opts.enable_bind_now))
    self.extra_config_opts.append("--enable-profile={}".format(opts.enable_profile))
    if opts.enable_multiarch == False:
      self.extra_config_opts.append("--disable-multi-arch")
    if opts.disable_werror == True:
      self.extra_config_opts.append("--disable-werror")
    if opts.with_kernel:
      self.extra_config_opts.append("--enable-kernel={}".format(opts.with_kernel))
    if opts.hardcoded:
      self.extra_config_opts.append("--enable-hardcoded-path-in-tests")
    if opts.cflags:
      self.extra_config_opts.append("CFLAGS={}".format(opts.cflags))


    self.keep = opts.keep
    self.status_log_list = []
    self.glibc_configs = {}
    self.configs = {}
    self.add_all_configs()

  CMD_MAP = OrderedDict([
    ("copylibs",
      (lambda self, abi : self.glibc_configs[abi].copylibs(),
       [])),
    ("configure",
      (lambda self, abi : self.glibc_configs[abi].configure(self.extra_config_opts),
       ["configure", "copylibs"])),
    ("make",
      (lambda self, abi : self.glibc_configs[abi].build(),
       ["configure", "copylibs", "make"])),
    ("check",
      (lambda self, abi : self.glibc_configs[abi].check(),
       ["configure", "copylibs", "make", "check"])),
    ("check-abi",
      (lambda self, abi : self.glibc_configs[abi].check_abi(),
       ["configure", "make", "check-abi"])),
    ("update-abi",
      (lambda self, abi : self.glibc_configs[abi].update_abi(),
       ["configure", "make", "update-abi"])),
    ("bench-build",
      (lambda self, abi : self.glibc_configs[abi].bench_build(),
       ["configure", "make", "bench-build"])),
  ])

  def run(self, action, glibcs):
    if not glibcs:
      glibcs = sorted(self.glibc_configs.keys())

    if action == "list":
      return self.list_configs(glibcs)

    cmds = OrderedDict((action, OrderedDict()) for action in ACTIONS)

    cmd = self.CMD_MAP[action]
    for abi in glibcs:
      for act in cmd[1]:
        cmds[act][abi] = self.CMD_MAP[act][0](self, abi)

      if self.keep is False:
        remove_recreate_dirs(build_dir (abi))

    with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallelize) \
         as executor:
      for action, cmds in cmds.items():
        if len(cmds) == 0:
          continue

        future_to_abi = {executor.submit(run_cmd, abi, action, cmds[abi]) : \
                         abi for abi in cmds.keys()}
        for future in concurrent.futures.as_completed(future_to_abi):
          abi = future_to_abi[future]
          try:
            (abi, resultcode) = future.result()
          except Exception as exc:
            print('%r generated an exception: %s' % (abi, exc))
          else:
            msg = "%s | %s" % (action, abi)
            if resultcode == 0:
              print (bcolors.OKBLUE + "PASS : " + bcolors.ENDC + msg)
            else:
              print (bcolors.FAIL + "FAIL : " + bcolors.ENDC + msg)


  def add_config(self, **args):
    """Add an individual build configuration."""
    cfg = Config(self, **args)
    if cfg.name in self.configs:
      print('error: duplicate config %s' % cfg.name)
      exit(1)
    self.configs[cfg.name] = cfg
    for c in cfg.all_glibcs:
      if c.name in self.glibc_configs:
        print('error: duplicate glibc config %s' % c.name)
        exit(1)
      self.glibc_configs[c.name] = c


  def add_all_configs(self):
    """Add all known glibc build configurations."""
    self.add_config(arch='aarch64',
                    os_name='linux-gnu')
    self.add_config(arch='aarch64_be',
                    os_name='linux-gnu')
    self.add_config(arch='arc',
                    os_name='linux-gnu')
    self.add_config(arch='arc',
                    os_name='linux-gnuhf')
    self.add_config(arch='arceb',
                    os_name='linux-gnu')
    self.add_config(arch='alpha',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'ev6', 'ccopts': '-mcpu=ev6'}])
    self.add_config(arch='arm',
                    os_name='linux-gnueabi',
                    glibcs=[{},
                            {'arch' : 'armv7', 'ccopts': '-march=armv7-a'}])
    self.add_config(arch='arm',
                    os_name='linux-gnueabihf',
                    glibcs=[{},
                            {'arch' : 'armv5',
                             'ccopts': '-march=armv5te -mfpu=vfpv3'},
                            {'arch' : 'armv6',
                             'ccopts': '-march=armv6 -mfpu=vfpv3'},
                            {'arch' : 'armv6t2',
                             'ccopts': '-march=armv6t2 -mfpu=vfpv3'},
                            {'arch' : 'armv7',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3'},
                            {'arch' : 'armv7-thumb',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3 -mthumb'},
                            {'variant': 'armv7-disable-multi-arch',
			     'ccopts' : '-march=armv7-a -mfpu=vfpv3',
			     'cfg' : ["--disable-multi-arch"]},
                            {'arch' : 'armv7-neon',
                             'ccopts': '-march=armv7-a -mfpu=neon'},
                            {'arch' : 'armv7-neonhard',
                             'ccopts': '-march=armv7-a -mfpu=neon -mfloat-abi=hard'}])
    self.add_config(arch='armeb',
                    os_name='linux-gnueabihf',
                    glibcs=[{},
                            {'arch' : 'armeb-v5',
                             'ccopts': '-march=armv5te -mfpu=vfpv3'},
                            {'arch' : 'armeb-v6',
                             'ccopts': '-march=armv6 -mfpu=vfpv3'},
                            {'arch' : 'armeb-v6t2',
                             'ccopts': '-march=armv6t2 -mfpu=vfpv3'},
                            {'arch' : 'armeb-v7',
                             'ccopts': '-march=armv7-a -mfpu=vfpv3'},
                            {'variant': 'armv7-disable-multi-arch',
			     'ccopts' : '-march=armv7-a -mfpu=vfpv3',
			     'cfg' : ["--disable-multi-arch"]},
                            {'arch' : 'armeb-v7neon',
                             'ccopts': '-march=armv7-a -mfpu=neon'},
                            {'arch' : 'armeb-v7neonhard',
                             'ccopts': '-march=armv7-a -mfpu=neon -mfloat-abi=hard'}])
    self.add_config(arch='hppa',
                    os_name='linux-gnu')
    self.add_config(arch='ia64',
                    os_name='linux-gnu')
    self.add_config(arch='i686',
                    os_name='gnu')
    self.add_config(arch='m68k',
                    os_name='linux-gnu')
    self.add_config(arch='m68k',
                    os_name='linux-gnu',
                    variant='coldfire')
    self.add_config(arch='microblaze',
                    os_name='linux-gnu')
    self.add_config(arch='microblazeel',
                    os_name='linux-gnu')
    self.add_config(arch='mips64',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'mips64-n32'},
                            {'arch': 'mips',
                             'ccopts': '-mabi=32'},
                            {'arch': 'mips',
                             'variant' : 'mips16',
                             'ccopts': '-mabi=32 -mips16'},
                            {'arch': 'mips64',
                             'ccopts': '-mabi=64'}])
    self.add_config(arch='mips64',
                    os_name='linux-gnu',
                    variant='soft',
                    glibcs=[{'arch': 'mips', 'variant' : 'soft',
                             'ccopts': '-mabi=32'}])
    self.add_config(arch='mips64el',
                    os_name='linux-gnu',
                    glibcs=[{'arch': 'mips64el-n32'},
                            {'arch': 'mipsel',
                             'ccopts': '-mabi=32'},
                            {'arch': 'mips64el',
                             'ccopts': '-mabi=64'}])
    self.add_config(arch='nios2',
                    os_name='linux-gnu')
    self.add_config(arch='or1k',
                    os_name='linux-gnu',
                    variant='soft')
    self.add_config(arch='powerpc',
                    os_name='linux-gnu',
                    variant='soft')
    self.add_config(arch='powerpc',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'power4',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4"]},
                            {'variant': 'power5',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5"]},
                            {'variant': 'power5+', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+"]},
                            {'variant': 'power6',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6"]},
                            {'variant': 'power6x', 'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6"]},
                            {'variant': 'power7',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7"]},
                            {'variant': 'power8',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8"]},
                            {'variant': 'power9',  'ccopts': '-mcpu=power9',  'cfg' : ["--with-cpu=power9"]},
                            {'variant': 'power10',  'ccopts': '-mcpu=power10',  'cfg' : ["--with-cpu=power10"]},
                            {'variant': 'power4-disable-multi-arch',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4",  "--disable-multi-arch"]},
                            {'variant': 'power5-disable-multi-arch',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5",  "--disable-multi-arch"]},
                            {'variant': 'power5+-disable-multi-arch', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+", "--disable-multi-arch"]},
                            {'variant': 'power6-disable-multi-arch',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6",  "--disable-multi-arch"]},
                            {'variant': 'power6x-disable-multi-arch', 'ccopts': '-mcpu=power6x', 'cfg' : ["--with-cpu=power6x", "--disable-multi-arch"]},
                            {'variant': 'power7-disable-multi-arch',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7",  "--disable-multi-arch"]},
                            {'variant': 'power8-disable-multi-arch',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8",  "--disable-multi-arch"]},
                            {'variant': 'disable-multi-arch', 'cfg' :  ["--disable-multi-arch"]}])
    self.add_config(arch='powerpc64',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'power4',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4"]},
                            {'variant': 'power5',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5"]},
                            {'variant': 'power5+', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+"]},
                            {'variant': 'power6',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6"]},
                            {'variant': 'power6x', 'ccopts': '-mcpu=power6x', 'cfg' : ["--with-cpu=power6x"]},
                            {'variant': 'power7',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7"]},
                            {'variant': 'power8',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8"]},
                            {'variant': 'power4-disable-multi-arch',  'ccopts': '-mcpu=power4',  'cfg' : ["--with-cpu=power4",  "--disable-multi-arch"]},
                            {'variant': 'power5-disable-multi-arch',  'ccopts': '-mcpu=power5',  'cfg' : ["--with-cpu=power5",  "--disable-multi-arch"]},
                            {'variant': 'power5+-disable-multi-arch', 'ccopts': '-mcpu=power5+', 'cfg' : ["--with-cpu=power5+", "--disable-multi-arch"]},
                            {'variant': 'power6-disable-multi-arch',  'ccopts': '-mcpu=power6',  'cfg' : ["--with-cpu=power6",  "--disable-multi-arch"]},
                            {'variant': 'power6x-disable-multi-arch', 'ccopts': '-mcpu=power6x', 'cfg' : ["--with-cpu=power6x", "--disable-multi-arch"]},
                            {'variant': 'power7-disable-multi-arch',  'ccopts': '-mcpu=power7',  'cfg' : ["--with-cpu=power7",  "--disable-multi-arch"]},
                            {'variant': 'power8-disable-multi-arch',  'ccopts': '-mcpu=power8',  'cfg' : ["--with-cpu=power8",  "--disable-multi-arch"]},
                            {'variant': 'disable-multi-arch', 'cfg' : ["--disable-multi-arch"]}])
    self.add_config(arch='powerpc64le',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'power8', 'ccopts': '-mcpu=power8', 'cfg' : ["--with-cpu=power8"]},
                            {'variant': 'power9', 'ccopts': '-mcpu=power9', 'cfg' : ["--with-cpu=power9"]},
                            {'variant': 'power10', 'ccopts': '-mcpu=power10', 'cfg' : ["--with-cpu=power10"]},
                            {'variant': 'power7-disable-multi-arch', 'ccopts': '-mcpu=power7', 'cfg' : ["--with-cpu=power8", "--disable-multi-arch"]},
                            {'variant': 'power8-disable-multi-arch', 'ccopts': '-mcpu=power8', 'cfg' : ["--with-cpu=power8", "--disable-multi-arch"]},
                            {'variant': 'power9-disable-multi-arch', 'ccopts': '-mcpu=power9', 'cfg' : ["--with-cpu=power9", "--disable-multi-arch"]},
                            {'variant': 'power10-disable-multi-arch', 'ccopts': '-mcpu=power10', 'cfg' : ["--with-cpu=power10", "--disable-multi-arch"]},
                            {'variant': 'disable-multi-arch', 'cfg' : ["--disable-multi-arch"]}])
    self.add_config(arch='riscv32',
                    os_name='linux-gnu',
                    variant='rv32imac-ilp32')
    self.add_config(arch='riscv32',
                    os_name='linux-gnu',
                    variant='rv32imafdc-ilp32')
    self.add_config(arch='riscv32',
                    os_name='linux-gnu',
                    variant='rv32imafdc-ilp32d')
    self.add_config(arch='riscv64',
                    os_name='linux-gnu',
                    variant='rv64imac-lp64')
    self.add_config(arch='riscv64',
                    os_name='linux-gnu',
                    variant='rv64imafdc-lp64')
    self.add_config(arch='riscv64',
                    os_name='linux-gnu',
                    variant='rv64imafdc-lp64d')
    self.add_config(arch='s390x',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'z900', 'ccopts': '-march=z900'}, # arch5
                            {'variant': 'z10', 'ccopts': '-march=z10'},   # arch8
                            {'variant': 'z196', 'ccopts': '-march=z196'}, # arch9
                            {'arch'   : 's390', 'ccopts': '-m31'}])
    self.add_config(arch='csky',
                    os_name='linux-gnuabiv2',
                    variant='soft')
    self.add_config(arch='csky',
                    os_name='linux-gnuabiv2')
    self.add_config(arch='sh4',
                    os_name='linux-gnu')
    self.add_config(arch='sh4eb',
                    os_name='linux-gnu')
    self.add_config(arch='sh4',
                    os_name='linux-gnu',
                    variant='soft')
    self.add_config(arch='sh4eb',
                    os_name='linux-gnu',
                    variant='soft')
    self.add_config(arch='sparc64',
                    os_name='linux-gnu',
                    glibcs=[{'ccopts' : "-mcpu=niagara"},
                            {'arch': 'sparc',
                             'ccopts': '-m32 -mlong-double-128 -mcpu=leon3'},
                            {'arch': 'sparcv8',
                             'ccopts': '-m32 -mlong-double-128 -mcpu=leon3'},
                            {'arch': 'sparcv9',
                             'ccopts': '-m32 -mlong-double-128 -mcpu=v9'}],
                    extra_glibcs=[{'variant': 'disable-multi-arch',
                                   'cfg': ['--disable-multi-arch']},
                                  {'variant': 'disable-multi-arch',
                                   'arch': 'sparcv9',
                                   'ccopts': '-m32 -mlong-double-128 -mcpu=v9',
                                   'cfg': ['--disable-multi-arch']}])
    self.add_config(arch='x86_64',
                    os_name='linux-gnu',
                    glibcs=[{},
                            {'variant': 'x32', 'ccopts': '-mx32'},
                            {'arch': 'i686', 'ccopts': '-m32 -march=i686'},
                            {'variant': 'v2', 'ccopts' : '-march=x86-64-v2'},
                            {'variant': 'v3', 'ccopts' : '-march=x86-64-v3'},
                            {'variant': 'v4', 'ccopts' : '-march=x86-64-v4'}],
                    extra_glibcs=[{'variant': 'disable-multi-arch',
                                   'cfg': ['--disable-multi-arch']},
                                  {'variant': 'disable-multi-arch',
                                   'arch': 'i686',
                                   'ccopts': '-m32 -march=i686',
                                   'cfg': ['--disable-multi-arch']},
                                  {'arch': 'i486',
                                   'ccopts': '-m32 -march=i486'},
                                  {'arch': 'i586',
                                   'ccopts': '-m32 -march=i586'},
                                  {'variant': 'fp',
                                   'arch': 'i686',
                                   'ccopts': '-m32 -march=i686 -fno-omit-frame-pointer'}])

  def list_configs(self, glibcs):
    for abi in glibcs:
      print(abi)


class Glibc(object):
  """A configuration for building glibc."""

  def __init__(self, compiler, arch=None, os_name=None, variant=None,
               cfg=None, ccopts=None):
    """Initialize a Glibc object."""
    self.ctx = compiler.ctx
    self.compiler = compiler
    if arch is None:
      self.arch = compiler.arch
    else:
      self.arch = arch
    if os_name is None:
      self.os = compiler.os
    else:
      self.os = os_name
    self.variant = variant
    if variant is None:
      self.name = '%s-%s' % (self.arch, self.os)
    else:
      self.name = '%s-%s-%s' % (self.arch, self.os, variant)
    self.triplet = '%s-glibc-%s' % (self.arch, self.os)
    self.host_triplet = '%s-%s' % (self.arch, self.os)
    if cfg is None:
      self.cfg = []
    else:
      self.cfg = cfg
    self.ccopts = ccopts

  def tool_name(self, tool):
    """Return the name of a cross-compilation tool."""
    ctool = PATHS["compilers"] + '/' + self.compiler.name + '/bin/';
    ctool += self.compiler.triplet + '-' + tool
    if self.ccopts and (tool == 'gcc' or tool == 'g++'):
      ctool = '%s %s' % (ctool, self.ccopts)
    return ctool

  def configure(self, extra_config_opts):
    cmd = [PATHS["srcdir"] + '/configure',
           '--prefix=/usr',
           '--build=%s' % build_triplet(),
           '--host=%s' % self.host_triplet,
           'CC=%s' % self.tool_name('gcc'),
           'CXX=%s' % self.tool_name('g++'),
           'AR=%s' % self.tool_name('ar'),
           'AS=%s' % self.tool_name('as'),
           'LD=%s' % self.tool_name('ld'),
           'NM=%s' % self.tool_name('nm'),
           'OBJCOPY=%s' % self.tool_name('objcopy'),
           'OBJDUMP=%s' % self.tool_name('objdump'),
           'RANLIB=%s' % self.tool_name('ranlib'),
           'READELF=%s' % self.tool_name('readelf'),
           'STRIP=%s' % self.tool_name('strip')]
    if self.os == 'gnu':
       cmd += ['MIG=%s' % self.tool_name('mig')]
    cmd = cmd + extra_config_opts
    cmd += self.cfg
    return cmd

  def lib_name(self, lib):
    cmd = self.tool_name("gcc") + " -print-file-name={}".format(lib)
    stdout = subprocess.getoutput(cmd)
    return stdout

  def copylibs(self):
    libgcc = self.lib_name("libgcc_s.so.1")
    if libgcc == "libgcc_s.so.1":
      libgcc = self.lib_name("libgcc_s.so")
    libstdcxx = self.lib_name("libstdc++.so.6")
    return ["cp", libgcc, libstdcxx, build_dir (self.name)]

  def build(self):
    return ['make',
            '-j%d' % (self.ctx.build_jobs)]

  def check(self):
    return ['make',
            'check',
            'run-built-tests=%s' % (self.ctx.run_built_tests),
            '-j%d' % (self.ctx.build_jobs)]

  def check_abi(self):
    return ['make',
            'check-abi',
            '-j%d' % (self.ctx.build_jobs)]

  def update_abi(self):
    return ['make',
            'update-abi',
            '-j%d' % (self.ctx.build_jobs)]

  def bench_build(self):
    return ['make',
            'bench-build',
            '-j%d' % (self.ctx.build_jobs)]


def parallelize_type(string):
  fields = string.split(':')
  if len(fields) == 1:
    return [ int(fields[0]), 1 ]
  return [ int(fields[0]), int(fields[1]) ]

SPECIAL_LISTS = {
  # Most of the supported Linux ABIs
  "linux" : [
    "aarch64-linux-gnu",
    "alpha-linux-gnu",
    "arc-linux-gnuhf",
    "arm-linux-gnueabihf",
    "csky-linux-gnuabiv2",
    "hppa-linux-gnu",
    "i686-linux-gnu",
    "ia64-linux-gnu",
    "m68k-linux-gnu",
    "microblaze-linux-gnu",
    "mips64-linux-gnu",
    "mips64-n32-linux-gnu",
    "mips-linux-gnu",
    "nios2-linux-gnu",
    "or1k-linux-gnu-soft",
    "powerpc64le-linux-gnu",
    "powerpc64-linux-gnu",
    "powerpc-linux-gnu",
    "riscv32-linux-gnu-rv32imafdc-ilp32d",
    "riscv64-linux-gnu-rv64imafdc-lp64d",
    "s390-linux-gnu",
    "s390x-linux-gnu",
    "sh4-linux-gnu",
    "sparc64-linux-gnu",
    "sparcv9-linux-gnu",
    "x86_64-linux-gnu",
    "x86_64-linux-gnu-x32",
  ],

  # All support triple ABIs with *.abilist (used to update-abi command).
  "abi" : [
    "aarch64-linux-gnu",
    "alpha-linux-gnu",
    "arc-linux-gnuhf",
    "arm-linux-gnueabihf",
    "armeb-linux-gnueabihf",
    "csky-linux-gnuabiv2",
    "hppa-linux-gnu",
    "i686-linux-gnu",
    "ia64-linux-gnu",
    "m68k-linux-gnu",
    "m68k-linux-gnu-coldfire",
    "microblaze-linux-gnu",
    "microblazeel-linux-gnu",
    "mips64-linux-gnu",
    "mips64-n32-linux-gnu",
    "mips-linux-gnu",
    "mips-linux-gnu-soft",
    "nios2-linux-gnu",
    "or1k-linux-gnu-soft",
    "powerpc64le-linux-gnu",
    "powerpc64-linux-gnu",
    "powerpc-linux-gnu",
    "powerpc-linux-gnu-soft",
    "riscv32-linux-gnu-rv32imafdc-ilp32d",
    "riscv64-linux-gnu-rv64imafdc-lp64d",
    "s390-linux-gnu",
    "s390x-linux-gnu",
    "sh4-linux-gnu",
    "sh4eb-linux-gnu",
    "sparc64-linux-gnu",
    "sparcv9-linux-gnu",
    "x86_64-linux-gnu",
    "x86_64-linux-gnu-x32",

    "i686-gnu",
  ],

  "abi32" : [
    "arm-linux-gnueabihf",
    "armeb-linux-gnueabihf",
    "csky-linux-gnuabiv2",
    "hppa-linux-gnu",
    "i686-linux-gnu",
    "m68k-linux-gnu",
    "m68k-linux-gnu-coldfire",
    "microblaze-linux-gnu",
    "microblazeel-linux-gnu",
    "mips64-n32-linux-gnu",
    "mips-linux-gnu",
    "mips-linux-gnu-soft",
    "nios2-linux-gnu",
    "or1k-linux-gnu-soft",
    "powerpc-linux-gnu",
    "powerpc-linux-gnu-soft",
    "s390-linux-gnu",
    "sh4-linux-gnu",
    "sh4eb-linux-gnu",
    "sparcv9-linux-gnu",
  ],

  "powerpc64le" : [
    "powerpc64le-linux-gnu",
    "powerpc64le-linux-gnu-disable-multi-arch",
    "powerpc64le-linux-gnu-power10",
    "powerpc64le-linux-gnu-power9",
    "powerpc64le-linux-gnu-power8",
    "powerpc64le-linux-gnu-power10-disable-multi-arch",
    "powerpc64le-linux-gnu-power9-disable-multi-arch",
    "powerpc64le-linux-gnu-power8-disable-multi-arch",
  ],

  "powerpc64": [
    "powerpc64-linux-gnu-power8",
    "powerpc64-linux-gnu-power7",
    "powerpc64-linux-gnu-power6x",
    "powerpc64-linux-gnu-power6",
    "powerpc64-linux-gnu-power5+",
    "powerpc64-linux-gnu-power5",
    "powerpc64-linux-gnu-power4",
    "powerpc64-linux-gnu-power8-disable-multi-arch",
    "powerpc64-linux-gnu-power7-disable-multi-arch",
    "powerpc64-linux-gnu-power6x-disable-multi-arch",
    "powerpc64-linux-gnu-power6-disable-multi-arch",
    "powerpc64-linux-gnu-power5+-disable-multi-arch",
    "powerpc64-linux-gnu-power5-disable-multi-arch",
    "powerpc64-linux-gnu-power4-disable-multi-arch",
    "powerpc64-linux-gnu",
    "powerpc64-linux-gnu-disable-multi-arch",
  ],

  "powerpc": [
    "powerpc-linux-gnu-power8",
    "powerpc-linux-gnu-power7",
    "powerpc-linux-gnu-power6x",
    "powerpc-linux-gnu-power6",
    "powerpc-linux-gnu-power5+",
    "powerpc-linux-gnu-power5",
    "powerpc-linux-gnu-power4",
    "powerpc-linux-gnu-power8-disable-multi-arch",
    "powerpc-linux-gnu-power7-disable-multi-arch",
    "powerpc-linux-gnu-power6x-disable-multi-arch",
    "powerpc-linux-gnu-power6-disable-multi-arch",
    "powerpc-linux-gnu-power5+-disable-multi-arch",
    "powerpc-linux-gnu-power5-disable-multi-arch",
    "powerpc-linux-gnu-power4-disable-multi-arch",
    "powerpc-linux-gnu",
  ],

  "zseries": [
    "s390-linux-gnu",
    "s390x-linux-gnu",
    "s390x-linux-gnu-z10",
    "s390x-linux-gnu-z196",
    "s390x-linux-gnu-z900",
  ],

  "arm": [
    "arm-linux-gnueabihf",
    "arm-linux-gnueabihf-armv7-disable-multi-arch",
    "armv5-linux-gnueabihf",
    "armv6-linux-gnueabihf",
    "armv6t2-linux-gnueabihf",
    "armv7-linux-gnueabihf",
    "armv7-neon-linux-gnueabihf",
    "armv7-neonhard-linux-gnueabihf",
    "armv7-thumb-linux-gnueabihf",
  ],

  "armeb": [
    "armeb-linux-gnueabihf",
    "armeb-v5-linux-gnueabihf",
    "armeb-v6-linux-gnueabihf",
    "armeb-v6t2-linux-gnueabihf",
    "armeb-v7-linux-gnueabihf",
    "armeb-v7neon-linux-gnueabihf",
    "armeb-v7neonhard-linux-gnueabihf",
  ]
}

def get_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('-p', dest='parallelize',
                      help='Run a number of parallel build with make -j',
                      type=parallelize_type, default="1:%s" % os.cpu_count())
  parser.add_argument('-k', dest='keep',
                      help='Keep old file and just run the command',
                      action='store_true', default=False)
  parser.add_argument('-t', dest='run_built_tests',
                      help='Run built tests',
                      action='store_true', default=False)
  parser.add_argument('--enable-stack-protector', dest='enable_stackprot',
                      help='Enable stack protection',
                      choices=('no', 'yes', 'all', 'strong'), default='all')
  parser.add_argument('--enable-tunables', dest='enable_tunables',
                      help='Enable tunables (default is yes)',
                      choices=('yes', 'no'), default='yes')
  parser.add_argument('--disable-default-pie', dest='disable_pie',
                      help='Disable PIE (default is yes)',
                      action='store_true', default=False)
  parser.add_argument('--enable-bind-now', dest='enable_bind_now',
                      help='Enable bind now (default is yes)',
                      choices=('yes', 'no'), default='yes')
  parser.add_argument('--enable-profile', dest='enable_profile',
                      help='Enable profile (default is no)',
                      choices=('yes', 'no'), default='no')
  parser.add_argument('--disable-multi-arch', dest='enable_multiarch',
                      help='Disable iFUNC sysdep selection',
                      action='store_false', default=True)
  parser.add_argument('--disable-werror', dest='disable_werror',
                      help='Do not use -Werror',
                      action='store_true', default=False)
  parser.add_argument('--enable-hardcoded-path-in-tests', dest='hardcoded',
                      help='Hardcode newly built glibc path in tests',
                      action='store_true', default=False)
  parser.add_argument('--enable-kernel', dest='with_kernel',
                      help='Build with --enable-kernel')
  parser.add_argument('--gccversion', dest='gccversion',
                      help='Use a different gcc version', default='')
  parser.add_argument('--cflags', dest='cflags',
                      help='Add the CFLAGS on build configuration', default='')
  parser.add_argument('action',
                      help='What to do',
                      choices=ACTIONS)
  parser.add_argument('configs',
                      help='Configurations to build (ex. x86_64-linux-gnu)',
                      nargs='*')
  return parser


def main(argv):
  parser = get_parser()
  opts = parser.parse_args(argv)
  ctx = Context(opts)

  read_config (opts.gccversion)

  configs = list(chain.from_iterable(SPECIAL_LISTS.get(c, [c]) for c in opts.configs))

  ctx.run(opts.action, configs)

if __name__ == "__main__":
  main(sys.argv[1:])
