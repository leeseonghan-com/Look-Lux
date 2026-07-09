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
    """숫자를 천단위 콤마 문자열로 변환 (실패 시 '0')"""
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
