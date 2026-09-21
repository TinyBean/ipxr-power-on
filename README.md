# IPXR 电源查询与单次开机脚本

支持 Python 3.10+，默认服务 ID 为 `10328`。使用 `--check-status` 只查询电源状态；不带此参数会提交一次开机请求。开机后的状态检查和后续决策由调用方负责。

## Windows 查询电源状态

本机可使用 uv 配置环境，在工程目录的 PowerShell 中执行：

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

有已导出的 Netscape Cookie 文件时，可以直接查询：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe ipxr_power_on.py --check-status --service-id 10328 --cookie-file .\cookies.txt
```

首次登录也可以在同一个 PowerShell 窗口中输入账号密码，密码输入时隐藏：

```powershell
$env:IPXR_USERNAME = Read-Host 'IPXR 登录邮箱'
$securePassword = Read-Host 'IPXR 密码' -AsSecureString
$credential = [System.Management.Automation.PSCredential]::new($env:IPXR_USERNAME, $securePassword)
$env:IPXR_PASSWORD = $credential.GetNetworkCredential().Password
$env:PYTHONIOENCODING = 'utf-8'
try {
    .\.venv\Scripts\python.exe ipxr_power_on.py --check-status --service-id 10328 --cookie-file .\cookies.txt
} finally {
    Remove-Item Env:IPXR_PASSWORD -ErrorAction SilentlyContinue
    Remove-Variable credential, securePassword -ErrorAction SilentlyContinue
}
```

成功登录后，会话保存到工程目录的 `cookies.txt`（已被 Git 忽略）。之后可重复执行只带 Cookie 的查询命令；若需要验证码，请按下文方式人工登录并导入 Cookie。

查询成功返回 `status: on`（运行中）、`off`（已关机）或 `process`（处理中），退出码为 0。`auth_required` / 退出码 2 表示缺少有效会话；`unknown` / 退出码 3 表示查询未能确认电源状态。查询路径使用 `POST /provision/default`，表单参数为 `id=10328`、`func=status`，不会调用开机接口。

Python 中可用 `from ipxr_power_on import power_status` 后调用 `power_status(service_id=10328, cookie_file="cookies.txt")`，返回字段与下文 `PowerOnResult` 一致，`request_no` 和 `idempotency_key` 为 null。

## Linux 安装与开机

在解压后的目录中执行（Debian/Ubuntu 若提示缺少 ensurepip，请先安装发行版的 python3-venv 软件包）：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

export IPXR_USERNAME='你的登录邮箱'
read -rsp 'IPXR 密码: ' IPXR_PASSWORD; echo
export IPXR_PASSWORD

.venv/bin/python ipxr_power_on.py
rc=$?
unset IPXR_PASSWORD
printf 'exit code: %s\n' "$rc"
```

密码通过环境变量传入，不需要修改代码。`.env.example` 仅作配置参考，脚本不会自动加载 `.env`。非交互运行时，由调用程序或部署环境注入环境变量。

可选参数：

```bash
.venv/bin/python ipxr_power_on.py --service-id 10328 \
  --cookie-file /path/to/ipxr-cookies.txt --timeout 45 --verbose
```

`--timeout` 是每次 HTTP 请求的读取超时，连接超时为 10 秒，不是整个程序的总时限。诊断信息写入 stderr；正常调用的 stdout 为一行 JSON。

## Python 调用

把 `ipxr_power_on.py` 放到调用程序可导入的位置：

```python
from ipxr_power_on import power_on

# 凭据默认读取环境变量，也可使用 username=、password= 传入。
result = power_on(service_id=10328, cookie_file="/path/to/ipxr-cookies.txt")
print(result.to_dict())
if result.exit_code == 3:
    # 交给你已有的状态检测程序处理；不要直接盲目重试。
    pass
```

该函数返回 `PowerOnResult`，字段如下：

| 字段 | 含义 |
| --- | --- |
| `status` | `accepted`、`already_on`、`failed`、`auth_required` 或 `unknown` |
| `message` | 不包含凭据或原始响应的中文结果说明 |
| `service_id` | 目标服务 ID |
| `exit_code` | 与 CLI 退出码一致 |
| `request_no` | 网站返回的操作编号，没有时为 null |
| `idempotency_key` | 本次开机请求的唯一标识；未提交时为 null |

| 退出码 | 含义 | 调用方处理 |
| --- | --- | --- |
| 0 | 接口已受理，或接口明确返回已开机 | 由你的程序检查实际电源状态 |
| 1 | 参数/认证准备失败，或网站明确拒绝/任务失败 | 排查后决定是否再次调用 |
| 2 | 需要有效登录会话或人工验证 | 更新凭据或 Cookie 后再调用 |
| 3 | 提交结果未知，例如超时、连接中断、网关异常或任务待核对 | 先由你的程序核实结果，避免重复提交 |

退出码 0 不表示已经确认机器启动成功。退出码 1 也不一概表示从未发送请求：网站明确返回任务失败时，请求已经发送。以 `idempotency_key` 是否为 null 区分是否进入提交阶段；有值也不保证服务器已收到请求。

## Cookie 保存与人工验证

默认会话文件为 `${XDG_STATE_HOME:-$HOME/.local/state}/ipxr/cookies.txt`。可通过 `IPXR_COOKIE_FILE` 或 `--cookie-file` 指定，命令行参数优先。不存在时自动创建；保存采用原子替换，在 Linux 上权限为 `0600`。

有有效 Cookie 时无需密码。会话失效后，若环境变量中有账号密码，脚本尝试登录一次；没有凭据或登录需要人工验证时返回退出码 2。

如遇滑块/验证码：

1. 在浏览器中人工登录 `https://www.ipxr.cn/login`，完成验证，并确认能访问服务页面。
2. 将该站点的 Cookie 导出为 **Netscape HTTP Cookie File** 格式，传到 Linux 主机。必须包含会话 Cookie；`document.cookie` 不能导出 HttpOnly Cookie，不适用于此用途。
3. 将文件权限设为 `600`，通过 `--cookie-file` 指定。脚本会验证会话并在使用后更新该文件。

文件格式示意（字段之间必须是 Tab；以下值均为占位符，不能直接登录）：

```text
# Netscape HTTP Cookie File
#HttpOnly_www.ipxr.cn	FALSE	/	TRUE	0	PHPSESSID	REPLACE_WITH_SESSION_VALUE
```

请按浏览器中的实际域名、路径、Secure 标记和过期时间导出，不要假定仅一个 `PHPSESSID` 就足够。有效 Cookie 如果绑定了 IP 或浏览器，跨机器导入仍可能失败；此时在运行脚本的网络环境中重新完成登录。Cookie 会过期，因此这种方式不能保证永久无人值守。

## 已核实的接口与行为

2026-09-20 从目标服务页面及其 `service-console/api.js` 核实：

- 登录：先获取页面动态 token，再向 `POST /login?action=email` 提交表单。
- 开机：`POST https://www.ipxr.cn/service-console/action`，JSON 为 `id=10328`、`host_id=10328`、`action="on"`、`params={}` 和唯一 `idempotency_key`。
- 请求使用登录 Cookie，并设置同源 Origin、Referer。
- 开机流程通过服务页标记验证身份和目标 ID，不调用电源状态、bootstrap、任务列表或任务轮询接口。只有显式使用 `--check-status` 或 `power_status()` 才查询电源状态。
- 开机 POST 禁止自动重试和自动跳转，不使用旧接口作为失败回退。每次函数调用/CLI 运行最多一次开机 POST。
- 每次调用生成新的幂等键，它不能防止多次独立调用产生多个操作。调用方负责避免并发调用和不明确结果后的盲目重试。
- 不创建计划任务、常驻服务或保活进程。此接口是网站前端实际使用的接口，网站改版后可能需要适配。

## 离线测试与验证记录

```bash
.venv/bin/python -m unittest -v test_ipxr_power_on.py
```

测试会拦截所有 HTTP 请求，不会访问真实网站或开机。覆盖登录、Cookie 复用和失效、验证码、错误密码、接口受理/拒绝、未知结果、禁止重试/跳转、凭据不出现在结果、CLI JSON 和退出码，以及 Linux Cookie 权限。

Windows 运行测试：`.\.venv\Scripts\python.exe -m unittest -v test_ipxr_power_on.py`。新增查询测试覆盖状态映射、查询表单、认证失败、超时、禁止跳转/重试，以及 `--check-status` 不执行开机操作。

真实验证记录不纳入版本控制，验证结果摘要如下。

已完成的验证：21 项离线测试分别在 Windows/Python 3.12 和 Linux/Python 3.14.4 通过，包括 Linux Cookie 文件权限。真实验证仅发送了一次开机 POST，接口返回 HTTP 200、业务状态 200、任务状态 success；未查询实际电源状态。

2026-09-21 在本机 Windows/Python 3.12.13 完成配置后，26 项离线测试通过。使用本地 `.env` 凭据注入本次查询进程，真实查询服务 `10328` 得到 `status=on`（运行中），HTTP 200、退出码 0，全程未提交开关机请求。该结果是查询时的状态。登录 Cookie 已保存到工程目录 `cookies.txt`，后续可直接使用上文带 `--check-status --cookie-file .\cookies.txt` 的命令查询。脚本本身仍不自动加载 `.env`。
