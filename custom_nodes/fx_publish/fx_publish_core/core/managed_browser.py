"""Persistent, product-owned Chrome profile; never copies a personal profile."""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

LOGIN_URLS = (
    'https://creator.douyin.com/',
    'https://cp.kuaishou.com/',
    'https://channels.weixin.qq.com/',
)


def profile_directory():
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share')
    return root / 'FXAgent' / 'publish-browser' / 'User Data'


def _profile_process_running(profile):
    import psutil
    expected = os.path.normcase(str(profile.resolve()))
    for process in psutil.process_iter(['cmdline']):
        try:
            args = process.info.get('cmdline') or []
            if any(arg.startswith('--type=') for arg in args):
                continue
            for arg in args:
                if arg.startswith('--user-data-dir='):
                    value = arg.split('=', 1)[1].strip('"')
                    if os.path.normcase(str(Path(value).resolve())) == expected:
                        return True
        except (psutil.Error, OSError, ValueError):
            continue
    return False


def _profile_endpoint(profile):
    try:
        lines = (profile / 'DevToolsActivePort').read_text().splitlines()
        port = int(lines[0])
        if not 1024 <= port <= 65535 or not lines[1].startswith('/devtools/browser/'):
            return None
        endpoint = f'http://127.0.0.1:{port}'
        # Explicitly bypass proxies for local browser discovery.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint + '/json/version', timeout=1) as response:
            info = json.loads(response.read())
        if not str(info.get('webSocketDebuggerUrl', '')).endswith(lines[1]):
            return None
        return port
    except (OSError, ValueError, IndexError, KeyError):
        return None


def ensure_publish_browser(executable, timeout=20):
    profile = profile_directory()
    profile.mkdir(parents=True, exist_ok=True)
    running = _profile_process_running(profile)
    if running:
        port = _profile_endpoint(profile)
        if port:
            return port
    else:
        if not executable:
            raise RuntimeError('未找到 Chrome/Edge，无法启动专用发布浏览器。')
        args = [executable, f'--user-data-dir={profile}', '--remote-debugging-address=127.0.0.1',
                '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check', *LOGIN_URLS]
        options = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        subprocess.Popen(args, **options)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = _profile_endpoint(profile)
        if port:
            return port
        time.sleep(0.25)
    raise RuntimeError('专用发布浏览器连接未就绪。已保留账号目录；请关闭该专用窗口后重试，不要清理登录数据。')
