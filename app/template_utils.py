"""모든 라우터가 공유하는 Jinja2Templates 인스턴스

권한별 금액 표시 정책:
- admin / view_amounts=True : 정상 표시 (예: 1,234,000)
- staff (view_amounts=False) : "—" 로 마스킹

`money` 필터는 컨텍스트 인식(@pass_context) — 템플릿에서 단순히
{{ price | money }} 만 호출해도 현재 요청자의 권한에 따라 자동 마스킹됩니다.
"""
from pathlib import Path
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _format_int(v):
    """숫자를 천단위 콤마 문자열로 변환 (실패 시 '0', 권한차단 → '—')"""
    if v is None or isinstance(v, Hidden if 'Hidden' in globals() else ()):
        return "—"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "0"


def _ctx_can_view(ctx) -> bool:
    """Jinja 컨텍스트에서 can_view_amounts 추출.
    - request.state.can_view_amounts 우선
    - 없으면 안전하게 True (서버사이드 일반 컨텍스트, 관리자 페이지 등)
    """
    try:
        req = ctx.get("request")
        if req is None:
            return True
        state = getattr(req, "state", None)
        if state is None:
            return True
        return bool(getattr(state, "can_view_amounts", True))
    except Exception:
        return True


@pass_context
def money(ctx, v, mask="—"):
    """권한 자동 마스킹 금액 필터.

    사용: {{ price | money }}            → "1,234,000" 또는 "—"
          {{ price | money('***') }}     → 마스크 문자 변경
    """
    if not _ctx_can_view(ctx):
        return mask
    return _format_int(v)


def money_raw(v):
    """권한 무시 — 인쇄용/관리자 전용 화면에서만 사용.
    필요 시 {{ price | money_raw }} 형태로 호출.
    """
    return _format_int(v)


@pass_context
def money_masked(ctx, v, can_view=None, mask="—"):
    """레거시 호환 필터. 기존 템플릿이
    {{ price | money_masked(request.state.can_view_amounts) }}
    형태로 부르는 것을 그대로 지원.
    can_view 인자가 명시되면 그 값을, 없으면 컨텍스트 자동 판정.
    """
    if can_view is None:
        can_view = _ctx_can_view(ctx)
    if not can_view:
        return mask
    return _format_int(v)


def format_money(v):
    """레거시: 파이썬 측에서 호출하는 코드용 (필터 아님)."""
    return _format_int(v)


templates.env.filters["money"] = money
templates.env.filters["money_raw"] = money_raw
templates.env.filters["money_masked"] = money_masked


# ── 영역별 금액 필터 (권한 리뉴얼) ──
def _flag(ctx, name, default=True):
    try:
        req = ctx.get("request")
        return bool(getattr(req.state, name, default)) if req is not None else default
    except Exception:
        return default


@pass_context
def money_sub(ctx, v, mask="—"):
    """외주 지급 금액 — view_sub_pay 권한"""
    return _format_int(v) if _flag(ctx, "can_view_sub") else mask


@pass_context
def money_wage(ctx, v, mask="—"):
    """알바 급여 금액 — view_wage_pay 권한"""
    return _format_int(v) if _flag(ctx, "can_view_wage") else mask


@pass_context
def money_profit(ctx, v, mask="—"):
    """순이익 — 매출·외주·급여 모두 볼 수 있을 때만"""
    return _format_int(v) if _flag(ctx, "can_view_profit") else mask


templates.env.filters["money_sub"] = money_sub
templates.env.filters["money_wage"] = money_wage
templates.env.filters["money_profit"] = money_profit


# ════════════════════════════════════════════════════════════════════
# ★ 금액 데이터 서버 차단 (권한 없는 금액은 화면에 도달하기 전에 제거)
#   템플릿이 어떤 방식으로 금액을 찍든(필터 없이 {{ q.total }} 등),
#   값 자체가 None 으로 지워져 있으므로 어떤 경로로도 노출되지 않는다.
# ════════════════════════════════════════════════════════════════════
REVENUE_KEYS = {
    "revenue", "supply_amount", "vat_amount", "subtotal", "discount", "after_discount",
    "vat", "total", "unit_price", "cost_price", "price", "consumer_price", "rental_daily",
    "purchase_price", "rental_price", "sale_price", "amount_input", "profit", "net_profit",
    "total_supply", "total_vat", "total_revenue", "filtered_supply", "filtered_revenue",
    "filtered_profit", "won_amount", "unpaid_amount", "margin", "margin_pct",
    "line_cost", "line_margin", "line_pct", "total_cost", "total_sell", "margin_info",
}
SUB_KEYS = {"total_expense", "sub_unpaid_amount", "sub_paid_amount", "unpaid_total", "paid_total",
            "grand_total", "grand_unpaid", "total_amount", "unpaid_amount_sub", "overdue_total", "expense"}
WAGE_KEYS = {"daily_wage", "default_daily_wage", "total_wage", "bonus_amount", "wage",
             "wage_unpaid_amount", "monthly_salary"}

# 화면별로 'amount' 같은 공통 이름이 무엇을 뜻하는지 지정
_TEMPLATE_KIND = {
    "expenses": "sub", "expenses_unpaid": "sub", "expenses_by_vendor": "sub",
    "expenses_by_vendor_detail": "sub", "expense_edit": "sub", "expense_new": "sub",
    "expense_new_simple": "sub", "mobile/expense_new": "sub", "mobile/expense_edit": "sub",
    "attendance": "wage", "attendance_new": "wage", "attendance_edit": "wage", "mobile/attendance": "wage",
}


class Hidden(int):
    """권한 없는 금액 — 계산·비교에서는 0 처럼 동작(화면 오류 방지), 출력은 '—'"""
    def __new__(cls):
        return int.__new__(cls, 0)
    def __str__(self): return "—"
    __repr__ = __str__
    def __format__(self, spec): return "—"
    def _h(self, *a): return HIDDEN
    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = _h
    __truediv__ = __rtruediv__ = __floordiv__ = __rfloordiv__ = __neg__ = __abs__ = _h
    def __bool__(self): return False


HIDDEN = Hidden()


def _scrub(obj, keys, depth=0):
    if depth > 6:
        return obj
    if isinstance(obj, dict):
        return {k: (_hide(v) if k in keys else _scrub(v, keys, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_scrub(v, keys, depth + 1) for v in obj) if isinstance(obj, list) else tuple(_scrub(v, keys, depth + 1) for v in obj)
    # SQLModel/ORM 객체: 복사본에 금액 속성을 비움 (DB 원본은 건드리지 않음)
    if hasattr(obj, "__table__") or hasattr(obj, "__fields__") or hasattr(obj, "model_fields"):
        try:
            data = {}
            fields = getattr(obj, "model_fields", None) or getattr(obj, "__fields__", {}) or {}
            for f in fields:
                data[f] = _hide(getattr(obj, f, None)) if f in keys else getattr(obj, f, None)
            class _Masked(dict):
                __getattr__ = dict.get
            return _Masked(data)
        except Exception:
            return obj
    return obj


def _hide(v):
    if isinstance(v, list):
        return []
    if isinstance(v, dict):
        return {}
    if isinstance(v, (int, float)) or v is None:
        return HIDDEN
    return HIDDEN


def mask_context(request, name: str, context: dict) -> dict:
    st = getattr(request, "state", None)
    if st is None or getattr(st, "is_admin", False):
        return context
    kind = _TEMPLATE_KIND.get(name.replace(".html", ""), "")
    keys = set()
    if not getattr(st, "can_view_revenue", False):
        keys |= REVENUE_KEYS
    if not getattr(st, "can_view_sub", False):
        keys |= SUB_KEYS
        if kind == "sub":
            keys |= {"amount", "supply_amount", "vat_amount"}
        keys.add("expenses_amount")
    if not getattr(st, "can_view_wage", False):
        keys |= WAGE_KEYS
        if kind == "wage":
            keys |= {"amount"}
    if not getattr(st, "can_view_profit", False):
        keys |= {"profit", "net_profit", "filtered_profit"}
    if not getattr(st, "can_view_settle", False):
        keys |= {"unpaid_amount", "unpaid_projects"}
    if kind != "sub" and not getattr(st, "can_view_revenue", False):
        keys.add("amount")   # 견적 품목 금액 등
    if not keys:
        return context
    safe = {}
    for k, v in (context or {}).items():
        if k in ("request",):
            safe[k] = v
        elif k in keys:
            safe[k] = _hide(v)
        else:
            safe[k] = _scrub(v, keys)
    return safe


_orig_tr = templates.TemplateResponse


def _masked_template_response(request, name, context=None, *a, **kw):
    try:
        context = mask_context(request, name, context or {})
    except Exception:
        pass
    return _orig_tr(request, name, context, *a, **kw)


templates.TemplateResponse = _masked_template_response


def _safe_money(v):
    return "—" if v is None else _format_int(v)


# 필터들도 None → "—"
templates.env.filters["money_raw"] = _safe_money
