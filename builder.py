import sys, os, errno, stat, signal
import vars, state, jwack, deps, logger
from helpers import unlink, close_on_exec, join
from log import log, log_e, debug, debug2, debug3, err, warn, log_cmd


def _default_do_files(filename):
    l = filename.split('.')
    for i in range(1,len(l)+1):
        basename = join('.', l[:i])
        ext = join('.', l[i:])
        if ext: ext = '.' + ext
        yield ("default%s.do" % ext), basename, ext
    

def _possible_do_files(t):
    dirname,filename = os.path.split(t)
    yield (dirname, "%s.do" % filename, '', filename, '')
    yield (dirname, "%s.do" % filename, '', filename, '')

    # It's important to try every possibility in a directory before resorting
    # to a parent directory.  Think about nested projects: I don't want
    # ../../default.o.do to take precedence over ../default.do, because
    # the former one might just be an artifact of someone embedding my project
    # into theirs as a subdir.  When they do, my rules should still be used
    # for building my project in *all* cases.
    dirbits = os.path.abspath(dirname).split('/')
    for i in range(len(dirbits), -1, -1):
        basedir = os.path.join(dirname,
                               join('/', ['..'] * (len(dirbits) - i)))
        subdir = join('/', dirbits[i:])
        for dofile,basename,ext in _default_do_files(filename):
            yield (basedir, dofile,
                   subdir, os.path.join(subdir, basename), ext)

def _possible_do_files_in_do_dir(t):
    for dodir,dofile,basedir,basename,ext in _possible_do_files(t):
        yield (dodir,dofile,basedir,basename,ext)

        dodir2 = os.path.join(dodir, "do")
        if os.path.islink(dodir2):
            dodir2 = os.path.realpath(dodir2)
            d = os.path.relpath(os.path.abspath(dodir), dodir2)
            basedir2 = os.path.join(d, basedir)
            basename2= os.path.join(d, basename)
        else:
            basedir2 = os.path.join("..", basedir)
            basename2= os.path.join("..", basename)
        yield (dodir2, dofile, basedir2, basename2, ext)

def _find_do_file(f):
    for dodir,dofile,basedir,basename,ext in _possible_do_files_in_do_dir(f.name):
        if dodir and not os.path.isdir(dodir):
            # we don't want to normpath() unless we have no other choice.
            # otherwise we could have odd behaviour with symlinks (ie.
            # x/y/../z might not be the same as x/z).  On the other hand,
            # if one of the path elements doesn't exist (yet), normpath
            # can help us find the .do file anyway, and that .do file might
            # create the sub-path.
            dodir = os.path.normpath(dodir)
        dopath = os.path.join(dodir, dofile)
        debug2('%s: %s:%s ?\n', f.name, dodir, dofile)
        dof = state.File(dopath)
        if os.path.exists(dopath):
            f.add_dep(dof)
            return dodir,dofile,basedir,basename,ext
        else:
            f.add_dep(dof)
    return None,None,None,None,None


def _try_stat(filename):
    try:
        return os.stat(filename)
    except OSError, e:
        if e.errno == errno.ENOENT:
            return None
        else:
            raise

def _interpreter_locations(dodir):
    dodir = os.path.realpath(dodir)
    dirbits = dodir.split('/')
    for i in range(len(dirbits), -1, -1):
        d = join('/', dirbits[:i])
        yield(d)
        yield(os.path.join(d, "do"))
    

def _find_interpreter(dodir, name):
    for d in _interpreter_locations(dodir):
        interp = os.path.join(d, name)
        if (os.path.exists(interp) and
            not os.path.isdir(interp) and
            os.access(interp, os.X_OK)):
            debug("interpreter found: %s\n", interp)
            return interp
        else:
            debug("interpreter not found: %s\n", interp)

class BuildJob:
    def __init__(self, target, result, add_dep_to=None, delegate=None, re_do=True):
        self.target   = target
        self.result   = result
        self.parent   = add_dep_to
        self.delegate = delegate
        self.re_do    = re_do

    def prepare(self):
        assert self.target.dolock().owned == state.LOCK_EX
        self.target.build_starting()
        self.before_t = _try_stat(self.target.name)

        newstamp = self.target.read_stamp()
        if newstamp.is_override_or_missing(self.target):
            if newstamp.is_missing():
                # was marked generated, but is now deleted
                debug3('oldstamp=%r newstamp=%r\n', self.target.stamp, newstamp)
                self.target.forget()
                self.target.refresh()
            elif vars.OVERWRITE:
                warn('%s: you modified it; overwrite\n', self.target.printable_name())
            else:
                warn('%s: you modified it; skipping\n', self.target.printable_name())
                return 0
        if self.target.exists_not_dir() and not self.target.is_generated:
            # an existing source file that was not generated by us.
            # This step is mentioned by djb in his notes.
            # For example, a rule called default.c.do could be used to try
            # to produce hello.c, but we don't want that to happen if
            # hello.c was created in advance by the end user.
            if vars.OVERWRITE:
                warn('%s: exists and not marked as generated; overwrite.\n',
                     self.target.printable_name())
            else:
                warn('%s: exists and not marked as generated; not redoing.\n',
                     self.target.printable_name())
                debug2('-- static (%r)\n', self.target.name)
                return 0

        (self.dodir, self.dofile, self.dobasedir, self.dobasename, self.doext) = _find_do_file(self.target)
        if not self.dofile:
            if newstamp.is_missing():
                err('no rule to make %r\n', self.target.name)
                return 1
            else:
                self.target.forget()
                debug2('-- forget (%r)\n', self.target.name)
                return 0  # no longer a generated target, but exists, so ok

        self.outdir = self._mkoutdir()
        # name connected to stdout
        self.tmpname_sout = self.target.tmpfilename('out.tmp')
        # name provided as $3
        self.tmpname_arg3 = os.path.join(self.outdir, self.target.basename())
        # name for the log file
        unlink(self.tmpname_sout)
        unlink(self.tmpname_arg3)
        self.log_fd = logger.open_log(self.target, truncate=True)
        self.tmp_sout_fd = os.open(self.tmpname_sout, os.O_CREAT|os.O_RDWR|os.O_EXCL, 0666)
        close_on_exec(self.tmp_sout_fd, True)
        self.tmp_sout_f = os.fdopen(self.tmp_sout_fd, 'w+')

        return None

    def _mkoutdir(self):
        outdir = self.target.tmpfilename("out")
        if os.path.exists(outdir):
            import shutil
            shutil.rmtree(outdir)
        os.makedirs(outdir)
        return outdir

    def build(self):
        debug3('running build job for %r\n', self.target.name)

        (dodir, dofile, basedir, basename, ext) = (
            self.dodir, self.dofile, self.dobasedir, self.dobasename, self.doext)

        # this will run in the dofile's directory, so use only basenames here
        if vars.OLD_ARGS:
            arg1 = basename  # target name (no extension)
            arg2 = ext       # extension (if any), including leading dot
        else:
            arg1 = basename + ext  # target name (including extension)
            arg2 = basename        # target name (without extension)
        argv = ['sh', '-e',
                dofile,
                arg1,
                arg2,
                # temp output file name
                os.path.relpath(self.tmpname_arg3, dodir),
                ]
        if vars.VERBOSE: argv[1] += 'v'
        if vars.XTRACE: argv[1] += 'x'
        if vars.VERBOSE or vars.XTRACE: log_e('\n')

        firstline = open(os.path.join(dodir, dofile)).readline().strip()
        if firstline.startswith('#!.../'):
            _, _, interp_argv = firstline.partition("/")
            interp_argv = interp_argv.split(' ')
            interpreter = _find_interpreter(self.dodir, interp_argv[0])
            if not interpreter:
                err('%s unable to find interpreter %s.\n', self.dofile, interp_argv[0])
                os._exit(208)
            self.target.add_dep(state.File(interpreter))
            argv[0:2] = [interpreter] + interp_argv[1:]
        elif firstline.startswith('#!/'):
            argv[0:2] = firstline[2:].split(' ')
        log('%s\n', self.target.printable_name())
        log_cmd("redo", self.target.name + "\n")

        try:
            dn = dodir
            os.environ['REDO_PWD'] = os.path.join(vars.PWD, dn)
            os.environ['REDO_TARGET'] = basename + ext
            os.environ['REDO_DEPTH'] = vars.DEPTH + '  '
            if dn:
                os.chdir(dn)
            l = logger.Logger(self.log_fd, self.tmp_sout_fd)
            l.fork()
            os.close(self.tmp_sout_fd)
            close_on_exec(1, False)
            if vars.VERBOSE or vars.XTRACE: log_e('* %s\n' % ' '.join(argv))
            signal.signal(signal.SIGPIPE, signal.SIG_DFL) # python ignores SIGPIPE
            os.execvp(argv[0], argv)
        except:
            import traceback
            sys.stderr.write(traceback.format_exc())
            err('internal exception - see above\n')
            raise
        finally:
            # returns only if there's an exception (exec in other case)
            os._exit(127)

    def done(self, t, rv):
        assert self.target.dolock().owned == state.LOCK_EX
        log_cmd("redo_done", self.target.name + "\n")
        try:
            after_t = _try_stat(self.target.name)
            st1 = os.fstat(self.tmp_sout_f.fileno())
            st2 = _try_stat(self.tmpname_arg3)
            
            if (after_t and 
                (not self.before_t or self.before_t.st_ctime != after_t.st_ctime) and
                not stat.S_ISDIR(after_t.st_mode)):
                    err('%s modified %s directly!\n', self.dofile, self.target.name)
                    err('...you should update $3 (a temp file) or stdout, not $1.\n')
                    rv = 206

            elif vars.OLD_STDOUT and st2 and st1.st_size > 0:
                err('%s wrote to stdout *and* created $3.\n', self.dofile)
                err('...you should write status messages to stderr, not stdout.\n')
                rv = 207

            elif vars.WARN_STDOUT and st1.st_size > 0:
                err('%s wrote to stdout, this is not longer supported.\n', self.dofile)
                err('...you should write status messages to stderr, not stdout.\n')
                err('...you should write target content to $3 using for example \'exec >"$3"`.\n')
                if not vars.OLD_STDOUT: rv = 207
            
            if rv==0:
                if st2:
                    os.rename(self.tmpname_arg3, self.target.name)
                    os.unlink(self.tmpname_sout)
                elif vars.OLD_STDOUT and st1.st_size > 0:
                    try:
                        os.rename(self.tmpname_sout, self.target.name)
                    except OSError, e:
                        if e.errno == errno.ENOENT:
                            unlink(self.target.name)
                        else:
                            raise
                else: # no output generated at all; that's ok
                    unlink(self.tmpname_sout)
                    unlink(self.target.name)
                if vars.VERBOSE or vars.XTRACE or vars.DEBUG:
                    log('%s (done)\n\n', self.target.printable_name())
            else:
                unlink(self.tmpname_sout)
                unlink(self.tmpname_arg3)

            if rv != 0:
                if vars.ONLY_LOG:
                    logger.print_log(self.target)
                err('%s: exit code %d\n', self.target.printable_name(), rv)
            self.target.build_done(exitcode=rv)
            self.target.refresh()

            self._move_extra_results(self.outdir, self.target.dirname() or ".", rv)

            self.result[0] += rv
            self.result[1] += 1
            if self.parent:
                self.parent.add_dep(self.target)

        finally:
            self.tmp_sout_f.close()
            self.target.dolock().unlock()

    def _move_extra_results(self, src, dest, rv):
        assert src
        assert dest
        if os.path.isdir(src) and os.path.isdir(dest):
            for f in os.listdir(src):
                sp = os.path.join(src, f)
                dp = os.path.join(dest, f)
                self._move_extra_results(sp, dp, rv)
            os.rmdir(src)
        else:
            sf = state.File(name=dest)
            if sf == self.delegate:
                dest = os.path.join(sf.tmpfilename("out"), sf.basename())
                debug("rename %r %r\n", src, dest)
                os.rename(src, dest)
                sf.copy_deps_from(self.target)
            else:
                sf.dolock().trylock()
                if sf.dolock().owned == state.LOCK_EX:
                    try:
                        sf.build_starting()
                        debug("rename %r %r\n", src, dest)
                        os.rename(src, dest)
                        sf.copy_deps_from(self.target)
                        sf.build_done(rv)
                    finally:
                        sf.dolock().unlock()
                else:
                    warn("%s: discarding (parallel build)\n", dest)
                    unlink(src)

    def schedule_job(self):
        assert self.target.dolock().owned == state.LOCK_EX
        rv = self.prepare()
        if rv != None:
            self.result[0] += rv
            self.result[1] += 1
        else:
            jwack.start_job(self.target, self.build, self.done)

def build(f, any_errors, should_build, add_dep_to=None, delegate=None, re_do=True):
    if f.dolock():
        if f.check_deadlocks():
            err("%s: recursive dependency, breaking deadlock\n", f.printable_name())
            any_errors[0] += 1
            any_errors[1] += 1
        else:
            jwack.get_token(f)
            f.dolock().waitlock()
            if any_errors[0] and not vars.KEEP_GOING:
                return False
            f.refresh()
            debug3('think about building %r\n', f.name)
            dirty = should_build(f)
            while dirty and dirty != deps.DIRTY:
                # FIXME: bring back the old (targetname) notation in the output
                #  when we need to do this.  And add comments.
                for t2 in dirty:
                    if not build(t2, any_errors, should_build, delegate, re_do):
                        return False
                jwack.wait_all()
                dirty = should_build(f)
            if dirty:
                job = BuildJob(f, any_errors, add_dep_to, delegate, re_do)
                add_dep_to = None
                job.schedule_job()
            else:
                f.dolock().unlock()
    if add_dep_to:
        f.refresh()
        add_dep_to.add_dep(f)
    return True

def main(targets, should_build = (lambda f: deps.DIRTY), parent=None, delegate=None, re_do=True):
    any_errors = [0, 0]
    if vars.SHUFFLE:
        import random
        random.shuffle(targets)

    if delegate:
        debug("delegated: %s\n", delegate)

    try:
        for t in targets:
            f = state.File(name=t)
            if not build(f, any_errors, should_build, add_dep_to=parent, delegate=delegate, re_do=re_do):
                break
        jwack.wait_all()
    finally:
        jwack.force_return_tokens()

    if any_errors[1] == 1:
        return any_errors[0]
    elif any_errors[0]:
        return 1
    else:
        return 0

