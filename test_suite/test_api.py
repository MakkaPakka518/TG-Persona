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
        # 新增测试端点必须经过真实 middleware/Pydantic，外部调用全部 mock。
        from unittest.mock import AsyncMock, patch
        from fastapi.testclient import TestClient
        guest=TestClient(main.app)
        check(guest.post('/api/ai/test',json={}).status_code==401, 'AI测试必须登录')
        check(guest.post('/api/captcha/test',json={'image':'invalid'}).status_code==401, '验证码测试必须登录')
        cfg={'ai_base':'https://example.invalid/v1','ai_key':'fake-api-fixture-key','ai_model':'fixture-model','ai_enabled':'0','ocr_enabled':'0'}
        with patch.object(main,'_call_ai',new=AsyncMock(return_value='测试成功')) as model:
            tested=https_client.post('/api/ai/test',json=cfg)
            check(tested.status_code==200 and tested.json()['ok'], '关闭识别开关仍可测试AI连接')
            check(tested.json()['reply']=='测试成功', 'AI测试展示实际回复')
            check(main.get_setting('ai_key','')=='', '临时测试密钥不保存')
            check(model.call_args.args[0]['model']=='fixture-model', '测试使用当前填写模型')
        with patch.object(main,'_call_ai',new=AsyncMock(return_value='A7B3')):
            image_test=https_client.post('/api/captcha/test',json={**cfg,'ai_enabled':'1','image':captcha['image'],'length':4})
            check(image_test.status_code==200 and image_test.json()['text']=='A7B3', '实际上传接口正确识别测试图片')
            check(image_test.json()['channel']=='ai', '上传接口返回使用通道')
        with patch.object(main,'_classify_local_image',return_value='OCR9'):
            image_test=https_client.post('/api/captcha/test',json={**cfg,'ocr_enabled':'1','image':captcha['image']})
            check(image_test.status_code==200 and image_test.json()['channel']=='ocr', '仅本地OCR的完整接口可用')
        check(https_client.post('/api/captcha/test',json={**cfg,'image':'invalid'}).status_code==400, '损坏图片参数拒绝')
        check(https_client.post('/api/captcha/test',json={**cfg,'image':captcha['image'],'length':17}).status_code==422, '字符长度上限校验')
        check(guest.get('/api/backups').status_code==401, '备份查询必须登录')
        check(guest.post('/api/backups/cleanup',json={'mode':'all'}).status_code==401, '备份清理必须登录')
        with patch.object(main.updater,'list_backups',return_value={'items':[],'count':0,'total_bytes':0,'cleanup_allowed':True,'reason':''}):
            check(https_client.get('/api/backups').status_code==200, '鉴权后的备份查询可用')
        with patch.object(main.updater,'cleanup_backups',return_value={'ok':True,'deleted':[],'freed_bytes':0,'remaining':1}) as cleanup:
            check(https_client.post('/api/backups/cleanup',json={'mode':'keep_latest','keep':1}).status_code==200, '保留最新备份接口可用')
            check(cleanup.call_args.args==('keep_latest',1), '保留策略参数正确')
            check(https_client.post('/api/backups/cleanup',json={'mode':'all'}).status_code==200, '清全部接口显式模式可用')
            check(cleanup.call_args.args==('all',1), '全部清理不调用其它数据路径')
            check(https_client.post('/api/backups/cleanup',json={'mode':'all','keep':True}).status_code==422, '清理保留数拒绝布尔值')
        with patch.object(main.updater,'cleanup_backups',side_effect=RuntimeError('更新正在运行')):
            check(https_client.post('/api/backups/cleanup',json={'mode':'all'}).status_code==409, '更新冲突时服务器拒绝清理')
        new_task={'name':'delay-fixture','target':'@dummy','command':'/start','enabled':0,'command_delay':0,'button_delay':3.5,'refresh_buttons':0}
        delayed=https_client.post('/api/autos',json=new_task)
        check(delayed.status_code==200, '两段延迟任务创建可用')
        delayed_row=next(a for a in https_client.get('/api/autos').json()['autos'] if a['id']==delayed.json()['id'])
        check(delayed_row['command_delay']==0 and delayed_row['button_delay']==3.5 and delayed_row['refresh_buttons']==0, '延迟零值和固定按钮模式真实持久保存')
        check(https_client.post('/api/autos',json={**new_task,'command_delay':-1}).status_code==422, '拒绝负数延迟')
        check(https_client.post('/api/autos',json={**new_task,'button_delay':121}).status_code==422, '拒绝超长延迟')
        check(https_client.post('/api/autos',json={**new_task,'refresh_buttons':2}).status_code==422, '拒绝未知连点模式')
        guest.close()
        main.conn.close(); client.close();https_client.close()
        print(f'隔离HTTP接口 {assertions} 项通过；无Telegram/线上操作')


if __name__ == '__main__':
    if '--probe' in sys.argv:
        probe()
    else:
        unittest.main()
