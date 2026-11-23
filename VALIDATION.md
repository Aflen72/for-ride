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

## ICU / Windows / FTP / 摘要更新的本地验收

- 使用 Linux、Python 3.12 与已安装 MCP SDK；`python -m unittest discover -s tests -q`：149 项通过。包含真实 MCP stdio、JSONL、模拟 HTTP、独立进程文件锁竞争/释放/进程退出与日志恢复。
- `python -m compileall -q indoor_coach tests` 及 `git diff --check` 通过。
- 验证已返回 POST ID 后 GET 失败不会误标拒绝或重发；合成测试覆盖 403、明确 1010、权限或传输错误，不连接真实 ICU。
- 验证当前室内 FTP 单值读取、手动优先、账户切换、历史查询、离线缓存失效以及不可变的课程预览；摘要与分页保留证据门槛、限制标记。
- 本机 SDK 下，空库 `analyze` 的 MCP 文本从完整 13,177 字符降为摘要 7,738 字符；实际体积随数据和 SDK 变化。
- 已配置 Windows / Ubuntu CI。此补丁尚未运行远端 Windows CI，亦未在 Windows 实机、真实 ICU 账号或其 Cloudflare 边缘端到端验证。
