"""Stable task names shared by the crawler, queue and cookie router.

Task type is deliberately separate from the HTTP request kind.  For example,
``id_followup`` and ``history_detail`` both use the detail endpoint, but they
must be allowed to use different authenticated lanes.
"""

from __future__ import annotations


TASK_LIST_NEW = "list_new"
TASK_LIST_ACTIVE = "list_active"
TASK_ID_FOLLOWUP = "id_followup"
TASK_HISTORY_DETAIL = "history_detail"
TASK_HISTORY_PROBE = "history_probe"

TASK_TYPES = (
    TASK_LIST_NEW,
    TASK_LIST_ACTIVE,
    TASK_ID_FOLLOWUP,
    TASK_HISTORY_DETAIL,
    TASK_HISTORY_PROBE,
)

TASK_ALIASES = {
    "list1": TASK_LIST_NEW,
    "list2": TASK_LIST_ACTIVE,
    "new_list": TASK_LIST_NEW,
    "active_list": TASK_LIST_ACTIVE,
    "id_table": TASK_ID_FOLLOWUP,
    "id_followup": TASK_ID_FOLLOWUP,
    "detail": TASK_ID_FOLLOWUP,
    "history": TASK_HISTORY_DETAIL,
    "history_detail": TASK_HISTORY_DETAIL,
    "old_detail": TASK_HISTORY_DETAIL,
    "probe": TASK_HISTORY_PROBE,
    "history_probe": TASK_HISTORY_PROBE,
}


def normalize_task_type(value: str | None, *, default: str = TASK_ID_FOLLOWUP) -> str:
    """Normalize a persisted/CLI task name and reject unknown routes."""

    text = str(value or "").strip().lower()
    text = TASK_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in TASK_TYPES:
        raise ValueError(
            f"unsupported crawler task type {value!r}; "
            f"expected one of {', '.join(TASK_TYPES)}"
        )
    return text


def task_kind(task_type: str) -> str:
    """Map a task route to the source quota kind."""

    task = normalize_task_type(task_type)
    if task == TASK_LIST_NEW:
        return "new_list"
    if task == TASK_LIST_ACTIVE:
        return "active_list"
    if task == TASK_HISTORY_PROBE:
        return "probe"
    return "detail"


def is_detail_task(task_type: str) -> bool:
    return task_kind(task_type) == "detail"


def is_current_task(task_type: str) -> bool:
    return normalize_task_type(task_type) in {
        TASK_LIST_NEW,
        TASK_LIST_ACTIVE,
        TASK_ID_FOLLOWUP,
    }


def is_history_task(task_type: str) -> bool:
    return normalize_task_type(task_type) in {
        TASK_HISTORY_DETAIL,
        TASK_HISTORY_PROBE,
    }
