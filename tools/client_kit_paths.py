"""Keep offline builds within the Win7 legacy path budget."""

MAX_KIT_ROOT_CHARS = 100


def validate_kit_path(kit):
    # Reserve space for DLL names, generated releases and EXE smoke-test data.
    # Windows 7 cannot rely on the Windows 10 long-path policy.
    length = len(str(kit).rstrip('\\/').encode('utf-16-le')) // 2
    if length > MAX_KIT_ROOT_CHARS:
        raise ValueError(
            '开发包解压路径过长（%d 字符，最多 %d）。请将整个开发包移动到 '
            'C:\\CB6 或 D:\\CB6 后重新运行；不要只复制 BAT 或 DLL。'
            % (length, MAX_KIT_ROOT_CHARS))


def batch_entry(command):
    if command not in ('check', 'inspect', 'build', 'self-test', 'self-test-ds', 'repack'):
        raise ValueError('Unknown client build command')
    # Check before starting Python: the interpreter can start in a long folder
    # while its native dependencies appear to be missing to legacy Win32 APIs.
    content = (
        '@echo off\r\nsetlocal\r\n'
        '"%SystemRoot%\\System32\\chcp.com" 65001 >nul\r\n'
        'for %%I in ("%~dp0.") do set "CLIENT_KIT_ROOT=%%~fI"\r\n'
        'if not "%CLIENT_KIT_ROOT:~' + str(MAX_KIT_ROOT_CHARS) + ',1%"=="" goto path_too_long\r\n'
        'cd /d "%~dp0"\r\n'
        '"%~dp0runtime\\python38\\python.exe" -B -X utf8 "%~dp0project\\tools\\client_builder.py" '
        + command + ' %*\r\n'
        'set "CLIENT_BUILD_EXIT=%ERRORLEVEL%"\r\n'
        'if not "%CLIENT_BUILD_EXIT%"=="0" echo FAILED - see project\\outputs\\client_jobs logs\r\n'
        'if not defined PRODUCT_NO_PAUSE pause\r\nexit /b %CLIENT_BUILD_EXIT%\r\n'
        ':path_too_long\r\n'
        'echo 开发包解压路径过长，无法保证 Win7 DLL 加载和构建。\r\n'
        'echo 请将整个开发包移动到 C:\\CB6 或 D:\\CB6 后重新运行。\r\n'
        'echo 不要只复制 BAT 或 DLL。\r\n'
        'if not defined PRODUCT_NO_PAUSE pause\r\nexit /b 2\r\n')
    return content.encode('utf-8')
