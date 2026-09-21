#!/usr/bin/env python3
"""Submit one IPXR power-on operation; never query power status or retry it."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from http.cookiejar import LoadError, MozillaCookieJar
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urljoin, urlsplit
import uuid
import warnings

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

BASE_URL = "https://www.ipxr.cn"
ACTION_URL = BASE_URL + "/service-console/action"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PowerOnResult:
    status: str
    message: str
    service_id: int
    exit_code: int
    request_no: str | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class _Stop(Exception):
    def __init__(self, status: str, message: str, exit_code: int):
        self.status, self.message, self.exit_code = status, message, exit_code


def _default_cookie_file() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root / "ipxr" / "cookies.txt"


def _same_origin(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "www.ipxr.cn"


def _get(session: requests.Session, url: str, timeout: float) -> requests.Response:
    for _ in range(6):
        if not _same_origin(url):
            raise _Stop("failed", "网站发生跨域跳转；未提交开机请求。", 1)
        response = session.get(url, timeout=(10, timeout), allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        if not location:
            break
        url = urljoin(url, location)
    raise _Stop("failed", "网站跳转异常；未提交开机请求。", 1)


def _is_login(response: requests.Response) -> bool:
    if urlsplit(response.url).path.rstrip("/") == "/login":
        return True
    soup = BeautifulSoup(response.content, "html.parser")
    return bool(soup.select_one('#email-login-form, form[action*="/login"] input[type="password"]'))


def _authenticated(response: requests.Response, service_id: int) -> bool:
    if response.status_code != 200 or _is_login(response):
        return False
    boot = BeautifulSoup(response.content, "html.parser").select_one("#serviceConsoleBoot")
    return bool(boot and boot.get("data-host-id") == str(service_id)
                and boot.get("data-endpoint") == "/service-console")


def _auth_failure(response: requests.Response) -> _Stop:
    # Inspect locally; never print server HTML or reflected credentials.
    text = BeautifulSoup(response.content, "html.parser").get_text(" ", strip=True)
    text += " " + unquote(response.headers.get("Location", ""))
    if any(word in text for word in ("密码错误", "密码不正确", "账户不存在", "账号不存在")):
        return _Stop("failed", "网站拒绝登录，请检查用户名和密码；未提交开机请求。", 1)
    return _Stop("auth_required", "登录未完成或需要验证码，请人工登录并更新 Netscape Cookie 文件。", 2)


def _authenticate(session: requests.Session, service_id: int, username: str | None,
                  password: str | None, timeout: float) -> None:
    detail_url = BASE_URL + f"/servicedetail?id={service_id}"
    if any(not cookie.is_expired() for cookie in session.cookies):
        response = _get(session, detail_url, timeout)
        if _authenticated(response, service_id):
            return
        if response.status_code >= 500 or response.status_code == 429:
            raise _Stop("failed", "验证会话时网站不可用或限流；未提交开机请求。", 1)
        if response.status_code == 200 and not _is_login(response):
            raise _Stop("failed", "无法识别目标服务页面，请检查服务权限或网站接口变更。", 1)
    if not username or not password:
        raise _Stop("auth_required", "缺少有效会话，请设置 IPXR_USERNAME、IPXR_PASSWORD，或导入 Cookie。", 2)
    session.cookies.clear()
    page = _get(session, BASE_URL + "/login", timeout)
    if page.status_code != 200:
        raise _Stop("failed", "无法加载登录页；未提交开机请求。", 1)
    form = BeautifulSoup(page.content, "html.parser").select_one("#email-login-form")
    token = form.select_one('input[name="token"]') if form else None
    if not token or not token.get("value"):
        raise _Stop("auth_required", "未找到登录 token，可能需要人工验证；请导入有效 Cookie。", 2)
    response = session.post(
        BASE_URL + "/login?action=email",
        data={"token": token["value"], "email": username, "password": password},
        headers={"Origin": BASE_URL, "Referer": BASE_URL + "/login"},
        timeout=(10, timeout), allow_redirects=False,
    )
    if response.status_code >= 500 or response.status_code == 429:
        raise _Stop("failed", "登录接口不可用或限流；未提交开机请求。", 1)
    redirect = response.headers.get("Location")
    if redirect and not _same_origin(urljoin(BASE_URL, redirect)):
        raise _Stop("auth_required", "登录要求外部验证；请人工登录并导入 Cookie。", 2)
    if response.status_code >= 400:
        raise _auth_failure(response)
    detail = _get(session, detail_url, timeout)
    if _authenticated(detail, service_id):
        return
    if detail.status_code == 200 and not _is_login(detail):
        raise _Stop("failed", "登录后无法识别目标服务页面，请检查服务权限或网站接口变更。", 1)
    failure = _auth_failure(response)
    if failure.exit_code == 1:
        raise failure
    raise _auth_failure(detail)


def _load_cookies(path: Path) -> MozillaCookieJar:
    jar = MozillaCookieJar(str(path))
    if path.is_symlink():
        raise _Stop("failed", "Cookie 路径不能是符号链接；未提交开机请求。", 1)
    if path.exists():
        if os.name == "posix":
            path.chmod(0o600)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # Malformed files must not echo Cookie values.
                jar.load(ignore_discard=True, ignore_expires=True)
        except (LoadError, ValueError, UnicodeError):
            raise _Stop("failed", "Cookie 文件无效，需要 Netscape 格式；未提交开机请求。", 1) from None
        for cookie in list(jar):
            if cookie.expires == 0:  # Browser Netscape exports use 0 for session cookies.
                cookie.expires, cookie.discard = None, True
            if cookie.is_expired() or cookie.domain.lstrip(".") not in ("ipxr.cn", "www.ipxr.cn"):
                jar.clear(cookie.domain, cookie.path, cookie.name)
    return jar


def _save_cookies(jar: MozillaCookieJar, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".ipxr-cookie-", dir=path.parent)
    os.close(fd)
    try:
        os.chmod(temporary, 0o600)
        jar.save(temporary, ignore_discard=True, ignore_expires=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}", value):
        return int(value)
    return None


def _interpret(response: requests.Response, service_id: int, key: str) -> PowerOnResult:
    request_no = None

    def result(status: str, message: str, code: int) -> PowerOnResult:
        return PowerOnResult(status, message, service_id, code, request_no, key)

    # Never follow an action redirect: 307/308 could replay the POST.
    if 300 <= response.status_code < 400:
        return result("unknown", "开机请求返回跳转，未跟随或重发；提交结果未知。", 3)
    try:
        payload = response.json()
    except ValueError:
        if response.status_code in (401, 403) or _is_login(response):
            return result("auth_required", "开机接口要求重新认证或人工验证；未重发请求。", 2)
        return result("unknown", "开机接口未返回有效 JSON；提交结果未知，未重发。", 3)
    if not isinstance(payload, dict):
        return result("unknown", "开机接口响应格式异常；提交结果未知，未重发。", 3)
    data = payload.get("data")
    task = data if isinstance(data, dict) else {}
    raw_no = task.get("request_no") or task.get("requestNo")
    if isinstance(raw_no, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", raw_no):
        request_no = raw_no
    status = _number(payload.get("status"))
    api_code = _number(payload.get("code"))
    error_code = str(payload.get("error_code") or payload.get("code") or "").upper()
    if (response.status_code == 401 or status == 401 or api_code == 401
            or error_code in {"SECOND_VERIFY_REQUIRED", "CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "UNAUTHENTICATED"}):
        return result("auth_required", "接口要求登录或二次验证，请人工处理后再调用；未自动重发。", 2)
    if response.status_code >= 500:
        return result("unknown", "开机接口或网关异常，操作可能已提交；未重发。", 3)
    task_status = str(task.get("status") or task.get("state") or "").lower()
    if task_status in {"unknown", "manual_review", "timeout"}:
        return result("unknown", "网站正在核对操作结果或需要人工核对；未轮询或重发。", 3)
    messages = [payload.get("msg"), payload.get("message"), task.get("user_message"), task.get("message")]
    already_messages = {"服务器已开机", "主机已开机", "实例已开机", "已处于开机状态", "already powered on", "instance is already running"}
    already_on = (error_code in {"ALREADY_ON", "ALREADY_POWERED_ON", "INSTANCE_ALREADY_RUNNING"}
                  or any(isinstance(m, str) and m.strip().rstrip("。.!！").lower() in already_messages for m in messages))
    if already_on:
        return result("already_on", "接口明确返回目标已开机；未查询电源状态。", 0)
    if task_status in {"failed", "failure", "error", "cancelled", "canceled"}:
        return result("failed", "网站明确返回开机任务失败或取消；未重发请求。", 1)
    if (response.status_code >= 400 or (status is not None and status not in (200, 202, 1000))
            or (api_code is not None and api_code >= 400)):
        return result("failed", "网站明确拒绝开机请求；未重发请求。", 1)
    if not 200 <= response.status_code < 300:
        return result("unknown", "接口响应无法确认开机请求是否受理；未重发。", 3)
    accepted_states = {"created", "submitting", "accepted", "queued", "pending", "running", "processing", "applying", "success", "completed", "complete", "done", "succeeded"}
    if task_status in accepted_states or (not task_status and status in (200, 202, 1000)):
        return result("accepted", "接口已接受开机请求；未检查实际电源状态。", 0)
    return result("unknown", "接口响应无法确认开机请求是否受理；未重发。", 3)


def power_status(*, service_id: int = 10328, username: str | None = None,
                 password: str | None = None, cookie_file: str | Path | None = None,
                 timeout: float = 45.0) -> PowerOnResult:
    """Query the provider's power state; never submit any action.

    Uses the same endpoint the web console polls (POST /provision/default,
    func=status). Returns PowerOnResult where `status` is the provider's
    state: "on" (running), "off" (powered off), or "unknown"/"failed" on
    errors; exit_code 0 when the state was read successfully.
    """
    if isinstance(service_id, bool) or not isinstance(service_id, int) or service_id <= 0:
        return PowerOnResult("failed", "service_id 必须是正整数；未提交请求。", 0, 1)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return PowerOnResult("failed", "timeout 必须是有限正数。", service_id, 1)
    username = username if username is not None else os.environ.get("IPXR_USERNAME")
    password = password if password is not None else os.environ.get("IPXR_PASSWORD")
    path = Path(cookie_file or os.environ.get("IPXR_COOKIE_FILE") or _default_cookie_file()).expanduser()
    try:
        jar = _load_cookies(path)
        with requests.Session() as session:
            session.cookies = jar
            session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; IPXR-PowerOn/1.0)", "Accept": "text/html,application/json"})
            session.mount("https://", HTTPAdapter(max_retries=0))
            _authenticate(session, service_id, username, password, timeout)
            _save_cookies(jar, path)
            response = session.post(
                BASE_URL + "/provision/default",
                data={"id": str(service_id), "func": "status"},
                headers={"X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL,
                         "Referer": BASE_URL + f"/servicedetail?id={service_id}",
                         "Accept": "application/json, text/javascript, */*; q=0.01"},
                timeout=(10, timeout), allow_redirects=False,
            )
            try:
                _save_cookies(jar, path)
            except OSError:
                LOG.warning("查询已完成，但 Cookie 更新未能保存。")
            if response.status_code in (401, 403) or _is_login(response):
                return PowerOnResult("auth_required", "状态接口要求重新认证；请人工处理。", service_id, 2)
            if response.status_code != 200:
                return PowerOnResult("unknown", f"状态接口返回 HTTP {response.status_code}；状态未知。", service_id, 3)
            try:
                payload = response.json()
            except ValueError:
                return PowerOnResult("unknown", "状态接口未返回有效 JSON；状态未知。", service_id, 3)
            if not isinstance(payload, dict) or _number(payload.get("status")) != 200:
                msg = payload.get("msg") if isinstance(payload, dict) else None
                return PowerOnResult("unknown", f"状态接口返回异常：{msg or '响应格式不符'}；状态未知。", service_id, 3)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            state = str(data.get("status") or "").strip().lower()
            des = str(data.get("des") or "").strip()
            if state == "on":
                return PowerOnResult("on", f"电源状态：运行中（{des or 'on'}）。", service_id, 0)
            if state == "off":
                return PowerOnResult("off", f"电源状态：已关机（{des or 'off'}）。", service_id, 0)
            if state in {"process", "pending", "waiting", "wait", "wait_reboot", "checking"}:
                return PowerOnResult("process", f"电源状态：任务处理中（{des or state}）；请稍后再操作。", service_id, 0)
            return PowerOnResult(state or "unknown", f"电源状态无法归类（status={state or '空'}, des={des}）。", service_id, 3)
    except _Stop as exc:
        return PowerOnResult(exc.status, exc.message, service_id, exc.exit_code)
    except (requests.RequestException, OSError, KeyboardInterrupt):
        return PowerOnResult("unknown", "查询状态时网络异常或超时；状态未知。", service_id, 3)


def power_on(*, service_id: int = 10328, username: str | None = None,
             password: str | None = None, cookie_file: str | Path | None = None,
             timeout: float = 45.0) -> PowerOnResult:
    """Authenticate and submit at most ONE startup POST; return structured outcome.

    Credentials default to IPXR_USERNAME/IPXR_PASSWORD. Cookies default to
    IPXR_COOKIE_FILE or $XDG_STATE_HOME/ipxr/cookies.txt. No status/task API is used.
    Each invocation gets a fresh idempotency key; do not blindly retry an unknown
    result. The caller owns actual power-state detection.
    """
    if isinstance(service_id, bool) or not isinstance(service_id, int) or service_id <= 0:
        return PowerOnResult("failed", "service_id 必须是正整数；未提交请求。", 0, 1)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return PowerOnResult("failed", "timeout 必须是有限正数；未提交请求。", service_id, 1)
    username = username if username is not None else os.environ.get("IPXR_USERNAME")
    password = password if password is not None else os.environ.get("IPXR_PASSWORD")
    path = Path(cookie_file or os.environ.get("IPXR_COOKIE_FILE") or _default_cookie_file()).expanduser()
    key = str(uuid.uuid4())
    submitted = False
    try:
        jar = _load_cookies(path)
        with requests.Session() as session:
            session.cookies = jar
            session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; IPXR-PowerOn/1.0)", "Accept": "text/html,application/json"})
            session.mount("https://", HTTPAdapter(max_retries=0))
            _authenticate(session, service_id, username, password, timeout)
            _save_cookies(jar, path)  # Storage failure must precede submission.
            LOG.info("认证完成，提交一次开机请求。")
            submitted = True
            response = session.post(
                ACTION_URL,
                json={"id": service_id, "host_id": service_id, "action": "on",
                      "params": {}, "idempotency_key": key},
                headers={"Origin": BASE_URL, "Referer": BASE_URL + f"/servicedetail?id={service_id}", "Accept": "application/json"},
                timeout=(10, timeout), allow_redirects=False,
            )
            result = _interpret(response, service_id, key)
            try:
                _save_cookies(jar, path)
            except OSError:
                LOG.warning("请求已完成，但 Cookie 更新未能保存；请勿因此重复提交。")
            return result
    except _Stop as exc:
        return PowerOnResult(exc.status, exc.message, service_id, exc.exit_code)
    except (requests.RequestException, OSError, KeyboardInterrupt):
        if submitted:
            return PowerOnResult("unknown", "提交期间网络异常、超时或中断；结果未知，未自动重发。", service_id, 3, idempotency_key=key)
        return PowerOnResult("failed", "认证、网络或本地会话文件处理失败；未提交开机请求。", service_id, 1)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Do not echo arguments; they might accidentally contain credentials.
        print(json.dumps(asdict(PowerOnResult("failed", "命令行参数无效，请使用 --help。", 0, 1)), ensure_ascii=False))
        self.exit(1)


def main() -> int:
    parser = _Parser(description="登录 IPXR 查询电源状态或提交一次开机请求；不重试。")
    parser.add_argument("--service-id", type=int, default=10328, help="目标服务 ID，默认 10328")
    parser.add_argument("--cookie-file", type=Path, help="Netscape Cookie 文件（不存在时创建）")
    parser.add_argument("--timeout", type=float, default=45.0, help="HTTP 读取超时秒数，默认 45")
    parser.add_argument("--check-status", action="store_true", help="只查询电源状态，不提交开机")
    parser.add_argument("--verbose", action="store_true", help="向 stderr 输出简短诊断信息")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s")
    if args.check_status:
        result = power_status(service_id=args.service_id, cookie_file=args.cookie_file, timeout=args.timeout)
    else:
        result = power_on(service_id=args.service_id, cookie_file=args.cookie_file, timeout=args.timeout)
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
