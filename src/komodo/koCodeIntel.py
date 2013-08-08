from xpcom.components import classes as Cc, interfaces as Ci, ProxyToMainThreadAsync, ProxyToMainThread
from xpcom import nsError as Cr, COMException
from xpcom.server import UnwrapObject
from zope.cachedescriptors.property import Lazy as LazyProperty

from argparse import Namespace
from codeintel2.common import PRIORITY_CURRENT, PRIORITY_IMMEDIATE
from os.path import join
import atexit
import bisect
import collections
import directoryServiceUtils
import functools
import json
import logging
import os.path
import operator
import process
import Queue
import re
import socket
import sys
import threading
import time
import urllib
import weakref

log = logging.getLogger("codeintel.komodo")

class KoCodeIntelService:
    _com_interfaces_ = [Ci.koICodeIntelService,
                        Ci.nsIObserver,
                        Ci.nsIMemoryMultiReporter]
    _reg_clsid_ = "{fc4ca276-64a7-4d87-ab89-791ba463188d}"
    _reg_contractid_ = "@activestate.com/koCodeIntelService;1"
    _reg_desc_ = "Komodo Code Intelligence Service"

    _enabled = False
    _queue = None # queue of requests submitted before the manager initialized

    mgr = None
    buffers = {}

    def __init__(self):
        self.log = log.getChild(self.__class__.__name__)
        self.debug = self.log.debug
        self.debug("__init__")

        # Outstanding (asynchronus) requests
        # The key is the request id; the value is ???
        self.requests = {}

        self._queue = Queue.Queue()

        self.buffers = weakref.WeakKeyDictionary()

        # The codeintel process; an instance of runtils.KoRunProcess
        self._proc = None

        self._koDirSvc = Cc["@activestate.com/koDirs;1"].getService(Ci.koIDirs)

        self._mgr_lock = threading.Lock()

        self._init_callbacks = Queue.Queue()
        """Callbacks that should be invoked on init"""

        try:
            Cc["@mozilla.org/memory-reporter-manager;1"]\
              .getService(Ci.nsIMemoryReporterManager)\
              .registerMultiReporter(self)
        except COMException as ex:
            if ex.errno != Cr.NS_ERROR_FAILURE:
                raise
            # This can fail during unit tests

    __db_preloader = None
    @property
    def _db_preloader(self):
        if not self.__db_preloader:
            self.__db_preloader = KoCodeIntelDBPreloader(self)
        return self.__db_preloader


    def activate(self, xpcom_callback, resetBrokenDB=False):
        self.debug("activating codeintel service: %r, %r",
                   xpcom_callback, resetBrokenDB)

        self._enabled = True

        try:
            self._init_callbacks.put(xpcom_callback.koIAsyncCallback.callback)
        except AttributeError:
            if callable(xpcom_callback):
                self._init_callbacks.put(xpcom_callback)

        def callback(result=Cr.NS_OK, message=None, success=None):
            if success is None:
                if Cr.NS_SUCCEEDED(result):
                    success = Ci.koIAsyncCallback.RESULT_SUCCESSFUL
                else:
                    success = Ci.koIAsyncCallback.RESULT_ERROR
            data = Namespace(result=result,
                             message=message,
                             _com_interfaces_=[Ci.koIErrorInfo])
            while True:
                try:
                    cb = self._init_callbacks.get_nowait()
                    try:
                        cb(success, data)
                    except:
                        self.log.exception("Failed to invoke init callback %r", cb)
                except Queue.Empty:
                    break # no more callbacks

        self._db_preloader.callback = callback

        # clean up dead managers
        with self._mgr_lock:
            if self.mgr and not self.mgr.is_alive():
                self.mgr = None
            # create a new manager as necessary
            if not self.mgr:
                self.mgr = KoCodeIntelManager(self,
                                              init_callback=self._db_preloader.progress,
                                              shutdown_callback=self._on_mgr_shutdown)
                while True:
                    try:
                        # Tell the manager to deal with it; note that this request
                        # will get queued by the manager for now, since we haven't
                        # actually started the manager.
                        self.mgr.send(**self._queue.get(False))
                    except Queue.Empty:
                        break # no more items
                # new codeintel manager; update all the buffers to use this new one
                for buf in self.buffers.values():
                    buf.mgr = self.mgr

        # run the new manager
        try:
            self.mgr.start(resetBrokenDB)
        except RuntimeError:
            # thread already started
            if self.mgr.state == self.mgr.STATE.CONNECTED:
                callback()

    def _genDBCatalogDirs(self):
        """Yield all possible dirs in which to look for API Catalogs.

        Note: This doesn't filter out non-existant directories.
        """
        yield join(self._koDirSvc.userDataDir, "apicatalogs")    # user
        for extensionDir in directoryServiceUtils.getExtensionDirectories():
            yield join(extensionDir, "apicatalogs")             # user-install exts
        yield join(self._koDirSvc.commonDataDir, "apicatalogs")  # site/common
        # factory: handled by codeintel system (codeintel2/catalogs/...)

    @property
    def enabled(self):
        return self._enabled

    @property
    def isBackEndActive(self):
        return bool(self.mgr and self.mgr.ready)

    def deactivate(self):
        with self._mgr_lock:
            if self.mgr:
                self.mgr.shutdown()
                self.mgr = None
        self._enabled = False

    def cancel(self):
        mgr = self.mgr
        if mgr:
            mgr.abort()

    def scan_document(self, doc, linesAdded, useFileMtime):
        """ Scan a given document """
        if not self.enabled:
            return
        lang = doc.language
        if doc.file:
            path = doc.file.path
        else: # unsaved
            path = join("<Unsaved>", doc.baseName)
        mtime = None
        if not useFileMtime:
            mtime = time.time()
        if (not doc.file) or doc.isDirty or not doc.file.isLocal:
            text = doc.buffer
        else:
            text = None

        self.send(command="scan-document",
                  discardable=True,
                  path=path,
                  priority=PRIORITY_IMMEDIATE if linesAdded
                           else PRIORITY_CURRENT,
                  language=doc.language,
                  encoding=doc.encoding.python_encoding_name,
                  text=text,
                  mtime=mtime,
                  callback=lambda request, response: None)

    def buf_from_koIDocument(self, doc):
        if not self.enabled:
            return
        try:
            doc = UnwrapObject(doc)
        except:
            pass
        self.debug("buf_from_koIDocument: %r [%s]", doc, doc.get_language())
        try:
            buf = self.buffers[doc]
            buf.lang = doc.get_language()
        except KeyError:
            if doc.file:
                path = doc.file.displayPath
            else:
                path = os.path.join("<Unsaved>", doc.baseName)
            self.debug("creating new %s document %s", doc.get_language(), path)
            buf = KoCodeIntelBuffer(lang=doc.get_language(),
                                    path=path,
                                    doc=doc,
                                    svc=self)
            self.buffers[doc] = buf
        return buf

    def is_cpln_lang(self, language):
        return language in self.get_cpln_langs()
    def get_cpln_langs(self):
        if not self.mgr:
            return []
        return self.mgr.cpln_langs

    def is_citadel_lang(self, language):
        return language in self.get_citadel_langs()
    def get_citadel_langs(self):
        if not self.mgr:
            return []
        return self.mgr.citadel_langs

    def is_xml_lang(self, language):
        return language in self.get_xml_langs()
    def get_xml_langs(self):
        return self.mgr.xml_langs if self.mgr else []

    @property
    def available_catalogs(self):
        """Used for the codeintel catalog tree view (prefs window)"""
        return self.mgr.available_catalogs if self.mgr else []

    def update_catalogs(self, update_callback=None):
        if self.mgr:
            self.mgr.update_catalogs(update_callback=update_callback)

    def send(self, discardable=False, **kwargs):
        """Send a request to the manager; the parameters are the same as
        KoCodeIntelManager.send
            @param discardable {boolean} If true, the request is discarded
                instead of being queued if the manager is not available
            @note This is used directly by the code browser implementation
        """
        assert self._enabled, \
            "KoCodeIntelManager.send() shouldn't be called when not enabled"
        if self.mgr:
            self.mgr.send(**kwargs)
        elif not discardable:
            self._queue.put(kwargs)
            self.activate(None)
        else:
            self.debug("discarding request %r", kwargs)

    def _on_mgr_shutdown(self, mgr):
        # The codeintel manager is going away, drop the reference to it
        with self._mgr_lock:
            if self.mgr is mgr:
                self.mgr = None

    # nsIMemoryMultiReporter
    name = "codeintel"
    def collectReports(self, cb, closure):
        have_response = set()
        def on_have_report(request, response):
            for path, data in response.get("memory", {}).items():
                amount = data.get("amount")
                if amount is None:
                    continue # This value was unavailable
                units = {"bytes": Ci.nsIMemoryReporter.UNITS_BYTES,
                         "count": Ci.nsIMemoryReporter.UNITS_COUNT}.get(
                    data.get("units"), Ci.nsIMemoryReporter.UNITS_COUNT)
                if path.startswith("explicit/"):
                    kind = Ci.nsIMemoryReporter.KIND_HEAP
                else:
                    kind = Ci.nsIMemoryReporter.KIND_OTHER
                try:
                    cb.callback("Code Intelligence", # process
                                path, kind, units, amount,
                                data.get("desc", "No description available."),
                                closure)
                except COMException as ex:
                    log.exception("Failed to report %s: %r", path, ex)
            have_response.add(True)

        self.send(command="memory-report", callback=on_have_report)
        thread = Cc["@mozilla.org/thread-manager;1"]\
                   .getService(Ci.nsIThreadManager)\
                   .currentThread
        while not have_response:
            thread.processNextEvent(True)


class KoCodeIntelDBPreloader(object):
    """Class to handle DB preloading notifications"""
    def __init__(self, svc, resetBrokenDB=False):
        """Start the database preloading
        @param svc {KoCodeIntelService} The service instance
        @param callback {FunctionType} A callback function; takes the arguments:
            @param result {nsresult} The status
            @param message {str or None} Some text about something
            Note that the callback may be called multiple times due to a DB
            reset.
        @param resetBrokenDB {bool} Whether to automatically attempt to reset
            the database.  If false, the user can still manually reset it via
            the notification actions.
        """
        self.svc = svc
        self.callback = None
        self.resetBrokenDB = resetBrokenDB
        self.log = log.getChild(self.__class__.__name__)
        self.debug = self.log.debug

    _notification = None
    @property
    def notification(self):
        if not self._notification:
            nm = UnwrapObject(Cc["@activestate.com/koNotification/manager;1"]
                                .getService(Ci.koINotificationManager))
            actions = [{
                "identifier": "stop",
                "label": "Abort",
                "handler": lambda notification, action: self.cancel(),
            }, {
                "identifier": "restart",
                "label": "Restart",
                "handler": lambda notification, action: self.restart(),
                "visible": False,
            }, {
                "identifier": "reset-db",
                "label": "Reset Database",
                "handler": lambda notification, action: self.resetDB(),
                "visible": False,
            }]

            self._notification = nm.add("Pre-loading code intelligence database",
                                       ["codeintel"], "codeintel-db-preload",
                                       timeout=0,
                                       progress=0,
                                       maxProgress=Ci.koINotificationProgress.PROGRESS_INDETERMINATE,
                                       interactive=False, # force status message
                                       actions=actions,
                                       details=
                                       "Pre-loading code intelligence database. "
                                       "This process will improve the speed of first "
                                       "time autocomplete and calltips. It typically "
                                       "takes less than a minute.")
            self._update_status_message()
        return self._notification

    @ProxyToMainThreadAsync
    def _update_status_message(self):
        """Update the status bar message with the notification"""
        if not self._notification:
            return
        Cc["@mozilla.org/observer-service;1"]\
          .getService(Ci.nsIObserverService)\
          .notifyObservers(self._notification, "status_message", None)

    def cancel(self):
        action = self.notification.getActions("stop")[0]
        action.label = "Aborting..."
        self.notification.updateAction(action)
        self.svc.cancel()

    def restart(self, *args, **kwargs):
        # This is a tad ugly...
        self.notification.summary = "Pre-loading code intelligence database"
        self.notification.severity = Ci.koINotification.SEVERITY_INFO
        self.notification.maxProgress = \
            Ci.koINotificationProgress.PROGRESS_INDETERMINATE
        self.showAction("stop")
        # clean up dead managers
        if self.svc.mgr and not self.svc.mgr.is_alive():
            self.svc.mgr = None
        # create a new manager as necessary
        if not self.svc.mgr:
            self.svc.mgr = KoCodeIntelManager(self.svc, self.progress)
        # run the new manager
        if not self.svc.mgr.is_alive():
            self.svc.mgr.start(self.resetBrokenDB)

    def resetDB(self, *args, **kwargs):
        self.resetBrokenDB = True
        self.restart()

    def showAction(self, *actions):
        self.notification.getActions("stop")[0].label = "Abort"
        for action in self.notification.getActions():
            action.visible = action.identifier in actions
            action.enabled = True
            self.notification.updateAction(action)

    def progress(self, message, progress=None):
        assert threading.current_thread().name == "MainThread", \
            "KoCodeIntelService.activate::post_startup() should run on main thread!"
        self.debug("Progress: [%s] %s @%s", message, progress,
                   self.mgr.state if self.mgr else "<None>")
        if progress == "(ABORTED)":
            # abort
            self.debug("Got abort message")
            self.showAction("restart")
            self.notification.summary = \
                "Code Intelligence Initialization Aborted"
            self.notification.severity = \
                Ci.koINotification.SEVERITY_ERROR
            self.notification.maxProgress = \
                Ci.koINotificationProgress.PROGRESS_NOT_APPLICABLE
            self._update_status_message()
            self.callback(result=Cr.NS_ERROR_FAILURE,
                          message=message,
                          success=Ci.koIAsyncCallback.RESULT_SUCCESSFUL)
        elif not self.mgr or self.mgr.state is KoCodeIntelManager.STATE.DESTROYED:
            # Startup died
            self.debug("startup failed: %s", message)
            self.notification.summary = message
            self.showAction()
            self.notification.maxProgress = \
                Ci.koINotificationProgress.PROGRESS_NOT_APPLICABLE
            self.notification.severity = \
                Ci.koINotification.SEVERITY_ERROR
            self._update_status_message()
            self.callback(result=Cr.NS_ERROR_FAILURE,
                          message=message)
        elif self.mgr.state is KoCodeIntelManager.STATE.BROKEN:
            # db is broken, needs manual intervention
            self.notification.summary = "There is an error with your code " \
                                         "intelligence database; it must be " \
                                         "reset before it can be used."
            self.notification.details = ""
            self.notification.severity = \
                Ci.koINotification.SEVERITY_ERROR
            self.notification.maxProgress = \
                Ci.koINotificationProgress.PROGRESS_NOT_APPLICABLE
            self.showAction("reset-db")
            self._update_status_message()
            self.callback(result=Cr.NS_ERROR_FAILURE,
                          message="Code intelligence database error",
                          success=Ci.koIAsyncCallback.RESULT_SUCCESSFUL)
        elif self.mgr.state is KoCodeIntelManager.STATE.READY:
            # db is good
            if self._notification:
                self.notification.maxProgress = \
                    Ci.koINotificationProgress.PROGRESS_NOT_APPLICABLE
                self.showAction()
                try:
                    self.notification.expiresAt = 1 # dawn of time; removes it
                    self._update_status_message()
                except:
                    log.exception("failed")
            self.callback(result=Cr.NS_OK, message=None,
                          success=Ci.koIAsyncCallback.RESULT_SUCCESSFUL)
        elif message is None and progress is None:
            pass # nothing to report
        else:
            # progress update, not finished yet
            if progress is Ci.koINotificationProgress.PROGRESS_NOT_APPLICABLE:
                self.notification.maxProgress = progress
            elif isinstance(progress, (int, float)):
                self.notification.progress = progress
                self.notification.maxProgress = 100
            else:
                pass # No useful progress (updating message only, most likely
                     # because an error occurred in init_child).
            if message is not None:
                details = self.notification.details
                if details:
                    details += "\n"
                self.notification.details = details + message
            # don't invoke callback

    @property
    def mgr(self):
        return self.svc.mgr

class KoCodeIntelManager(threading.Thread):
    """This class manages a connection to an out-of-process codeintel process.
    """

    _com_interfaces_ = [Ci.nsIObserver]

    class STATE(object):
        """The intialization state of the codeintel manager.
        This is used as an internal enum; not for external use."""
        UNINITIALIZED = ("uninitialized",) # not initialized
        CONNECTED = ("connected",) # child process spawned, connection up; not ready
        BROKEN = ("broken",) # database is broken and needs to be reset
        READY = ("ready",) # ready for use
        QUITTING = ("quitting",) # shutting down
        DESTROYED = ("destroyed",) # connection shut down, child process dead

    svc = None # reference to KoCodeIntelService
    proc = None # the child proces
    conn = None # A (TCP) connection to the child process
    pipe = None # file-like object to read/write with
    _state = STATE.UNINITIALIZED
    _state_condvar = None
    _abort = None # things to abort

    requests = {} # Outstanding requests; the key is the request id,
                  # the value is (callback, dict-of-request-args)
    unsent_requests = None # requests that have not yet been sent
                           # list of (callback, dict-of-request-args)

    _send_request_thread = None # background thread to send unsent requests
    _reset_db_as_necessary = False # whether to reset the db if it's broken
    _watchdog_thread = None # background thread to watch for process termination

    cpln_langs = []
    citadel_langs = []
    xml_langs = []
    _stdlib_langs = [] # languages which support standard libraries
    languages = {}
    available_catalogs = [] # see get-available-catalogs command

    def __init__(self, service, init_callback=None, shutdown_callback=None):
        """Construct a code intel manager
        @param service {KoCodeIntelService} Reference to the owning service
        @param init_callback {callable} A callback to be fired for
            initialization status updates. It has the following arguments:
                {str} An update message for the user
                {float} A percentage for the current progress
            The callback should inspect the manager for the current status.
        @param shutdown_callback {callback} A callback to be invoked when the
            manager is shutting down.  It takes one argument, which is this
            manager instance.
        """
        self.log = log.getChild(self.__class__.__name__)
        self.debug = self.log.debug
        self.debug("initializing")
        self.svc = service
        self._init_callback = ProxyToMainThreadAsync(init_callback)
        self._shutdown_callback = ProxyToMainThreadAsync(shutdown_callback)
        self._next_id = 0
        self._abort = set()
        self._state_condvar = threading.Condition()
        self.requests = {} # keyed by request id; value is tuple
                           # (callback, request data, time sent)
                           # requests will time out at some point...
        self.unsent_requests = Queue.Queue()
        threading.Thread.__init__(self,
                                  name="Komodo Codeintel Manager %s" % (id(self)))
        self.daemon = True
        atexit.register(self.kill)

        env = Cc["@activestate.com/koUserEnviron;1"].getService()
        self._global_env = KoCodeIntelEnvironment(environment=env,
                                                  pref_change_callback=self.set_global_environment)

        Cc["@activestate.com/koPrefService;1"]\
          .getService(Ci.koIPrefService)\
          .prefs\
          .prefObserverService\
          .addObserverForTopics(self, ["xmlCatalogPaths"], True)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        with self._state_condvar:
            self._state = value
            self._state_condvar.notifyAll()

    def start(self, resetDBAsNecessary=False):
        self._reset_db_as_necessary = resetDBAsNecessary
        threading.Thread.start(self)

    def init_child(self):
        """Initialize the manager, spawning the child process and set up
        communication.  This runs on the background thread.
        """
        assert threading.current_thread().name != "MainThread", \
            "KoCodeIntelService.init_child should run on background thread!"
        self.debug("initializing child process")
        try:
            koDirSvc = Cc["@activestate.com/koDirs;1"].getService(Ci.koIDirs)
            cmd = [koDirSvc.pythonExe,
                   join(koDirSvc.supportDir, "codeintel", "oop-driver.py"),
                   "--import-path", koDirSvc.komodoPythonLibDir,
                   "--database-dir", join(koDirSvc.userDataDir, "codeintel")]

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("localhost", 0))
            sock.listen(0)

            cmd += ["--connect", "%s:%s" % sock.getsockname()]

            # Logging
            try:
                for log_name in logging.Logger.manager.loggerDict.keys():
                    if not log_name.startswith("codeintel"):
                        continue
                    if logging.getLogger(log_name).level is logging.NOTSET:
                        continue
                    cmd += ["--log-level", "%s:%s" %
                            (log_name, logging.getLogger(log_name).getEffectiveLevel())]
            except:
                pass

            cmd += ["--log-file", join(koDirSvc.userDataDir, "codeintel.log")]
            self.debug("Running: %s", " ".join('"' + c + '"' for c in cmd))
            self.proc = process.ProcessOpen(cmd, cwd=None, env=None,
                                            stdin=None,
                                            stdout=None,
                                            stderr=None)
            self._watchdog_thread = threading.Thread(target=self._watchdog_thread,
                                                     name="CodeIntel Subprocess Watchdog",
                                                     args=(self.proc,))
            self._watchdog_thread.start()
            assert self.proc.returncode is None, "Early process death"

            self.conn = sock.accept()
            sock.close() # no need to keep listening
            self.pipe = self.conn[0].makefile("r+b", 0)
            self.state = KoCodeIntelManager.STATE.CONNECTED
        except Exception as ex:
            self.debug("Error initing child: %s", ex)
            self.pipe = None
            self.kill()
            self._init_callback(str(ex))
        else:
            self._send_init_requests()

    def _send_init_requests(self):
        assert threading.current_thread().name != "MainThread", \
            "KoCodeIntelService._send_init_requests should run on background thread!"
        self.debug("sending internal initial requests")

        outstanding_cpln_langs = set()

        def update(summary, response=None, state=KoCodeIntelManager.STATE.DESTROYED,
                   progress=Ci.koINotificationProgress.PROGRESS_NOT_APPLICABLE):
            STATE = KoCodeIntelManager.STATE
            if state in (STATE.DESTROYED, STATE.BROKEN):
                self.kill()
            if state is not None:
                self.state = state
            message = summary
            if response is not None:
                message += "\n" + response.get("message",
                                               "(No further information available)")
            self._init_callback(message, progress)

        def get_cpln_langs(request, response):
            if not response.get("success", False):
                update("Failed to get completion languages:", response)
                return
            self.cpln_langs = sorted(response.get("languages"))
            for lang in self.cpln_langs:
                outstanding_cpln_langs.add(lang)
                self._send(callback=get_lang_info, command="get-language-info",
                           language=lang)

        def get_citadel_langs(request, response):
            if not response.get("success", False):
                update("Failed to get citadel languages:", response)
                return
            self.citadel_langs = sorted(response.get("languages"))

        def get_xml_langs(request, response):
            if not response.get("success", False):
                update("Failed to get XML languages:", response)
                return
            self.xml_langs = sorted(response.get("languages"))

        def get_stdlib_langs(request, response):
            if not response.get("success", False):
                update("Failed to get languages which support standard libraries:", response)
                return
            self._stdlib_langs = sorted(response.get("languages"))

        def get_lang_info(request, response):
            lang = request["language"]
            if not response.get("success", False):
                update("Failed to get information for %s:" % (lang,), response)
                return
            info = self.languages[lang] = Namespace()
            info.cpln_fillup_chars = response["completion-fillup-chars"]
            info.cpln_stop_chars = response["completion-stop-chars"]
            outstanding_cpln_langs.discard(lang)

            if not outstanding_cpln_langs:
                fixup_db({}, {"success": True})

        def fixup_db(request, response):
            command = request.get("command")
            previous_command = request.get("previous-command")
            state = response.get("state")
            req_id = response.get("req_id")

            if req_id in self._abort:
                self.debug("Aborting startup")
                update("Codeintel startup aborted", progress="(ABORTED)")
                return

            update(response.get("message"),
                   state=None,
                   progress=response.get("progress"))

            if "success" not in response:
                # status update
                return

            if command != "database-info":
                if response.get("abort", False):
                    # The request was aborted, don't retry
                    return
                # We just ran some sort of db-fixing command; check current status
                self._send(callback=fixup_db, command="database-info",
                           previous_command=command)
                return

            # Possible db progression:
            # preload-needed -> (preload) -> ready
            # upgrade-needed -> (upgrade) -> preload-needed -> (preload) -> ready
            # upgrade-blocked -> (reset) -> preload-needed -> (preload) -> ready
            # broken -> (reset) -> preload-needed -> (preload) -> ready

            if state == "ready":
                # db is fine
                initialization_completed()
                return

            if state == "preload-needed":
                # database needs preloading
                if not previous_command in (None, "database-reset"):
                    update("Unexpected empty database after %s" %
                            (previous_command,),
                           state=KoCodeIntelManager.STATE.BROKEN)
                    return
                langs = {}
                for lang in self._stdlib_langs:
                    ver = None
                    try:
                        langAppInfo = Cc["@activestate.com/koAppInfoEx?app=%s;1" % lang] \
                                     .getService(Ci.koIAppInfoEx)
                        if langAppInfo.executablePath:
                            # Get the version and update this lang.
                            try:
                                ver_match = re.search("([0-9]+.[0-9]+)", langAppInfo.version)
                                if ver_match:
                                    ver = ver_match.group(1)
                            except:
                                self.log.error("failed to get langAppInfo.version for language %s", lang)
                    except:
                        # No AppInfo, update everything for this lang.
                        pass
                    langs[lang] = ver
                self._send(callback=fixup_db, command="database-preload",
                           languages=langs)
                return
            if state == "upgrade-needed":
                # database needs to be upgraded
                if previous_command is not None:
                    update("Unexpected database upgrade needed after %s" %
                            (previous_command,),
                           state=KoCodeIntelManager.STATE.BROKEN)
                self._send(callback=fixup_db, command="database-upgrade")
                return
            if state == "upgrade-blocked" or state == "broken":
                # database can't be upgraded but can't be used either
                if previous_command is not None:
                    update("Unexpected database requires wiping after %s" %
                            (previous_command,),
                           state=KoCodeIntelManager.STATE.BROKEN)
                if self._reset_db_as_necessary:
                    self._send(callback=fixup_db, command="database-reset")
                else:
                    update("Database is broken and must be reset",
                           state=KoCodeIntelManager.STATE.BROKEN)
                return
            update("Unexpected database state %s" % (state,),
                   state=KoCodeIntelManager.STATE.BROKEN)



        def initialization_completed():
            self.debug("internal initial requests completed")
            self._send_request_thread = threading.Thread(
                target=self._send_queued_requests,
                name="Komodo Codeintel Manager Request Sending Thread")
            self._send_request_thread.daemon = True
            self._send_request_thread.start()
            update("Codeintel ready.",
                   state=KoCodeIntelManager.STATE.READY)

        # register any extensions we have first
        try:
            catman = Cc["@mozilla.org/categorymanager;1"]\
                       .getService(Ci.nsICategoryManager)
            extension_contract_ids = catman.enumerateCategory("codeintel-command-extension")
            extension_contract_ids.QueryInterface(Ci.nsISimpleEnumerator)
            self.debug("got category entry %r", extension_contract_ids)
            while extension_contract_ids.hasMoreElements():
                try:
                    contractIdIface = extension_contract_ids.getNext()
                    contractIdIface.QueryInterface(Ci.nsISupportsCString)
                    contractId = urllib.unquote(contractIdIface.data)
                    self.debug("got contract id: %s", contractId)
                    extension_data = Cc[contractId].createInstance()
                    for path, name in UnwrapObject(extension_data):
                        self._send(command="load-extension",
                                   callback=lambda request, response:None,
                                   **{"module-path": path,
                                      "module-name": name})
                except:
                    log.exception("Error registering codeintel command extension")
        except:
            log.exception("Error registering codeintel extensions")

        # Extra catlogs
        extra_dirs = {}
        extra_dirs["catalog-dirs"] = \
            filter(os.path.exists, self.svc._genDBCatalogDirs())

        # Find extensions that may have codeintel lang-support modules.
        ext_module_dirs = set()
        ext_lexer_dirs = set()
        for ext_dir in directoryServiceUtils.getExtensionDirectories():
            ext_module_dir = join(ext_dir, "pylib")
            if os.path.exists(ext_module_dir):
                ext_module_dirs.add(ext_module_dir)
            ext_lexer_dir = join(ext_dir, "lexers")
            if os.path.exists(ext_lexer_dir):
                ext_lexer_dirs.add(ext_lexer_dir)
        extra_dirs["module-dirs"] = list(ext_module_dirs)
        extra_dirs["lexer-dirs"] = list(ext_lexer_dirs)

        self._send(callback=lambda request, response: None,
                   command="add-dirs",
                   **extra_dirs)

        self._send(callback=get_cpln_langs, command="get-languages",
                   type="cpln")
        self._send(callback=get_citadel_langs, command="get-languages",
                   type="citadel")
        self._send(callback=get_xml_langs, command="get-languages",
                   type="xml")
        self._send(callback=get_stdlib_langs, command="get-languages",
                   type="stdlib-supported")

        self.set_global_environment()
        def update_callback(response):
            if not response.get("success", False):
                update("Failed to get available catalogs:", response)

        self.update_catalogs(update_callback=update_callback)

        # Send the initial XML catalogs
        ProxyToMainThreadAsync(self.observe)(None, "xmlCatalogPaths", None)

    def set_global_environment(self):
        env = self._global_env.env
        self._send(command="set-environment",
                   env=env["env"],
                   prefs=env["prefs"])

    def shutdown(self):
        """Abort any outstanding requests and shut down gracefully"""
        self.abort = True
        if self.state is KoCodeIntelManager.STATE.DESTROYED:
            return # already dead
        if not self.pipe:
            # not quite dead, but already disconnected... ungraceful shutdown
            self.kill()
            return
        self._send(command="quit", callback=self.do_quit)
        self.state = KoCodeIntelManager.STATE.QUITTING

    def send(self, callback=None, **kwargs):
        """Public API for sending a request.
        Requests are expected to be well-formed (has a command, etc.)
        The callback recieves two arguments, the request and the response,
        both as dicts."""
        if self.state is KoCodeIntelManager.STATE.DESTROYED:
            raise RuntimeError("Manager already shut down")
        self.unsent_requests.put((callback, kwargs))

    def _send_queued_requests(self):
        """Worker to send unsent requests"""
        while True:
            with self._state_condvar:
                if self.state is KoCodeIntelManager.STATE.DESTROYED:
                    break # Manager already shut down
                if self.state is not KoCodeIntelManager.STATE.READY:
                    self._state_condvar.wait()
                    continue # wait...
            callback, kwargs = self.unsent_requests.get()
            if callback is None and kwargs is None:
                # end of queue (shutting down)
                break
            self._send(callback, **kwargs)

    def _send(self, callback=None, **kwargs):
        """Private API for sending; ignores the current state of the manager and
        just dumps things over.  The caller should check that it things are in
        the expected state. (Used for initialization.)  This will block the
        calling thread until the data has been written (though possibly not yet
        received on the other end)."""

        if self.state is KoCodeIntelManager.STATE.QUITTING:
            return # Nope, eating all commands during quit
        data = kwargs.copy()
        req_id = hex(self._next_id)
        self.requests[req_id] = (callback, kwargs.copy(), time.time())
        data["req_id"] = req_id
        self._next_id += 1
        text = json.dumps(data, separators=(",", ":"))
        self.debug("sending frame: %s", text)
        self.pipe.write("%i%s" % (len(text), text))

    def run(self):
        """Event loop for the codeintel manager background thread"""
        assert threading.current_thread().name != "MainThread", \
            "KoCodeIntelService.run should run on background thread!"
        self.init_child()
        if not self.proc:
            return # init child failed
        first_buf = True
        discard_time = 0.0
        try:
            buf = ""
            while self.proc and self.pipe:
                # Loop to read from the pipe
                ch = self.pipe.read(1)
                if ch == "{":
                    length = int(buf, 10)
                    buf = ch + self.pipe.read(length - 1)
                    self.debug("Got codeintel response: %s" % (buf,))
                    if first_buf and buf == "{}":
                        first_buf = False
                        buf = ""
                        continue
                    response = json.loads(buf)
                    # handle runs asynchronously and shouldn't raise exceptions
                    self.handle(response)
                    buf = ""
                else:
                    if ch not in "0123456789":
                        raise ValueError("Invalid frame length character " + ch)
                    buf += ch
                now = time.time()
                if now - discard_time > 60: # discard some stale results
                    for req_id, (callback, request, sent_time) in self.requests.items():
                        if sent_time < now - 30 * 60:
                            # sent 30 minutes ago - it's irrelevant now
                            try:
                                if callback:
                                    callback(request, {})
                            except:
                                self.log.exception("Failed timing out request")
                            else:
                                self.debug("Discarding request %r", request)
                            del self.requests[req_id]
        except:
            self.log.exception("Error reading data from codeintel")
            self.kill()

    @ProxyToMainThread
    def handle(self, response):
        """Handle a response from the codeintel process"""
        assert threading.current_thread().name == "MainThread", \
            "KoCodeIntelService.handle() should run on main thread!"
        self.debug("handling: %s", json.dumps(response))
        req_id = response.get("req_id")
        if not req_id:
            # unsolicited response, look for a handler
            try:
                command = str(response.get("command", ""))
                if not command:
                    raise ValueError("Invalid response frame %s" % (json.dumps(response),))
                meth = getattr(self, "do_" + command.replace("-", "_"), None)
                if not meth:
                    raise ValueError("Unknown unsolicited response \"%s\"" % (command,))
                meth(response)
            except:
                log.exception("Error handling unsolicited response")
            return
        callback, request, sent_time = self.requests.get(req_id, (None, None, None))
        if not request:
            try:
                log.error("Discard reponse for unknown request %s (command %s): have %s",
                          req_id, response["command"],
                          sorted(self.requests.keys()))
            except KeyError:
                log.error("Discard reponse for unknown request %s (%r): have %s",
                          req_id, response,
                          sorted(self.requests.keys()))
            return
        command = request.get("command", "")
        assert response.get("command", command) == command, \
            "Got unexpected response command %s from request %s" % (
                response.get("command"), command)
        if "success" in response:
            self.debug("Removing completed request %s", req_id)
            del self.requests[req_id]
        else:
            # unfinished reponse; update the sent time so it doesn't time out
            self.requests[req_id] = (callback, request, time.time())

        if callback:
            callback(request, response)

    def abort(self):
        """Abort something"""
        for req in list(self.requests.keys()):
            self._abort.add(req)
            self._send(command="abort", id=req,
                       callback=lambda request, response: None)

    def do_scan_complete(self, response):
        """Scan complete unsolicited response"""
        path = response.get("path")
        if path:
            Cc["@mozilla.org/observer-service;1"]\
              .getService(Ci.nsIObserverService)\
              .notifyObservers(None, "codeintel_buffer_scanned", path)

    def do_report_message(self, response):
        """Report a message from codeintel (typically, scan status) unsolicited
        response"""
        if response.get("message") is not None:
            self._notification.msg = response["message"]
        if response.get("type") == "scan-progress":
            total = response["total"]
            completed = response["completed"]
            if total <= 0:
                # remove the message
                self._notification.msg = None
            elif total <= completed:
                # all done!
                self._notification.maxProgress = \
                    Ci.koINotificationProgress.PROGRESS_NOT_APPLICABLE
                self._notification.iconURL = "chrome://fugue/skin/icons/tick.png"
                self._notification.timeout = 5000
            else:
                if total < 2:
                    # use indeterminate for one item, since jumping from empty to
                    # full (and invisibile) is useless
                    self._notification.maxProgress = \
                        Ci.koINotificationProgress.PROGRESS_INDETERMINATE
                else:
                    self._notification.progress = completed
                    self._notification.maxProgress = total
                self._notification.iconURL = None # remove any markings
                self._notification.timeout = 0
        self._update_notification()
        self.debug("Report: %r", response)

    @LazyProperty
    def _notification(self):
        """The notification used for database scan progress &c"""
        n = Cc["@activestate.com/koNotification/manager;1"]\
              .getService(Ci.koINotificationManager)\
              .createNotification("codeintel-status-message",
                                  ["codeintel"],
                                  None,
                                  Ci.koINotificationManager.TYPE_PROGRESS |
                                    Ci.koINotificationManager.TYPE_STATUS)
        n.log = True
        return n

    @ProxyToMainThreadAsync
    def _update_notification(self):
        """The notification must be updated from the main thread"""
        try:
            if self._notification.msg is not None:
                Cc["@activestate.com/koNotification/manager;1"]\
                  .getService(Ci.koINotificationManager)\
                  .addNotification(self._notification)
                Cc["@mozilla.org/observer-service;1"]\
                  .getService(Ci.nsIObserverService)\
                  .notifyObservers(self._notification, "status_message", None)
            else:
                Cc["@activestate.com/koNotification/manager;1"]\
                  .getService(Ci.koINotificationManager)\
                  .removeNotification(self._notification)
        except COMException, ex:
            pass

    def do_global_prefs_observe(self, response):
        """Add or remove global preference observers"""
        for name in response.get("remove", []):
            self._global_env.remove_pref_observer(name)

        for name in response.get("add", []):
            self._global_env.add_pref_observer(name)

    @LazyProperty
    def _codeintel_logger(self):
        return logging.getLogger("koCodeIntel")

    def do_report_error(self, response):
        """Report a codeintel error into the error log"""
        message = response.get("message")
        if message:
            self._codeintel_logger.error(message.rstrip())

    def do_quit(self, request, response):
        """Quit successful"""
        self.kill()
        self.debug("do_quit")
        assert threading.current_thread().name == "MainThread", \
            "KoCodeIntelService.activate::do_quit() should run on main thread!"
        if self.is_alive():
            self.join(1)

    def _watchdog_thread(self, proc):
        """Thread handler to watch when the subprocess dies"""
        self.debug("Waiting for process to die...")
        proc.wait()
        self.debug("Child process died: %i", proc.returncode)
        try:
            self.kill()
        except:
            pass # At app shutdown this can die uncleanly
                 # because KoCodeIntelManager is missing

    def kill(self):
        """Kill the subprocess. This may be safely called when the process has
        already exited.  This should *always* be called no matter how the
        process exits, in order to maintain the correct state."""
        try:
            self.proc.kill()
        except:
            pass
        try:
            self.pipe.close()
        except:
            pass # The other end is dead, this is kinda pointless
        try:
            self._global_env.clear_pref_observers()
        except:
            pass # not expecting that... but let it go anyway
        # Shut down the request sending thread (self._send_request_thread)
        try:
            self.unsent_requests.put((None, None))
        except:
            pass # umm... no idea?
        self.state = KoCodeIntelManager.STATE.DESTROYED
        self.pipe = None
        self._shutdown_callback(self)

    @property
    def ready(self):
        return self.state is KoCodeIntelManager.STATE.READY

    def update_catalogs(self, update_callback=None):
        if not update_callback:
            update_callback = lambda *args, **kwargs: None
        def get_available_catalogs(request, response):
            if response.get("success", False):
                self.available_catalogs = response.get("catalogs", [])
            update_callback(response)

        self._send(callback=get_available_catalogs,
                   command="get-available-catalogs")

    def observe(self, subject, topic, data):
        """Preference observer"""
        if topic == "xmlCatalogPaths":
            prefSvc = Cc["@activestate.com/koPrefService;1"].getService()
            catalogs = prefSvc.prefs.getString("xmlCatalogPaths", "")
            catalogs = filter(None, catalogs.split(os.pathsep))

            # get xml catalogs from extensions
            from directoryServiceUtils import getExtensionDirectories
            for dir in getExtensionDirectories():
                candidates = [
                    # The new, cleaner, location.
                    os.path.join(dir, "xmlcatalogs", "catalog.xml"),
                    # The old location (for compat). This is DEPRECATED
                    # and should be removed in a future Komodo version.
                    os.path.join(dir, "catalog.xml"),
                ]
                for candidate in candidates:
                    if os.path.exists(candidate):
                        catalogs.append(candidate)
                        break

            # add our default catalog file
            koDirs = Cc["@activestate.com/koDirs;1"].getService(Ci.koIDirs)
            catalogs.append(os.path.join(koDirs.supportDir, "catalogs", "catalog.xml"))
            self.send(command="set-xml-catalogs", catalogs=catalogs)

class TriggerWrapper(object):
    """Wrapper class to XPCOM-ify a trigger"""
    _com_interfaces_ = [Ci.koICodeIntelTrigger]
    def __init__(self, trg):
        assert trg is not None, "Null trigger!"
        self._trg_ = trg
    def __getattr__(self, name):
        try:
            return self._trg_[name]
        except KeyError:
            raise AttributeError("The attribute %s was not found on the trigger" % (name,))


class KoCodeIntelBuffer(object):
    """A buffer-like object for codeintel; this is specific to a
    KoCodeIntelManager instance."""
    _com_interfaces_ = [Ci.koICodeIntelBuffer]

    path = None # The path to the file for this buffer
    lang = None # The language name for this buffer
    project = None
    send = None

    def __init__(self, lang, path=None, doc=None, svc=None):
        """Create a buffer
        @param lang {str} The language name for this buffer
        @param mgr {KoCodeIntelManager} The owning manager
        @param path {unicode} The path for this buffer, or something like
            "<Unsaved>/Text-1.txt" for an unsaved file
        """
        self.log = log.getChild("KoCodeIntelBuffer")
        self.path = path
        self.lang = lang
        self.doc = doc
        self.send = svc.send
        self.svc = svc

    @property
    def cpln_fillup_chars(self):
        return self.svc.mgr.languages[self.lang].cpln_fillup_chars

    @property
    def cpln_stop_chars(self):
        return self.svc.mgr.languages[self.lang].cpln_stop_chars

    @property
    def env(self):
        """Get the buffer-specific codeintel environment information for this
        buffer (including prefs).
        @returns None if this has no codeintel environment, or a dict containing
            "env" and "prefs" keys.  See the codeintel oop spec (kd 290) for
            details.
        """
        cls = KoCodeIntelEnvironment
        try:
            path = self.doc.file.displayPath
            if path.startswith("macro://") or path.startswith("macro2://"):
                # Ensure macros get completion for the relevant Komodo APIs.
                if path.endswith(".js"):
                    cls = KoCodeIntelJavaScriptMacroEnvironment
                elif path.endswith(".py"):
                    cls = KoCodeIntelPythonMacroEnvironment
        except AttributeError:
            pass # use default environment
        return cls(self.doc, self.project).env

    def _do_error_callback(self, errorCallback, msg):
        if hasattr(errorCallback, "onError"):
            errorCallback.onError(msg)
        else:
            errorCallback(msg)

    def _post_trg_from_pos_handler(self, callback, errorCallback,
                                  context, request, response):
        if not response.get("success"):
            if errorCallback:
                msg = (response.get("message")
                       or ("%s: Can't get a trigger for position %d" %
                           (context, pos)))
                self._do_error_callback(errorCallback, msg)
                return
            else:
                trg = None
        else:
            trg = response["trg"]
            if trg:
                trg = TriggerWrapper(trg)
        try:
            callback.onGetTrigger(trg)
        except:
            self.log.exception("Error calling %s callback", context)

    def trg_from_pos(self, pos, implicit, callback, errorCallback=None):
        self.send(command="trg-from-pos",
                  path=self.path,
                  language=self.lang,
                  pos=pos,
                  env=self.env,
                  implicit=implicit,
                  text=self.doc.buffer if self.doc else None,
                  callback=functools.partial(self._post_trg_from_pos_handler,
                                             callback, errorCallback,
                                             "trg_from_pos"))

    def preceding_trg_from_pos(self, pos, curr_pos, callback, errorCallback=None):
        self.send(command="trg-from-pos",
                  path=self.path,
                  language=self.lang,
                  pos=pos,
                  env=self.env,
                  text=self.doc.buffer if self.doc else None,
                  callback=functools.partial(self._post_trg_from_pos_handler,
                                             callback, errorCallback,
                                             "preceding_trg_from_pos"),
                  **{"curr-pos": curr_pos})

    def defn_trg_from_pos(self, trg_pos, callback, errorCallback=None):
        self.send(command="trg-from-pos",
                  type="defn",
                  path=self.path,
                  language=self.lang,
                  pos=trg_pos,
                  env=self.env,
                  text=self.doc.buffer if self.doc else None,
                  callback=functools.partial(self._post_trg_from_pos_handler,
                                             callback, errorCallback,
                                             "defn_trg_from_pos"))

    EVAL_SILENT = Ci.koICodeIntelBuffer.EVAL_SILENT
    EVAL_QUEUE = Ci.koICodeIntelBuffer.EVAL_QUEUE

    def async_eval_at_trg(self, trg, handler, flags=0):
        """Evaluate a trigger
        @param trg {TriggerWrapper} The trigger to evaluate
        @param handler {koICodeIntelCompletionUIHandler} Handler to report
            results to
        @param flags {int} bitfield of EVAL_* constants
        """
        trg = UnwrapObject(trg)
        assert isinstance(trg, TriggerWrapper), "Invalid trigger"

        @ProxyToMainThreadAsync
        def callback(request, response):
            if not response.get("success"):
                try:
                    handler.setStatusMessage(response.get("message", ""),
                                             response.get("highlight", False))
                except:
                    self.log.exception("Error reporting async_eval_at_trg error: %s",
                                       response.get("message", "<error not available>"))
                return
            try:
                if "retrigger" in response:
                    trg.retriggerOnCompletion = response["retrigger"]

                if "cplns" in response:
                    # split into separate lists
                    types, strings = zip(*response["cplns"])
                    try:
                        handler.setAutoCompleteInfo(strings, types, trg)
                    except:
                        self.log.exception("Error calling setAutoCompleteInfo")
                elif "calltip" in response:
                    try:
                        handler.setCallTipInfo(response["calltip"],
                                               trg,
                                               request.get("explicit", False))
                    except:
                        self.log.exception("Error calling setCallTipInfo")
                elif "defns" in response:
                    defns = map(KoCodeIntelDefinition,
                                response["defns"])
                    handler.setDefinitionsInfo(defns, trg)
            finally:
                handler.done()

        self.send(command="eval",
                  trg=trg._trg_,
                  silent=bool(flags & KoCodeIntelBuffer.EVAL_SILENT),
                  keep_existing=bool(flags & KoCodeIntelBuffer.EVAL_QUEUE),
                  callback=callback)

    def get_calltip_arg_range(self, trg_pos, calltip, curr_pos,
                              callback, errorCallback=None):
        @ProxyToMainThreadAsync
        def callback_wrapper(request, response):
            if not response.get("success") and errorCallback:
                msg = (response.get("message")
                       or ("get_calltip_arg_range: Can't get a calltip at position %d"
                           % (curr_pos)))
                self._do_error_callback(errorCallback, msg)
                return
            start = response.get("start", -1)
            end = response.get("end", -1)
            try:
                callback.onGetCalltipRange(start, end)
            except:
                self.log.exception("Error calling get_calltip_arg_range callback")

        self.send(command="calltip-arg-range",
                  path=self.path,
                  language=self.lang,
                  text=self.doc.buffer if self.doc else None,
                  trg_pos=trg_pos,
                  calltip=calltip,
                  curr_pos=curr_pos,
                  env=self.env,
                  callback=callback_wrapper)


class KoCodeIntelEnvironment(object):
    """Helper object to get the environment to use for codeintel"""

    _com_interfaces_ = [Ci.nsIObserver]

    # XXX marky: we only support observing the global prefs for now; this is
    # fine because we re-send all prefs on each request
    def __init__(self, doc=None, project=None, environment=None,
                 pref_change_callback=None):
        self.doc = doc
        self.project = project
        self.environment = environment
        self._observed_prefs = {}
        self._global_prefs = Cc["@activestate.com/koPrefService;1"] \
                               .getService(Ci.koIPrefService) \
                               .prefs
        self._pref_change_callback = pref_change_callback

    @property
    def env(self):
        """Get the buffer-specific codeintel environment information for this
        buffer (including prefs).  If this environment has no buffer, the global
        enviroment is used.
        @returns None if this has no codeintel environment, or a dict containing
            "env" and "prefs" keys.  See the codeintel oop spec (kd 290) for
            details.
        """
        if self.doc:
            if not self.doc.prefs:
                return None # nothing document-specific
            doc_prefs = self.doc.prefs
        else:
            # global environment
            doc_prefs = None
        proj_prefs = getattr(self.project, "prefset", None)

        result = {"prefs": []}
        if self.environment:
            result["env"] = dict((name, self.environment.get(name))
                                 for name in self.environment.keys())
        prefsets = (doc_prefs,
                    proj_prefs,
                    self._global_prefs)
        for prefset in prefsets:
            if not prefset:
                continue
            level = {}
            for name, data in self._prefs_allowed.items():
                komodo_name = data.komodo_name or name
                if not prefset.hasPref(komodo_name):
                    continue
                try:
                    level[name] = data.getter(prefset, komodo_name)
                except:
                    log.exception("Error getting preference %s", komodo_name)

            result["prefs"].append(level)
        return result

    def add_pref_observer(self, name):
        if name not in self._prefs_allowed:
            log.debug("Refusing to observe pref %s", name)
            return # Nope!
        try:
            self._observed_prefs[name] += 1
            log.debug("Reusing pref observer for %s", name)
        except KeyError:
            # new pref being observed
            self._observed_prefs[name] = 1
            komodo_name = self._prefs_allowed[name].komodo_name or name
            log.debug("Adding new pref observer for %s (%s)",
                      name, komodo_name)
            self._global_prefs\
                .prefObserverService\
                .addObserverForTopics(self,
                                      [komodo_name],
                                      False)

    def remove_pref_observer(self, name):
        if name not in self._prefs_allowed:
            return # Nope!
        self._observed_prefs[name] -= 1
        if self._observed_prefs[name] < 1:
            komodo_name = self._prefs_allowed[name].komodo_name or name
            self._global_prefs\
                .prefObserverService\
                .removeObserverForTopics(self,
                                         [name],
                                         False)
            del self._observed_prefs[name]

    def clear_pref_observers(self):
        for name in self._observed_prefs.keys()[:]:
            self.remove_pref_observer(name)

    @LazyProperty
    def _prefs_allowed(self):
        """Return the whitelist of allowed preferences"""
        _T = collections.namedtuple("PrefData", "getter komodo_name")

        def get_str(prefset, name, default=None):
            """Get a string preference"""
            return prefset.getString(name, default)
        def get_int(prefset, name, default=None):
            """Get an integer preference"""
            return prefset.getLong(name, default)
        def get_bool(prefset, name, default=None):
            """Get a boolean preference"""
            return prefset.getBoolean(name, default)

        def get_json(prefset, name, default=None):
            """Get a JSON-serialized preference"""
            return json.loads(prefset.getString(name, default))

        def get_json_or_eval(prefset, name, default=None):
            """Special fallback for cases where we used eval()"""
            data = prefset.getString(name, default)
            try:
                return json.loads(data)
            except ValueError:
                return eval(data)


        def T(getter=get_str, komodo_name=None):
            """Create a codeintel pref whitelist entry
            @param getter {callable} A function that gets the preference;
                defaults to the string pref getter.  Takes the folloing
                arguments:
                    @param prefset {koIPreferenceSet} The preference set
                    @param name {str} The name of the preference
                    @param default {object} (optional) The default value
            @param komodo_name {str} The preference name Komodo uses;
                defaults to the same as the name codeintel uses.
            """
            return _T(getter=getter, komodo_name=komodo_name)

        # Preferences that may be used for codeintel (whitelist)
        # Each preference may have a "getter" parameter; it should be a callable
        # similar to get_str above.  This defaults to get_str.
        # Each preference may have a "komodo_name" parameter; if given, the
        # pref name codeintel uses does not match the pref name Komodo uses (and the
        # value given is the Komodo name).
        result = {
            "codeintel_scan_files_in_project": T(getter=get_bool),
            "codeintel_max_recursive_dir_depth": T(getter=get_int),
            "codeintel_selected_catalogs": T(getter=get_json_or_eval),
            "javascriptExtraPaths": T(),
            "nodejsDefaultInterpreter": T(),
            "nodejsExtraPaths": T(),
            "perl": T(komodo_name="perlDefaultInterpreter"),
            "perlExtraPaths": T(),
            "php": T(komodo_name="phpDefaultInterpreter"),
            "phpConfigFile": T(),
            "phpExtraPaths": T(),
            "python": T(komodo_name="pythonDefaultInterpreter"),
            "pythonExtraPaths": T(),
            "python3": T(komodo_name="python3DefaultInterpreter"),
            "python3ExtraPaths": T(),
            "ruby": T(komodo_name="rubyDefaultInterpreter"),
            "rubyExtraPaths": T(),
        }
        # Set the result on the class, no need to recompute
        setattr(self.__class__, "_prefs_allowed", result)
        return result

    def observe(self, subject, topic, data):
        """Observe a preference change (at the global level)"""
        if self._pref_change_callback:
            self._pref_change_callback()

class KoCodeIntelJavaScriptMacroEnvironment(KoCodeIntelEnvironment):
    """A codeintel runtime Environment class for Komodo JS macros. Basically
    the Komodo JavaScript API catalog should always be selected.
    """
    @property
    def env(self):
        env = KoCodeIntelEnvironment.env.__get__(self)
        env["prefs"].insert(0, {"codeintel_selected_catalogs": ["komodo"]})
        return env

class KoCodeIntelPythonMacroEnvironment(KoCodeIntelEnvironment):
    """A codeintel runtime Environment class for Komodo Python macros. Basically
    the Komodo Python libs are added to the extra dirs.
    """
    _komodo_python_lib_dir = None
    @LazyProperty
    def komodo_python_lib_dir(self):
        if KoCodeIntelPythonMacroEnvironment._komodo_python_lib_dir is None:
            koDirSvc = Cc["@activestate.com/koDirs;1"].getService(Ci.koIDirs)
            KoCodeIntelPythonMacroEnvironment._komodo_python_lib_dir = \
                join(koDirSvc.mozBinDir, "python")
        return KoCodeIntelPythonMacroEnvironment._komodo_python_lib_dir

    @property
    def env(self):
        env = KoCodeIntelEnvironment.env.__get__(self)
        env["prefs"].insert(0, {"pythonExtraPaths": self.komodo_python_lib_dir})
        return env

class KoCodeIntelDefinition(object):
    _com_interfaces_ = [Ci.koICodeIntelDefinition]
    def __init__(self, data):
        self.__dict__.update(data)

    def equals(self, other):
        """ Equality comparision for XPCOM """
        try:
            other = UnwrapObject(other)
        except:
            pass
        for attr in ("lang", "path", "blobname", "lpath", "name", "line", "ilk",
                     "citdl", "doc", "signature", "attributes", "returns"):
            if getattr(self, attr) != getattr(other, attr):
                return False
        return True

    def toString(self):
        return repr(self)

