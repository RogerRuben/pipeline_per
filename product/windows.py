"""Windows job ownership: closing the launcher kills only its own workers."""
import ctypes
from ctypes import wintypes as W


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(key, ctypes.c_ulonglong) for key in
                ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
                 'ReadTransferCount','WriteTransferCount','OtherTransferCount')]


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),
                ('LimitFlags',W.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),
                ('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',W.DWORD),
                ('Affinity',ctypes.c_size_t),('PriorityClass',W.DWORD),('SchedulingClass',W.DWORD)]


class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [('BasicLimitInformation',BASIC_LIMIT),('IoInfo',IO_COUNTERS),
                ('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
                ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]


class WorkerJob:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p,W.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = W.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [W.HANDLE,W.HANDLE]
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None,None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle,W.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def watch_parent(pid):
    """Fallback for the brief spawn/assignment interval, without PID reuse races."""
    import os
    import threading
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD,W.BOOL,W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.WaitForSingleObject.argtypes = [W.HANDLE,W.DWORD]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.OpenProcess(0x100000,False,int(pid))  # SYNCHRONIZE
    if not handle:
        os._exit(71)
    def wait():
        kernel.WaitForSingleObject(handle,0xffffffff)
        kernel.CloseHandle(handle)
        os._exit(72)
    threading.Thread(target=wait,daemon=True).start()
