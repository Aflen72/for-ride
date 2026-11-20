# for-ride · 本地骑行台分析与课程建议

面向 AI / agent 的可运行 Python MVP：本地 SQLite、Intervals.icu 只读同步、手动健康记录、七日参考、数据优先证据工作流及下一课／一周建议。不是医疗诊断，也不是已验证的恢复评分产品。

## 五分钟运行

Python 3.10+。源码直接放在仓库根目录，无须解压或寻找构建产物。JSONL 接口只需 Python 标准库；标准 MCP 接口需要安装可选 SDK。

### 1. 克隆

```sh
git clone https://github.com/Aflen72/for-ride.git
cd for-ride
```

### 2. 立即试跑（无需账号、无需安装依赖）

```sh
python -m indoor_coach --list-tools
python -m unittest discover -s tests -v
```

仓库只包含插件源码、使用说明及独立构造的合成自动化测试；不提交个人试用输入、输出、活动或健康记录。

### 3. 安装给 AI / agent 使用

建议建立独立虚拟环境。macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ".[mcp]"
.venv/bin/indoor-coach --list-tools
```

Windows PowerShell（无需修改执行策略或激活脚本）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[mcp]"
.\.venv\Scripts\indoor-coach.exe --list-tools
```

不需要 MCP 时可改为 `python -m pip install .`。安装后也可以从其他目录启动 `indoor-coach` / `indoor-coach-mcp`；将下方 MCP 配置的 `command` 指向该虚拟环境 Python 的绝对路径。首次安装依赖需要联网；本地分析不需要账号或网络。

生产默认数据库 `~/.indoor-coach/coach.sqlite3`。SQLite 是明文文件，不是加密保险箱；请使用系统磁盘加密和适当目录权限，避免云同步目录。

## Agent 接口

**支持官方 SDK 的 MCP stdio，以及无需依赖的 JSONL 协议。** 宿主启动 `python -m indoor_coach --db <本地路径>`，每行输入一个对象，每行 stdout 返回一个结果；进程支持多次请求。其他 agent 可用 subprocess 包装工具，也可直接导入 `indoor_coach.service.Service`。

```json
{"id":"a1","tool":"analyze","arguments":{"as_of":"2040-11-15","ftp":240}}
```

成功 `{"id":"a1","result":...}`，错误 `{"id":"a1","error":{"code":...,"message":...}}`。错误不回显原始数据或认证信息。`--list-tools` 查看所有工具。

| 工具 | 参数 | 副作用 |
|---|---|---|
| prepare_analysis | as_of、可选 ftp / target_id / history_days / refresh | 默认尝试一次授权 ICU 读取并写缓存；只输出有据分析 |
| import_activities | records 数组 | 写本地数据库 |
| import_wellness | records 数组 | 写本地数据库 |
| import_baseline | record 对象 | 写本地数据库 |
| sync_icu | oldest, newest：YYYY-MM-DD | 读取 ICU，写本地数据库 |
| analyze | as_of, 可选 ftp | 无 |
| propose_plan | as_of, 可选 ftp | 保存本地草案并回读校验 |
| list_proposals | 空对象 | 无 |
| preview_icu | proposal_id、account、timezone、ftp、可选 selection | 只读最小课程导出，不批准、不写远端 |

### MCP 接入其他 AI / agent

```sh
pip install '.[mcp]'
python -m indoor_coach.mcp_server
```

宿主配置示例（替换绝对路径；不在配置中硬编码凭据）：

```json
{"mcpServers":{"indoor-coach":{"command":"/absolute/path/to/python","args":["-m","indoor_coach.mcp_server"],"env":{"INDOOR_COACH_DB":"/absolute/path/to/coach.sqlite3"}}}}
```

使用安装了本项目和 MCP SDK 的 Python；Windows 可指向虚拟环境的 `Scripts/python.exe`。MCP 与 JSONL 暴露相同九个工具，无外部写入工具。已通过真实子进程 MCP 握手、工具发现、提案与回读测试。SDK 可能向 stderr 输出非致命依赖警告，stdout 保持协议格式。

### 数据格式与缺失

活动：`id`、`date` 必需；可选 `hr_zone_upper_bpm`（逐区心率上界）、`hr_zone_time_s`（对应秒数）、`sport`、`indoor`（true/false/null）、`duration_s`、`avg_power_w`、`normalized_power_w`、`avg_hr_bpm`、`source`、`restricted`，连接器还会记录 `available_fields`、`withheld_reason` 与 `source_status`（independent / restricted / unknown）。明确室内骑行才进入训练统计，未知类型不会冒充骑行台。没有数据的日期不意味着休息。

每日健康：`date` 必需；可选 `sleep_hours`、`bedtime`（HH:MM）、`hrv_ms`、`hrv_type`（如 RMSSD）、`device`、`resting_hr_bpm`、`deep_sleep_pct`、`light_sleep_pct`、`fatigue`（1–10）。暂无请省略或传 null，不要填 0。日期使用用户自己的当地日历日，所有输入须统一时区；本版不自动跨时区校正。睡眠时长由用户提供，不用入睡时刻推算。

手动七日均值：健康字段同上（不含 date/fatigue），必需 `period_start`、`period_end`、`sample_count`（1–7），可选 `source`；区间最大七天。它是独立的汇总参考，不伪装成七条观测。HRV 要保留指标类型及设备来源，不同来源不混算。七天仅为初始参考，非充分验证的长期生理基线。

### 同日多来源与冲突

手动与 ICU 规范化记录分别保存在 SQLite。同步时 **手动非空值优先**，ICU 的 null 不抹掉手动睡眠、HRV、静息心率或设备；非空冲突保留两份并在同步/分析结果的来源信息中报告。HRV 数值、指标类型、设备作为一组选择，绝不把手动设备名贴到 ICU 数值上。ICU 未提供设备的 HRV 不跨来源比较；当前保守实现中不会生成 HRV 相对基线判断。

重复手动录入按字段更新，省略字段保留已有手动值；显式 null 清除该字段的手动优先值，允许回落到 ICU 来源。清除 HRV 数值时整组回落。来源快照是每个来源的最新状态，并非不可篡改审计历史。旧版无来源记录保守迁移为手动数据。

FTP 参数是用户提供的当前室内 FTP，不从单次低心率或运动自动上调。历史活动的 TSS 也是按该当前 FTP 估算，明确不是活动当时 FTP 下的原始 TSS；不能当作平台历史负荷事实。没有 FTP 时只输出适合缺失数据的非精确建议。

## Intervals.icu 只读接入

仅在你已授权的本地环境设置 `INTERVALS_API_KEY`、可选 `INTERVALS_ATHLETE_ID`（默认 0）。不要把 key 发给 LLM，不要写进示例、仓库或数据库。代码使用官方个人 API Key 的 Basic 认证（用户名 API_KEY）。通过系统安全配置注入环境变量；命令参数没有 key 入口。

```json
{"id":7,"tool":"sync_icu","arguments":{"oldest":"2040-11-08","newest":"2040-11-15"}}
```

固定官方 HTTPS 域名，只读 activities/wellness，不支持任意 URL，不跟随重定向。Strava 来源活动可能因第三方限制仅有元数据，本版保留限制标记和缺失项，不尝试绕过，不能据此断言没有训练或恢复良好。

### 来源判定与旧记录修复

`strava_id` 只可能是合并关联，不能单凭它认定活动来自 Strava。连接器规范化来源枚举，仅明确 `source=STRAVA` 或显式布尔限制标记判为 `restricted`。目前核验的独立来源为 UPLOAD、MANUAL、GARMIN_CONNECT、OAUTH_CLIENT、DROPBOX、POLAR、SUUNTO、COROS、WAHOO；其记录即使含有 Strava 关联 ID，也不会因此被排除，但其他来源条款仍须遵守。

缺失、新增或未知来源枚举，以及格式异常的限制标记，一律标为 `source_status=unknown`、`restricted=null`，暂不计入训练指标。它们分别记入 `unknown_source_count`，不宣称是 Strava 限制，也不等于零负荷。共享规则同时用于 ICU、`import_activities` 验证和分析读取，包括旧库：`restricted=false` 或 `source_status=independent` 不能覆盖明确 STRAVA 来源。旧草案若含受限/未知来源活动上下文，`list_proposals` 仅返回草案ID与需要重新生成的提示，不再转发原内容，原本地草案保留。数据不完整与权限来源分别判断：独立来源没有 NP 时仍不估算 TSS。新的平台来源需核验后扩展枚举，不能依据名称子串自动放行。用户自己记录或设备原始文件的规范化数据可使用 `source=MANUAL` 或 `UPLOAD`，不能把 Strava API 数据改标签冒充原始文件。缺失来源的直接输入也暂不纳入分析；来源标签由调用方如实提供，本版不能证明文件真实来源。

修复旧版误排：更新代码后，对受影响日期范围重新调用 `sync_icu`。同步根据活动 ID 覆盖旧规范化记录，不会重复新增，只有当前官方 API 返回的、明确独立来源记录才解除旧误判。不会扫描本地库批量清除限制，不会改变明确 Strava 来源的状态；未重新返回的历史记录保持原样。重新同步成功后再运行 `analyze`。这只是重新读取已授权 ICU API，不绕过限制，不新增数据来源或网络端点。

依据：[ICU 字段定义](https://forum.intervals.icu/t/server-side-data-model-for-scripts/25781/16)、[官方关于合并关联的说明](https://forum.intervals.icu/t/api-access-to-intervals-icu/609/419)。本工具不代理 Strava API/MCP，也不将已知 Strava API 指标转给 AI；参见[现行 Strava API Policy](https://www.strava.com/legal/api_policy)。

**未做真人账号端到端联调。** 测试使用合成记录及模拟 HTTP；真实账号接入仍需用户在本地设置凭据后验证。上游零时长、零心率等记录可能触发严格校验，导致该批同步失败；当前不支持坏记录隔离，遇到此情况应检查数据，不应把失败当成无训练。面向多用户分发应增加官方 OAuth / PKCE 和宿主授权，不分发个人 key。

## 数据优先工作流：先核查已有数据，再下结论

宿主先调用 `prepare_analysis`，不要先猜测用户心率区间或让用户重复提供已经存在的数据：

```json
{"id":"review-1","tool":"prepare_analysis","arguments":{"as_of":"2040-11-15","target_id":"your-activity-id","history_days":28,"ftp":240,"refresh":true}}
```

1. 默认检查本机 ICU 凭据。已配置时只请求一次所选窗口的 activities 和 wellness；未配置/失败会留下查询状态并继续用本地记录，不循环追问或冒称已核验最新。`refresh=false` 只查看缓存。
2. 先检查用户自己的活动心率分区与分区时间。连接器映射 `icu_hr_zones` / `icu_hr_zone_times`，字段缺失就明确缺失。**这只是活动保存的配置，不是当前账户运动设置；当前版本没有读取 settings 或逐秒 streams 的能力。** 如结论必须依赖它们，宿主需使用另一个确实有权限和能力的工具，或最后只问用户那项缺失证据。
3. 以明确时间窗检索此前同类骑行台记录：默认28日（可选7–90日），早于目标活动日期、时长±25%、平均功率±15%。这只是筛选对照，不是训练效果的因果证明。明确 `target_id` 不存在时不会悄悄换成另一条；省略时取最新日期，同行程同日以ID作确定性排序，不假装知道先后时间。
4. 输出观察事实、计算值与暂定解释的边界：活动ID、日期、来源、窗口、对照筛选、分区依据、缺失项和下一步。平均心率落入某区不证明整堂课的区间时间，也不证明恢复、疲劳、FTP或有氧效果。不得用年龄公式补个人分区，不猜课程结束原因。

MCP 提供 `evidence_first_review` prompt，并在服务器 instructions 和结果中传递工作流。**提示词不是强制模型守约的保证**，因此旧入口也有代码防线：

- `analyze` 不联网，始终重新审阅当前缓存，输出 `data_review`（证据审核，不是健康分）与内容快照ID。
- `propose_plan` 不接受调用方“已核查”标记，先重新执行相同审核。没有最近7日可用训练时返回 `status=needs_data`、`plan=null`，不保存完整周课；有部分证据则允许明确标为暂定的模板，没有FTP就不编瓦数。
- 手动/ICU冲突、HRV来源不可比、样本不足、未知/受限来源、缺少分区或对照、失败刷新都会进入审核问题清单。缺失项限制结论，不等于异常或休息。
- 同步日志只保存窗口、时间和状态，不保存密钥或原始错误。历史曾成功查询不代表现在数据仍新鲜，最新失败会明确显示。成功查询也不保证用户记录了每一场训练。

宿主应先利用可访问数据；只有核查后仍影响具体结论的缺项才问。用户明确“暂无”的数据应保留缺失状态，不反复索取。当前仍是汇总分析与模板，不是自动教练或生理诊断。

## 计划与确认边界

本版只建议和保存有最低证据支持的草案；证据不足时不保存课程。下一课以 `as_of` 后一天为起点，一周建议覆盖之后七天；后续每天输入新数据重新生成提案。所有日期、时长、强度与分段都在提案内。

**MCP/JSONL 没有批准或外部写入工具。** `confirmed=true` 无法开启写入。本地草案和导出都不代表批准。新增 `preview_icu` 只生成可审核的最小课程导出；用户在隔离环境亲自运行独立 CLI 后才能确认、创建和回读核验。真实路线生成不在此 MVP。

## 可解释性与健康边界

逐维度呈现数据、参考、偏差、缺失和提示，不输出貌似有临床依据的综合健康百分数。深浅睡比例只记录/展示，不据此直接判疲劳。手动均值没有标准差，不能制造 z-score。跨午夜入睡时刻使用圆周均值。平均功率不能冒充 NP 算标准 TSS；无 NP 时保留负荷未知。

**当前课程是保守静态模板，不是个性化或周期化训练模型。** 七日活动与负荷会提供给宿主 LLM 作上下文，但当前模板只用当天已知负荷、健康提示及数据完整度选择休息/轻松/耐力课，尚未按前几天累计负荷、目标、训练年龄或可用时间优化。一周草案不应直接当作已完成个性化评估的计划。

规则是透明、版本化的训练建议启发式，不是论文验证的个人医疗模型。输出供宿主 LLM解释，不让 LLM凭空补数、下诊断或自动执行。身体不适优先停训并寻求适当专业帮助，不靠一个分数决定是否安全。

## 隐私

应用本身不调用 LLM。`sync_icu` 访问 ICU；`prepare_analysis` 默认 `refresh=true`，本地已配置凭据时同样访问 ICU。`prepare_analysis(refresh=false)` 和 `analyze` 仅使用本地数据。独立 writer 会读取账户、日历和单课，用户确认后才 POST 新课；`--reconcile-only` 只 GET。**如果宿主是云端 AI，工具结果可能被宿主传给远程模型；本地存储不等于数据绝不离开设备。** 请在宿主侧告知并取得所需授权，最小化返回字段。API key 不会作为工具输出、数据库字段或错误日志返回。数据库保留按日健康数据，应自行制定备份/删除策略。本版单用户，非多租户安全系统。

## 科学与接口依据

- Intervals.icu 官方 API：https://forum.intervals.icu/t/api-access-to-intervals-icu/609
- 官方集成 cookbook：https://forum.intervals.icu/t/intervals-icu-api-integration-cookbook/80090
- HRV 引导训练研究：Vesterinen 2016，https://doi.org/10.1249/MSS.0000000000000910 （跑者研究，不能直接验证本插件骑行规则）
- 可穿戴睡眠分期误差：https://pubmed.ncbi.nlm.nih.gov/33378539/ （设备/样本依赖）
- 恢复评分综述：https://doi.org/10.1515/teb-2025-0001 （不支持随意混合指标后声称科学验证）

## 当前局限

只分析汇总级数据，暂无 FIT/TCX 解析、功率曲线、HR 解耦、个体训练适应模型；不把简化规则当完整教练。无 UI、托管插件商店注册、OAuth、多用户、跨机协调或真实路线。连接层和单位映射在合成/模拟数据上测试，不能等同于真人数据联调完成。


## 开发与验证

```sh
python -m pip install ".[mcp]"
python -m unittest discover -s tests -v
python -m compileall -q indoor_coach tests
```

开发约束：个人试用的数据、样例、日志、导出、账户标识和凭据仅保留在隔离的私有环境，不得提交，也不得通过改标签冒充合成样例。自动化测试必须独立构造，与个人记录无关。提交前检查暂存文件及拟公开历史对象。

测试全用合成数据与模拟网络，**不会调用真人 ICU 账号**。未安装 MCP SDK 时 MCP 集成测试会明确跳过，不能据此宣称完整通过。GitHub Actions 在 Python 3.10 / 3.12 上运行同一套检查；详见 [验证记录](VALIDATION.md)。

```text
indoor_coach/    分析、SQLite、ICU 只读适配器、独立用户 writer、JSONL 与 MCP 服务
tests/          单元及真实 MCP stdio 集成测试
```

向 agent 提供工具时，请要求它区分观测值、估算、缺失和建议；展示关键依据与数据覆盖，不能自动批准草案。用户确认只覆盖具体版本，不能扩展为无限期自动执行授权。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。


## ICU 课程导出与独立用户写入（首版）

`preview_icu` 参数：`proposal_id`（已有草案ID）、`account`（具体 ICU 账户ID，不能为0）、`timezone`（与账户一致的 IANA 时区）、`ftp`（与草案一致）、`selection`（`next` 或 `week`）。调用不联网、不持有 writer 凭据、不批准。将 JSONL 返回的 **result 对象** 或 MCP 返回的导出对象保存为 `preview.json`，交给用户环境；不要连同 JSONL 的 id/result 外壳保存。休息日不上传，next 是草案第一天，第一天休息时拒绝空导出。草案缺 FTP 或功率与 FTP 不符时需重新生成。

```json
{"tool":"preview_icu","arguments":{"proposal_id":"<草案ID>","account":"<实际账户ID>","timezone":"Asia/Shanghai","ftp":200,"selection":"week"}}
```

导出只包含账户、日期/时区、固定瓦数/秒数、总时长、FTP 依据、create 动作、冲突策略、不可变提案ID、24小时有效期及 SHA256。原草案范围转换成 FTP 区间中点并取整到瓦，属于需要重新审阅的具体课程，不能暗中沿用旧批准。每步不再依赖 ICU 当前 FTP；远端 `icu_ftp` 可能不同，不应当作固定瓦数的转换依据。睡眠、HRV、疲劳、活动历史与建议理由不会上传。哈希只能检测内容变化，不能证明作者或用户身份。

以下命令**只允许真人在 agent 无法访问的独立用户环境运行**（当前 writer 支持 Linux/macOS POSIX 锁；Windows agent 工具不受影响，writer 暂不支持原生 Windows）：

```sh
python -m indoor_coach.writer preview.json
# 或安装后：indoor-coach-writer preview.json
# 遇到不确定结果，仅回读协调，允许过期导出：
python -m indoor_coach.writer preview.json --reconcile-only
```

CLI 用隐藏输入获取个人 API key（Basic 用户名 `API_KEY`），不会读取 `INTERVALS_API_KEY`，不会写凭据到日志。先 GET `/athlete/0` 校验具体账户与时区，再读取每个日期全部日历项目。展示完整 JSON（含每段秒数/瓦数、总秒数、FTP依据和哈希），要求用户输入带哈希的一次性完整挑战，5分钟失效；POST 前再次校验有效期、账户和日历。没有 `--yes`、`confirmed` 参数、可持久复用的批准文件或数据库 approved 标记。OAuth 接入尚未实现；官方写权限是 `CALENDAR:WRITE`，不是只读权限。

**信任边界：TTY 只是交互要求，不是身份认证。拥有相同 OS shell、文件权限、终端或进程控制能力的 AI 能伪造输入、修改代码/日志或调用 Python 内部函数。不能在这种环境提供 writer 密钥或让 agent 代输确认。** 需要用户独立设备/操作系统账户、隔离凭据与可信代码安装；同 OS 账号下的“另开一个终端”不构成安全隔离。代码不能自行检测 AI 是否在操纵 shell。无法建立这个边界时只导出，禁止执行。读取分析用的个人 ICU key 本身也可能有写能力，不能把真实可写 key 交给不可信 agent；优先隔离采集进程或使用上游只读授权。本项目工具白名单不能约束宿主 shell 的任意联网能力。

写入只用 `POST /api/v1/athlete/0/events/bulk?upsert=false&upsertOnUid=false&updatePlanApplied=false`，每请求一课，category=WORKOUT/type=Ride，本地日期 T00:00:00 和原生 `- 600s 110w` 文本，不发送私有 `icu_workout` 字段。external_id 来自应用命名空间、具体账户、不可变提案ID与课序号，不以日期作为唯一标识。任何日历项目都导致默认停止，包括备注/其他安排和同 external_id 的已有课程，不更新或删除。首次预检全周，逐课 POST 前再查；用户已编辑的课不会被覆盖。

持久日志位于独立用户的 `~/.indoor-coach-writer/executions.sqlite3`，全流程单机文件锁；不要删除日志、更换 writer home 或在另一机器并行运行。每课 POST 前提交 `submitting`；异常/5xx 为 `unknown`，401/403/429 为 `rejected`，不存在自动重试。后续运行对曾尝试课程只回读协调；没有找到不代表没有创建，unknown 禁止重发。尚有未解决课程时也停止后续新课；周计划不会整批重做。已 verified 的课不重传，重新生成相同课程导出不会重传。verified 仅代表上次核验结果；每次再次运行（包括 `--reconcile-only`）都会重新 GET 核对。远端修改为 `verification_failed`，日期列表中消失或读取失败为 `unknown`（不据此断言已删除），都不会重发。更改已尝试课程的绑定直接拒绝，需要人工调查。崩溃、缺ID或读失败保留不确定状态；`--reconcile-only` 永不 POST。

独立 GET 单课核验账户、日期、type/category、external_id、原生文本、平台 moving_time/joules，以及 `workout_doc` 的顺序、每段 duration、固定 w 功率和总 duration。只支持平坦定时恒功率结构；缺字段、未知结构、百分比、重复/渐变、舍入差异均 `verification_failed`。这不验证 Garmin 等下游设备接收，也不修改用户 FTP。200 或返回ID不代表验证成功。readback失败不会回滚、修改或重传；请在 ICU 手工检查。

不承诺 exactly-once：无上游事务性唯一约束或跨机锁，预检与POST之间其他客户端仍可能创建冲突。我们避免覆盖并停止已知冲突，但无法消除此竞态。请只运行一个隔离 writer，暂停其他自动排课。日志丢失、跨机、未知结果需人工调查，不能换一个提案ID绕过不确定状态。

协议依据：[创建课程指南](https://forum.intervals.icu/t/uploading-planned-workouts-to-intervals-icu/63624)、[官方 OpenAPI](https://intervals.icu/api/v1/docs)、[课程文本语法](https://forum.intervals.icu/t/workout-builder/1163/1)、[结构化回读指南](https://forum.intervals.icu/t/downloading-planned-workouts-from-the-api/93737)。仅合成/mock 回归测试，无真实账户写入或真人联调；真实平台字段差异将保守拒绝核验，不能将 mock 通过视为生产兼容保证。
