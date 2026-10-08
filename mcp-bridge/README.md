# Workspace MCP Bridge 接入说明（已连通）

## 1. 连接信息

| 项 | 值 |
| --- | --- |
| 端点 | `https://preflight-lyrics-fleshy.ngrok-free.dev/mcp/d40c349c560f231c45567ac5aad3b27777af07c8a1874cb8529c9485f93fd359` |
| 传输 | Streamable HTTP（`POST`，`Accept: application/json, text/event-stream`，响应为 SSE） |
| 服务端 | `workspace-mcp-bridge` v1.1.0，协议 `2025-06-18` |
| 会话 | 响应头 `mcp-session-id`，客户端自动保存于 `/tmp/mcp_session.txt` |
| 远程工作区 | `C:\Users\fm\Desktop\deepseek-pp-private` |
| 远程环境 | Windows PowerShell **5.1**（5.1.19041.6456）、node v22.22.0、git 2.47.0.windows.2、主机 DESKTOP-U2SQM9M、输出日志目录 `C:\Users\fm\AppData\Local\WorkspaceMcpBridge\command-output\` |

本沙箱是 Linux/bash + python3 + curl + jq；**PowerShell 规则只作用于远程 MCP 工具的参数内容**，本地命令用 bash 语法。

## 2. 客户端（本仓库）

`mcp-bridge/mcp_client.py`：极简 MCP Streamable-HTTP 客户端（自动 initialize、保存会话、解析 SSE、按 id 取结果）。

```bash
python3 /workspace/mcp-bridge/mcp_client.py tools          # 列出工具与 schema
python3 /workspace/mcp-bridge/mcp_client.py init           # 强制新建会话
python3 /workspace/mcp-bridge/mcp_client.py call <tool> '<json args>'
python3 /workspace/mcp-bridge/mcp_client.py raw <method> '<json params>'
```

结果统一为 `{"content":[{"type":"text","text":"<内层 JSON>"}]}`，用 `jq -r '.content[0].text | fromjson'` 拆开。

## 3. 工具清单（11 个）

| 工具 | 用途 | 关键参数 |
| --- | --- | --- |
| `initialize`（协议方法） | 已由客户端自动完成 | — |
| `list_directory` | 列目录（浅层） | `path`, `depth`, `include_hidden`, `max_entries` |
| `find_files` | 按路径/文件名 glob 递归查找 | `patterns[]`, `path`, `exclude[]`, `max_results`, `sort` |
| `search_files` | 文本搜索，返回命中位置+有界片段 | `pattern`(必填), `path`, `is_regex`, `case_sensitive`, `include[]`, `context_lines`, `max_results`, `max_line_characters`(500), `max_response_bytes`(48000) |
| `read_files` | 批量读文件，返回 `sha256:<hash>` | `files:[{path, start_line?, end_line?}]` |
| `apply_patch` | 原子创建/修改/移动/删除文本文件 | `patch`, `expected_versions`（必须带 read_files 得到的 sha256） |
| `run_command` | 执行 PowerShell 5.1 | `command`, `background`, `timeout_seconds`(默认600/最大3600), `shell_session_id`, `request_id`, `foreground_wait_seconds` |
| `get_command_output` | 增量读输出，可跨 MCP 连接 | `command_id`, `offset`(=上次 `next_offset`), `max_output_characters` |
| `send_command_input` | 向运行中的命令喂 stdin | `command_id`, `text`, `submit` |
| `stop_command` | 停止任务（含子进程，重置该 shell） | `command_id` |
| `list_commands` | 列出近期任务元数据（无正文） | `request_id`, `limit` |

## 4. 使用规则要点（服务器 instructions + 实测）

**执行与等待**
- `run_command` 前台默认只等 5 秒；超时即返回 `background=true` + `command_id`（状态可能是 `queued`/`running`），改由 `get_command_output` 查询，**不要重跑**。
- 输出分页（默认 8000 字符）：`has_more=true` 就把 `next_offset` 作为 `offset` 继续读，**命令结束后仍可能有未读输出**；长输出直接读 `output_file`。
- `timeout_seconds` 对后台同样生效（默认 600，上限 3600）。

**幂等 / 找回**
- 有副作用的命令先给唯一 `request_id`；重试必须用相同键与相同参数。
- 响应丢失时先 `list_commands(request_id)` 找回，不要盲目重跑。
- 任务与重试键只存在桥进程内存：空闲会话约 30 分钟后过期，桥重启或记录过期后先看落盘 `output_file` 再决定是否重跑。

**会话保持**
- 跨 MCP 连接保持 `Set-Location`、变量、环境变量、已导入模块：把上次的 `shell_session_id` 传回。
- `stop_command` 会终止该 shell 及子进程并重置其变量/目录。
- `get_command_output` / `send_command_input` / `stop_command` 可按 `command_id` 跨连接定位。

**搜索**
- 超长行只给首个命中附近片段，`column` / `text_start_column` 是 1-based UTF-16 列号，`text_truncated`、`before_truncated` / `after_truncated` 标记截断。
- `truncated` / `truncation_reasons` 标记受行片段、数量(`max_results`)或响应预算(`max_response_bytes`)限制；结果不足时缩小 `path`/`pattern`，**片段不能替代原文件**。

**编辑安全**
- 改文件前必须 `read_files`；`apply_patch` 的 `expected_versions` 传入对应 `sha256:<hash>`。
- 返回 `STALE_FILE` 说明文件已变，重新 `read_files` 再改。
- 相对路径按远程工作区解析；本机为个人全信任版，也接受绝对 Windows 路径。

**PowerShell 5.1 语法**
- 禁止 bash 写法：`&&`、`export`、`source`、`ls -la`、`rm -rf`；用 `;` 分隔、`$env:VAR="x"`。
- 重定向默认 UTF-16LE，需要 UTF-8 时用 `Out-File -Encoding utf8`。
- 单条 `run_command` 上限 131072 字符；大逻辑先落 `.ps1` 再短命令调用。

## 5. 实测记录（2026-10-08）

| 验证项 | 结果 |
| --- | --- |
| `initialize` 握手 | 成功，SSE 返回 capabilities( logging, tools.listChanged ) |
| `tools/list` | 11 个工具，schema 已抓取 |
| `list_directory .` | 成功，返回工程根目录条目（AGENTS.md、core、packages、scripts、tests…） |
| `read_files` | 成功，返回 `version: sha256:5234061e…c71ca2f3`（opencode.json） |
| `search_files` | 成功，命中带 `line/column/match_length/before/after`；`max_results` 生效时 `truncation_reasons=["max_results"]` |
| `run_command` 前台 | 成功，`PSVersion 5.1.19041.6456`、cwd=`C:\Users\fm\Desktop\deepseek-pp-private`、exit_code=0 |
| `run_command` 后台 | `Start-Sleep -Seconds 8` → `background=true` + `command_id`；10 秒后 `get_command_output` 得 `BACKGROUND-TEST-OK`，`has_more=false` |
| `shell_session_id` 续用 | 传入上次 sid 后，`Set-Location $env:TEMP` 与 `$clineProbe=42` 在**新的 MCP 调用**中仍生效；已把 cwd 还原到工作区 |
| `list_commands` | 成功，返回近期任务（含 status/exit_code/output_file/shell_session_id） |
| `apply_patch` | 未实测（避免改动远程工作区文件），流程按 `read_files` → `expected_versions` 使用 |

## 6. 备用：不经 python 客户端的裸 curl

```bash
URL='https://preflight-lyrics-fleshy.ngrok-free.dev/mcp/d40c349c560f231c45567ac5aad3b27777af07c8a1874cb8529c9485f93fd359'
curl -sS -i -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'mcp-session-id: <sid>' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```
