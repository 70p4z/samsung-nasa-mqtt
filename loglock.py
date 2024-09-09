import threading
import inspect
import os
import traceback
import sys
import logging

LOGLEVELLOCK = os.environ.get('LOGLEVELLOCK', 'ERROR')

threadLocks={}

# Class to wrap Lock and simplify logging of lock usage
class LogLock(object):
    """
    Wraps a standard Lock, so that attempts to use the
    lock according to its API are logged for debugging purposes
    """
    def __init__(self, name):
        self.name = str(name)
        self.log = logging.getLogger('lock')
        self.log.setLevel(LOGLEVELLOCK)
        self.lock = threading.RLock()
        self.log.debug("{0} created {1}".format(inspect.stack()[1][3], self.name))

    def acquire(self, blocking=True):
        caller = inspect.stack()[1][3]
        if caller == "__enter__":
            caller = inspect.stack()[2][3]
        self.log.debug("{0} trying to acquire {1}".format(caller, self.name))
        ret = self.lock.acquire(blocking)
        if ret == True:
            self.log.debug("{0} acquired {1}".format(caller, self.name))
            #traceback.print_stack(sys._current_frames()[threading.get_ident()])
            if not threading.get_ident() in threadLocks:
                threadLocks[threading.get_ident()] = []
            threadLocks[threading.get_ident()].append((caller, self.name))
            for lc in threadLocks[threading.get_ident()]:
                self.log.debug("  {0} has {1}".format(*lc))
        else:
            self.log.debug("{0} non-blocking acquire of {1} lock failed".format(caller, self.name))
        return ret

    def release(self):
        caller = inspect.stack()[1][3]
        if caller == "__exit__":
            caller = inspect.stack()[2][3]
        self.log.debug("{0} releasing {1}".format(caller, self.name))
        threadLocks[threading.get_ident()].pop()
        for lc in threadLocks[threading.get_ident()]:
            self.log.debug("  {0} has {1}".format(*lc))
        self.lock.release()

    def __enter__(self):
        self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False # Do not swallow exceptions
