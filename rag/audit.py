import json
import logging
from datetime import datetime, timezone
from getpass import getuser
from uuid import UUID

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())
logger.propagate = False


def emit(event: str, trace_id: UUID, *, outcome: str, **fields) -> None:
    logger.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "trace_id": trace_id,
        "outcome": outcome,
        **fields,
    }, default=str))


def emit_operator(event: str, trace_id: UUID, *, outcome: str, **fields) -> None:
    try:
        actor = getuser()
    except (OSError, KeyError):
        actor = "unknown"
    emit(event, trace_id, outcome=outcome, actor_type="operator", actor=actor, **fields)
