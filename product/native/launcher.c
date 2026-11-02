/* No CRT or third-party bootstrap imports. Compile for x64 Windows 6.1. */
#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#define _WIN32_WINNT 0x0601
#include <windows.h>
/* MinGW linker anchor. This launcher has no TLS callbacks or CRT startup. */
DWORD _tls_index=0;

static SIZE_T length(const WCHAR *s) { SIZE_T n=0; while(s[n]) ++n; return n; }
static BOOL append(WCHAR *out,const WCHAR *value) {
    SIZE_T a=length(out),b=length(value),i;
    if(a+b>=32767)return FALSE;
    for(i=0;i<=b;++i)out[a+i]=value[i];
    return TRUE;
}
static BOOL contains(const WCHAR *s,const WCHAR *p) {
    SIZE_T i,j; for(i=0;s[i];++i) { for(j=0;p[j]&&s[i+j]==p[j];++j){} if(!p[j])return TRUE; } return FALSE;
}
static void fail(const WCHAR *message,DWORD code) {
    const WCHAR *args=GetCommandLineW();
    if(!contains(args,L"--no-browser")&&!contains(args,L"--report"))
        MessageBoxW(NULL,message,L"PipelinePer 启动失败",MB_OK|MB_ICONERROR);
    ExitProcess(code);
}
void WINAPI entry(void) {
    WCHAR *base=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,32768*sizeof(WCHAR));
    WCHAR *python=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,32768*sizeof(WCHAR));
    WCHAR *command=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,32768*sizeof(WCHAR));
    WCHAR digits[12]; DWORD pid=GetCurrentProcessId(),exit_code=1,n; SIZE_T i,j;
    STARTUPINFOW si; PROCESS_INFORMATION pi; const WCHAR *tail=GetCommandLineW();
    SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX|SEM_NOOPENFILEERRORBOX);
    if(!base||!python||!command)fail(L"无法分配启动内存。",10);
    n=GetModuleFileNameW(NULL,base,32768);
    if(!n||n>=32767)fail(L"软件路径过长，请解压到较短目录。",11);
    i=n;while(i&&base[i-1]!=L'\\'&&base[i-1]!=L'/')--i;
    if(!i)fail(L"无法定位软件目录。",12);base[i-1]=0;
    if(*tail==L'\"'){++tail;while(*tail&&*tail!=L'\"')++tail;if(*tail)++tail;}
    else while(*tail&&*tail!=L' '&&*tail!=L'\t')++tail;
    while(*tail==L' '||*tail==L'\t')++tail;
    j=0;do{digits[j++]=(WCHAR)(L'0'+pid%10);pid/=10;}while(pid);digits[j]=0;
    for(i=0;i<j/2;++i){WCHAR t=digits[i];digits[i]=digits[j-1-i];digits[j-1-i]=t;}
    if(!append(python,base)||!append(python,L"\\runtime\\python38\\pythonw.exe")||
       !append(command,L"\"")||!append(command,python)||
       !append(command,L"\" -I -B -X utf8 \"")||!append(command,base)||
       !append(command,L"\\payload\\product\\desktop_entry.py\" --package-root \"")||
       !append(command,base)||!append(command,L"\" --launcher-pid ")||!append(command,digits)||
       !append(command,L" ")||!append(command,tail))fail(L"命令过长，请缩短路径和参数。",13);
    for(i=0;i<sizeof(si);++i)((BYTE*)&si)[i]=0;
    for(i=0;i<sizeof(pi);++i)((BYTE*)&pi)[i]=0;
    si.cb=sizeof(si);
    if(!CreateProcessW(python,command,NULL,NULL,FALSE,CREATE_NO_WINDOW,NULL,base,&si,&pi))
        fail(L"无法启动包内 Python。请完整解压软件，检查 runtime\\python38 和目标机运行库。",14);
    CloseHandle(pi.hThread);WaitForSingleObject(pi.hProcess,INFINITE);
    GetExitCodeProcess(pi.hProcess,&exit_code);CloseHandle(pi.hProcess);
    if(exit_code>=0xc0000000UL)
        fail(L"包内运行环境异常退出。请先执行环境检查，核对 Win7 系统补丁与 VC/UCRT 运行库，并保留错误记录。",exit_code);
    ExitProcess(exit_code);
}
