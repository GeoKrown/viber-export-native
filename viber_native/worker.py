"""Isolated, bounded Frida worker. Parent process owns timeout and persistence."""
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]


def check_runtime(pid):
    """Reject untested native ABI versions before loading any hooks."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000,False,pid)
    if not handle:
        raise RuntimeError('process_access_failed')
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel.QueryFullProcessImageNameW(handle,0,buf,ctypes.byref(size)):
            raise RuntimeError('process_path_failed')
        library = str(Path(buf.value).parent / 'Qt6Core.dll')
    finally:
        kernel.CloseHandle(handle)
    version = ctypes.WinDLL('version',use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID]
    version.VerQueryValueW.argtypes = [wintypes.LPCVOID,wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(wintypes.UINT)]
    size = version.GetFileVersionInfoSizeW(library,None)
    if not 0 < size <= 1048576:
        raise RuntimeError('version_unavailable')
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(library,0,size,data):
        raise RuntimeError('version_unavailable')
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(data,'\\',ctypes.byref(pointer),ctypes.byref(length)) or length.value < 52:
        raise RuntimeError('version_unavailable')
    info = ctypes.cast(pointer,ctypes.POINTER(wintypes.DWORD*13)).contents
    detected = (info[2]>>16,info[2]&65535,info[3]>>16,info[3]&65535)
    if info[0] != 0xFEEF04BD or detected != (6,11,2,0):
        raise RuntimeError('unsupported_qt_version')


def validate_request(request):
    if request.get('mode') not in ('list', 'collect', 'history', 'media', 'media-request'):
        raise ValueError('invalid_mode')
    for field, low, high in [('wait_seconds',1,55),('batch_size',1,200),('recent_limit',0,100)]:
        value = request.get(field,100 if field == 'recent_limit' else None)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('invalid_bounds')
    ids = request.get('chat_ids')
    if not isinstance(ids,list) or len(ids)>10 or any(type(x) is not int or not 1<=x<=2147483647 for x in ids):
        raise ValueError('invalid_chat_ids')
    if len(set(ids)) != len(ids) or (request['mode'] in ('collect','history','media','media-request') and not ids):
        raise ValueError('invalid_chat_ids')
    if request['mode'] in ('media', 'media-request'):
        events = request.get('event_ids')
        if not isinstance(events, list) or not 1 <= len(events) <= 20 or any(
                type(x) is not int or not 1 <= x <= 9007199254740991 for x in events) or len(set(events)) != len(events):
            raise ValueError('invalid_media_events')
    if request['mode'] == 'media-request':
        if type(request.get('expected_pid')) is not int or request['expected_pid'] <= 0:
            raise ValueError('invalid_expected_process')
    after = request.get('after_id')
    if not isinstance(after,str) or not after.isdigit() or int(after)>9223372036854775807:
        raise ValueError('invalid_cursor')
    if request['mode']=='history':
        for field in ('cutoff_ms','before_id'):
            if type(request.get(field)) is not int or not 0<=request[field]<=9223372036854775807:
                raise ValueError('invalid_history_bounds')


def read_native(request):
    validate_request(request)
    import frida
    event = threading.Event()
    result = {}
    session = script = None
    device = frida.get_local_device()
    processes = [p for p in device.enumerate_processes() if p.name.lower() == 'viber.exe']
    if len(processes) != 1:
        return {'status': 'unavailable', 'error': 'expected_one_viber_process'}
    if request['mode'] == 'media-request' and processes[0].pid != request['expected_pid']:
        return {'status': 'unavailable', 'error': 'viber_process_changed'}
    check_runtime(processes[0].pid)
    try:
        session = device.attach(processes[0].pid)
        filename = 'media_request.js' if request['mode'] == 'media-request' else 'native.js'
        source = Path(__file__).with_name(filename).read_text(encoding='utf-8')
        script = session.create_script(source.replace('__CONFIG__', json.dumps(request)))

        def message(msg, _data):
            if msg['type'] == 'send' and msg['payload'].get('status') != 'ready':
                result.update(msg['payload'])
                event.set()
            elif msg['type'] == 'error':
                result.update(status='error', error='native_script_error')
                event.set()

        def detached(*_):
            result.update(status='unavailable', error='viber_disconnected')
            event.set()

        session.on('detached', detached)
        script.on('message', message)
        script.load()
        if not event.wait(request['wait_seconds']):
            return {'status': 'idle', 'error': 'waiting_for_database_activity'}
        return dict(result, process_id=processes[0].pid)
    finally:
        if script:
            try:
                script.unload()
            except Exception:
                pass
        if session:
            try:
                session.detach()
            except Exception:
                pass


def main():
    try:
        request = json.loads(sys.stdin.read(16384))
        result = read_native(request)
    except Exception as error:
        # Never forward arbitrary native errors, paths, SQL, or credentials.
        safe_codes = {'unsupported_qt_version','version_unavailable','process_access_failed','process_path_failed'}
        transient = {'ProcessNotFoundError','ProcessNotRespondingError','TransportError',
                     'ServerNotRunningError','InvalidOperationError','TimedOutError'}
        result = {'status': 'unavailable' if type(error).__name__ in transient else 'error',
                  'error': str(error) if str(error) in safe_codes else type(error).__name__}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
