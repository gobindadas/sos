# Copyright (C) 2006 Steve Conklin <sconklin@redhat.com>

# This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

""" This exports methods available for use by plugins for sos """

from __future__ import with_statement

from sos.utilities import (sos_get_command_output, import_module, grep,
                           fileobj, tail, is_executable)
import os
import glob
import re
import stat
from time import time
import logging
import fnmatch
import errno

# PYCOMPAT
import six
from six.moves import zip, filter

# FileNotFoundError does not exist in 2.7, so map it to IOError
try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError


def _to_u(s):
    if not isinstance(s, six.text_type):
        # Workaround python.six mishandling of strings ending in '\' by
        # adding a single space following any '\' at end-of-line.
        # See Six issue #60.
        if s.endswith('\\'):
            s += " "
        s = six.u(s)
    return s


def regex_findall(regex, fname):
    '''Return a list of all non overlapping matches in the string(s)'''
    try:
        with fileobj(fname) as f:
            return re.findall(regex, f.read(), re.MULTILINE)
    except AttributeError:
        return []


def _mangle_command(command, name_max):
    mangledname = re.sub(r"^/(usr/|)(bin|sbin)/", "", command)
    mangledname = re.sub(r"[^\w\-\.\/]+", "_", mangledname)
    mangledname = re.sub(r"/", ".", mangledname).strip(" ._-")
    mangledname = mangledname[0:name_max]
    return mangledname


def _path_in_path_list(path, path_list):
    return any(p in path for p in path_list)


def _node_type(st):
    """ return a string indicating the type of special node represented by
    the stat buffer st (block, character, fifo, socket).
    """
    _types = [
        (stat.S_ISBLK, "block device"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISFIFO, "named pipe"),
        (stat.S_ISSOCK, "socket")
    ]
    for t in _types:
        if t[0](st.st_mode):
            return t[1]


def _file_is_compressed(path):
    """Check if a file appears to be compressed

    Return True if the file specified by path appears to be compressed,
    or False otherwise by testing the file name extension against a
    list of known file compression extentions.
    """
    return path.endswith(('.gz', '.xz', '.bz', '.bz2'))


class SoSPredicate(object):
    """A class to implement collection predicates.

        A predicate gates the collection of data by an sos plugin. For any
        `add_cmd_output()`, `add_copy_spec()` or `add_journal()` call, the
        passed predicate will be evaulated and collection will proceed if
        the result is `True`, and not otherwise.

        Predicates may be used to control conditional data collection
        without the need for explicit conditional blocks in plugins.
    """
    #: The plugin that owns this predicate
    _owner = None

    #: Skip all collection?
    _dry_run = False

    #: Kernel module enablement list
    _kmods = []

    #: Services enablement list
    _services = []

    def __str(self, quote=False, prefix="", suffix=""):
        """Return a string representation of this SoSPredicate with
            optional prefix, suffix and value quoting.
        """
        quotes = '"%s"'
        pstr = "dry_run=%s, " % self._dry_run

        kmods = self._kmods
        kmods = [quotes % k for k in kmods] if quote else kmods
        pstr += "kmods=[%s], " % (",".join(kmods))

        services = self._services
        services = [quotes % s for s in services] if quote else services
        pstr += "services=[%s]" % (",".join(services))

        return prefix + pstr + suffix

    def __str__(self):
        """Return a string representation of this SoSPredicate.

            "dry_run=False, kmods=[], services=[]"
        """
        return self.__str()

    def __repr__(self):
        """Return a machine readable string representation of this
            SoSPredicate.

            "SoSPredicate(dry_run=False, kmods=[], services=[])"
        """
        return self.__str(quote=True, prefix="SoSPredicate(", suffix=")")

    def __nonzero__(self):
        """Predicate evaluation hook.
        """
        pvalue = False
        for k in self._kmods:
            pvalue |= self._owner.is_module_loaded(k)

        for s in self._services:
            pvalue |= self._owner.service_is_running(s)

        # Null predicate?
        if not any([self._kmods, self._services, self._dry_run]):
            return True

        return pvalue and not self._dry_run

    def __init__(self, owner, dry_run=False, kmods=[], services=[]):
        """Initialise a new SoSPredicate object.
        """
        self._owner = owner
        self._kmods = list(kmods)
        self._services = list(services)
        self._dry_run = dry_run | self._owner.commons['cmdlineopts'].dry_run


class SoSCommand(object):
    """A class to represent a command to be collected.

    A SoSCommand() object is instantiated for each command handed to an
    _add_cmd_output() call, so that we no longer need to pass around a very
    long tuple to handle the parameters.

    Any option supported by _add_cmd_output() is passed to the SoSCommand
    object and converted to an attribute. SoSCommand.__dict__ is then passed to
    _get_command_output_now() for each command to be collected.
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __str__(self):
        """Return a human readable string representation of this SoSCommand
        """
        return ', '.join("%s=%r" % (param, val) for (param, val) in
                         sorted(self.__dict__.items()))


class Plugin(object):
    """ This is the base class for sosreport plugins. Plugins should subclass
    this and set the class variables where applicable.

    plugin_name is a string returned by plugin.name(). If this is set to None
    (the default) class\\_.__name__.tolower() will be returned. Be sure to set
    this if you are defining multiple plugins that do the same thing on
    different platforms.

    requires_root is a boolean that specifies whether or not sosreport should
    execute this plugin as a super user.

    version is a string representing the version of the plugin. This can be
    useful for post-collection tooling.

    packages (files) is an iterable of the names of packages (the paths
    of files) to check for before running this plugin. If any of these packages
    or files is found on the system, the default implementation of
    check_enabled will return True.

    profiles is an iterable of profile names that this plugin belongs to.
    Whenever any of the profiles is selected on the command line the plugin
    will be enabled (subject to normal check_enabled tests).
    """

    plugin_name = None
    requires_root = True
    version = 'unversioned'
    packages = ()
    files = ()
    commands = ()
    kernel_mods = ()
    services = ()
    archive = None
    profiles = ()
    sysroot = '/'
    plugin_timeout = 300
    _timeout_hit = False

    # Default predicates
    predicate = None
    cmd_predicate = None

    def __init__(self, commons):
        if not getattr(self, "option_list", False):
            self.option_list = []

        self.copied_files = []
        self.executed_commands = []
        self._env_vars = set()
        self.alerts = []
        self.custom_text = ""
        self.opt_names = []
        self.opt_parms = []
        self.commons = commons
        self.forbidden_paths = []
        self.copy_paths = set()
        self.copy_strings = []
        self.collect_cmds = []
        self.sysroot = commons['sysroot']
        self.policy = commons['policy']

        self.soslog = self.commons['soslog'] if 'soslog' in self.commons \
            else logging.getLogger('sos')

        # add the 'timeout' plugin option automatically
        self.option_list.append(('timeout', 'timeout in seconds for plugin',
                                 'fast', -1))

        # get the option list into a dictionary
        for opt in self.option_list:
            self.opt_names.append(opt[0])
            self.opt_parms.append({'desc': opt[1], 'speed': opt[2],
                                   'enabled': opt[3]})

        # Initialise the default --dry-run predicate
        self.set_predicate(SoSPredicate(self))

    @property
    def timeout(self):
        '''Returns either the default plugin timeout value, the value as
        provided on the commandline via -k plugin.timeout=value, or the value
        of the global --plugin-timeout option.
        '''
        _timeout = None
        try:
            opt_timeout = self.get_option('plugin_timeout')
            own_timeout = int(self.get_option('timeout'))
            if opt_timeout is None:
                _timeout = own_timeout
            elif opt_timeout is not None and own_timeout == -1:
                _timeout = int(opt_timeout)
            elif opt_timeout is not None and own_timeout > -1:
                _timeout = own_timeout
            else:
                return None
        except ValueError:
            return self.plugin_timeout  # Default to known safe value
        if _timeout is not None and _timeout > -1:
            return _timeout
        return self.plugin_timeout

    def check_timeout(self):
        '''
        Checks to see if the plugin has hit its timeout.

        This is set when the sos.collect_plugin() method hits a timeout and
        terminates the thread. From there, a Popen() call can still continue to
        run, and we need to manually terminate it. Thus, check_timeout() should
        only be called in sos_get_command_output().

        Since sos_get_command_output() is not plugin aware, this method is
        handed to that call to use as a polling method, to avoid passing the
        entire plugin object.

        Returns True if timeout has been hit, else False.

        '''
        return self._timeout_hit

    @classmethod
    def name(cls):
        """Returns the plugin's name as a string. This should return a
        lowercase string.
        """
        if cls.plugin_name:
            return cls.plugin_name
        return cls.__name__.lower()

    def _format_msg(self, msg):
        return "[plugin:%s] %s" % (self.name(), msg)

    def _log_error(self, msg):
        self.soslog.error(self._format_msg(msg))

    def _log_warn(self, msg):
        self.soslog.warning(self._format_msg(msg))

    def _log_info(self, msg):
        self.soslog.info(self._format_msg(msg))

    def _log_debug(self, msg):
        self.soslog.debug(self._format_msg(msg))

    def join_sysroot(self, path):
        if path[0] == os.sep:
            path = path[1:]
        return os.path.join(self.sysroot, path)

    def strip_sysroot(self, path):
        if not self.use_sysroot():
            return path
        if path.startswith(self.sysroot):
            return path[len(self.sysroot):]
        return path

    def use_sysroot(self):
        return self.sysroot != os.path.abspath(os.sep)

    def tmp_in_sysroot(self):
        paths = [self.sysroot, self.archive.get_tmp_dir()]
        return os.path.commonprefix(paths) == self.sysroot

    def is_installed(self, package_name):
        '''Is the package $package_name installed?'''
        return self.policy.pkg_by_name(package_name) is not None

    def is_service(self, name):
        '''Does the service $name exist on the system?'''
        return self.policy.init_system.is_service(name)

    def service_is_enabled(self, name):
        '''Is the service $name enabled?'''
        return self.policy.init_system.is_enabled(name)

    def service_is_disabled(self, name):
        '''Is the service $name disabled?'''
        return self.policy.init_system.is_disabled(name)

    def service_is_running(self, name):
        '''Is the service $name currently running?'''
        return self.policy.init_system.is_running(name)

    def get_service_status(self, name):
        '''Return the reported status for service $name'''
        return self.policy.init_system.get_service_status(name)['status']

    def set_predicate(self, pred):
        """Set or clear the default predicate for this plugin.
        """
        self.predicate = pred

    def set_cmd_predicate(self, pred):
        """Set or clear the default predicate for command collection
            for this plugin. If set, this predecate takes precedence
            over the `Plugin` default predicate for command and journal
            data collection.
        """
        self.cmd_predicate = pred

    def get_predicate(self, cmd=False, pred=None):
        """Get the current default `Plugin` or command predicate. If the
            `cmd` argument is `True`, the current command predicate is
            returned if set, otherwise the default `Plugin` predicate
            will be returned (which may be `None`).

            If no default predicate is set and a `pred` value is passed
            it will be returned.
        """
        if pred is not None:
            return pred
        if cmd and self.cmd_predicate is not None:
            return self.cmd_predicate
        return self.predicate

    def test_predicate(self, cmd=False, pred=None):
        """Test the current predicate and return its value.

            :param cmd: ``True`` if the predicate is gating a command or
                        ``False`` otherwise.
            :param pred: An optional predicate to override the current
                         ``Plugin`` or command predicate.
        """
        pred = self.get_predicate(cmd=cmd, pred=pred)
        if pred is not None:
            return bool(pred)
        return False

    def do_cmd_private_sub(self, cmd):
        '''Remove certificate and key output archived by sosreport. cmd
        is the command name from which output is collected (i.e. exlcuding
        parameters). Any matching instances are replaced with: '-----SCRUBBED'
        and this function does not take a regexp or substituting string.

        This function returns the number of replacements made.
        '''
        globstr = '*' + cmd + '*'
        self._log_debug("Scrubbing certs and keys for commands matching %s"
                        % (cmd))

        if not self.executed_commands:
            return 0

        replacements = None
        try:
            for called in self.executed_commands:
                if called['file'] is None:
                    continue
                if called['binary'] == 'yes':
                    self._log_warn("Cannot apply regex substitution to binary"
                                   " output: '%s'" % called['exe'])
                    continue
                if fnmatch.fnmatch(called['exe'], globstr):
                    path = os.path.join(self.commons['cmddir'], called['file'])
                    readable = self.archive.open_file(path)
                    certmatch = re.compile("-----BEGIN.*?-----END", re.DOTALL)
                    result, replacements = certmatch.subn(
                        "-----SCRUBBED", readable.read())
                    if replacements:
                        self.archive.add_string(result, path)
        except Exception as e:
            msg = "Certificate/key scrubbing failed for '%s' with: '%s'"
            self._log_error(msg % (called['exe'], e))
            replacements = None
        return replacements

    def do_cmd_output_sub(self, cmd, regexp, subst):
        '''Apply a regexp substitution to command output archived by sosreport.
        cmd is the command name from which output is collected (i.e. excluding
        parameters). The regexp can be a string or a compiled re object. The
        substitution string, subst, is a string that replaces each occurrence
        of regexp in each file collected from cmd. Internally 'cmd' is treated
        as a glob with a leading and trailing '*' and each matching file from
        the current module's command list is subjected to the replacement.

        This function returns the number of replacements made.
        '''
        globstr = '*' + cmd + '*'
        self._log_debug("substituting '%s' for '%s' in commands matching '%s'"
                        % (subst, regexp, globstr))

        if not self.executed_commands:
            return 0

        replacements = None
        try:
            for called in self.executed_commands:
                # was anything collected?
                if called['file'] is None:
                    continue
                if called['binary'] == 'yes':
                    self._log_warn("Cannot apply regex substitution to binary"
                                   " output: '%s'" % called['exe'])
                    continue
                if fnmatch.fnmatch(called['exe'], globstr):
                    path = os.path.join(self.commons['cmddir'], called['file'])
                    self._log_debug("applying substitution to '%s'" % path)
                    readable = self.archive.open_file(path)
                    result, replacements = re.subn(
                        regexp, subst, readable.read())
                    if replacements:
                        self.archive.add_string(result, path)

        except Exception as e:
            msg = "regex substitution failed for '%s' with: '%s'"
            self._log_error(msg % (called['exe'], e))
            replacements = None
        return replacements

    def do_file_sub(self, srcpath, regexp, subst):
        '''Apply a regexp substitution to a file archived by sosreport.
        srcpath is the path in the archive where the file can be found.  regexp
        can be a regexp string or a compiled re object.  subst is a string to
        replace each occurance of regexp in the content of srcpath.

        This function returns the number of replacements made.
        '''
        try:
            path = self._get_dest_for_srcpath(srcpath)
            self._log_debug("substituting scrpath '%s'" % srcpath)
            self._log_debug("substituting '%s' for '%s' in '%s'"
                            % (subst, regexp, path))
            if not path:
                return 0
            readable = self.archive.open_file(path)
            content = readable.read()
            if not isinstance(content, six.string_types):
                content = content.decode('utf8', 'ignore')
            result, replacements = re.subn(regexp, subst, content)
            if replacements:
                self.archive.add_string(result, srcpath)
            else:
                replacements = 0
        except (OSError, IOError) as e:
            # if trying to regexp a nonexisting file, dont log it as an
            # error to stdout
            if e.errno == errno.ENOENT:
                msg = "file '%s' not collected, substitution skipped"
                self._log_debug(msg % path)
            else:
                msg = "regex substitution failed for '%s' with: '%s'"
                self._log_error(msg % (path, e))
            replacements = 0
        return replacements

    def do_path_regex_sub(self, pathexp, regexp, subst):
        '''Apply a regexp substituation to a set of files archived by
        sos. The set of files to be substituted is generated by matching
        collected file pathnames against pathexp which may be a regular
        expression string or compiled re object. The portion of the file
        to be replaced is specified via regexp and the replacement string
        is passed in subst.'''
        if not hasattr(pathexp, "match"):
            pathexp = re.compile(pathexp)
        match = pathexp.match
        file_list = [f for f in self.copied_files if match(f['srcpath'])]
        for file in file_list:
            self.do_file_sub(file['srcpath'], regexp, subst)

    def do_regex_find_all(self, regex, fname):
        return regex_findall(regex, fname)

    def _copy_symlink(self, srcpath):
        # the target stored in the original symlink
        linkdest = os.readlink(srcpath)
        dest = os.path.join(os.path.dirname(srcpath), linkdest)
        # Absolute path to the link target. If SYSROOT != '/' this path
        # is relative to the host root file system.
        absdest = os.path.normpath(dest)
        # adjust the target used inside the report to always be relative
        if os.path.isabs(linkdest):
            # Canonicalize the link target path to avoid additional levels
            # of symbolic links (that would affect the path nesting level).
            realdir = os.path.realpath(os.path.dirname(srcpath))
            reldest = os.path.relpath(linkdest, start=realdir)
            # trim leading /sysroot
            if self.use_sysroot():
                reldest = reldest[len(os.sep + os.pardir):]
            self._log_debug("made link target '%s' relative as '%s'"
                            % (linkdest, reldest))
        else:
            reldest = linkdest

        self._log_debug("copying link '%s' pointing to '%s' with isdir=%s"
                        % (srcpath, linkdest, os.path.isdir(absdest)))

        dstpath = self.strip_sysroot(srcpath)
        # use the relative target path in the tarball
        self.archive.add_link(reldest, dstpath)

        if os.path.isdir(absdest):
            self._log_debug("link '%s' is a directory, skipping..." % linkdest)
            return

        self.copied_files.append({'srcpath': srcpath,
                                  'dstpath': dstpath,
                                  'symlink': "yes",
                                  'pointsto': linkdest})

        # Check for indirect symlink loops by stat()ing the next step
        # in the link chain.
        try:
            os.stat(absdest)
        except OSError as e:
            if e.errno == 40:
                self._log_debug("link '%s' is part of a file system "
                                "loop, skipping target..." % dstpath)
                return

        # copy the symlink target translating relative targets
        # to absolute paths to pass to _do_copy_path.
        self._log_debug("normalized link target '%s' as '%s'"
                        % (linkdest, absdest))

        # skip recursive copying of symlink pointing to itself.
        if (absdest != srcpath):
            self._do_copy_path(self.strip_sysroot(absdest))
        else:
            self._log_debug("link '%s' points to itself, skipping target..."
                            % linkdest)

    def _copy_dir(self, srcpath):
        try:
            for afile in os.listdir(srcpath):
                self._log_debug("recursively adding '%s' from '%s'"
                                % (afile, srcpath))
                self._do_copy_path(os.path.join(srcpath, afile), dest=None)
        except OSError as e:
            if e.errno == errno.ELOOP:
                msg = "Too many levels of symbolic links copying"
                self._log_error("_copy_dir: %s '%s'" % (msg, srcpath))
                return
            raise

    def _get_dest_for_srcpath(self, srcpath):
        if self.use_sysroot():
            srcpath = self.join_sysroot(srcpath)
        for copied in self.copied_files:
            if srcpath == copied["srcpath"]:
                return copied["dstpath"]
        return None

    def _is_forbidden_path(self, path):
        if self.use_sysroot():
            path = self.join_sysroot(path)
        return _path_in_path_list(path, self.forbidden_paths)

    def _copy_node(self, path, st):
        dev_maj = os.major(st.st_rdev)
        dev_min = os.minor(st.st_rdev)
        mode = st.st_mode
        self.archive.add_node(path, mode, os.makedev(dev_maj, dev_min))

    # Methods for copying files and shelling out
    def _do_copy_path(self, srcpath, dest=None):
        '''Copy file or directory to the destination tree. If a directory, then
        everything below it is recursively copied. A list of copied files are
        saved for use later in preparing a report.
        '''
        if self._timeout_hit:
            return

        if self._is_forbidden_path(srcpath):
            self._log_debug("skipping forbidden path '%s'" % srcpath)
            return ''

        if not dest:
            dest = srcpath

        if self.use_sysroot():
            dest = self.strip_sysroot(dest)

        try:
            st = os.lstat(srcpath)
        except (OSError, IOError):
            self._log_info("failed to stat '%s'" % srcpath)
            return

        if stat.S_ISLNK(st.st_mode):
            self._copy_symlink(srcpath)
            return
        else:
            if stat.S_ISDIR(st.st_mode) and os.access(srcpath, os.R_OK):
                self._copy_dir(srcpath)
                return

        # handle special nodes (block, char, fifo, socket)
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            ntype = _node_type(st)
            self._log_debug("creating %s node at archive:'%s'"
                            % (ntype, dest))
            self._copy_node(srcpath, st)
            return

        # if we get here, it's definitely a regular file (not a symlink or dir)
        self._log_debug("copying path '%s' to archive:'%s'" % (srcpath, dest))

        # if not readable(srcpath)
        if not st.st_mode & 0o444:
            # FIXME: reflect permissions in archive
            self.archive.add_string("", dest)
        else:
            self.archive.add_file(srcpath, dest)

        self.copied_files.append({
            'srcpath': srcpath,
            'dstpath': dest,
            'symlink': "no"
        })

    def add_forbidden_path(self, forbidden):
        """Specify a path, or list of paths, to not copy, even if it's
            part of a copy_specs[] entry.
        """
        if isinstance(forbidden, six.string_types):
            forbidden = [forbidden]

        if self.use_sysroot():
            forbidden = [self.join_sysroot(f) for f in forbidden]

        for forbid in forbidden:
            self._log_info("adding forbidden path '%s'" % forbid)
            for path in glob.glob(forbid):
                self.forbidden_paths.append(path)

    def get_all_options(self):
        """return a list of all options selected"""
        return (self.opt_names, self.opt_parms)

    def set_option(self, optionname, value):
        """Set the named option to value. Ensure the original type
           of the option value is preserved.
        """
        for name, parms in zip(self.opt_names, self.opt_parms):
            if name == optionname:
                # FIXME: ensure that the resulting type of the set option
                # matches that of the default value. This prevents a string
                # option from being coerced to int simply because it holds
                # a numeric value (e.g. a password).
                # See PR #1526 and Issue #1597
                defaulttype = type(parms['enabled'])
                if defaulttype != type(value) and defaulttype != type(None):
                    value = (defaulttype)(value)
                parms['enabled'] = value
                return True
        else:
            return False

    def get_option(self, optionname, default=0):
        """Returns the first value that matches 'optionname' in parameters
        passed in via the command line or set via set_option or via the
        global_plugin_options dictionary, in that order.

        optionaname may be iterable, in which case the first option that
        matches any of the option names is returned.
        """

        global_options = ('verify', 'all_logs', 'log_size', 'plugin_timeout')

        if optionname in global_options:
            return getattr(self.commons['cmdlineopts'], optionname)

        for name, parms in zip(self.opt_names, self.opt_parms):
            if name == optionname:
                val = parms['enabled']
                if val is not None:
                    return val

        return default

    def get_option_as_list(self, optionname, delimiter=",", default=None):
        '''Will try to return the option as a list separated by the
        delimiter.
        '''
        option = self.get_option(optionname)
        try:
            opt_list = [opt.strip() for opt in option.split(delimiter)]
            return list(filter(None, opt_list))
        except Exception:
            return default

    def _add_copy_paths(self, copy_paths):
        self.copy_paths.update(copy_paths)

    def add_copy_spec(self, copyspecs, sizelimit=None, tailit=True, pred=None):
        """Add a file or glob but limit it to sizelimit megabytes. If fname is
        a single file the file will be tailed to meet sizelimit. If the first
        file in a glob is too large it will be tailed to meet the sizelimit.
        """
        if not self.test_predicate(pred=pred):
            self._log_info("skipped copy spec '%s' due to predicate (%s)" %
                           (copyspecs, self.get_predicate(pred=pred)))
            return

        if sizelimit is None:
            sizelimit = self.get_option("log_size")

        if self.get_option('all_logs'):
            sizelimit = None

        if sizelimit:
            sizelimit *= 1024 * 1024  # in MB

        if not copyspecs:
            return False

        if isinstance(copyspecs, six.string_types):
            copyspecs = [copyspecs]

        for copyspec in copyspecs:
            if not (copyspec and len(copyspec)):
                return False

            if self.use_sysroot():
                copyspec = self.join_sysroot(copyspec)

            files = self._expand_copy_spec(copyspec)

            if len(files) == 0:
                continue

            # Files hould be sorted in most-recently-modified order, so that
            # we collect the newest data first before reaching the limit.
            def getmtime(path):
                try:
                    return os.path.getmtime(path)
                except OSError:
                    return 0

            files.sort(key=getmtime, reverse=True)
            current_size = 0
            limit_reached = False
            _file = None

            for _file in files:
                if self._is_forbidden_path(_file):
                    self._log_debug("skipping forbidden path '%s'" % _file)
                    continue
                try:
                    current_size += os.stat(_file)[stat.ST_SIZE]
                except OSError:
                    self._log_info("failed to stat '%s'" % _file)
                if sizelimit and current_size > sizelimit:
                    limit_reached = True
                    break
                self._add_copy_paths([_file])

            if limit_reached and tailit and not _file_is_compressed(_file):
                file_name = _file

                if file_name[0] == os.sep:
                    file_name = file_name.lstrip(os.sep)
                strfile = file_name.replace(os.path.sep, ".") + ".tailed"
                self.add_string_as_file(tail(_file, sizelimit), strfile)
                rel_path = os.path.relpath('/', os.path.dirname(_file))
                link_path = os.path.join(rel_path, 'sos_strings',
                                         self.name(), strfile)
                self.archive.add_link(link_path, _file)

    def get_command_output(self, prog, timeout=300, stderr=True,
                           chroot=True, runat=None, env=None,
                           binary=False, sizelimit=None):
        if self._timeout_hit:
            return

        if chroot or self.commons['cmdlineopts'].chroot == 'always':
            root = self.sysroot
        else:
            root = None

        result = sos_get_command_output(prog, timeout=timeout, stderr=stderr,
                                        chroot=root, chdir=runat,
                                        env=env, binary=binary,
                                        sizelimit=sizelimit,
                                        poller=self.check_timeout)

        if result['status'] == 124:
            self._log_warn("command '%s' timed out after %ds"
                           % (prog, timeout))

        # command not found or not runnable
        if result['status'] == 126 or result['status'] == 127:
            # automatically retry chroot'ed commands in the host namespace
            if root and root != '/':
                if self.commons['cmdlineopts'].chroot != 'always':
                    self._log_info("command '%s' not found in %s - "
                                   "re-trying in host root"
                                   % (prog.split()[0], root))
                    return self.get_command_output(prog, timeout=timeout,
                                                   chroot=False, runat=runat,
                                                   env=env,
                                                   binary=binary)
            self._log_debug("could not run '%s': command not found" % prog)
        return result

    def call_ext_prog(self, prog, timeout=300, stderr=True,
                      chroot=True, runat=None):
        """Execute a command independantly of the output gathering part of
        sosreport.
        """
        return self.get_command_output(prog, timeout=timeout, stderr=stderr,
                                       chroot=chroot, runat=runat)

    def check_ext_prog(self, prog):
        """Execute a command independently of the output gathering part of
        sosreport and check the return code. Return True for a return code of 0
        and False otherwise.
        """
        return self.call_ext_prog(prog)['status'] == 0

    def _add_cmd_output(self, **kwargs):
        """Internal helper to add a single command to the collection list."""
        pred = kwargs.pop('pred') if 'pred' in kwargs else None
        soscmd = SoSCommand(**kwargs)
        self._log_debug("packed command: " + soscmd.__str__())
        if self.test_predicate(cmd=True, pred=pred):
            self.collect_cmds.append(soscmd)
            self._log_info("added cmd output '%s'" % soscmd.cmd)
        else:
            self._log_info("skipped cmd output '%s' due to predicate (%s)" %
                           (soscmd.cmd,
                            self.get_predicate(cmd=True, pred=pred)))

    def add_cmd_output(self, cmds, suggest_filename=None,
                       root_symlink=None, timeout=300, stderr=True,
                       chroot=True, runat=None, env=None, binary=False,
                       sizelimit=None, pred=None, subdir=None):
        """Run a program or a list of programs and collect the output"""
        if isinstance(cmds, six.string_types):
            cmds = [cmds]
        if len(cmds) > 1 and (suggest_filename or root_symlink):
            self._log_warn("ambiguous filename or symlink for command list")
        if sizelimit is None:
            sizelimit = self.get_option("log_size")
        for cmd in cmds:
            self._add_cmd_output(cmd=cmd, suggest_filename=suggest_filename,
                                 root_symlink=root_symlink, timeout=timeout,
                                 stderr=stderr, chroot=chroot, runat=runat,
                                 env=env, binary=binary, sizelimit=sizelimit,
                                 pred=pred, subdir=subdir)

    def get_cmd_output_path(self, name=None, make=True):
        """Return a path into which this module should store collected
        command output
        """
        cmd_output_path = os.path.join(self.archive.get_tmp_dir(),
                                       'sos_commands', self.name())
        if name:
            cmd_output_path = os.path.join(cmd_output_path, name)
        if make:
            os.makedirs(cmd_output_path)

        return cmd_output_path

    def file_grep(self, regexp, *fnames):
        """Returns lines matched in fnames, where fnames can either be
        pathnames to files to grep through or open file objects to grep through
        line by line.
        """
        return grep(regexp, *fnames)

    def _mangle_command(self, exe):
        name_max = self.archive.name_max()
        return _mangle_command(exe, name_max)

    def _make_command_filename(self, exe, subdir=None):
        """The internal function to build up a filename based on a command."""

        plugin_dir = self.name()
        if subdir:
            # only allow a single level of subdir to be created
            plugin_dir += "/%s" % subdir.split('/')[0]
        outfn = os.path.join(self.commons['cmddir'], plugin_dir,
                             self._mangle_command(exe))

        # check for collisions
        if os.path.exists(outfn):
            inc = 2
            while True:
                newfn = "%s_%d" % (outfn, inc)
                if not os.path.exists(newfn):
                    outfn = newfn
                    break
                inc += 1

        return outfn

    def add_env_var(self, name):
        """Add an environment variable to the list of to-be-collected env vars.

        Accepts either a single variable name or a list of names. Any value
        given will be added as provided to the method, as well as an upper-
        and lower- cased version.
        """
        if not isinstance(name, list):
            name = [name]
        for env in name:
            # get both upper and lower cased vars since a common support issue
            # is setting the env vars to the wrong case, and if the plugin
            # adds a mixed case variable name, still get that as well
            self._env_vars.update([env, env.upper(), env.lower()])

    def add_string_as_file(self, content, filename, pred=None):
        """Add a string to the archive as a file named `filename`"""

        # Generate summary string for logging
        summary = content.splitlines()[0] if content else ''
        if not isinstance(summary, six.string_types):
            summary = content.decode('utf8', 'ignore')

        if not self.test_predicate(cmd=False, pred=pred):
            self._log_info("skipped string ...'%s' due to predicate (%s)" %
                           (summary, self.get_predicate(pred=pred)))
            return

        self.copy_strings.append((content, filename))
        self._log_debug("added string ...'%s' as '%s'" % (summary, filename))

    def _get_cmd_output_now(self, cmd, suggest_filename=None,
                            root_symlink=False, timeout=300, stderr=True,
                            chroot=True, runat=None, env=None,
                            binary=False, sizelimit=None, subdir=None):
        """Execute a command and save the output to a file for inclusion in the
        report.
        """
        if self._timeout_hit:
            return

        start = time()

        result = self.get_command_output(cmd, timeout=timeout, stderr=stderr,
                                         chroot=chroot, runat=runat,
                                         env=env, binary=binary,
                                         sizelimit=sizelimit)
        self._log_debug("collected output of '%s' in %s"
                        % (cmd.split()[0], time() - start))

        if suggest_filename:
            outfn = self._make_command_filename(suggest_filename, subdir)
        else:
            outfn = self._make_command_filename(cmd, subdir)

        outfn_strip = outfn[len(self.commons['cmddir'])+1:]
        if binary:
            self.archive.add_binary(result['output'], outfn)
        else:
            self.archive.add_string(result['output'], outfn)
        if root_symlink:
            self.archive.add_link(outfn, root_symlink)

        # save info for later
        self.executed_commands.append({'exe': cmd, 'file': outfn_strip,
                                       'binary': 'yes' if binary else 'no'})

        return os.path.join(self.archive.get_archive_path(), outfn)

    def get_cmd_output_now(self, exe, suggest_filename=None,
                           root_symlink=False, timeout=300, stderr=True,
                           chroot=True, runat=None, env=None,
                           binary=False, sizelimit=None, pred=None):
        """Execute a command and save the output to a file for inclusion in the
        report.
        """
        if not self.test_predicate(cmd=True, pred=pred):
            self._log_info("skipped cmd output '%s' due to predicate (%s)" %
                           (exe, self.get_predicate(cmd=True, pred=pred)))
            return None

        return self._get_cmd_output_now(exe, timeout=timeout, stderr=stderr,
                                        chroot=chroot, runat=runat,
                                        env=env, binary=binary,
                                        sizelimit=sizelimit)

    def is_module_loaded(self, module_name):
        """Return whether specified moudle as module_name is loaded or not"""
        if len(grep("^" + module_name + " ", "/proc/modules")) == 0:
            return False
        else:
            return True

    # For adding output
    def add_alert(self, alertstring):
        """Add an alert to the collection of alerts for this plugin. These
        will be displayed in the report
        """
        self.alerts.append(alertstring)

    def add_custom_text(self, text):
        """Append text to the custom text that is included in the report. This
        is freeform and can include html.
        """
        self.custom_text += text

    def add_journal(self, units=None, boot=None, since=None, until=None,
                    lines=None, allfields=False, output=None, timeout=None,
                    identifier=None, catalog=None, sizelimit=None, pred=None):
        """Collect journald logs from one of more units.

        :param units: A string, or list of strings specifying the
                       systemd units for which journal entries will be
                       collected.

        :param boot: A string selecting a boot index using the
                      journalctl syntax. The special values 'this' and
                      'last' are also accepted.

        :param since: A string representation of the start time for
                       journal messages.

        :param until: A string representation of the end time for
                       journal messages.

        :param lines: The maximum number of lines to be collected.

        :param allfields: A bool. Include all journal fields
                           regardless of size or non-printable
                           characters.

        :param output: A journalctl output control string, for
                        example "verbose".

        :param timeout: An optional timeout in seconds.
        :param identifier: An optional message identifier.
        :param catalog: Bool. If True, augment lines with descriptions
                        from the system catalog.
        :param sizelimit: Limit to the size of output returned in MB.
                          Defaults to the value of --log-size.
        """
        journal_cmd = "journalctl --no-pager "
        unit_opt = " --unit %s"
        boot_opt = " --boot %s"
        since_opt = " --since %s"
        until_opt = " --until %s"
        lines_opt = " --lines %s"
        output_opt = " --output %s"
        identifier_opt = " --identifier %s"
        catalog_opt = " --catalog"

        journal_size = 100
        all_logs = self.get_option("all_logs")
        log_size = sizelimit or self.get_option("log_size")
        log_size = max(log_size, journal_size) if not all_logs else 0

        if isinstance(units, six.string_types):
            units = [units]

        if units:
            for unit in units:
                journal_cmd += unit_opt % unit

        if identifier:
            journal_cmd += identifier_opt % identifier

        if catalog:
            journal_cmd += catalog_opt

        if allfields:
            journal_cmd += " --all"

        if boot:
            if boot == "this":
                boot = ""
            if boot == "last":
                boot = "-1"
            journal_cmd += boot_opt % boot

        if since:
            journal_cmd += since_opt % since

        if until:
            journal_cmd += until_opt % until

        if lines:
            journal_cmd += lines_opt % lines

        if output:
            journal_cmd += output_opt % output

        self._log_debug("collecting journal: %s" % journal_cmd)
        self._add_cmd_output(cmd=journal_cmd, timeout=timeout,
                             sizelimit=log_size, pred=pred)

    def add_udev_info(self, device, attrs=False):
        """Collect udevadm info output for a given device

        :param device: A string or list of strings of device names or sysfs
                       paths. E.G. either '/sys/class/scsi_host/host0' or
                       '/dev/sda' is valid.
        :param attrs: If True, run udevadm with the --attribute-walk option.
        """
        udev_cmd = 'udevadm info'
        if attrs:
            udev_cmd += ' -a'

        if isinstance(device, six.string_types):
            device = [device]

        for dev in device:
            self._log_debug("collecting udev info for: %s" % dev)
            self._add_cmd_output(cmd='%s %s' % (udev_cmd, dev))

    def _expand_copy_spec(self, copyspec):
        return glob.glob(copyspec)

    def _collect_copy_specs(self):
        for path in self.copy_paths:
            self._log_info("collecting path '%s'" % path)
            self._do_copy_path(path)

    def _collect_cmd_output(self):
        for soscmd in self.collect_cmds:
            self._log_debug("unpacked command: " + soscmd.__str__())
            self._log_info("collecting output of '%s'" % soscmd.cmd)
            self._get_cmd_output_now(**soscmd.__dict__)

    def _collect_strings(self):
        for string, file_name in self.copy_strings:
            if self._timeout_hit:
                return
            content = ''
            if string:
                content = string.splitlines()[0]
                if not isinstance(content, six.string_types):
                    content = content.decode('utf8', 'ignore')
            self._log_info("collecting string ...'%s' as '%s'"
                           % (content, file_name))
            try:
                self.archive.add_string(string,
                                        os.path.join('sos_strings',
                                                     self.name(),
                                                     file_name))
            except Exception as e:
                self._log_debug("could not add string '%s': %s"
                                % (file_name, e))

    def collect(self):
        """Collect the data for a plugin."""
        start = time()
        self._collect_copy_specs()
        self._collect_cmd_output()
        self._collect_strings()
        fields = (self.name(), time() - start)
        self._log_debug("collected plugin '%s' in %s" % fields)

    def get_description(self):
        """ This function will return the description for the plugin"""
        try:
            if hasattr(self, '__doc__') and self.__doc__:
                return self.__doc__.strip()
            return super(self.__class__, self).__doc__.strip()
        except Exception:
            return "<no description available>"

    def check_enabled(self):
        """This method will be used to verify that a plugin should execute
        given the condition of the underlying environment.

        The default implementation will return True if none of class.files,
        class.packages, nor class.commands is specified. If any of these is
        specified the plugin will check for the existence of any of the
        corresponding paths, packages or commands and return True if any
        are present.

        For SCLPlugin subclasses, it will check whether the plugin can be run
        for any of installed SCLs. If so, it will store names of these SCLs
        on the plugin class in addition to returning True.

        For plugins with more complex enablement checks this method may be
        overridden.
        """
        # some files or packages have been specified for this package
        if any([self.files, self.packages, self.commands, self.kernel_mods,
                self.services]):
            if isinstance(self.files, six.string_types):
                self.files = [self.files]

            if isinstance(self.packages, six.string_types):
                self.packages = [self.packages]

            if isinstance(self.commands, six.string_types):
                self.commands = [self.commands]

            if isinstance(self.kernel_mods, six.string_types):
                self.kernel_mods = [self.kernel_mods]

            if isinstance(self.services, six.string_types):
                self.services = [self.services]

            if isinstance(self, SCLPlugin):
                # save SCLs that match files or packages
                type(self)._scls_matched = []
                for scl in self._get_scls():
                    files = [f % {"scl_name": scl} for f in self.files]
                    packages = [p % {"scl_name": scl} for p in self.packages]
                    commands = [c % {"scl_name": scl} for c in self.commands]
                    services = [s % {"scl_name": scl} for s in self.services]
                    if self._check_plugin_triggers(files,
                                                   packages,
                                                   commands,
                                                   services):
                        type(self)._scls_matched.append(scl)
                return len(type(self)._scls_matched) > 0

            return self._check_plugin_triggers(self.files,
                                               self.packages,
                                               self.commands,
                                               self.services)

        if isinstance(self, SCLPlugin):
            # if files and packages weren't specified, we take all SCLs
            type(self)._scls_matched = self._get_scls()

        return True

    def _check_plugin_triggers(self, files, packages, commands, services):
        kernel_mods = self.policy.lsmod()

        def have_kmod(kmod):
            return kmod in kernel_mods

        return (any(os.path.exists(fname) for fname in files) or
                any(self.is_installed(pkg) for pkg in packages) or
                any(is_executable(cmd) for cmd in commands) or
                any(have_kmod(kmod) for kmod in self.kernel_mods) or
                any(self.is_service(svc) for svc in services))

    def default_enabled(self):
        """This decides whether a plugin should be automatically loaded or
        only if manually specified in the command line."""
        return True

    def setup(self):
        """Collect the list of files declared by the plugin. This method
        may be overridden to add further copy_specs, forbidden_paths, and
        external programs if required.
        """
        self.add_copy_spec(list(self.files))

    def setup_verify(self):
        if not hasattr(self, "verify_packages") or not self.verify_packages:
            if hasattr(self, "packages") and self.packages:
                # Limit automatic verification to only the named packages
                self.verify_packages = [p + "$" for p in self.packages]
            else:
                return

        pm = self.policy.package_manager
        verify_cmd = pm.build_verify_command(self.verify_packages)
        if verify_cmd:
            self.add_cmd_output(verify_cmd)

    def postproc(self):
        """Perform any postprocessing. To be replaced by a plugin if required.
        """
        pass

    def report(self):
        """ Present all information that was gathered in an html file that
        allows browsing the results.
        """
        # make this prettier
        html = u'<hr/><a name="%s"></a>\n' % self.name()

        # Intro
        html = html + "<h2> Plugin <em>" + self.name() + "</em></h2>\n"

        # Files
        if len(self.copied_files):
            html = html + "<p>Files copied:<br><ul>\n"
            for afile in self.copied_files:
                html = html + '<li><a href="%s">%s</a>' % \
                    (u'..' + _to_u(afile['dstpath']), _to_u(afile['srcpath']))
                if afile['symlink'] == "yes":
                    html = html + " (symlink to %s)" % _to_u(afile['pointsto'])
                html = html + '</li>\n'
            html = html + "</ul></p>\n"

        # Command Output
        if len(self.executed_commands):
            html = html + "<p>Commands Executed:<br><ul>\n"
            # convert file name to relative path from our root
            # don't use relpath - these are HTML paths not OS paths.
            for cmd in self.executed_commands:
                if cmd["file"] and len(cmd["file"]):
                    cmd_rel_path = u"../" + _to_u(self.commons['cmddir']) \
                        + "/" + _to_u(cmd['file'])
                    html = html + '<li><a href="%s">%s</a></li>\n' % \
                        (cmd_rel_path, _to_u(cmd['exe']))
                else:
                    html = html + '<li>%s</li>\n' % (_to_u(cmd['exe']))
            html = html + "</ul></p>\n"

        # Alerts
        if len(self.alerts):
            html = html + "<p>Alerts:<br><ul>\n"
            for alert in self.alerts:
                html = html + '<li>%s</li>\n' % _to_u(alert)
            html = html + "</ul></p>\n"

        # Custom Text
        if self.custom_text != "":
            html = html + "<p>Additional Information:<br>\n"
            html = html + _to_u(self.custom_text) + "</p>\n"

        if six.PY2:
            return html.encode('utf8')
        else:
            return html

    def check_process_by_name(self, process):
        """Checks if a named process is found in /proc/[0-9]*/cmdline.
        Returns either True or False."""
        status = False
        cmd_line_glob = "/proc/[0-9]*/cmdline"
        try:
            cmd_line_paths = glob.glob(cmd_line_glob)
            for path in cmd_line_paths:
                f = open(path, 'r')
                cmd_line = f.read().strip()
                if process in cmd_line:
                    status = True
        except IOError as e:
            return False
        return status

    def get_process_pids(self, process):
        """Returns PIDs of all processes with process name.
        If the process doesn't exist, returns an empty list"""
        pids = []
        cmd_line_glob = "/proc/[0-9]*/cmdline"
        cmd_line_paths = glob.glob(cmd_line_glob)
        for path in cmd_line_paths:
            try:
                with open(path, 'r') as f:
                    cmd_line = f.read().strip()
                    if process in cmd_line:
                        pids.append(path.split("/")[2])
            except IOError as e:
                continue
        return pids


class RedHatPlugin(object):
    """Tagging class for Red Hat's Linux distributions"""
    pass


class SCLPlugin(RedHatPlugin):
    """Superclass for plugins operating on Software Collections (SCLs).

    Subclasses of this plugin class can specify class.files and class.packages
    using "%(scl_name)s" interpolation. The plugin invoking mechanism will try
    to match these against all found SCLs on the system. SCLs that do match
    class.files or class.packages are then accessible via self.scls_matched
    when the plugin is invoked.

    Additionally, this plugin class provides "add_cmd_output_scl" (run
    a command in context of given SCL), and "add_copy_spec_scl" and
    "add_copy_spec_limit_scl" (copy package from file system of given SCL).

    For example, you can implement a plugin that will list all global npm
    packages in every SCL that contains "npm" package:

    class SCLNpmPlugin(Plugin, SCLPlugin):
        packages = ("%(scl_name)s-npm",)

        def setup(self):
            for scl in self.scls_matched:
                self.add_cmd_output_scl(scl, "npm ls -g --json")
    """

    @property
    def scls_matched(self):
        if not hasattr(type(self), '_scls_matched'):
            type(self)._scls_matched = []
        return type(self)._scls_matched

    def _get_scls(self):
        output = sos_get_command_output("scl -l")["output"]
        return [scl.strip() for scl in output.splitlines()]

    def convert_cmd_scl(self, scl, cmd):
        """wrapping command in "scl enable" call and adds proper PATH
        """
        # load default SCL prefix to PATH
        prefix = self.policy.get_default_scl_prefix()
        # read prefix from /etc/scl/prefixes/${scl} and strip trailing '\n'
        try:
            prefix = open('/etc/scl/prefixes/%s' % scl, 'r').read()\
                     .rstrip('\n')
        except Exception as e:
            self._log_error("Failed to find prefix for SCL %s, using %s"
                            % (scl, prefix))

        # expand PATH by equivalent prefixes under the SCL tree
        path = os.environ["PATH"]
        for p in path.split(':'):
            path = '%s/%s%s:%s' % (prefix, scl, p, path)

        scl_cmd = "scl enable %s \"PATH=%s %s\"" % (scl, path, cmd)
        return scl_cmd

    def add_cmd_output_scl(self, scl, cmds, **kwargs):
        """Same as add_cmd_output, except that it wraps command in
        "scl enable" call and sets proper PATH.
        """
        if isinstance(cmds, six.string_types):
            cmds = [cmds]
        scl_cmds = []
        for cmd in cmds:
            scl_cmds.append(self.convert_cmd_scl(scl, cmd))
        self.add_cmd_output(scl_cmds, **kwargs)

    # config files for Software Collections are under /etc/${prefix}/${scl} and
    # var files are under /var/${prefix}/${scl} where the ${prefix} is distro
    # specific path. So we need to insert the paths after the appropriate root
    # dir.
    def convert_copyspec_scl(self, scl, copyspec):
        scl_prefix = self.policy.get_default_scl_prefix()
        for rootdir in ['etc', 'var']:
            p = re.compile('^/%s/' % rootdir)
            copyspec = p.sub('/%s/%s/%s/' % (rootdir, scl_prefix, scl),
                             copyspec)
        return copyspec

    def add_copy_spec_scl(self, scl, copyspecs):
        """Same as add_copy_spec, except that it prepends path to SCL root
        to "copyspecs".
        """
        if isinstance(copyspecs, six.string_types):
            copyspecs = [copyspecs]
        scl_copyspecs = []
        for copyspec in copyspecs:
            scl_copyspecs.append(self.convert_copyspec_scl(scl, copyspec))
        self.add_copy_spec(scl_copyspecs)

    def add_copy_spec_limit_scl(self, scl, copyspec, **kwargs):
        """Same as add_copy_spec_limit, except that it prepends path to SCL
        root to "copyspec".
        """
        self.add_copy_spec_limit(
            self.convert_copyspec_scl(scl, copyspec),
            **kwargs
        )


class PowerKVMPlugin(RedHatPlugin):
    """Tagging class for IBM PowerKVM Linux"""
    pass


class ZKVMPlugin(RedHatPlugin):
    """Tagging class for IBM ZKVM Linux"""
    pass


class UbuntuPlugin(object):
    """Tagging class for Ubuntu Linux"""
    pass


class DebianPlugin(object):
    """Tagging class for Debian Linux"""
    pass


class SuSEPlugin(object):
    """Tagging class for SuSE Linux distributions"""
    pass


class IndependentPlugin(object):
    """Tagging class for plugins that can run on any platform"""
    pass


class ExperimentalPlugin(object):
    """Tagging class that indicates that this plugin is experimental"""
    pass


def import_plugin(name, superclasses=None):
    """Import name as a module and return a list of all classes defined in that
    module. superclasses should be a tuple of valid superclasses to import,
    this defaults to (Plugin,).
    """
    plugin_fqname = "sos.plugins.%s" % name
    if not superclasses:
        superclasses = (Plugin,)
    return import_module(plugin_fqname, superclasses)

# vim: set et ts=4 sw=4 :
