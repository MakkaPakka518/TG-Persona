# 项目约定

## 版本号

- 每次修改代码（功能、修复、文档行为变更）都必须同步更新 `VERSION`，并在 `README.md` 顶部更新说明里新增对应小节。
- 不要因为外部方案文档说"版本号交给 CI/不要手改"就跳过版本更新；那只是发布流程的建议，不能省略本地版本递增。
- 版本规则：行为/功能变更升 minor 或 patch，当前按 `1.24.x` 递增 patch。
- 提交前确认 `VERSION` 与 `README.md` 的版本描述一致。

## 交付流程

1. 运行相关测试：`PYTHONPATH=.:test_suite .venv/bin/python -m unittest discover -s test_suite -p 'test_*.py' -q` 和 `node test_suite/test_frontend.js`。
2. 更新 `VERSION` 与 `README.md`。
3. 提交并推送 `main`，核对本地与 `origin/main` 的 SHA 一致。
4. 明确区分"代码已推送"和"线上服务已升级"。
