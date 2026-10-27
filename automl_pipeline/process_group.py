# -*- coding: utf-8 -*-
"""Own only launched model services; Windows kills them even on console X/parent death."""
import atexit
import os
import subprocess


def process_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    if os.name!='nt':
        try:os.kill(pid,0);return True
        except ProcessLookupError:return False
        except PermissionError:return True
    import ctypes
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)];kernel.GetExitCodeProcess.restype=w.BOOL
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:return ctypes.get_last_error()==5
    try:
        code=w.DWORD();return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
    finally:kernel.CloseHandle(handle)


class WindowsJob(object):
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes=ctypes;self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        size=ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),('LimitFlags',w.DWORD),
                ('MinimumWorkingSetSize',size),('MaximumWorkingSetSize',size),('ActiveProcessLimit',w.DWORD),('Affinity',size),
                ('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',size),('JobMemoryLimit',size),('PeakProcessMemoryUsed',size),('PeakJobMemoryUsed',size)]
        self.kernel.CreateJobObjectW.argtypes=[w.LPVOID,w.LPCWSTR];self.kernel.CreateJobObjectW.restype=w.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD];self.kernel.SetInformationJobObject.restype=w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];self.kernel.AssignProcessToJobObject.restype=w.BOOL
        self.kernel.CloseHandle.argtypes=[w.HANDLE];self.kernel.CloseHandle.restype=w.BOOL
        self.handle=self.kernel.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        info=Extended();info.BasicLimitInformation.LimitFlags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            error=ctypes.WinError(ctypes.get_last_error());self.close();raise error
    def assign(self,child):
        if not self.kernel.AssignProcessToJobObject(self.handle,int(child._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
    def close(self):
        if getattr(self,'handle',None):self.kernel.CloseHandle(self.handle);self.handle=None


class OwnedServices(object):
    def __init__(self):
        self.job=WindowsJob() if os.name=='nt' else None;self.children=[];self.closed=False
        atexit.register(self.close)
    def spawn(self,command,**options):
        child=subprocess.Popen(command,**options)
        try:
            if self.job:self.job.assign(child)
        except OSError:
            # Win7 cannot nest jobs. Retry outside an inherited job only if the
            # containing job explicitly permits breakaway; never leave an orphan.
            child.terminate();child.wait(timeout=5)
            retry=dict(options);retry['creationflags']=retry.get('creationflags',0)|0x01000000
            child=subprocess.Popen(command,**retry)
            try:self.job.assign(child)
            except BaseException:
                child.terminate();child.wait(timeout=5);raise RuntimeError('无法建立价格/效能服务生命周期保护，已清理子进程；请从普通命令窗口启动。')
        self.children.append(child);return child
    def close(self):
        if self.closed:return
        self.closed=True
        if self.job:self.job.close()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        atexit.unregister(self.close)
