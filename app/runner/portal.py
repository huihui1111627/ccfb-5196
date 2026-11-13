"""Playwright client for the mock customs portal.

Everything the runner needs to drive the real UI lives here.  The runner does
not touch SQLite from inside this module; it just calls these methods and
records the outcome.

Notes on robustness
-------------------
* Dynamic fields appear only after the portal answers `/api/trade-mode/<code>`.
  `fill_header` waits for that response *and* for the field nodes to exist.
* The portal's session expires server-side.  `SessionExpired` is raised when we
  detect a 401 JSON body or a redirect to /login; the runner re-logs in and
  continues from the last saved item.
* Item entry is page-by-page; each line is persisted with 暂存 before moving on,
  which is what makes crash-resume possible.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from common import catalog
from common.formatting import clean_text

LOGIN_PATH = "/login"
NEW_DECL_PATH = "/declaration/new"


class SessionExpired(Exception):
    pass


class SubmitNotSent(Exception):
    """Server-side proof that the submit request was not processed."""


class PortalError(Exception):
    pass


class MockPortal:
    def __init__(self, base_url: str, username: str, password: str,
                 headless: bool = True, slow_mo_ms: int = 0,
                 screenshot_dir: str | Path | None = None,
                 step_timeout_ms: int = 15000, field_timeout_ms: int = 15000):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.step_timeout_ms = step_timeout_ms
        self.field_timeout_ms = field_timeout_ms
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.shot_counter = 0
        self.relogin_count = 0
        self.current_run_dir: Path | None = None
        self.last_response_declaration_no = ""

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless,
                                                 slow_mo=self.slow_mo_ms)
        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
        )
        self.context.set_default_timeout(self.step_timeout_ms)
        self.page = self.context.new_page()

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
        finally:
            if self._pw:
                self._pw.stop()
                self._pw = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- screenshots ------------------------------------------------------
    def set_run_dir(self, path: str | Path) -> None:
        self.current_run_dir = Path(path)
        self.current_run_dir.mkdir(parents=True, exist_ok=True)

    def shot(self, label: str) -> str:
        """Save a screenshot and return its path relative to the screenshots root."""
        if not self.current_run_dir or self.page is None:
            return ""
        self.shot_counter += 1
        safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", label)[:60]
        name = f"{self.shot_counter:04d}_{safe}.png"
        target = self.current_run_dir / name
        try:
            self.page.screenshot(path=str(target), full_page=True, timeout=8000)
        except Exception:  # noqa: BLE001
            try:
                self.page.screenshot(path=str(target), timeout=8000)
            except Exception:  # noqa: BLE001
                return ""
        from app.config import SCREENSHOT_DIR
        try:
            return str(target.relative_to(Path(SCREENSHOT_DIR).resolve()))
        except ValueError:
            return str(target)

    def url(self) -> str:
        return self.page.url if self.page else ""

    # -- auth -------------------------------------------------------------
    def login(self) -> None:
        assert self.page is not None
        self.page.goto(f"{self.base_url}{LOGIN_PATH}", wait_until="domcontentloaded")
        self.page.fill("#username", self.username)
        self.page.fill("#password", self.password)
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.click("#login-submit")
        if "#login-error" in self.page.content() or "/login" in self.page.url:
            raise PortalError("登录失败：用户名或密码错误")
        if "/login" in self.page.url:
            raise PortalError("登录后仍停留在登录页")

    def _is_login_page(self) -> bool:
        return "/login" in self.url()

    def ensure_session(self) -> None:
        """Re-login if the portal says the session is gone."""
        if self._is_login_page():
            self.relogin_count += 1
            self.login()
            return
        # Cheap probe: the declarations page redirects to /login on expiry.
        self.page.goto(f"{self.base_url}/declarations", wait_until="domcontentloaded")
        if self._is_login_page():
            self.relogin_count += 1
            self.login()

    def _check_api_response(self, resp) -> dict:
        if resp.status == 401:
            body = _safe_json(resp)
            raise SessionExpired(body.get("error", "会话已过期"))
        if resp.status >= 400:
            body = _safe_json(resp)
            raise PortalError(body.get("error", f"HTTP {resp.status}"))
        return _safe_json(resp)

    # -- declaration entry ------------------------------------------------
    def open_new_declaration(self, ref: str) -> None:
        assert self.page is not None
        self.page.goto(f"{self.base_url}{NEW_DECL_PATH}?ref={ref}",
                       wait_until="domcontentloaded")
        if self._is_login_page():
            raise SessionExpired("打开申报页时会话过期")
        self.page.wait_for_selector("#header-form", timeout=self.step_timeout_ms)

    def load_draft_or_blank(self, ref: str) -> dict | None:
        """Return the portal-side draft (used to resume after a crash)."""
        assert self.page is not None
        resp = self.page.request.get(f"{self.base_url}/api/drafts/{ref}")
        if resp.status == 401:
            raise SessionExpired("查询草稿时会话过期")
        return resp.json()

    def select_trade_mode(self, code: str) -> dict:
        """Select the mode and wait for the asynchronously rendered fields."""
        assert self.page is not None
        panel = self.page.locator("#mode-panel")
        with self.page.expect_response(
            lambda r: f"/api/trade-mode/{code}" in r.url,
            timeout=self.field_timeout_ms,
        ) as resp_info:
            self.page.select_option("#trade_mode", code)
        resp = resp_info.value
        if resp.status == 401:
            raise SessionExpired("加载联动字段时会话过期")
        payload = resp.json()
        if not payload.get("ok"):
            raise PortalError(f"加载贸易方式联动字段失败：{payload.get('error')}")
        # Wait for the fields to actually exist in the DOM, not just the network.
        self.page.wait_for_function(
            """(expected) => {
                const panel = document.querySelector('#mode-panel');
                if (!panel) return false;
                const fields = panel.querySelectorAll('.dynamic-field input');
                return panel.classList.contains('loaded') && fields.length === expected;
            }""",
            arg=len(payload["fields"]),
            timeout=self.field_timeout_ms,
        )
        return payload

    def fill_header(self, header: dict, trade_mode: str) -> None:
        assert self.page is not None
        self.select_trade_mode(trade_mode)
        self.page.select_option("#declaration_type", header.get("declaration_type") or "出口")
        if header.get("port"):
            self.page.select_option("#port", header["port"])
        for key in ("consignor", "consignee", "remark", "destination_country", "currency"):
            value = clean_text(header.get(key, ""))
            if not value:
                continue
            if key in ("destination_country", "currency"):
                try:
                    self.page.select_option(f"#{key}", value)
                except PWTimeout:
                    pass
            else:
                self.page.fill(f"#{key}", value)
        for field in catalog.required_fields(trade_mode):
            value = clean_text(header.get(field, ""))
            selector = f"#dynamic-fields [data-field='{field}']"
            self.page.wait_for_selector(selector, timeout=self.field_timeout_ms)
            self.page.fill(selector, value)

    def save_header(self) -> dict:
        assert self.page is not None
        with self.page.expect_response(
            lambda r: "/api/drafts" in r.url and r.request.method == "POST",
            timeout=self.step_timeout_ms,
        ) as resp_info:
            self.page.click("#save-header")
        return self._check_api_response(resp_info.value)

    def lookup_hs(self, hs_code: str) -> dict:
        assert self.page is not None
        with self.page.expect_response(
            lambda r: f"/api/hs/{hs_code}" in r.url, timeout=self.field_timeout_ms,
        ) as resp_info:
            self.page.click("#hs-lookup")
        resp = resp_info.value
        if resp.status == 401:
            raise SessionExpired("查询税则时会话过期")
        payload = resp.json()
        if not payload.get("ok"):
            raise PortalError(f"HS编码{hs_code}查询失败：{payload.get('error')}")
        self.page.wait_for_function(
            """(expected) => {
                const fields = document.querySelectorAll('#element-fields .element-field');
                return fields.length === expected;
            }""",
            arg=len(payload["elements"]),
            timeout=self.field_timeout_ms,
        )
        return payload

    def debug_expire_sessions(self) -> None:
        """Mock-only: force the server session to expire (test hook)."""
        if not self._debug_enabled:
            return
        try:
            self.page.request.post(
                f"{self.base_url}/api/debug/expire-sessions", data="{}")
        except Exception:  # noqa: BLE001
            pass

    _debug_enabled = True

    def fill_item(self, item: dict, item_page: int) -> dict:
        """Open item page N and fill it (without saving)."""
        assert self.page is not None
        # 项号 field is read-only; jump to the page instead (mirrors the portal UI).
        self.page.fill("#item-jump", str(item_page))
        self.page.click("#item-jump-go")
        self.page.wait_for_function(
            "(n) => document.querySelector('#item-no').value === String(n)",
            arg=item_page, timeout=self.step_timeout_ms,
        )
        hs = item["hs_code"]
        # Set the code without triggering the portal's change-handler auto-lookup,
        # then use the explicit lookup button exactly once.
        self.page.evaluate(
            """(code) => {
                const el = document.querySelector('#hs_code');
                el.value = code;
            }""", hs)
        info = self.lookup_hs(hs)
        self.page.fill("#goods_name", item.get("goods_name") or info.get("name", ""))
        self.page.fill("#spec_model", item.get("spec_model", ""))
        self.page.fill("#quantity", str(item.get("quantity", "")))
        self.page.fill("#unit", item.get("unit", ""))
        self.page.fill("#unit_price", str(item.get("unit_price", "")))
        self.page.fill("#total_price", str(item.get("total_price", "")))
        if item.get("currency"):
            self.page.select_option("#item_currency", item["currency"])
        if item.get("origin_country"):
            self.page.select_option("#origin_country", item["origin_country"])
        if item.get("destination_country"):
            self.page.select_option("#destination_country_item", item["destination_country"])
        elements = item.get("elements") or {}
        missing = []
        for el in info["elements"]:
            name = el["name"]
            value = clean_text(elements.get(name, ""))
            if not value:
                missing.append(name)
                continue
            # element inputs are generated in order; select by data-element
            self.page.wait_for_selector(
                f"#element-fields [data-element='{name}']",
                timeout=self.field_timeout_ms,
            )
            loc = self.page.locator(f"#element-fields [data-element='{name}']")
            if el.get("vocab"):
                try:
                    loc.select_option(value)
                except PWTimeout:
                    # portal rejects unknown option: leave blank, validation will flag
                    missing.append(f"{name}(值不合法:{value})")
            else:
                loc.fill(value)
        return {"hs": info, "missing_elements": missing}

    def save_item(self, item_no: int) -> dict:
        assert self.page is not None
        with self.page.expect_response(
            lambda r: "/items" in r.url and r.request.method == "POST",
            timeout=self.step_timeout_ms,
        ) as resp_info:
            self.page.click("#item-save")
        data = self._check_api_response(resp_info.value)
        if not data.get("ok"):
            raise PortalError(data.get("error") or "暂存明细失败")
        return data

    def item_status(self) -> str:
        assert self.page is not None
        return _text_or_empty(self.page, "#item-status")

    def item_page_indicator(self) -> int:
        assert self.page is not None
        text = self.page.inner_text("#item-page-indicator")
        m = re.search(r"(\d+)", text)
        return int(m.group(1)) if m else 0

    def submit(self) -> dict:
        """Press 提交申报, confirm, and parse the result page.

        Raises `SubmitNotSent` when we are certain the POST never left the
        browser (e.g. session expired before the click handler ran).  Any other
        exception means "unknown" and the caller must not retry.
        """
        assert self.page is not None
        self.page.click("#submit-final")
        self.page.wait_for_selector("#confirm-modal:not([hidden])",
                                    timeout=self.step_timeout_ms)
        # The confirm button first awaits a 暂存 API call, then submits a form.
        # Watch for the POST itself (the point of no return), then the page load.
        try:
            with self.page.expect_response(
                lambda r: "/declaration/submit" in r.url and r.request.method == "POST",
                timeout=self.step_timeout_ms,
            ) as resp_info:
                self.page.click("#confirm-submit-yes")
                self.page.wait_for_selector("#result-card",
                                            state="attached",
                                            timeout=self.step_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"提交点击后未观察到提交请求：{exc}") from exc
        try:
            self.page.wait_for_load_state("domcontentloaded",
                                          timeout=self.step_timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.wait_for_selector("#result-card", state="visible",
                                        timeout=self.step_timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        if self._is_login_page():
            # The POST was never accepted (server redirected us to login).
            raise SubmitNotSent("提交时会话过期，提交未发出")
        self.last_response_declaration_no = self._extract_declaration_no()
        return self._parse_result()

    def _extract_declaration_no(self) -> str:
        assert self.page is not None
        body = self.page.content()
        if 'id="result-card"' not in body:
            return ""
        return _text_or_empty(self.page, "#result-declaration-no")

    def _parse_result(self) -> dict:
        assert self.page is not None
        body = self.page.content()
        if 'id="result-card"' not in body:
            return {"kind": "unknown", "raw": self.page.inner_text("body")[:1000],
                    "url": self.url()}
        title = _text_or_empty(self.page, "#result-title")
        card_class = self.page.get_attribute("#result-card", "class") or ""
        accepted = "success" in card_class
        if accepted:
            declaration_no = _text_or_empty(self.page, "#result-declaration-no")
            return {"kind": "accepted", "declaration_no": declaration_no,
                    "title": title, "url": self.url()}
        reason = _text_or_empty(self.page, "#result-message")
        code = _text_or_empty(self.page, "#result-code")
        hint_text = ""
        try:
            hint_text = self.page.inner_text(".result-card .muted")
            # The portal renders "处理建议：xxx"; keep only the advice itself.
            hint_text = re.sub(r"^\s*处理建议[:：]\s*", "", hint_text).strip()
        except Exception:  # noqa: BLE001
            pass
        return {"kind": "rejected", "reason_code": code, "reason_text": reason,
                "hint": hint_text, "title": title, "url": self.url()}

    # -- query / evidence for the fence ------------------------------------
    def probe_external_ref(self, ref: str) -> dict:
        """Ask the portal what it knows about an external ref.

        Uses the portal's own JSON query endpoint through the logged-in browser
        context, so it reflects exactly what the server committed.
        """
        assert self.page is not None
        resp = self.page.request.get(
            f"{self.base_url}/api/query", params={"external_ref": ref})
        if resp.status == 401:
            raise SessionExpired("查询申报记录时会话过期")
        data = resp.json()
        attempts = data.get("attempts") or []
        declarations = data.get("declarations") or []
        latest = attempts[0] if attempts else None
        accepted = [d for d in declarations if d.get("external_ref") == ref]
        last_outcome = latest["outcome"] if latest else None
        # A rejection attempt after an acceptance doesn't undo the acceptance.
        return {
            "declarations": accepted,
            "attempts": attempts,
            "accepted": len(accepted) > 0,
            "declaration_no": accepted[0]["declaration_no"] if accepted else None,
            "duplicate_count": len(accepted),
            "last_outcome": last_outcome,
            "last_reason_code": latest.get("reason_code") if latest else None,
            "last_reason_message": latest.get("reason_message") if latest else None,
        }

    def read_declaration(self, declaration_no: str) -> dict:
        """Read back a saved declaration (header fields + item rows)."""
        assert self.page is not None
        resp = self.page.request.get(
            f"{self.base_url}/api/declarations/{declaration_no}")
        if resp.status == 401:
            raise SessionExpired("回读申报单时会话过期")
        if resp.status == 404:
            return {}
        return resp.json()

    def open_declaration_page(self, declaration_no: str) -> None:
        assert self.page is not None
        self.page.goto(f"{self.base_url}/query/{declaration_no}",
                       wait_until="domcontentloaded")
        if self._is_login_page():
            raise SessionExpired("回读页会话过期")


def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _text_or_empty(page: Page, selector: str) -> str:
    try:
        return page.inner_text(selector, timeout=3000).strip()
    except Exception:  # noqa: BLE001
        return ""


def check_window_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
