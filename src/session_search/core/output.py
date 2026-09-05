"""A budget covers the complete serialized response, including its metadata."""

from copy import deepcopy

from session_search.core.records import canonical_json


def bounded_response(response: dict, budget: int = 16384) -> dict:
    if not 1024 <= budget <= 65536:
        raise ValueError("output budget must be between 1024 and 65536 bytes")
    result = deepcopy(response)
    result.update(truncated=False, omitted_results=0, omitted_events=0)

    def size():
        return len(canonical_json(result).encode())

    # Context first drops neighboring events, retaining each requested match.
    while size() > budget:
        removed = False
        for item in reversed(result.get("results", [])):
            events = item.get("events", [])
            target = item.get("citation", {}).get("event_id")
            for event in list(reversed(events)):
                if event["event_id"] != target:
                    events.remove(event)
                    result["omitted_events"] += 1
                    result["truncated"] = removed = True
                    break
            if removed:
                break
        if removed:
            continue
        # Shorten text before losing a match. Keep the immutable citation intact.
        candidates = []
        for item in result.get("results", []):
            if len(item.get("excerpt", "")) > 128:
                candidates.append((item, "excerpt"))
            for event in item.get("events", []):
                if len(event.get("text", "")) > 128:
                    candidates.append((event, "text"))
        if candidates:
            obj, key = max(candidates, key=lambda pair: len(pair[0][pair[1]]))
            obj[key] = obj[key][:max(128, len(obj[key]) // 2)] + "…"
            obj["text_truncated"] = True
            result["truncated"] = True
        elif result.get("results"):
            result["results"].pop()
            result["omitted_results"] += 1
            result["truncated"] = True
        else:
            raise ValueError("response metadata exceeds output budget")
    return result
