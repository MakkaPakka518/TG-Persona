"""完整 FastAPI 接口回归：子进程复制代码到临时目录，绝不读取真实 data。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ('fastapi', 'telethon', 'httpx')


class IsolatedAPITests(unittest.TestCase):
    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in DEPENDENCIES), '需要 fastapi/telethon/httpx')
    def test_isolated_http_api_probe(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe'],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def probe():
    import logging
    import time
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory(prefix='tg-api-test-') as tmp:
        base = Path(tmp)
        for name in ('main.py', 'updater.py', 'VERSION'):
            (base / name).write_bytes((ROOT / name).read_bytes())
        (base / 'web').mkdir()
        (base / 'web/index.html').write_bytes((ROOT / 'web/index.html').read_bytes())
        os.environ['PANEL_PASSWORD'] = 'Only-test-password-123'
        os.environ['TRUSTED_PROXIES'] = ''
        sys.path.insert(0, str(base))
        import main
        logging.getLogger('httpx').setLevel(logging.WARNING)
        client = TestClient(main.app)
        assertions = 0

        def check(condition, label):
            nonlocal assertions
            if not condition:
                raise AssertionError(label)
            assertions += 1

        check(main.DB_PATH.parent == base / 'data', '数据库必须位于隔离目录')
        check(client.get('/').status_code == 200, '首页正常')
        check(client.get('/api/settings').status_code == 401, '未登录API必须拒绝')
        captcha = client.get('/api/panel/captcha').json()
        check(captcha['image'].startswith('data:image/png;base64,'), '验证码为PNG dataURL')
        check('svg' not in captcha, '验证码不携带明文SVG')
        import base64
        png = base64.b64decode(captcha['image'].split(',')[1])
        check(png.startswith(b'\x89PNG\r\n\x1a\n'), '验证码PNG格式正确')
        check(b'tEXt' not in png and b'iTXt' not in png, '验证码无答案文本元数据')
        main._login_fail.clear(); main._captcha.clear()
        response = client.post('/api/panel/login', json={'password': 'Only-test-password-123'},
                               headers={'x-forwarded-proto': 'https'})
        check(response.status_code == 200, '正确密码登录成功')
        check('secure' not in response.headers['set-cookie'].lower(), '不可信XFP不能伪造HTTPS')
        check(client.get('/api/settings').status_code == 200, '有效会话可访问API')
        check(client.post('/api/panel/password', json={'password': 'Another-test-password-456'}).status_code in (400,403), '改密要求原密码')
        check(client.post('/api/panel/password', json={'old': 'wrong-dummy', 'password': 'Another-test-password-456'}).status_code == 403, '错误原密码被拒绝')
        check(client.post('/api/panel/password', json={'old': 'Only-test-password-123', 'password': 'Another-test-password-456'}).status_code == 200, '正确原密码可改密')
        check(client.get('/api/settings').status_code == 200, '改密后当前设备续会话')
        stale = main.create_session('testclient', 'dummy')
        main.conn.execute('UPDATE panel_sessions SET created=? WHERE token=?', (int(time.time())-60*86400,stale)); main.conn.commit()
        client.cookies.clear(); client.cookies.set('panel_token', stale)
        check(client.get('/api/settings').status_code == 401, '60天旧token必须拒绝')
        check(main.conn.execute('SELECT 1 FROM panel_sessions WHERE token=?',(stale,)).fetchone() is None, '过期token已清理')
        main._login_fail.clear();main._captcha.clear();client.cookies.clear()
        for index in range(10):
            main._captcha['testclient'] = ('DUMMY', time.time())
            response = client.post('/api/panel/login',json={'password':'invalid-dummy','captcha':'DUMMY'},
                                   headers={'x-forwarded-for':f'198.51.100.{index+1}'})
            check(response.status_code == 403, '错误密码计数不中途被伪造XFF重置')
        response = client.post('/api/panel/login',json={'password':'Another-test-password-456'},
                               headers={'x-forwarded-for':'203.0.113.99'})
        check(response.status_code == 429, '换XFF不能绕过锁定')
        main._login_fail.clear();main._captcha.clear()
        https_client = TestClient(main.app, base_url='https://testserver')
        response=https_client.post('/api/panel/login',json={'password':'Another-test-password-456'})
        check(response.status_code == 200 and 'secure' in response.headers['set-cookie'].lower(), 'HTTPS登录启用Secure cookie')
        check(https_client.get('/api/autos').status_code == 200, '自动任务接口可用')
        result=https_client.post('/api/autos',json={'name':'test-signin','type':'signin','target':'@dummy','command':'/sign','enabled':0})
        check(result.status_code == 200, '创建禁用测试签到任务成功')
        tasks=https_client.get('/api/autos').json()['autos']
        check(len(tasks)==1 and tasks[0]['signed_today'] is False, '新任务不误显示今日已签')
        main.conn.execute('UPDATE automations SET last_at=? WHERE id=?',('2000-01-01',tasks[0]['id']));main.conn.commit()
        check(https_client.get('/api/autos').json()['autos'][0]['signed_today'] is False, '旧成功日期不显示今日已签')
        main.conn.close(); client.close();https_client.close()
        print(f'隔离HTTP接口 {assertions} 项通过；无Telegram/线上操作')


if __name__ == '__main__':
    if '--probe' in sys.argv:
        probe()
    else:
        unittest.main()
