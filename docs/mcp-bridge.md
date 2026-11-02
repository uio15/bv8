# Workspace MCP Bridge 接入说明

本仓库通过 `scripts/mcp_bridge_client.py` 连接用户本机的 Windows 工作区桥（MCP over Streamable HTTP），
用于读取/修改本机文件并执行 PowerShell。

## 1. 连接信息（实测）

| 项 | 值 |
| --- | --- |
| Server | `workspace-mcp-bridge` v1.1.0 |
| Protocol | `2025-06-18`（Streamable HTTP，SSE 回包） |
| 传输 | `POST` JSON-RPC 2.0；需 `Accept: application/json, text/event-stream` |
| 必带头 | `Mcp-Session-Id`（initialize 响应头返回）、`MCP-Protocol-Version`、`ngrok-skip-browser-warning: 1` |
| 工具数 | 10 |

**端点 URL 含访问令牌，不入库。** 三种给法，优先级从高到低：

1. `--url <endpoint>`
2. 环境变量 `MCP_BRIDGE_URL`（状态文件路径用 `MCP_BRIDGE_STATE`）
3. 状态文件 `.mcp/state.json`：`{"url": "..."}`（已在 `.gitignore` 中忽略）

状态文件同时保存 `session_id` / `protocol_version` / `server_info`，因此每次调用无需重复握手。

## 2. 客户端用法

```bash
python3 scripts/mcp_bridge_client.py init                       # 握手，落盘 session
python3 scripts/mcp_bridge_client.py tools -v                   # 工具清单（-v 带参数名）
python3 scripts/mcp_bridge_client.py schema run_command          # 单个工具完整 schema
python3 scripts/mcp_bridge_client.py call list_directory --args '{"depth":2}'
python3 scripts/mcp_bridge_client.py run 'Get-Date' --session <shell_session_id>
python3 scripts/mcp_bridge_client.py out <command_id> --offset 8000
python3 scripts/mcp_bridge_client.py rpc tools/list              # 原始方法逃生口
python3 scripts/mcp_bridge_client.py reset                       # 丢弃过期 session
```

约定：

- `call` 默认自动解包 `content[0].text`（桥把结果序列化成 JSON 字符串再塞进 text），加 `--raw` 看原始信封。
- `run` / `out` 是 `run_command` / `get_command_output` 的快捷方式，省掉转义 JSON 的麻烦。
- session 过期（HTTP 404/400 且提到 session）时自动重新 initialize 并重试一次。
- 单次 HTTP 超时默认 180s，可用 `--timeout` 调整。

## 3. 可用工具

| 工具 | 必填 | 关键参数 / 用途 |
| --- | --- | --- |
| `list_directory` | – | `path`,`depth`(1\|2),`include_hidden`,`no_ignore`,`max_entries`(≤500)。只看已知目录，不递归找文件 |
| `find_files` | `patterns` | 1-20 个 glob（`**/*.py`），`path`,`exclude`,`sort`(modified_desc/path_asc),`max_results`(≤500)。搜路径不搜内容 |
| `search_files` | `pattern` | `is_regex`,`case_sensitive`(省略=smart-case),`include`/`exclude`,`context_lines`(0-5),`max_results`(≤500),`max_matches_per_file`(≤100),`max_line_characters`(80-2000,默认500),`max_response_bytes`(4096-48000) |
| `read_files` | `files` | 1-20 条 `{path,start_line,end_line}`；返回 `sha256:<hash>` 版本用于改文件 |
| `apply_patch` | `patch`,`expected_versions` | `*** Begin Patch` + Add/Update/Move to/Delete File；每个已存在文件都要带 sha256 版本；返回 `STALE_FILE` 就重读再改 |
| `run_command` | `command` | PS 5.1；`foreground_wait_seconds`(1-10,默认5)、`timeout_seconds`(1-3600,默认600)、`shell_session_id`、`request_id`、`max_output_characters`(2-32000,默认8000,上限 131072 字符命令体) |
| `get_command_output` | `command_id` | `offset` 传回上次 `next_offset`；`has_more=true` 继续读，已完成也可能有未读 |
| `send_command_input` | `command_id`,`text` | `submit`(默认 true) 追加回车 |
| `stop_command` | `command_id` | 停运行中命令会杀 PowerShell 及子进程并重置该 shell 的变量/目录 |
| `list_commands` | – | `request_id` 精确找回、`limit`(1-100,默认20)。响应丢失时先查它，不要重跑 |

## 4. 操作规则（V1.1 落实版）

1. **PowerShell 5.1 only**：`;` 分隔语句、`$env:VAR="x"`、`Get-ChildItem`；禁用 `&&`、`export`、`source`、`ls -la`、`rm -rf`。
2. **5 秒前台**：`run_command` 最多等 5s，未完成返回 `background=true` + `command_id`；此后只用 `get_command_output` 轮询，**不重复执行**。
3. **副作用先给键**：写文件/改配置/发请求类命令先定唯一 `request_id`（1-128 字符）；响应丢失先 `list_commands(request_id)` 找回；重试必须同键同参（实测同键返回同一 `command_id`，不会二次执行）。
4. **跨会话传 `shell_session_id`**：变量、`$env:`、`Set-Location`、导入的模块都会在该壳内保持（已实测）；不传则可能落到别的壳。
5. **大型任务落盘再跑**：命令体上限 131072 字符，复杂逻辑先写 `.ps1` 再用短命令调用；输出优先看 `output_file`。
6. **分页读输出**：`next_offset` 原样传回 `offset`，`has_more=true` 就继续。
7. **搜索是片段不是全文**：`column`/`text_start_column` 为 1-based UTF-16 列号；`truncated`/`truncation_reasons`/`*_truncated` 表示省略；结果不足就缩小 `path`/`pattern`，**不要用片段覆盖原文件**。
8. **改文件先读**：`read_files` 拿 sha256 → `apply_patch` 带上 `expected_versions`；`STALE_FILE` 就重读。
9. **保活**：任务只在桥进程内存里，空闲壳 30 分钟过期，桥重启后先查落盘 `output_file` 再决定是否重跑。
10. **不做未经授权的全量测试 / 构建 / 部署**。

## 5. 目标机环境（实测）

| 项 | 值 |
| --- | --- |
| PowerShell | 5.1.19041.6456（`powershell.exe`）|
| OS | Windows 10 19045 |
| 用户 | `fm` |
| 活动工作区 | `C:\Users\fm\Desktop\aaaaaaa`（当前为空目录）|
| 命令输出落盘 | `C:\Users\fm\AppData\Local\WorkspaceMcpBridge\command-output\command-*.log` |
| 已装 | git `E:\Git\cmd\git.exe`、python 3.13、py、node/npm/npx（nvm4w）、pwsh、VS Code `code.cmd` |

跨 MCP 连接查任务：`get_command_output` / `send_command_input` / `stop_command` 允许跨会话，凭 `command_id` 即可。
