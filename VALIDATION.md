# 验证记录

- 本地 Python 3.12：`python -m unittest discover -s tests -v`，117 项测试通过。
- 包含真实 MCP stdio 子进程握手、工具发现和调用。
- ICU 网络全部模拟，无真实账户、凭据或健康记录参与测试。
- writer 覆盖确认绑定、账户/时区变化、冲突、超时协调、部分成功、并发锁、回读不符与已核验课程重新读取。
- `git diff --check` 通过。
- 自动化测试使用独立构造的固定数值及虚构日期；仓库不包含个人试用样例或其输出。
- 当前发布候选为无旧父提交的根提交；个人试用数据不得进入后续提交。
- Python 3.10 / 3.12 的安装、测试和编译检查由 GitHub Actions 执行。
- 未做真人 ICU 联调、Windows 实机验证、OAuth 或下游设备交付核验。writer 当前依赖 POSIX 文件锁。
- MCP SDK 的非致命 Pydantic 警告不影响现有协议测试。
