"""Run-kind selection for offline consumers of mixed runtime event files."""


def matches_run_kind(event: dict, run_kind: str = "default") -> bool:
    actual = str(event.get("run_kind") or "live").strip().lower()
    # Existing offline consumers accepted every kind before cafe_clip isolation.
    if run_kind == "default":
        return actual != "cafe_clip"
    return run_kind == "all" or actual == run_kind
