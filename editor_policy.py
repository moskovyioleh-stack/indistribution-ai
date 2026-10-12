"""Safety policy for future Tilda browser editing.

This module deliberately performs no browser or publishing actions.
"""
from dataclasses import dataclass
from urllib.parse import urlparse

APPROVED_PROJECTS = {
    "12112685": "indistribution.ru",
    "14178377": "molot.website",
    "23291206": "powders.indistribution.ru",
    "30763609": "heinedasaw.indistribution.ru",
}
PILOT_PROJECT_ID = "14178377"
PILOT_PAGE_ID = "296498609"


class EditScopeError(ValueError):
    """An edit was requested outside the approved pilot scope."""


@dataclass(frozen=True)
class EditRequest:
    project_id: str
    page_id: str
    change_type: str
    description: str


def validate_edit_request(request: EditRequest, *, enable_other_sites: bool = False) -> dict:
    """Validate scope before any editor connection or browser interaction."""
    if request.project_id not in APPROVED_PROJECTS:
        raise EditScopeError("Unapproved Tilda project")
    if not enable_other_sites and request.project_id != PILOT_PROJECT_ID:
        raise EditScopeError("Only molot.website is enabled in the pilot")
    if not enable_other_sites and request.page_id != PILOT_PAGE_ID:
        raise EditScopeError("Only the approved MОLOT page is enabled in the pilot")
    if request.change_type not in {"text", "image", "layout", "style"}:
        raise EditScopeError("Unsupported change type")
    if not request.description.strip():
        raise EditScopeError("An explicit change description is required")
    return {
        "domain": APPROVED_PROJECTS[request.project_id],
        "project_id": request.project_id,
        "page_id": request.page_id,
        "change_type": request.change_type,
        "status": "validated_not_executed",
        "publishing_allowed": False,
    }


def validate_tilda_editor_url(url: str) -> str:
    """Prevent navigation to arbitrary hosts when session data is available."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"tilda.cc", "www.tilda.cc"}:
        raise EditScopeError("Only HTTPS Tilda editor URLs are allowed")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise EditScopeError("Unexpected URL credentials or port")
    return url
