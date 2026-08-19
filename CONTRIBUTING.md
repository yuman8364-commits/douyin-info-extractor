# 贡献指南

## 开发环境

建议使用 Windows 11、PowerShell 7 和 Python 3.11：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

涉及 Tkinter 布局、Windows 文件占用或发布 EXE 的改动，必须在真实 Windows 环境补做验证；Linux 上的纯逻辑测试不能替代这一项。

## 修改原则

- 数据安全优先于抓取速度和新增功能。
- 继续只支持公开作品，不加入 Cookie、登录态或私密内容绕过。
- 批量抓取保持串行并保留随机间隔。
- 用户填写的文案、业务“类型”、额外表格列和其它工作表均视为不可覆盖数据。
- 抖音未返回有效数据时必须区分网络失败、风控和解析变化，不得直接断言作品已删除。
- 同一作品以 `aweme_id` 去重；文件大小只能作为旧数据迁移时的人工提示。

## 事务不变量

任何会修改媒体或工作簿的代码都必须保证：

1. 新内容先写入同盘暂存位置。
2. 响应状态、内容类型、实际字节数和图集数量经过验证。
3. 旧内容在正式替换前进入可恢复备份。
4. 工作簿临时副本可重新打开后才能提交。
5. 任一步失败时，旧媒体、旧文案和旧工作簿保持不变或被完整恢复。
6. 取消和窗口关闭最终都会清理 `.part` 与 `.staging`。

## 测试要求

每次改动至少运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

涉及文件事务时，应增加故障注入测试，包括下载中断、图集第 N 张失败、封面失败、工作簿占用/损坏/保存失败和回滚失败提示。涉及界面时，应验证任务状态恢复、输入锁定、取消和安全退出。

测试数据必须使用虚构链接和隔离临时目录，不得引用个人缓存、真实工作簿或下载内容。

## 提交前检查

```powershell
git status --short
git diff --check
git grep -n -E 'C:\\Users\\|https?://v\.douyin\.com/' -- .
```

确认没有提交：

- `config.json`、`input_cache.txt`、日志；
- Excel、媒体、封面、字幕和人工文案；
- `dist/`、`build/`、虚拟环境；
- `PROGRESS.md` 或其它本机协作状态。

## Pull Request

PR 描述应包含：问题、实现方式、数据安全影响、测试结果和仍未完成的真实 Windows/联网验证。不要把离线替身测试表述为真实 GUI 或真实联网验收。
