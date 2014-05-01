#!/usr/bin/env python
import sys, os
import options
from helpers import atoi

optspec = """
redo [targets...]
--
j,jobs=    maximum number of jobs to build at once
d,debug    print dependency checks as they happen
v,verbose  print commands as they are read from .do files (variables intact)
x,xtrace   print commands as they are executed (variables expanded)
k,keep-going  keep going as long as possible even if some targets fail
overwrite  overwrite files even if generated outside of redo
log        activate log recording (slower)
only-log   print only failed targets from log
shuffle    randomize the build order to find dependency bugs
debug-locks  print messages about file locking (useful for debugging)
debug-pids   print process ids as part of log messages (useful for debugging)
version    print the current version and exit
color      force enable color (--no-color to disable)
old-args   use old-style definitions of $1,$2,$3 (deprecated)
old-stdout use old-style stdout to create target (deprecated)
warn-stdout warn if stdout is used
main=      Choose which redo flavour to execute
"""

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

def read_opts():
    redo_flavour = os.path.splitext(os.path.basename(sys.argv[0]))[0]
    if redo_flavour == "redo-exec":
        return False, 1, redo_flavour, sys.argv[1:]

    o = options.Options(optspec)
    (opt, flags, extra) = o.parse(sys.argv[1:])

    if opt.overwrite:
        os.environ['REDO_OVERWRITE'] = '1'
    if opt.version:
        from version import TAG
        print TAG
        sys.exit(0)
    if opt.debug:
        os.environ['REDO_DEBUG'] = str(opt.debug or 0)
    if opt.verbose:
        os.environ['REDO_VERBOSE'] = '1'
    if opt.xtrace:
        os.environ['REDO_XTRACE'] = '1'
    if opt.keep_going:
        os.environ['REDO_KEEP_GOING'] = '1'
    if opt.shuffle:
        os.environ['REDO_SHUFFLE'] = '1'
    if opt.debug_locks:
        os.environ['REDO_DEBUG_LOCKS'] = '1'
    if opt.debug_pids:
        os.environ['REDO_DEBUG_PIDS'] = '1'
    if opt.old_args:
        os.environ['REDO_OLD_ARGS'] = '1'
    if opt.old_stdout:
        os.environ['REDO_OLD_STDOUT'] = '1'
    if opt.warn_stdout:
        os.environ['REDO_WARN_STDOUT'] = '1'
    if opt.color != None:
        os.environ['REDO_COLOR'] = str(opt.color)
    if opt.log != None:
        os.environ['REDO_LOG'] = str(opt.log)
    if opt.only_log:
        os.environ['REDO_ONLY_LOG'] = '1'
    if opt.main:
        redo_flavour = opt.main

    return True, atoi(opt.jobs or 1), redo_flavour, extra

def set_main(arg0):
    # When the module is imported, change the process title.
    # We do it here because this module is imported by all the scripts.
    try:
      	from setproctitle import setproctitle
    except ImportError:
      	pass
    else:
        args = sys.argv[1:]
        args.insert(0, arg0)
      	setproctitle(" ".join(args))


def init(targets, redo_flavour, redo_binaries=[]):
    if not os.environ.get('REDO'):
        if not targets and redo_flavour == 'redo':
            targets.append('all')

        dirname = os.path.dirname(os.path.realpath(__file__))
        paths = [os.path.join(dirname, "bin"),
                 os.path.join(dirname, "redo-sh")]

        bindir = None
        shdir = None

        for p in paths:
            p_redo = os.path.join(p, "redo")
            if not bindir and os.path.exists(p_redo):
                try:
                    from version import TAG as myver
                except:
                    pass
                else:
                    with os.popen("'%s' --version" % p_redo.replace("'", "'\"'\"'")) as f:
                        ver = f.read().strip()
                        if ver == myver:
                            bindir = p
                        elif os.environ.get('REDO_DEBUG'):
                            sys.stderr.write("%s: version %s different than %s\n" % (p_redo, ver, myver))
            elif not shdir and os.path.exists(os.path.join(p, "sh")):
                shdir = p
            if shdir and bindir:
                break

        if not bindir:
            bindir = os.path.join(os.getcwd(), ".redo", "bin")
            try: os.makedirs(bindir)
            except: pass
            main = os.path.realpath(__file__)
            for exe in redo_binaries:
                exe = os.path.join(bindir, exe)
                try: os.unlink(exe)
                except: pass
                os.symlink(main, exe)

        if bindir: os.environ['PATH'] = bindir + ":" + os.environ['PATH']
        if shdir:  os.environ['PATH'] = shdir  + ":" + os.environ['PATH']
        os.environ['REDO'] = os.path.join(bindir, "redo")

    if not os.environ.get('REDO_STARTDIR'):
        import runid
        os.environ['REDO_STARTDIR'] = os.getcwd()
        os.environ['REDO_RUNID_FILE'] = '.redo/runid'
        runid.change('.redo/runid')

    if not os.environ.get('REDO_STDIO'):
        os.environ['REDO_STDIO'] = "%d,%d,%d" % (os.dup(0), os.dup(1), os.dup(2))

try:
    from main import mains
    do_init, jobs, redo_flavour, targets = read_opts()
    
    if do_init:
        init(targets, redo_flavour, mains.keys())
        from log import err, debug
        import jwack

        if not redo_flavour.startswith("redo"):
            redo_flavour = "redo-%s" % redo_flavour
        if redo_flavour not in mains:
            err("invalid redo: %s\n", redo_flavour)
            sys.exit(1)

        set_main(redo_flavour)
        
        if jobs < 1 or jobs > 1000:
            err('invalid --jobs value: %r\n', opt.jobs)
        jwack.setup(jobs)
    
        debug("%s %r\n", redo_flavour, targets)

        import vars
        vars.init()

    sys.exit(mains[redo_flavour](redo_flavour, targets) or 0)
except KeyboardInterrupt:
    sys.exit(200)
